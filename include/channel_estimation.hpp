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
    // mu       : step size (0.001 – 0.1; larger = faster but unstable)
    LMSEqualizer(int num_taps = 11, float mu = 0.01f)
        : num_taps_(num_taps),
          mu_(mu),
          delay_((num_taps - 1) / 2),
          weights_(num_taps, std::complex<float>(0.0f)),
          buf_(num_taps, std::complex<float>(0.0f))
    {
        // Initialise centre tap to 1 (identity start)
        weights_[delay_] = {1.0f, 0.0f};
        std::cout << "[LMSEqualizer] taps=" << num_taps
                  << "  mu=" << mu
                  << "  delay=" << delay_ << "\n";
    }

    // Train on known preamble.
    // received : received preamble symbols (at 1-sps after timing recovery)
    // ideal    : ideal preamble symbols
    void train(const std::vector<std::complex<float>>& received,
               const std::vector<std::complex<float>>& ideal)
    {
        size_t N = std::min(received.size(), ideal.size());
        float total_error = 0.0f;
        for (size_t n = 0; n < N; ++n) {
            // Shift new sample into buffer
            shift_in(received[n]);
            // Filter output
            std::complex<float> y = filter_output();
            // Error: desired - output (with delay compensation)
            size_t ref_idx = (n >= static_cast<size_t>(delay_))
                                 ? n - delay_ : 0;
            std::complex<float> d = ideal[std::min(ref_idx, ideal.size()-1)];
            std::complex<float> e = d - y;
            total_error += std::norm(e);
            // LMS update: w += mu * e * conj(x)
            for (int k = 0; k < num_taps_; ++k)
                weights_[k] += mu_ * e * std::conj(buf_[k]);
        }
        std::cout << "[LMSEqualizer] Training done  N=" << N
                  << "  MSE=" << total_error / N << "\n";
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
                // Decision-directed: make hard decision, use as reference
                const auto& C = mod->get_constellation();
                int nearest = 0;
                float min_d = std::norm(y - C[0]);
                for (int j = 1; j < static_cast<int>(C.size()); ++j) {
                    float d = std::norm(y - C[j]);
                    if (d < min_d) { min_d = d; nearest = j; }
                }
                std::complex<float> e = C[nearest] - y;
                for (int k = 0; k < num_taps_; ++k)
                    weights_[k] += mu_ * e * std::conj(buf_[k]);
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
    int num_taps_, delay_;
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

    std::cout << "[channel_eq_thread] Started  type="
              << (eq_type==EqType::LMS?"LMS":eq_type==EqType::RLS?"RLS":"DFE")
              << "  taps=" << num_taps << "\n";

    while (!stop_sign || input_fifo.size() > 0) {
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

        // Train + equalize
        std::vector<std::complex<float>> eq_preamble, eq_data;
        const Modulator* dd_mod = decision_directed ? &mod : nullptr;

        if (eq_type == EqType::LMS) {
            lms->train(rx_preamble, preamble);
            eq_preamble = lms->equalize(rx_preamble);
            eq_data     = lms->equalize(rx_data, dd_mod);
        } else if (eq_type == EqType::RLS) {
            rls->train(rx_preamble, preamble);
            eq_preamble = rls->equalize(rx_preamble);
            eq_data     = rls->equalize(rx_data, dd_mod);
        } else {
            dfe->train(rx_preamble, preamble, mod);
            eq_preamble = dfe->equalize(rx_preamble, mod);
            eq_data     = dfe->equalize(rx_data,    mod);
        }

        // The preamble has done its job (training); strip it and forward only
        // the equalized DATA symbols to the demodulator. (eq_preamble is kept
        // above only so the equalizer state is warmed up over the pilot.)
        (void)eq_preamble;
        output_fifo.push({msg.first, std::move(eq_data)});
        ++processed;
    }

    std::cout << "[channel_eq_thread] Stopped. Processed "
              << processed << " blocks.\n";
}
