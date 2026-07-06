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
    enum class Method { PILOT_AIDED, SCHMIDL_COX };

    CFOCorrector(double sample_rate, int sps,
                 const std::vector<std::complex<float>>& preamble,
                 Method method = Method::PILOT_AIDED)
        : estimator_(sample_rate, sps),
          shifter_(0.0, sample_rate),
          sample_rate_(sample_rate),
          preamble_(preamble),
          method_(method),
          last_cfo_hz_(0.0)
    {}

    // Estimate CFO from the front of 'block', then correct the whole block.
    // Returns the corrected signal; also stores the estimate in last_cfo_hz_.
    std::vector<std::complex<float>>
    correct(const std::vector<std::complex<float>>& block,
            int schmidl_cox_half_len = 0)
    {
        // 1. Estimate
        if (method_ == Method::PILOT_AIDED) {
            last_cfo_hz_ = estimator_.estimate_pilot_aided(block, preamble_);
        } else {
            int L = (schmidl_cox_half_len > 0)
                        ? schmidl_cox_half_len
                        : static_cast<int>(preamble_.size());
            last_cfo_hz_ = estimator_.estimate_schmidl_cox(block, L);
        }

        std::cout << "[CFOCorrector] Estimated CFO = " << last_cfo_hz_ << " Hz  →  applying -CFO\n";

        // 2. Correct: apply the negative of the estimated offset
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
    std::atomic<bool>& stop_sign)
{
    CFOCorrector corrector(sample_rate, sps, preamble, method);

    std::pair<size_t, std::vector<std::complex<float>>> msg;
    size_t processed = 0;

    std::cout << "[CFO_thread] Started\n";

    while (!stop_sign || input_fifo.size() > 0) {
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
