#pragma once
// ============================================================
//  channel_estimation.hpp
//
//  Single-carrier channel estimation and equalization.
//
//  Contents
//  ────────
//  1. SNREstimator       – estimates Eb/N0 from pilot symbols or
//                          decision-directed variance.
//  2. LMSEqualizer       – adaptive linear equalizer (LMS).
//  3. RLSEqualizer       – adaptive linear equalizer (RLS).
//                          Faster convergence than LMS, higher cost.
//  4. DFEqualizer        – Decision-Feedback Equalizer (LMS-updated).
//  5. channel_eq_thread  – pipeline stage; sits between
//                          phase_offset_thread and demodulation_thread.
//
//  All equalizers:
//   • train on the known preamble symbols
//   • switch to decision-directed mode for the data portion
//   • output the equalized symbols ready for hard/soft decisions
// ============================================================

#include <complex>
#include <vector>
#include <cmath>
#include <iostream>
#include <numeric>
#include <stdexcept>
#include <atomic>
#include <thread>
#include <chrono>
#include "FIFO.hpp"
#include "modulator.hpp"


// ─────────────────────────────────────────────────────────────
//  1.  SNREstimator
// ─────────────────────────────────────────────────────────────
class SNREstimator {
public:
    // Pilot-aided: compares received pilot symbols to the ideal
    // Returns estimated SNR in dB.
    static float estimate_pilot(
        const std::vector<std::complex<float>>& received,
        const std::vector<std::complex<float>>& pilot,
        int num_pilots = -1)
    {
        int N = (num_pilots < 0)
                    ? static_cast<int>(std::min(received.size(), pilot.size()))
                    : std::min(num_pilots,
                               static_cast<int>(std::min(received.size(), pilot.size())));
        if (N == 0) return 0.0f;

        float signal_power = 0.0f, noise_power = 0.0f;
        for (int i = 0; i < N; ++i) {
            signal_power += std::norm(pilot[i]);
            noise_power  += std::norm(received[i] - pilot[i]);
        }
        signal_power /= N;
        noise_power  /= N;

        float snr_linear = (noise_power > 1e-12f) ? signal_power / noise_power : 1e6f;
        float snr_db = 10.0f * std::log10(snr_linear);

        std::cout << "[SNREstimator][Pilot]  Signal=" << signal_power
                  << "  Noise=" << noise_power
                  << "  SNR=" << snr_db << " dB\n";
        return snr_db;
    }

    // Decision-directed: uses hard decisions as the reference.
    // Less accurate at low SNR but works without a pilot.
    static float estimate_decision_directed(
        const std::vector<std::complex<float>>& received,
        const Modulator& mod)
    {
        const auto& C = mod.get_constellation();
        float signal_power = 0.0f, noise_power = 0.0f;
        for (const auto& r : received) {
            int nearest = 0;
            float min_d = std::norm(r - C[0]);
            for (int j = 1; j < static_cast<int>(C.size()); ++j) {
                float d = std::norm(r - C[j]);
                if (d < min_d) { min_d = d; nearest = j; }
            }
            signal_power += std::norm(C[nearest]);
            noise_power  += min_d;
        }
        signal_power /= received.size();
        noise_power  /= received.size();

        float snr_db = (noise_power > 1e-12f)
                           ? 10.0f * std::log10(signal_power / noise_power)
                           : 60.0f;
        std::cout << "[SNREstimator][DD]  SNR=" << snr_db << " dB\n";
        return snr_db;
    }

    // Convert SNR (dB) → noise variance σ² for a given signal power
    static float snr_db_to_noise_variance(float snr_db, float signal_power = 1.0f) {
        float snr_linear = std::pow(10.0f, snr_db / 10.0f);
        return signal_power / snr_linear;
    }
};


// ─────────────────────────────────────────────────────────────
//  2.  LMSEqualizer
//
//  Linear transversal equalizer, LMS weight update.
//  Taps: complex FIR filter of length num_taps.
//  Centred on tap (num_taps-1)/2.
//
//  Usage:
//    1. train()  – on the known preamble symbols
//    2. equalize() – on the data symbols (decision-directed)
// ─────────────────────────────────────────────────────────────
class LMSEqualizer {
public:
    // num_taps : equalizer length (odd number recommended, e.g. 11)
    // mu       : NLMS step size (0.05 – 0.6; normalised, so scale-independent)
    // epochs   : how many passes over the (short) preamble during training —
    //            NLMS converges an 11-tap eq on a 31-symbol preamble in a few passes
    LMSEqualizer(int num_taps = 11, float mu = 0.3f, int epochs = 12)
        : num_taps_(num_taps),
          mu_(mu),
          epochs_(std::max(1, epochs)),
          delay_((num_taps - 1) / 2),
          weights_(num_taps, std::complex<float>(0.0f)),
          buf_(num_taps, std::complex<float>(0.0f))
    {
        // Initialise centre tap to 1 (identity start)
        weights_[delay_] = {1.0f, 0.0f};
        std::cout << "[LMSEqualizer] taps=" << num_taps
                  << "  mu=" << mu << "  epochs=" << epochs_
                  << "  delay=" << delay_ << "\n";
    }

    // Symbols of delay the equalizer output lags its input by (centre tap). The
    // caller must feed `delay()` extra runway symbols and read from output[delay].
    int delay() const { return delay_; }

    // Train on the known preamble.
    // received : received preamble symbols (at 1-sps after timing recovery)
    // ideal    : ideal preamble symbols
    // If `ideal` is a COMPLEX (2-D) sequence (e.g. Zadoff-Chu), a one-shot
    // least-squares solve gives the optimal equalizer directly. If `ideal` is
    // real-only (BPSK m-sequence), LS is skipped (a real preamble under-determines
    // a complex equalizer — it perfectly fits the preamble but mangles QAM data);
    // the light NLMS training below plus decision-directed adaptation on the data
    // is used instead. The buffer is left holding the preamble tail for a seamless
    // hand-off into the data.
    void train(const std::vector<std::complex<float>>& received,
               const std::vector<std::complex<float>>& ideal)
    {
        int P = static_cast<int>(std::min(received.size(), ideal.size()));
        int M = num_taps_;

        bool complex_pre = false;
        for (int i = 0; i < P; ++i)
            if (std::abs(ideal[i].imag()) > 1e-3f) { complex_pre = true; break; }

        if (complex_pre && P >= M) {
            // Optimal LS: solve (R^H R + load I) w = R^H d.
            std::vector<std::vector<std::complex<float>>> A(
                M, std::vector<std::complex<float>>(M, std::complex<float>(0)));
            std::vector<std::complex<float>> b(M, std::complex<float>(0));
            for (int n = M - 1; n < P; ++n) {
                std::complex<float> d = ideal[n - delay_];
                for (int i = 0; i < M; ++i) {
                    std::complex<float> xi = received[n - i];
                    b[i] += std::conj(xi) * d;
                    for (int j = 0; j < M; ++j)
                        A[i][j] += std::conj(xi) * received[n - j];
                }
            }
            float tr = 0.0f; for (int i = 0; i < M; ++i) tr += A[i][i].real();
            float load = 0.01f * tr / M + 1e-6f;
            for (int i = 0; i < M; ++i) A[i][i] += std::complex<float>(load, 0.0f);
            solve_linear(A, b, weights_);
            std::cout << "[LMSEqualizer] LS-trained on complex preamble (P=" << P << ")\n";
        } else {
            // Light NLMS pass(es) — keep the start near identity so the following
            // decision-directed data adaptation (which uses complex QAM decisions)
            // can open both axes.
            for (int ep = 0; ep < epochs_; ++ep) {
                std::fill(buf_.begin(), buf_.end(), std::complex<float>(0.0f));
                for (int n = 0; n < P; ++n) {
                    shift_in(received[n]);
                    std::complex<float> y = filter_output();
                    int ref = (n >= delay_) ? n - delay_ : 0;
                    nlms_update(ideal[ref] - y);
                }
            }
            std::cout << "[LMSEqualizer] NLMS-trained on real preamble (P=" << P << ")\n";
        }
        // Prime the buffer with the tail of the preamble.
        std::fill(buf_.begin(), buf_.end(), std::complex<float>(0.0f));
        for (int k = 0; k < M && k < P; ++k) buf_[k] = received[P - 1 - k];
    }

    // Equalize a block of received symbols.
    // If mod != nullptr, uses decision-directed updates.
    std::vector<std::complex<float>>
    equalize(const std::vector<std::complex<float>>& received,
             const Modulator* mod = nullptr)
    {
        std::vector<std::complex<float>> out;
        out.reserve(received.size());
        for (const auto& r : received) {
            shift_in(r);
            std::complex<float> y = filter_output();
            out.push_back(y);

            if (mod) {
                // Decision-directed: hard decision as reference, NLMS update.
                const auto& C = mod->get_constellation();
                int nearest = 0;
                float min_d = std::norm(y - C[0]);
                for (int j = 1; j < static_cast<int>(C.size()); ++j) {
                    float d = std::norm(y - C[j]);
                    if (d < min_d) { min_d = d; nearest = j; }
                }
                nlms_update(C[nearest] - y);
            }
        }
        return out;
    }

    void reset() {
        std::fill(weights_.begin(), weights_.end(), std::complex<float>(0.0f));
        std::fill(buf_.begin(), buf_.end(), std::complex<float>(0.0f));
        weights_[delay_] = {1.0f, 0.0f};
    }

    const std::vector<std::complex<float>>& weights() const { return weights_; }

private:
    int num_taps_, epochs_, delay_;
    float mu_;
    std::vector<std::complex<float>> weights_;
    std::vector<std::complex<float>> buf_;

    void shift_in(std::complex<float> x) {
        // Shift buffer right and insert at front
        for (int k = num_taps_ - 1; k > 0; --k)
            buf_[k] = buf_[k-1];
        buf_[0] = x;
    }

    std::complex<float> filter_output() const {
        std::complex<float> y(0.0f);
        for (int k = 0; k < num_taps_; ++k)
            y += weights_[k] * buf_[k];
        return y;
    }

    // NLMS weight update: w += mu * e * conj(x) / (eps + ||x||^2). Normalising by
    // the input energy makes the step scale-independent (robust to the AGC level)
    // and stable — the key fix vs the old fixed-step LMS that diverged.
    void nlms_update(std::complex<float> e) {
        float energy = 1e-6f;
        for (int k = 0; k < num_taps_; ++k) energy += std::norm(buf_[k]);
        std::complex<float> g = (mu_ / energy) * e;
        for (int k = 0; k < num_taps_; ++k)
            weights_[k] += g * std::conj(buf_[k]);
    }

    // Solve A x = b for a small dense complex system (Gaussian elimination with
    // partial pivoting). A is M×M, b and x are length M.
    static void solve_linear(std::vector<std::vector<std::complex<float>>> A,
                             std::vector<std::complex<float>> b,
                             std::vector<std::complex<float>>& x)
    {
        int M = static_cast<int>(b.size());
        for (int col = 0; col < M; ++col) {
            int piv = col; float best = std::abs(A[col][col]);
            for (int r = col + 1; r < M; ++r)
                if (std::abs(A[r][col]) > best) { best = std::abs(A[r][col]); piv = r; }
            if (piv != col) { std::swap(A[piv], A[col]); std::swap(b[piv], b[col]); }
            std::complex<float> d = A[col][col];
            if (std::abs(d) < 1e-12f) d = std::complex<float>(1e-12f, 0.0f);
            for (int r = col + 1; r < M; ++r) {
                std::complex<float> f = A[r][col] / d;
                for (int c = col; c < M; ++c) A[r][c] -= f * A[col][c];
                b[r] -= f * b[col];
            }
        }
        x.assign(M, std::complex<float>(0));
        for (int r = M - 1; r >= 0; --r) {
            std::complex<float> s = b[r];
            for (int c = r + 1; c < M; ++c) s -= A[r][c] * x[c];
            std::complex<float> d = A[r][r];
            if (std::abs(d) < 1e-12f) d = std::complex<float>(1e-12f, 0.0f);
            x[r] = s / d;
        }
    }
};


// ─────────────────────────────────────────────────────────────
//  3.  RLSEqualizer
//
//  Recursive Least Squares equalizer.
//  Converges faster than LMS at the cost of O(N²) per symbol.
//  Good for channels that change faster than LMS can track.
// ─────────────────────────────────────────────────────────────
class RLSEqualizer {
public:
    // num_taps : equalizer length
    // lambda   : forgetting factor (0.99 – 0.999; closer to 1 = longer memory)
    // delta    : initialisation constant for P matrix (e.g. 100)
    RLSEqualizer(int num_taps = 11, float lambda = 0.99f, float delta = 100.0f)
        : num_taps_(num_taps),
          lambda_(lambda),
          delay_((num_taps - 1) / 2),
          weights_(num_taps, std::complex<float>(0.0f)),
          buf_(num_taps, std::complex<float>(0.0f)),
          P_(num_taps, std::vector<std::complex<float>>(num_taps, {0.0f}))
    {
        weights_[delay_] = {1.0f, 0.0f};
        // Initialise P = delta * I
        for (int i = 0; i < num_taps; ++i)
            P_[i][i] = {delta, 0.0f};
        std::cout << "[RLSEqualizer] taps=" << num_taps
                  << "  lambda=" << lambda << "\n";
    }

    int delay() const { return delay_; }

    void train(const std::vector<std::complex<float>>& received,
               const std::vector<std::complex<float>>& ideal)
    {
        size_t N = std::min(received.size(), ideal.size());
        for (size_t n = 0; n < N; ++n) {
            shift_in(received[n]);
            size_t ref_idx = (n >= static_cast<size_t>(delay_)) ? n - delay_ : 0;
            std::complex<float> d = ideal[std::min(ref_idx, ideal.size()-1)];
            rls_update(d);
        }
        std::cout << "[RLSEqualizer] Training done  N=" << N << "\n";
    }

    std::vector<std::complex<float>>
    equalize(const std::vector<std::complex<float>>& received,
             const Modulator* mod = nullptr)
    {
        std::vector<std::complex<float>> out;
        out.reserve(received.size());
        for (const auto& r : received) {
            shift_in(r);
            std::complex<float> y(0.0f);
            for (int k = 0; k < num_taps_; ++k) y += weights_[k] * buf_[k];
            out.push_back(y);

            if (mod) {
                const auto& C = mod->get_constellation();
                int nearest = 0;
                float min_d = std::norm(y - C[0]);
                for (int j = 1; j < static_cast<int>(C.size()); ++j) {
                    float d = std::norm(y - C[j]);
                    if (d < min_d) { min_d = d; nearest = j; }
                }
                rls_update(C[nearest]);
            }
        }
        return out;
    }

    void reset() {
        std::fill(weights_.begin(), weights_.end(), std::complex<float>(0.0f));
        std::fill(buf_.begin(), buf_.end(), std::complex<float>(0.0f));
        weights_[delay_] = {1.0f, 0.0f};
        for (int i = 0; i < num_taps_; ++i)
            for (int j = 0; j < num_taps_; ++j)
                P_[i][j] = (i == j) ? std::complex<float>(100.0f) : std::complex<float>(0.0f);
    }

private:
    int  num_taps_, delay_;
    float lambda_;
    std::vector<std::complex<float>> weights_;
    std::vector<std::complex<float>> buf_;
    std::vector<std::vector<std::complex<float>>> P_;  // num_taps × num_taps

    void shift_in(std::complex<float> x) {
        for (int k = num_taps_ - 1; k > 0; --k) buf_[k] = buf_[k-1];
        buf_[0] = x;
    }

    void rls_update(std::complex<float> d) {
        // k = P * x / (lambda + x^H * P * x)
        std::vector<std::complex<float>> Px(num_taps_, {0.0f});
        for (int i = 0; i < num_taps_; ++i)
            for (int j = 0; j < num_taps_; ++j)
                Px[i] += P_[i][j] * buf_[j];

        std::complex<float> denom = {lambda_, 0.0f};
        for (int j = 0; j < num_taps_; ++j)
            denom += std::conj(buf_[j]) * Px[j];

        std::vector<std::complex<float>> k(num_taps_);
        for (int i = 0; i < num_taps_; ++i)
            k[i] = Px[i] / denom;

        // Error
        std::complex<float> y(0.0f);
        for (int i = 0; i < num_taps_; ++i) y += weights_[i] * buf_[i];
        std::complex<float> e = d - y;

        // w += k * e
        for (int i = 0; i < num_taps_; ++i) weights_[i] += k[i] * std::conj(e);

        // P = (P - k * x^H * P) / lambda
        for (int i = 0; i < num_taps_; ++i)
            for (int j = 0; j < num_taps_; ++j)
                P_[i][j] = (P_[i][j] - k[i] * std::conj(Px[j])) / lambda_;
    }
};


// ─────────────────────────────────────────────────────────────
//  4.  DFEqualizer  – Decision-Feedback Equalizer
//
//  Forward filter (FF) of length ff_taps on received signal.
//  Feedback filter (FB) of length fb_taps on past decisions.
//  LMS update on both filter banks.
// ─────────────────────────────────────────────────────────────
class DFEqualizer {
public:
    DFEqualizer(int ff_taps = 7, int fb_taps = 3, float mu = 0.005f)
        : ff_taps_(ff_taps), fb_taps_(fb_taps), mu_(mu),
          ff_delay_((ff_taps - 1) / 2),
          ff_w_(ff_taps, {0.0f}), fb_w_(fb_taps, {0.0f}),
          ff_buf_(ff_taps, {0.0f}), fb_buf_(fb_taps, {0.0f})
    {
        ff_w_[ff_delay_] = {1.0f, 0.0f};
        std::cout << "[DFEqualizer] FF=" << ff_taps
                  << "  FB=" << fb_taps
                  << "  mu=" << mu << "\n";
    }

    int delay() const { return ff_delay_; }

    void reset() {
        std::fill(ff_w_.begin(), ff_w_.end(), std::complex<float>(0.0f));
        std::fill(fb_w_.begin(), fb_w_.end(), std::complex<float>(0.0f));
        std::fill(ff_buf_.begin(), ff_buf_.end(), std::complex<float>(0.0f));
        std::fill(fb_buf_.begin(), fb_buf_.end(), std::complex<float>(0.0f));
        ff_w_[ff_delay_] = {1.0f, 0.0f};
    }

    void train(const std::vector<std::complex<float>>& received,
               const std::vector<std::complex<float>>& ideal,
               const Modulator& mod)
    {
        size_t N = std::min(received.size(), ideal.size());
        for (size_t n = 0; n < N; ++n) {
            ff_shift(received[n]);
            std::complex<float> y = ff_out() - fb_out();
            size_t ref = (n >= static_cast<size_t>(ff_delay_)) ? n - ff_delay_ : 0;
            std::complex<float> d = ideal[std::min(ref, ideal.size()-1)];
            std::complex<float> e = d - y;

            for (int k = 0; k < ff_taps_; ++k)
                ff_w_[k] += mu_ * e * std::conj(ff_buf_[k]);
            for (int k = 0; k < fb_taps_; ++k)
                fb_w_[k] -= mu_ * e * std::conj(fb_buf_[k]);  // note sign

            // Use ideal as the feedback decision during training
            fb_shift(d);
        }
        std::cout << "[DFEqualizer] Training done  N=" << N << "\n";
    }

    std::vector<std::complex<float>>
    equalize(const std::vector<std::complex<float>>& received, const Modulator& mod)
    {
        const auto& C = mod.get_constellation();
        std::vector<std::complex<float>> out;
        out.reserve(received.size());
        for (const auto& r : received) {
            ff_shift(r);
            std::complex<float> y = ff_out() - fb_out();
            out.push_back(y);

            // Hard decision for feedback
            int nearest = 0;
            float min_d = std::norm(y - C[0]);
            for (int j = 1; j < static_cast<int>(C.size()); ++j) {
                float d = std::norm(y - C[j]);
                if (d < min_d) { min_d = d; nearest = j; }
            }
            std::complex<float> decision = C[nearest];

            std::complex<float> e = decision - y;
            for (int k = 0; k < ff_taps_; ++k)
                ff_w_[k] += mu_ * e * std::conj(ff_buf_[k]);
            for (int k = 0; k < fb_taps_; ++k)
                fb_w_[k] -= mu_ * e * std::conj(fb_buf_[k]);

            fb_shift(decision);
        }
        return out;
    }

private:
    int ff_taps_, fb_taps_, ff_delay_;
    float mu_;
    std::vector<std::complex<float>> ff_w_, fb_w_, ff_buf_, fb_buf_;

    void ff_shift(std::complex<float> x) {
        for (int k = ff_taps_-1; k > 0; --k) ff_buf_[k] = ff_buf_[k-1];
        ff_buf_[0] = x;
    }
    void fb_shift(std::complex<float> x) {
        for (int k = fb_taps_-1; k > 0; --k) fb_buf_[k] = fb_buf_[k-1];
        fb_buf_[0] = x;
    }
    std::complex<float> ff_out() const {
        std::complex<float> y(0.0f);
        for (int k = 0; k < ff_taps_; ++k) y += ff_w_[k] * ff_buf_[k];
        return y;
    }
    std::complex<float> fb_out() const {
        std::complex<float> y(0.0f);
        for (int k = 0; k < fb_taps_; ++k) y += fb_w_[k] * fb_buf_[k];
        return y;
    }
};


// ─────────────────────────────────────────────────────────────
//  5.  channel_eq_thread
//
//  Sits between phase_offset_thread and demodulation_thread.
//  Trains on the preamble portion of each block then equalizes the rest.
// ─────────────────────────────────────────────────────────────
enum class EqType { LMS, RLS, DFE };

void channel_eq_thread(
    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& input_fifo,
    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& output_fifo,
    const std::vector<std::complex<float>>& preamble,
    const Modulator& mod,
    EqType eq_type,
    int num_taps,
    float step_size,    // mu for LMS/DFE, lambda for RLS
    bool decision_directed,
    std::atomic<bool>& stop_sign)
{
    // Create selected equalizer
    std::unique_ptr<LMSEqualizer> lms;
    std::unique_ptr<RLSEqualizer> rls;
    std::unique_ptr<DFEqualizer>  dfe;

    if (eq_type == EqType::LMS)
        lms = std::make_unique<LMSEqualizer>(num_taps, step_size);
    else if (eq_type == EqType::RLS)
        rls = std::make_unique<RLSEqualizer>(num_taps, step_size);
    else
        dfe = std::make_unique<DFEqualizer>(num_taps, num_taps/2, step_size);

    std::pair<size_t, std::vector<std::complex<float>>> msg;
    size_t processed = 0;
    int plen = static_cast<int>(preamble.size());

    // Differential schemes need the last preamble symbol as the decoder's phase
    // reference (the TX encoded the first data symbol relative to preamble.back()).
    // After equalization the data is back in the IDEAL constellation frame, so the
    // correct reference is the ideal preamble.back() — we prepend it to the
    // equalized data so differential_decode returns exactly N symbols (not N-1),
    // keeping the FEC/CRC framing aligned. Mirrors the no-equalizer path.
    const bool differential = mod.is_differential_scheme();
    const std::complex<float> diff_ref =
        preamble.empty() ? std::complex<float>(1.0f, 0.0f) : preamble.back();

    std::cout << "[channel_eq_thread] Started  type="
              << (eq_type==EqType::LMS?"LMS":eq_type==EqType::RLS?"RLS":"DFE")
              << "  taps=" << num_taps << "\n";

    DrainGate gate;
    while (gate.keep_going(stop_sign, input_fifo)) {
        if (!input_fifo.pop(msg)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
            continue;
        }

        const auto& syms = msg.second;
        if (static_cast<int>(syms.size()) <= plen) {
            // Not enough symbols for any data after the preamble — nothing to
            // demodulate. Forward an empty block to keep block-id ordering.
            std::cout << "[channel_eq_thread] Block " << msg.first
                      << " too short (" << syms.size() << " <= preamble "
                      << plen << ") — no data\n";
            output_fifo.push({msg.first, std::vector<std::complex<float>>{}});
            continue;
        }

        // Split into preamble and data
        std::vector<std::complex<float>> rx_preamble(syms.begin(),
                                                      syms.begin() + plen);
        std::vector<std::complex<float>> rx_data    (syms.begin() + plen,
                                                      syms.end());

        // SNR estimate
        float snr = SNREstimator::estimate_pilot(rx_preamble, preamble);
        std::cout << "[channel_eq_thread] Block " << msg.first
                  << "  SNR=" << snr << " dB\n";

        // Each burst is independent: RESET the equalizer, TRAIN on this burst's
        // preamble, then equalize the data. The equalizer has a centre-tap DELAY
        // of `D` symbols, so its output at index i estimates data[i-D]. We feed
        // `D` zero-runway symbols after the data so every data symbol is emitted,
        // then take the delay-aligned window out[D .. D+ndata-1]. (Without this
        // the whole data block was shifted by D and demod/decode misaligned —
        // that was the equalizer's real failure, not "divergence".)
        const Modulator* dd_mod = decision_directed ? &mod : nullptr;
        const int ndata = static_cast<int>(rx_data.size());
        std::vector<std::complex<float>> eq_data;
        int D = 0;

        if (eq_type == EqType::LMS) {
            lms->reset();
            lms->train(rx_preamble, preamble);
            D = lms->delay();
            std::vector<std::complex<float>> run = rx_data;
            run.insert(run.end(), D, std::complex<float>(0.0f));
            auto y = lms->equalize(run, dd_mod);
            if ((int)y.size() >= D + ndata)
                eq_data.assign(y.begin() + D, y.begin() + D + ndata);
        } else if (eq_type == EqType::RLS) {
            rls->reset();
            rls->train(rx_preamble, preamble);
            D = rls->delay();
            std::vector<std::complex<float>> run = rx_data;
            run.insert(run.end(), D, std::complex<float>(0.0f));
            auto y = rls->equalize(run, dd_mod);
            if ((int)y.size() >= D + ndata)
                eq_data.assign(y.begin() + D, y.begin() + D + ndata);
        } else {
            dfe->reset();
            dfe->train(rx_preamble, preamble, mod);
            D = dfe->delay();
            std::vector<std::complex<float>> run = rx_data;
            run.insert(run.end(), D, std::complex<float>(0.0f));
            auto y = dfe->equalize(run, mod);
            if ((int)y.size() >= D + ndata)
                eq_data.assign(y.begin() + D, y.begin() + D + ndata);
        }

        // Forward the equalized, delay-aligned DATA symbols. For differential
        // schemes prepend the ideal reference so the decoder sees [ref | data]
        // and returns exactly ndata symbols.
        if (differential) {
            eq_data.insert(eq_data.begin(), diff_ref);
        }
        output_fifo.push({msg.first, std::move(eq_data)});
        ++processed;
    }

    std::cout << "[channel_eq_thread] Stopped. Processed "
              << processed << " blocks.\n";
}
