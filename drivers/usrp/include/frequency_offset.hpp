// frequency_offset.hpp — measuring and removing carrier frequency offset.
//
//   CFOEstimator    : how far apart are the two radios' oscillators, in Hz, from
//                     the phase ramp across the preamble.
//   FrequencyShifter: applies a fixed rotation per sample to take it out.
//   CFOCorrector    : the two together, per burst.
//
// Two radios with free-running oscillators are always offset -- a few kHz at
// 5 GHz is only about one part per million, which is ordinary for a TCXO. Left
// uncorrected it spins the constellation and a coherent scheme cannot decode at
// all; a differential scheme survives it, because a constant per-symbol rotation
// cancels when each symbol is measured against its predecessor.
//
// The shifter's phase accumulator is reset whenever the offset is set, so each
// burst starts from a known phase instead of inheriting the previous one's.

#pragma once

#include <complex>
#include <vector>
#include <cmath>
#include <iostream>
#include <numeric>
#include <algorithm>
#include <atomic>
#include <thread>

#include "FIFO.hpp"

// ============================================================
//  Frequency Offset Utilities
//  Covers three things:
//    1. FrequencyShifter   – apply a known Hz offset to a sample stream
//    2. CFOEstimator       – estimate carrier-frequency offset (CFO)
//                           using either the pilot-aided or the
//                           auto-correlation (Moose / Schmidl-Cox)
//                           algorithm on a known preamble
//    3. CFOCorrector       – run estimation + correction in one call
//
//  All classes work on std::vector<std::complex<float>> so they
//  slot directly into the existing FIFO pipeline.
// ============================================================


// ─────────────────────────────────────────────────────────────
//  1.  FrequencyShifter
//
//  Multiplies every input sample n by  exp(j·2π·f_offset·n/fs).
//  Maintains a running phase accumulator so it can be used block-
//  by-block without phase discontinuities.
// ─────────────────────────────────────────────────────────────
class FrequencyShifter {
public:
    // f_offset_hz : desired shift in Hz (positive = up, negative = down)
    // sample_rate : sample rate in Hz
    FrequencyShifter(double f_offset_hz, double sample_rate)
        : phase_(0.0f),
          phase_increment_(static_cast<float>(2.0 * M_PI * f_offset_hz / sample_rate))
    {
        std::cout << "[FrequencyShifter] f_offset=" << f_offset_hz
                  << " Hz  fs=" << sample_rate
                  << " Hz  Δφ=" << phase_increment_ << " rad/sample\n";
    }

    // Apply the shift to a block of samples (in-place is fine too –
    // pass the same vector as both in and out).
    std::vector<std::complex<float>>
    shift(const std::vector<std::complex<float>>& input)
    {
        std::vector<std::complex<float>> output;
        output.reserve(input.size());
        for (const auto& s : input) {
            output.push_back(s * std::polar(1.0f, phase_));
            phase_ += phase_increment_;
            // Keep phase in [-π, π] to avoid float precision drift
            if (phase_ >  static_cast<float>(M_PI)) phase_ -= static_cast<float>(2.0 * M_PI);
            if (phase_ < -static_cast<float>(M_PI)) phase_ += static_cast<float>(2.0 * M_PI);
        }
        return output;
    }

    // Reset phase accumulator (use when starting a new burst)
    void reset() { phase_ = 0.0f; }

    void set_offset(double f_offset_hz, double sample_rate) {
        phase_increment_ = static_cast<float>(2.0 * M_PI * f_offset_hz / sample_rate);
        phase_ = 0.0f;
        std::cout << "[FrequencyShifter] Updated: f_offset=" << f_offset_hz
                  << " Hz  Δφ=" << phase_increment_ << " rad/sample\n";
    }

private:
    float phase_;
    float phase_increment_;
};


// ─────────────────────────────────────────────────────────────
//  2.  CFOEstimator
//
//  Two methods are provided:
//
//  (A) Pilot-aided / preamble correlation
//      Compares the received preamble against a local copy to
//      extract the residual phase slope across the known symbols.
//      Works well when sps > 1 and the preamble is long (≥ 16 sym).
//
//  (B) Schmidl–Cox auto-correlation
//      Exploits a repeated preamble (two identical halves).
//      Returns an estimate in the range  ±fs/(2·L)  where L is
//      the half-length in samples.
//      Suitable even without a local reference.
// ─────────────────────────────────────────────────────────────
class CFOEstimator {
public:
    // sample_rate : receiver sample rate in Hz
    // sps         : samples per symbol at the point estimation is done
    explicit CFOEstimator(double sample_rate, int sps)
        : sample_rate_(sample_rate), sps_(sps)
    {}

    // ── Method A: pilot-aided (preamble-correlation) ──────────────
    // received_samples : block that starts at the detected preamble
    // preamble         : ideal preamble symbols (one per symbol)
    // Returns estimated CFO in Hz.
    double estimate_pilot_aided(
        const std::vector<std::complex<float>>& received_samples,
        const std::vector<std::complex<float>>& preamble) const
    {
        size_t Np = preamble.size();
        if (received_samples.size() < Np * static_cast<size_t>(sps_)) {
            std::cerr << "[CFOEstimator] ERROR: received block too short for pilot-aided estimation\n";
            return 0.0;
        }

        // Compute cross-correlation angle between successive preamble symbols
        // φ_k = angle( r[k·sps] · conj(r[(k-1)·sps]) · conj(p[k]) · p[k-1] )
        // Average over all symbol pairs to reduce noise.
        double phase_acc = 0.0;
        int count = 0;
        for (size_t k = 1; k < Np; ++k) {
            std::complex<float> r_curr = received_samples[k * sps_];
            std::complex<float> r_prev = received_samples[(k - 1) * sps_];
            // Differential: removes data phase, leaves CFO phase
            std::complex<float> diff = r_curr * std::conj(r_prev)
                                     * std::conj(preamble[k]) * preamble[k - 1];
            phase_acc += std::arg(diff);
            ++count;
        }
        if (count == 0) return 0.0;

        double mean_phase = phase_acc / count;
        // mean_phase ≈ 2π·f_cfo / fs  (per sample at rate fs/sps, i.e. per symbol)
        double f_cfo = mean_phase * sample_rate_ / (2.0 * M_PI * sps_);

        std::cout << "[CFOEstimator][Pilot] Mean phase/sym=" << mean_phase
                  << " rad  CFO=" << f_cfo << " Hz\n";
        return f_cfo;
    }

    // ── Method B: Schmidl–Cox auto-correlation ────────────────────
    // received_samples : raw received block (at least 2·half_len long)
    // half_len         : half-length in samples of the repeated preamble
    // Returns estimated CFO in Hz (range: ±fs/(2·half_len)).
    double estimate_schmidl_cox(
        const std::vector<std::complex<float>>& received_samples,
        int half_len) const
    {
        if (static_cast<int>(received_samples.size()) < 2 * half_len) {
            std::cerr << "[CFOEstimator] ERROR: block too short for Schmidl-Cox\n";
            return 0.0;
        }

        // P = Σ r[n+L] · conj(r[n])  for n = 0..L-1
        std::complex<double> P(0.0, 0.0);
        for (int n = 0; n < half_len; ++n) {
            P += static_cast<std::complex<double>>(
                     received_samples[n + half_len] * std::conj(received_samples[n]));
        }

        double phase = std::arg(P);   // in [-π, π]
        // f_cfo = phase / (2π · L/fs)  = phase·fs / (2π·L)
        double f_cfo = phase * sample_rate_ / (2.0 * M_PI * half_len);

        std::cout << "[CFOEstimator][Schmidl-Cox] P_angle=" << phase
                  << " rad  CFO=" << f_cfo << " Hz\n";
        return f_cfo;
    }

    // ── Method C: data-aided least-squares phase-slope (ML/CRLB) ──
    // received_samples : block that starts at the detected preamble (1 sym/stride sps)
    // preamble         : ideal preamble symbols (one per symbol)
    // Returns estimated CFO in Hz.
    //
    // Why this beats Method A (adjacent-symbol differential): A only uses lag-1
    // pairs, so its noise variance is high (differencing amplifies noise). Here we
    // de-rotate each received preamble symbol by the known symbol
    //   θ_k = arg( r[k·sps] · conj(p[k]) )                 (removes the modulation)
    // which for A·p·e^{jφ} with |p|=1 leaves A·e^{jφ_k}, i.e. the pure carrier phase
    // progression. Unwrapping and doing a magnitude-weighted least-squares straight-
    // line fit gives the phase slope (rad/symbol) using ALL symbols jointly — the
    // data-aided ML estimate, ~L² lower variance than lag-1 differencing. Same
    // unambiguous range as A (±symbol_rate/2), which already dwarfs the ~kHz CFOs we
    // see, so this is a pure variance win. Magnitude weighting down-weights faded /
    // noisy symbols.
    double estimate_ls_slope(
        const std::vector<std::complex<float>>& received_samples,
        const std::vector<std::complex<float>>& preamble) const
    {
        size_t Np = preamble.size();
        if (Np < 3 ||
            received_samples.size() < (Np - 1) * static_cast<size_t>(sps_) + 1) {
            std::cerr << "[CFOEstimator] ERROR: block too short / preamble < 3 sym "
                         "for LS-slope estimation\n";
            return 0.0;
        }

        // 1. De-rotate by the known preamble → carrier phase per symbol, + weights.
        std::vector<double> theta(Np), w(Np);
        for (size_t k = 0; k < Np; ++k) {
            std::complex<float> d = received_samples[k * sps_] * std::conj(preamble[k]);
            theta[k] = std::arg(d);
            w[k]     = std::abs(d);   // magnitude weight (∝ instantaneous SNR)
        }

        // 2. Phase unwrap (cumulative, adjacent difference kept in [-π, π]).
        for (size_t k = 1; k < Np; ++k) {
            double dphi = theta[k] - theta[k - 1];
            while (dphi >  M_PI) dphi -= 2.0 * M_PI;
            while (dphi < -M_PI) dphi += 2.0 * M_PI;
            theta[k] = theta[k - 1] + dphi;
        }

        // 3. Weighted least-squares fit  θ_k ≈ a + b·k ; keep slope b (rad/symbol).
        double Sw = 0, Swx = 0, Swy = 0, Swxx = 0, Swxy = 0;
        for (size_t k = 0; k < Np; ++k) {
            double x = static_cast<double>(k), y = theta[k], wk = w[k];
            Sw += wk; Swx += wk * x; Swy += wk * y;
            Swxx += wk * x * x; Swxy += wk * x * y;
        }
        double denom = Sw * Swxx - Swx * Swx;
        if (std::abs(denom) < 1e-12) return 0.0;
        double slope = (Sw * Swxy - Swx * Swy) / denom;   // rad per symbol

        // slope = 2π·f_cfo·(sps/fs)  →  f_cfo = slope·fs / (2π·sps)
        double f_cfo = slope * sample_rate_ / (2.0 * M_PI * sps_);

        std::cout << "[CFOEstimator][LS] slope=" << slope
                  << " rad/sym  CFO=" << f_cfo << " Hz\n";
        return f_cfo;
    }

private:
    double sample_rate_;
    int    sps_;
};


// ─────────────────────────────────────────────────────────────
//  3.  CFOCorrector
//
//  Combines estimation + correction.
//  Typical usage in the receive chain (after match filter):
//
//    CFOCorrector cfo_corr(sample_rate, sps, preamble);
//    auto corrected = cfo_corr.correct(received_block);
// ─────────────────────────────────────────────────────────────
class CFOCorrector {
public:
    // PILOT_LS   : data-aided least-squares phase-slope (default; lowest variance).
    // PILOT_AIDED: adjacent-symbol differential (legacy; kept for comparison).
    // SCHMIDL_COX: autocorrelation on a *repeated* preamble (not the m-seq default).
    enum class Method { PILOT_LS, PILOT_AIDED, SCHMIDL_COX };

    CFOCorrector(double sample_rate, int sps,
                 const std::vector<std::complex<float>>& preamble,
                 Method method = Method::PILOT_LS)
        : estimator_(sample_rate, sps),
          shifter_(0.0, sample_rate),
          sample_rate_(sample_rate),
          preamble_(preamble),
          method_(method),
          last_cfo_hz_(0.0),
          prior_hz_(0.0),
          has_prior_(false),
          prior_smoothing_(1.0)
    {}

    // Cross-burst smoothing factor α for the estimate EMA:
    //   applied = α·(this-burst estimate) + (1-α)·(previous applied estimate)
    // α = 1.0 (default) → pure per-burst, no memory: correct for a COLD per-fire LO
    //   (two-host range test, DQPSK) where the CFO changes every burst.
    // α < 1.0 → blend history: use only with a WARM resident LO (stable burst-to-
    //   burst CFO), where averaging further cuts the estimate variance and helps a
    //   coherent carrier tracker lock. e.g. 0.5 halves the per-burst jitter.
    void set_prior_smoothing(double alpha) {
        if (alpha < 0.0) alpha = 0.0;
        if (alpha > 1.0) alpha = 1.0;
        prior_smoothing_ = alpha;
    }

    // Estimate CFO from the front of 'block', then correct the whole block.
    // Returns the corrected signal; also stores the applied estimate in last_cfo_hz_.
    std::vector<std::complex<float>>
    correct(const std::vector<std::complex<float>>& block,
            int schmidl_cox_half_len = 0)
    {
        // 1. Optionally pre-derotate by the running cross-burst prior so the
        //    residual we measure is small (unwrap-safe). Only when smoothing is on
        //    AND we have history; otherwise estimate on the raw block.
        double pre = 0.0;
        const std::vector<std::complex<float>>* est_src = &block;
        std::vector<std::complex<float>> pre_block;
        if (prior_smoothing_ < 1.0 && has_prior_) {
            pre = prior_hz_;
            shifter_.set_offset(-pre, sample_rate_);
            pre_block = shifter_.shift(block);
            est_src = &pre_block;
        }

        // 2. Estimate the residual CFO on est_src.
        double residual;
        switch (method_) {
            case Method::PILOT_LS:
                residual = estimator_.estimate_ls_slope(*est_src, preamble_);
                break;
            case Method::SCHMIDL_COX: {
                int L = (schmidl_cox_half_len > 0)
                            ? schmidl_cox_half_len
                            : static_cast<int>(preamble_.size());
                residual = estimator_.estimate_schmidl_cox(*est_src, L);
                break;
            }
            case Method::PILOT_AIDED:
            default:
                residual = estimator_.estimate_pilot_aided(*est_src, preamble_);
                break;
        }
        double this_burst = pre + residual;

        // 3. Cross-burst EMA (no-op when α = 1.0 → last_cfo_hz_ = this_burst).
        last_cfo_hz_ = has_prior_
            ? prior_smoothing_ * this_burst + (1.0 - prior_smoothing_) * prior_hz_
            : this_burst;
        prior_hz_  = last_cfo_hz_;
        has_prior_ = true;

        std::cout << "[CFOCorrector] Estimated CFO = " << last_cfo_hz_
                  << " Hz  →  applying -CFO\n";

        // 4. Correct the ORIGINAL block by the (smoothed) estimate.
        shifter_.set_offset(-last_cfo_hz_, sample_rate_);
        return shifter_.shift(block);
    }

    double get_last_cfo_hz() const { return last_cfo_hz_; }

private:
    CFOEstimator  estimator_;
    FrequencyShifter shifter_;
    double        sample_rate_;
    std::vector<std::complex<float>> preamble_;
    Method        method_;
    double        last_cfo_hz_;
    double        prior_hz_;         // previous applied estimate (EMA state)
    bool          has_prior_;        // false until first burst processed
    double        prior_smoothing_;  // α ∈ [0,1]; 1.0 = pure per-burst (default)
};


// ─────────────────────────────────────────────────────────────
//  4.  CFO_correction_thread
//
//  Drop-in pipeline stage.  Sits between the match_filter output
//  and the TimeSync input (or wherever you need it).
//
//  Parameters match the style used in the rest of the project.
// ─────────────────────────────────────────────────────────────
void CFO_correction_thread(
    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& input_fifo,
    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& output_fifo,
    const std::vector<std::complex<float>>& preamble,
    double sample_rate,
    int sps,
    CFOCorrector::Method method,
    std::atomic<bool>& stop_sign,
    double prior_smoothing = 1.0)
{
    CFOCorrector corrector(sample_rate, sps, preamble, method);
    corrector.set_prior_smoothing(prior_smoothing);

    std::pair<size_t, std::vector<std::complex<float>>> msg;
    size_t processed = 0;

    std::cout << "[CFO_thread] Started\n";

    DrainGate gate;
    while (gate.keep_going(stop_sign, input_fifo)) {
        if (!input_fifo.pop(msg)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
            continue;
        }

        auto corrected = corrector.correct(msg.second);
        output_fifo.push({msg.first, std::move(corrected)});
        ++processed;

        std::cout << "[CFO_thread] Block " << msg.first
                  << " corrected  CFO=" << corrector.get_last_cfo_hz() << " Hz\n";
    }

    std::cout << "[CFO_thread] Stopped. Processed " << processed << " blocks.\n";
}
