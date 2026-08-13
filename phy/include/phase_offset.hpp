#pragma once

// ============================================================
//  phase_offset.hpp
//
//  Symbol-level carrier phase offset estimation and tracking
//  for single-carrier constellations (BPSK, QPSK, 16-QAM, …).
//
//  Background
//  ──────────
//  After timing sync the decision statistics are complex symbols
//  at the correct sample instants, but they carry a residual
//  phase offset  φ  from three sources:
//
//    1. Static phase offset  – oscillator phase difference at
//       the moment of acquisition.
//    2. Residual CFO         – any uncorrected frequency error
//       becomes a *linearly growing* phase ramp across the packet.
//    3. Channel phase        – multipath or hardware phase shift.
//
//  For differential modulations (DBPSK, DQPSK …) source 1 & 3
//  cancel automatically because demodulation looks at *changes*
//  in phase, and a static offset drops out.  Source 2 (ramp)
//  still causes problems there but is addressed by the CFO
//  corrector in frequency_offset.hpp.
//
//  For absolute modulations (BPSK, QPSK, 16-QAM, …) all three
//  sources must be removed before hard decisions are made.
//
//  This file provides:
//
//    PhaseOffsetEstimator  – one-shot estimators using the preamble
//       • Preamble correlation (ML phase estimate)
//       • M-th power (blind, for M-PSK)
//       • Decision-directed (decision-feedback, for QAM)
//
//    PhaseTracker          – block-by-block tracking loop (PLL-style)
//       • First-order loop: tracks static offset + slow drift
//       • Second-order loop: tracks offset + constant frequency ramp
//
//    PhaseOffsetCorrector  – combines estimation + tracking in one call
//
//    phase_offset_thread   – drop-in FIFO pipeline stage; sits between
//                            TimeSync output and demodulation input
// ============================================================

#include <complex>
#include <vector>
#include <cmath>
#include <iostream>
#include <algorithm>
#include <numeric>
#include <stdexcept>
#include <atomic>
#include <thread>
#include <chrono>

#include "FIFO.hpp"
#include "modulator.hpp"   // for Modulator, ModulationType


// ─────────────────────────────────────────────────────────────
//  Helper: rotate an entire block by a fixed angle
// ─────────────────────────────────────────────────────────────
inline std::vector<std::complex<float>>
rotate_block(const std::vector<std::complex<float>>& in, float angle_rad)
{
    std::complex<float> phasor = std::polar(1.0f, angle_rad);
    std::vector<std::complex<float>> out;
    out.reserve(in.size());
    for (const auto& s : in)
        out.push_back(s * phasor);
    return out;
}


// ─────────────────────────────────────────────────────────────
//  1.  PhaseOffsetEstimator
//
//  All methods return an estimated phase offset in radians.
//  Correct the signal by multiplying with exp(-j·φ_est).
// ─────────────────────────────────────────────────────────────
class PhaseOffsetEstimator {
public:

    // ── Method A: Preamble correlation (ML estimate) ──────────
    //
    // Uses the known preamble to get a maximum-likelihood estimate
    // of the static phase offset.  Best accuracy; requires the
    // ideal preamble to be available.
    //
    // received_syms : decision statistics starting at the first
    //                 preamble symbol (one sample per symbol).
    // preamble      : ideal preamble symbol sequence.
    // num_syms      : how many preamble symbols to use (default: all).
    //
    // Returns φ_est in [-π, π].
    static float estimate_preamble(
        const std::vector<std::complex<float>>& received_syms,
        const std::vector<std::complex<float>>& preamble,
        int num_syms = -1)
    {
        int N = (num_syms < 0)
                    ? static_cast<int>(std::min(received_syms.size(), preamble.size()))
                    : std::min(num_syms,
                               static_cast<int>(
                                   std::min(received_syms.size(), preamble.size())));

        if (N == 0) {
            std::cerr << "[PhaseOffsetEstimator][Preamble] No overlap – returning 0\n";
            return 0.0f;
        }

        // ML estimate: φ = arg( Σ r[n] · conj(s[n]) )
        std::complex<float> acc(0.0f, 0.0f);
        for (int n = 0; n < N; ++n)
            acc += received_syms[n] * std::conj(preamble[n]);

        float phi = std::arg(acc);
        std::cout << "[PhaseEstimator][Preamble] N=" << N
                  << "  φ_est=" << phi * 180.0f / static_cast<float>(M_PI)
                  << "°\n";
        return phi;
    }


    // ── Method B: M-th power (blind, PSK only) ────────────────
    //
    // Raises every received symbol to the M-th power to remove
    // the data modulation (for M-PSK the data phases are multiples
    // of 2π/M, so x^M always points in the same direction).
    // The phase of the averaged result divided by M is the offset.
    //
    // Works for BPSK (M=2), QPSK (M=4), 8-PSK (M=8).
    // NOT suitable for QAM (amplitude variation breaks the trick).
    //
    // received_syms : raw received symbols (any number).
    // M             : PSK order (must match the modulation used).
    //
    // Returns φ_est in [-π/M, π/M]  (there is an M-fold ambiguity;
    // the correct quadrant is resolved by the differential encoding
    // or by comparing to the preamble).
    static float estimate_mth_power(
        const std::vector<std::complex<float>>& received_syms,
        int M)
    {
        if (M <= 0) throw std::invalid_argument("[PhaseEstimator] M must be > 0");
        if (received_syms.empty()) return 0.0f;

        std::complex<float> acc(0.0f, 0.0f);
        for (const auto& s : received_syms) {
            // s^M: use polar form for numerical stability
            float mag = std::abs(s);
            float ang = std::arg(s);
            acc += std::polar(std::pow(mag, static_cast<float>(M)),
                              static_cast<float>(M) * ang);
        }

        float phi_M = std::arg(acc);      // phase of Σ s^M
        float phi   = phi_M / M;         // unwrap to one period

        std::cout << "[PhaseEstimator][Mth-power] M=" << M
                  << "  arg(Σs^M)=" << phi_M * 180.0f / static_cast<float>(M_PI)
                  << "°  φ_est=" << phi * 180.0f / static_cast<float>(M_PI)
                  << "°  (M-fold ambiguity – resolve with preamble)\n";
        return phi;
    }


    // ── Method C: Decision-directed (for QAM) ─────────────────
    //
    // Makes a tentative hard decision on each received symbol,
    // then measures the residual angle between the received point
    // and the decision.  Averages these to get a phase estimate.
    //
    // Most accurate for QAM when the SNR is moderate-to-high.
    // Requires a Modulator object (for the constellation).
    //
    // received_syms : raw received symbols.
    // mod           : Modulator with the correct constellation.
    // num_syms      : symbols to use (-1 = all).
    //
    // Returns φ_est in [-π, π].
    static float estimate_decision_directed(
        const std::vector<std::complex<float>>& received_syms,
        const Modulator& mod,
        int num_syms = -1)
    {
        const auto& C = mod.get_constellation();
        int N = (num_syms < 0)
                    ? static_cast<int>(received_syms.size())
                    : std::min(num_syms, static_cast<int>(received_syms.size()));

        if (N == 0) return 0.0f;

        std::complex<float> acc(0.0f, 0.0f);
        int used = 0;
        for (int i = 0; i < N; ++i) {
            // Nearest-neighbour decision
            int nearest = 0;
            float min_d = std::norm(received_syms[i] - C[0]);
            for (int j = 1; j < static_cast<int>(C.size()); ++j) {
                float d = std::norm(received_syms[i] - C[j]);
                if (d < min_d) { min_d = d; nearest = j; }
            }
            // Residual phasor: received / ideal
            if (std::abs(C[nearest]) > 1e-6f) {
                acc += received_syms[i] * std::conj(C[nearest]);
                ++used;
            }
        }

        if (used == 0) return 0.0f;

        float phi = std::arg(acc);
        std::cout << "[PhaseEstimator][DD] used=" << used
                  << "  φ_est=" << phi * 180.0f / static_cast<float>(M_PI)
                  << "°\n";
        return phi;
    }
};


// ─────────────────────────────────────────────────────────────
//  2.  PhaseTracker
//
//  Tracks a slowly time-varying phase offset symbol-by-symbol
//  using a digital PLL.  Two loop orders are supported:
//
//  First-order  (loop_bw only):
//    φ[n+1] = φ[n] + α · e[n]
//    Tracks a static offset; cannot follow a frequency ramp.
//
//  Second-order (loop_bw + damping):
//    Adds an integrator to track a constant phase ramp (= residual
//    CFO that survived the frequency-domain correction).
//
//  Error signal e[n] is computed decision-directed.
// ─────────────────────────────────────────────────────────────
class PhaseTracker {
public:

    // loop_bw   : normalised loop bandwidth (BnT), e.g. 0.01–0.05.
    //             Larger = faster tracking but more noise.
    // damping   : damping factor ζ for 2nd-order loop (0.707 is standard).
    //             Set to 0 for a first-order loop.
    // mod       : modulator for constellation decisions.
    explicit PhaseTracker(float loop_bw, float damping,
                          const Modulator& mod)
        : alpha_(compute_alpha(loop_bw, damping)),
          beta_ (compute_beta (loop_bw, damping)),
          phi_(0.0f),
          freq_(0.0f),
          mod_(mod),
          loop_bw_(loop_bw),
          damping_(damping)
    {
        std::cout << "[PhaseTracker] BnT=" << loop_bw
                  << "  ζ=" << damping
                  << "  α=" << alpha_
                  << "  β=" << beta_ << "\n";
    }

    // Process one block of received symbols.
    // Returns phase-corrected symbols; also tracks phase internally
    // so consecutive calls work across block boundaries.
    std::vector<std::complex<float>>
    process(const std::vector<std::complex<float>>& syms)
    {
        const auto& C = mod_.get_constellation();
        std::vector<std::complex<float>> out;
        out.reserve(syms.size());

        for (const auto& r : syms) {
            // 1. Apply current phase estimate
            std::complex<float> corrected = r * std::polar(1.0f, -phi_);
            out.push_back(corrected);

            // 2. Decision
            int nearest = 0;
            float min_d = std::norm(corrected - C[0]);
            for (int j = 1; j < static_cast<int>(C.size()); ++j) {
                float d = std::norm(corrected - C[j]);
                if (d < min_d) { min_d = d; nearest = j; }
            }

            // 3. Phase error: angle between corrected sample and ideal point
            //    e[n] = Im{ corrected · conj(decision) }  (approximate, small angle)
            std::complex<float> err_phasor = corrected * std::conj(C[nearest]);
            float e = std::arg(err_phasor);   // exact angle error in [-π,π]

            // 4. Loop filter update
            freq_ += beta_  * e;   // integrator (zero for 1st-order loop)
            phi_  += alpha_ * e + freq_;

            // Keep phi_ in [-π, π]
            while (phi_ >  static_cast<float>(M_PI)) phi_ -= 2.0f * static_cast<float>(M_PI);
            while (phi_ < -static_cast<float>(M_PI)) phi_ += 2.0f * static_cast<float>(M_PI);
        }

        return out;
    }

    // Reset the loop state (call between packets)
    void reset(float initial_phase = 0.0f) {
        phi_  = initial_phase;
        freq_ = 0.0f;
    }

    float get_phase()     const { return phi_;  }
    float get_frequency() const { return freq_; }

private:
    float alpha_, beta_;
    float phi_;       // current phase estimate (rad)
    float freq_;      // frequency accumulator for 2nd-order loop
    const Modulator& mod_;
    float loop_bw_, damping_;

    // Gardner / Proakis second-order loop coefficient derivation
    static float compute_alpha(float Bn_T, float zeta) {
        // For first-order loop (zeta == 0), alpha = Bn_T directly
        if (zeta < 1e-6f) return Bn_T;
        float theta_n = Bn_T / (zeta + 1.0f / (4.0f * zeta));
        return (4.0f * zeta * theta_n) / (1.0f + 2.0f * zeta * theta_n
                                           + theta_n * theta_n);
    }

    static float compute_beta(float Bn_T, float zeta) {
        if (zeta < 1e-6f) return 0.0f;
        float theta_n = Bn_T / (zeta + 1.0f / (4.0f * zeta));
        return (4.0f * theta_n * theta_n) / (1.0f + 2.0f * zeta * theta_n
                                              + theta_n * theta_n);
    }
};


// ─────────────────────────────────────────────────────────────
//  2b.  FreqTracker  —  blind M-th-power frequency-locked loop
//
//  Purpose: give DIFFERENTIAL schemes (DBPSK/DQPSK/8-DPSK) the mid-burst
//  residual-CFO tracking that the decision-directed PhaseTracker cannot provide
//  for them (differential data has no absolute-phase decision to lock onto). The
//  one-shot CFO stage (§5.4) leaves a slow phase ramp = residual CFO; for a
//  differential burst nothing currently follows it, so a long burst rotates.
//
//  How it works: raising an M-PSK symbol to the M-th power strips the data
//  (s^M = const · e^{jMφ}), leaving a pure carrier tone at M×(residual CFO). A
//  cross-product discriminator on consecutive M-th-power samples measures that
//  residual frequency WITHOUT any constellation decision, so it is immune to the
//  differential-vs-coherent distinction. The estimate drives an NCO that
//  derotates the stream — a first-order FLL (frequency integrator + phase accum).
//
//  M must be the number of DISTINCT phase points (2/4/8). Note: π/4-DQPSK alternates
//  two QPSK grids → use M=8 (its 8 distinct phases), not 4, or the M-th power picks
//  up a spurious ±π per symbol. Blind M-th power does NOT strip QAM — this loop is
//  for PSK/DPSK only (dense QAM is clock-limited anyway, §13).
//
//  STATUS — retained as a building block, NOT wired into the pipeline. Hardware-free
//  sims (scratchpad/fll_test.cpp, fll_coh_test.cpp) showed it does not help this
//  link: differential detection is already immune to constant/slow residual CFO up
//  to ±45°, so an FLL only adds M-th-power noise there; and the coherent path's
//  existing 2nd-order phase PLL (§5.5) already pulls in CFO to its decision limit, so
//  an FLL front-end adds nothing. With the §5.4-C LS estimator leaving ~milliradian
//  residual per symbol, no tracker is needed. Kept for a future large-CFO / coherent
//  acquisition experiment; if you wire it, re-validate against the phase PLL first.
// ─────────────────────────────────────────────────────────────
class FreqTracker {
public:
    // M       : PSK order (distinct phases): 2=BPSK/DBPSK, 4=QPSK/DQPSK, 8=8-PSK/8-DPSK.
    // loop_bw : FLL gain (per-symbol); small, e.g. 0.005–0.02. Larger = faster
    //           pull-in but noisier (noise is raised to the M-th power).
    FreqTracker(int M, float loop_bw)
        : M_(M < 1 ? 1 : M), mu_(loop_bw),
          phi_(0.0f), freq_(0.0f), have_prev_(false), prev_(0.0f, 0.0f)
    {
        std::cout << "[FreqTracker] M=" << M_ << "  loop_bw=" << mu_ << "\n";
    }

    std::vector<std::complex<float>>
    process(const std::vector<std::complex<float>>& syms)
    {
        std::vector<std::complex<float>> out;
        out.reserve(syms.size());
        for (const auto& r : syms) {
            // 1. Derotate by the current NCO estimate.
            std::complex<float> c = r * std::polar(1.0f, -phi_);
            out.push_back(c);

            // 2. Blind M-th-power frequency discriminator (needs a previous sample).
            if (have_prev_) {
                std::complex<float> a = ipow(c,     M_);
                std::complex<float> b = ipow(prev_, M_);
                // arg(a·conj(b)) = M·(residual phase advance per symbol); /M → rad/sym.
                float e = std::arg(a * std::conj(b)) / static_cast<float>(M_);

                // 3. First-order FLL: integrate frequency, advance phase.
                freq_ += mu_ * e;
                phi_  += freq_;
                while (phi_ >  static_cast<float>(M_PI)) phi_ -= 2.0f * static_cast<float>(M_PI);
                while (phi_ < -static_cast<float>(M_PI)) phi_ += 2.0f * static_cast<float>(M_PI);
            }
            prev_      = c;
            have_prev_ = true;
        }
        return out;
    }

    void  reset() { phi_ = 0.0f; freq_ = 0.0f; have_prev_ = false; prev_ = {0.0f, 0.0f}; }
    float get_frequency() const { return freq_; }   // rad/symbol

private:
    static std::complex<float> ipow(std::complex<float> x, int n) {
        std::complex<float> r(1.0f, 0.0f);
        for (int i = 0; i < n; ++i) r *= x;
        return r;
    }
    int   M_;
    float mu_;
    float phi_;        // NCO phase (rad)
    float freq_;       // NCO frequency (rad/sym) — the residual-CFO estimate
    bool  have_prev_;
    std::complex<float> prev_;
};


// ─────────────────────────────────────────────────────────────
//  3.  PhaseOffsetCorrector
//
//  Full pipeline:
//    (a) One-shot preamble-based estimate → rotate the whole block.
//    (b) Optionally run a PhaseTracker over the data portion to
//        follow any remaining slow drift.
//
//  Constructor parameters
//  ──────────────────────
//  mod              : modulator (determines constellation + modulation type)
//  preamble         : ideal preamble symbols (length = preamble_len)
//  preamble_len     : symbols to use for the initial estimate
//  use_tracker      : enable decision-directed tracking after bulk rotation
//  loop_bw          : normalised PLL bandwidth (ignored if !use_tracker)
//  damping          : PLL damping factor (0 = first-order loop)
// ─────────────────────────────────────────────────────────────
class PhaseOffsetCorrector {
public:

    enum class EstimationMethod {
        PREAMBLE,          // ML from known preamble (best, requires preamble)
        MTH_POWER,         // blind M-th power (PSK only)
        DECISION_DIRECTED  // blind DD  (QAM, moderate SNR)
    };

    PhaseOffsetCorrector(
        const Modulator& mod,
        const std::vector<std::complex<float>>& preamble,
        int  preamble_len       = -1,
        bool use_tracker        = true,
        float loop_bw           = 0.02f,
        float damping           = 0.707f,
        EstimationMethod method = EstimationMethod::PREAMBLE)
        : mod_(mod),
          preamble_(preamble),
          preamble_len_(preamble_len < 0
                            ? static_cast<int>(preamble.size())
                            : preamble_len),
          use_tracker_(use_tracker),
          method_(method),
          tracker_(loop_bw, damping, mod),
          last_phase_est_(0.0f)
    {}

    // Correct phase in one block of decision statistics.
    //
    // syms : symbols after timing sync (one per symbol epoch).
    //        The first preamble_len symbols are the known preamble.
    //
    // Returns phase-corrected symbols (same length as input).
    std::vector<std::complex<float>>
    correct(const std::vector<std::complex<float>>& syms)
    {
        if (syms.empty()) return {};

        // ── Step 1: bulk phase estimate from preamble ────────
        float phi_est = 0.0f;

        if (method_ == EstimationMethod::PREAMBLE && !preamble_.empty()) {
            phi_est = PhaseOffsetEstimator::estimate_preamble(
                          syms, preamble_, preamble_len_);

        } else if (method_ == EstimationMethod::MTH_POWER) {
            // Determine M from the constellation size
            int M = mod_.get_constellation_size();
            phi_est = PhaseOffsetEstimator::estimate_mth_power(syms, M);

        } else if (method_ == EstimationMethod::DECISION_DIRECTED) {
            // Use the first 32 symbols for a blind estimate
            int N = std::min(32, static_cast<int>(syms.size()));
            phi_est = PhaseOffsetEstimator::estimate_decision_directed(
                          syms, mod_, N);
        }

        last_phase_est_ = phi_est;
        std::cout << "[PhaseOffsetCorrector] Bulk rotation: -"
                  << phi_est * 180.0f / static_cast<float>(M_PI) << "°\n";

        // ── Step 2: bulk rotation ────────────────────────────
        auto rotated = rotate_block(syms, -phi_est);

        // ── Step 3: optional PLL tracking ───────────────────
        if (use_tracker_) {
            // Track over the DATA symbols only. Two reasons the preamble must be
            // excluded from the decision-directed loop:
            //  (1) The block was already bulk-derotated in step 2, so the loop
            //      must start from zero (seeding it with -phi_est re-rotated the
            //      block by +phi_est on entry and reintroduced the whole offset).
            //  (2) The preamble is BPSK (points at 0°/180°); it does NOT lie on
            //      the data constellation. Feeding it to a decision-directed loop
            //      that decides against the data constellation produces a spurious
            //      ±(180/M)° error on every preamble symbol, so the loop drifts
            //      (e.g. QPSK slipped to −90°, APSK wandered ~20°) before the data
            //      even begins. With clean data and a zero seed the error stays ~0
            //      and the loop only follows genuine residual drift.
            // The preamble is stripped before demodulation anyway, so its
            // (bulk-rotated) values are left untouched here.
            tracker_.reset(0.0f);
            int start = std::min(preamble_len_, static_cast<int>(rotated.size()));
            std::vector<std::complex<float>> data_part(rotated.begin() + start,
                                                       rotated.end());
            auto tracked = tracker_.process(data_part);
            for (size_t i = 0; i < tracked.size(); ++i)
                rotated[start + i] = tracked[i];

            std::cout << "[PhaseOffsetCorrector] Tracker residual phase: "
                      << tracker_.get_phase() * 180.0f / static_cast<float>(M_PI)
                      << "°  freq: " << tracker_.get_frequency() << " rad/sym\n";
        }

        return rotated;
    }

    float get_last_phase_estimate() const { return last_phase_est_; }
    PhaseTracker& tracker() { return tracker_; }

private:
    const Modulator&                 mod_;
    std::vector<std::complex<float>> preamble_;
    int                              preamble_len_;
    bool                             use_tracker_;
    EstimationMethod                 method_;
    PhaseTracker                     tracker_;
    float                            last_phase_est_;
};


// ─────────────────────────────────────────────────────────────
//  4.  phase_offset_thread
//
//  Drop-in pipeline stage.  Insert between TimeSync_thread
//  output (synced_fifo) and demodulation_thread input.
//
//  synced_fifo   → [phase_offset_thread] → phase_corrected_fifo
//                                                ↓
//                                         demodulation_thread
//
//  Parameters
//  ──────────
//  input_fifo   : synced_fifo from TimeSync_thread
//  output_fifo  : feeds demodulation_thread
//  mod          : Modulator (determines which estimator to use)
//  preamble     : ideal preamble symbols (at symbol rate)
//  preamble_len : how many symbols to use for the bulk estimate
//  use_tracker  : enable decision-directed PLL tracking
//  loop_bw      : PLL normalised bandwidth (e.g. 0.01 – 0.05)
//  damping      : PLL damping (0.707 = critically damped)
//  method       : PREAMBLE / MTH_POWER / DECISION_DIRECTED
//  stop_sign    : shared stop flag
// ─────────────────────────────────────────────────────────────
void phase_offset_thread(
    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& input_fifo,
    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& output_fifo,
    const Modulator& mod,
    const std::vector<std::complex<float>>& preamble,
    int   preamble_len,
    bool  use_tracker,
    float loop_bw,
    float damping,
    PhaseOffsetCorrector::EstimationMethod method,
    std::atomic<bool>& stop_sign)
{
    PhaseOffsetCorrector corrector(mod, preamble, preamble_len,
                                   use_tracker, loop_bw, damping, method);

    std::pair<size_t, std::vector<std::complex<float>>> msg;
    size_t processed = 0;

    std::cout << "[phase_offset_thread] Started"
              << "  use_tracker=" << use_tracker
              << "  loop_bw=" << loop_bw
              << "  damping=" << damping << "\n";

    DrainGate gate;
    while (gate.keep_going(stop_sign, input_fifo)) {
        if (!input_fifo.pop(msg)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
            continue;
        }

        auto corrected = corrector.correct(msg.second);
        output_fifo.push({msg.first, std::move(corrected)});
        ++processed;

        std::cout << "[phase_offset_thread] Block " << msg.first
                  << "  φ_est="
                  << corrector.get_last_phase_estimate() * 180.0f
                     / static_cast<float>(M_PI)
                  << "°  symbols_out=" << corrected.size() << "\n";
    }

    std::cout << "[phase_offset_thread] Stopped. Processed "
              << processed << " blocks.\n";
}
