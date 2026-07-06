#pragma once
// =============================================================
//  timing_recovery.hpp
//
//  Symbol timing recovery for single-carrier modulations.
//
//  Contents
//  ────────
//  1. FarrowInterpolator  – cubic Lagrange fractional-delay filter
//  2. GardnerTED          – Gardner Timing Error Detector + 2nd-order
//                           loop filter + Farrow interpolator
//  3. timing_recovery_thread – drop-in FIFO pipeline stage
//
//  Where it sits in the pipeline
//  ─────────────────────────────
//    match_filter_thread   (output: 2 sps oversampled symbols)
//        ↓  filtered_fifo
//    timing_recovery_thread             ← this file
//        ↓  timed_fifo  (output: 1 sps, timing-corrected symbols)
//    CFO_correction_thread
//
//  Why it is needed
//  ────────────────
//  TX and RX are driven by independent crystal oscillators with a
//  typical tolerance of ±20 ppm.  Over a 1000-symbol packet at
//  1 Msym/s this accumulates up to a 20-sample timing error at the
//  tail of the packet, causing ISI and BER degradation regardless
//  of SNR.  The Gardner TED estimates and corrects this in real time.
//
//  Theory reference
//  ────────────────
//  F. Gardner, "A BPSK/QPSK timing-error detector for sampled
//  receivers", IEEE Trans. Commun., vol. 34, no. 5, May 1986.
//  U. Mengali & A. D'Andrea, "Synchronization Techniques for Digital
//  Receivers", Plenum 1997, Ch. 8-9.
// =============================================================

#include <complex>
#include <vector>
#include <deque>
#include <cmath>
#include <iostream>
#include <algorithm>
#include <atomic>
#include <thread>
#include <chrono>
#include "FIFO.hpp"


// ─────────────────────────────────────────────────────────────
//  1.  FarrowInterpolator
//
//  Cubic Lagrange polynomial interpolation.
//  Given four consecutive samples at integer offsets -1, 0, +1, +2
//  and a fractional delay mu ∈ [0, 1), returns the sample at
//  offset mu.  Used inside GardnerTED for sub-sample accuracy.
// ─────────────────────────────────────────────────────────────
class FarrowInterpolator {
public:
    // Interpolate between y0 and y1.
    // ym1 = sample before y0; y2 = sample after y1.
    // mu  = fractional offset in [0, 1)
    static std::complex<float>
    interpolate(std::complex<float> ym1,
                std::complex<float> y0,
                std::complex<float> y1,
                std::complex<float> y2,
                float mu)
    {
        // Cubic Farrow / Lagrange coefficients
        // This is numerically equivalent to 4-point cubic spline
        std::complex<float> c0 =  y0;
        std::complex<float> c1 = -ym1 * (1.0f/6.0f) + y0 * 0.5f  +  y1 * (1.0f/3.0f) - y2 * (1.0f/6.0f);
        std::complex<float> c2 =  ym1 * 0.5f         - y0          +  y1 * 0.5f;
        std::complex<float> c3 = -ym1 * (1.0f/6.0f) + y0 * 0.5f  -  y1 * 0.5f         + y2 * (1.0f/6.0f);
        return ((c3 * mu + c2) * mu + c1) * mu + c0;  // Horner's method
    }
};


// ─────────────────────────────────────────────────────────────
//  2.  GardnerTED
//
//  Symbol timing recovery operating at exactly 2 samples per symbol.
//
//  Algorithm
//  ─────────
//  • NCO accumulates a fractional timing offset mu.
//  • When mu crosses 1.0, a new output symbol is produced via
//    Farrow cubic interpolation.
//  • Every other strobe (= once per symbol epoch) the Gardner
//    timing error is computed:
//      e[k] = Re{ (r[k] - r[k-1]) · conj(r[k - 0.5]) }
//    where r[k] and r[k-1] are successive output symbols and
//    r[k-0.5] is the interpolated half-symbol sample.
//  • A second-order PI loop filter updates the NCO frequency.
//
//  Parameters
//  ──────────
//  loop_bw : normalised loop bandwidth BnT (e.g. 0.01 – 0.03)
//             Larger → faster acquisition, more noise on steady state.
//  damping : loop damping factor ζ (0.707 = critically damped)
//  sps     : input samples per symbol (must be ≥ 2; recommended = 2)
// ─────────────────────────────────────────────────────────────
class GardnerTED {
public:
    explicit GardnerTED(float loop_bw = 0.015f,
                        float damping = 0.707f,
                        int   sps     = 2)
        : sps_(sps),
          mu_(0.5f),               // start at midpoint of first symbol
          // The Gardner TED needs TWO strobes per symbol: one at the symbol
          // centre and one at the half-symbol point. The NCO therefore advances
          // 2/sps per input sample (a strobe every sps/2 input samples), NOT
          // 1/sps. With 1/sps there is only one strobe per symbol, so the
          // "mid-sample" the error detector uses is actually a full symbol away
          // and the loop tracks on garbage — the preamble correlation collapses
          // and demodulation fails. (Works for any sps >= 2.)
          omega_(2.0f / sps),      // nominal NCO step per input sample (2 strobes/sym)
          omega_min_(omega_ * 0.9f),
          omega_max_(omega_ * 1.1f),
          freq_adj_(0.0f),
          buf_(4, std::complex<float>(0, 0)),
          buf_write_(0),
          strobe_count_(0),
          last_output_(0, 0),
          mid_sample_(0, 0),
          have_mid_(false)
    {
        // Second-order loop coefficients
        // Derived from Gardner / Proakis PI loop filter design:
        //   theta_n = BnT / (zeta + 1/(4*zeta))
        //   K1 (proportional) = 4*zeta*theta_n / denom
        //   K2 (integral)     = 4*theta_n^2    / denom
        float theta_n = loop_bw / (damping + 1.0f / (4.0f * damping));
        float denom   = 1.0f + 2.0f * damping * theta_n + theta_n * theta_n;
        K1_ = (4.0f * damping * theta_n) / denom;
        K2_ = (4.0f * theta_n * theta_n) / denom;

        std::cout << "[GardnerTED] BnT=" << loop_bw
                  << "  zeta=" << damping
                  << "  sps=" << sps
                  << "  K1=" << K1_
                  << "  K2=" << K2_
                  << "  omega=" << omega_ << "\n";
    }

    // ── Push one input sample; returns true + fills 'out' when
    //    a new output symbol is ready.
    bool push_sample(std::complex<float> sample,
                     std::complex<float>& out)
    {
        // Write to circular buffer
        buf_[buf_write_ & 3] = sample;
        buf_write_++;

        mu_ += omega_ + freq_adj_;

        if (mu_ < 1.0f) return false;   // no strobe yet
        mu_ -= 1.0f;

        // Strobe: compute interpolated output
        // Buffer indices relative to current write position
        int n = buf_write_;
        std::complex<float> ym1 = buf_[(n - 4) & 3];
        std::complex<float> y0  = buf_[(n - 3) & 3];
        std::complex<float> y1  = buf_[(n - 2) & 3];
        std::complex<float> y2  = buf_[(n - 1) & 3];
        out = FarrowInterpolator::interpolate(ym1, y0, y1, y2, mu_);

        strobe_count_++;

        // Two strobes per symbol (omega = 2/sps). Odd strobes (1st, 3rd, …) are
        // symbol-centre samples; even strobes are the half-symbol midpoints.
        // Only the symbol-centre strobes are emitted as output symbols, so the
        // stage still produces exactly one sample per symbol.
        if (strobe_count_ % 2 == 0) {
            // Even strobe: the half-symbol (mid) sample between two symbols.
            mid_sample_ = out;
            have_mid_   = true;
            return false;             // not a symbol output
        }

        // Odd strobe: a full-symbol output.
        if (have_mid_) {
            // Gardner error:  e = Re{ (r[k] - r[k-1]) * conj(r[k-1/2]) }
            float e = ((out - last_output_) * std::conj(mid_sample_)).real();

            // PI loop filter
            freq_adj_ += K2_ * e;
            freq_adj_  = std::max(omega_min_ - omega_,
                                  std::min(omega_max_ - omega_, freq_adj_));
            mu_ -= K1_ * e;
        }

        last_output_ = out;
        return true;
    }

    // ── Process a whole block of oversampled samples.
    //    Returns timing-corrected 1-sps symbols.
    std::vector<std::complex<float>>
    process(const std::vector<std::complex<float>>& in)
    {
        std::vector<std::complex<float>> out;
        out.reserve(in.size() / sps_ + 4);
        std::complex<float> sym;
        for (const auto& s : in)
            if (push_sample(s, sym))
                out.push_back(sym);
        return out;
    }

    // Reset between packets (burst mode operation)
    void reset()
    {
        mu_           = 0.5f;
        freq_adj_     = 0.0f;
        buf_write_    = 0;
        strobe_count_ = 0;
        have_mid_     = false;
        last_output_  = {0, 0};
        mid_sample_   = {0, 0};
        std::fill(buf_.begin(), buf_.end(), std::complex<float>(0, 0));
    }

    float get_timing_offset()    const { return mu_;       }
    float get_frequency_adjust() const { return freq_adj_; }
    int   get_strobe_count()     const { return strobe_count_; }

private:
    int   sps_;
    float mu_;          // fractional timing offset (NCO phase)
    float omega_;       // nominal NCO step = 1/sps
    float omega_min_, omega_max_;
    float freq_adj_;    // accumulated frequency adjustment
    float K1_, K2_;     // PI loop filter gains

    std::vector<std::complex<float>> buf_;   // circular sample buffer (size 4)
    int  buf_write_;
    int  strobe_count_;

    std::complex<float> last_output_;  // previous full-symbol output
    std::complex<float> mid_sample_;   // half-symbol interpolated sample
    bool have_mid_;
};


// ─────────────────────────────────────────────────────────────
//  3.  timing_recovery_thread
//
//  Pipeline stage.  Input: 2+ sps oversampled (match filter output).
//  Output: 1 sps timing-corrected symbols.
//
//  Parameters
//  ──────────
//  input_fifo  : filtered_fifo from match_filter_thread
//  output_fifo : timed_fifo → fed into CFO_correction_thread
//  loop_bw     : Gardner loop bandwidth BnT (start with 0.01)
//  damping     : damping factor (0.707)
//  sps         : input samples per symbol (must match match_filter output)
//  stop_sign   : shared stop flag
// ─────────────────────────────────────────────────────────────
void timing_recovery_thread(
    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& input_fifo,
    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& output_fifo,
    float loop_bw,
    float damping,
    int   sps,
    std::atomic<bool>& stop_sign)
{
    GardnerTED ted(loop_bw, damping, sps);

    std::pair<size_t, std::vector<std::complex<float>>> msg;
    size_t processed = 0;

    std::cout << "[TimingRecovery] Started"
              << "  BnT=" << loop_bw
              << "  zeta=" << damping
              << "  sps=" << sps << "\n";

    while (!stop_sign || input_fifo.size() > 0) {
        if (!input_fifo.pop(msg)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
            continue;
        }

        // Reset per packet: each burst re-acquires timing independently.
        // This is correct for packet-mode operation (not continuous streaming).
        ted.reset();

        auto syms = ted.process(msg.second);

        std::cout << "[TimingRecovery] Block " << msg.first
                  << "  in=" << msg.second.size()
                  << "  out=" << syms.size()
                  << "  mu=" << ted.get_timing_offset()
                  << "  freq_adj=" << ted.get_frequency_adjust() << "\n";

        if (!syms.empty())
            output_fifo.push({msg.first, std::move(syms)});

        ++processed;
    }

    std::cout << "[TimingRecovery] Stopped. Processed " << processed << " blocks.\n";
}
