#pragma once
// ============================================================
//  modulator_extended.hpp
//
//  Extends the existing Modulator / ModulationType infrastructure
//  with additional schemes.  Include this file AFTER modulator.hpp.
//
//  Added schemes
//  ─────────────
//  PI4_QPSK    – π/4-shifted DQPSK; robust to non-constant envelope
//  APSK16      – 16-APSK (DVB-S2 style): 4+12 ring layout
//  APSK32      – 32-APSK:               4+12+16 ring layout
//  DQAM16      – already in ModulationType; adds string name "DQAM16"
//
//  Also provides:
//  • string_to_mod_type()  – extended version that covers all schemes
//  • make_modulator()      – factory that returns a configured Modulator
// ============================================================

#include "modulator.hpp"
#include <complex>
#include <vector>
#include <string>
#include <stdexcept>
#include <cmath>
#include <iostream>

// ─────────────────────────────────────────────────────────────
//  π/4-QPSK helper
//
//  The constellation alternates between two QPSK grids offset by π/4.
//  This is a differential scheme, so each symbol encodes the *change*
//  in phase rather than an absolute angle.
// ─────────────────────────────────────────────────────────────
class PI4QPSKModulator {
public:
    // Encode bits (must be even length) → time-domain complex symbols
    std::vector<std::complex<float>>
    encode(const std::vector<uint8_t>& bits) {
        std::vector<std::complex<float>> out;
        out.reserve(bits.size() / 2);
        for (size_t i = 0; i + 1 < bits.size(); i += 2) {
            // Dibit → phase shift, indexed by plain binary idx=(b0<<1)|b1.
            // Order chosen so consecutive phases differ by one bit (Gray):
            //   00→+π/4, 01→+3π/4, 10→-π/4, 11→-3π/4
            static const float deltas[4] = {
                static_cast<float>( M_PI / 4.0),        // 00
                static_cast<float>( 3.0 * M_PI / 4.0),  // 01
                static_cast<float>(-M_PI / 4.0),        // 10
                static_cast<float>(-3.0 * M_PI / 4.0)   // 11
            };
            int idx = (bits[i] << 1) | bits[i + 1];
            phase_ += deltas[idx];
            // Normalise phase
            if (phase_ >  static_cast<float>(M_PI)) phase_ -= static_cast<float>(2.0 * M_PI);
            if (phase_ < -static_cast<float>(M_PI)) phase_ += static_cast<float>(2.0 * M_PI);
            out.push_back(std::polar(1.0f, phase_));
        }
        return out;
    }

    // Decode received symbols → bits
    std::vector<uint8_t>
    decode(const std::vector<std::complex<float>>& syms) {
        std::vector<uint8_t> bits;
        bits.reserve(syms.size() * 2);
        for (size_t i = 1; i < syms.size(); ++i) {
            float delta = std::arg(syms[i] * std::conj(syms[i - 1]));
            // Map delta → 2 bits (nearest of the four phase shifts).
            // Must match encode(): plain-binary index → dibit.
            static const float deltas[4] = {
                static_cast<float>( M_PI / 4.0),        // 00
                static_cast<float>( 3.0 * M_PI / 4.0),  // 01
                static_cast<float>(-M_PI / 4.0),        // 10
                static_cast<float>(-3.0 * M_PI / 4.0)   // 11
            };
            static const int indices[4][2] = {{0,0},{0,1},{1,0},{1,1}};
            int best = 0;
            float best_err = std::abs(delta - deltas[0]);
            for (int j = 1; j < 4; ++j) {
                float err = std::abs(delta - deltas[j]);
                if (err < best_err) { best_err = err; best = j; }
            }
            bits.push_back(indices[best][0]);
            bits.push_back(indices[best][1]);
        }
        return bits;
    }

    void reset() { phase_ = 0.0f; }

private:
    float phase_ = 0.0f;
};


// ─────────────────────────────────────────────────────────────
//  APSK helper (ring-based constellations)
// ─────────────────────────────────────────────────────────────
class APSKModulator {
public:
    struct Ring {
        int    num_points;
        float  radius;
        float  phase_offset;   // radians, first point angle
    };

    explicit APSKModulator(const std::vector<Ring>& rings)
        : rings_(rings)
    {
        build_constellation();
        std::cout << "[APSK] " << constellation_.size()
                  << "-point constellation built across "
                  << rings_.size() << " rings\n";
    }

    // Hard-decision demodulation
    std::vector<uint8_t>
    demodulate(const std::vector<std::complex<float>>& syms) const {
        std::vector<uint8_t> bits;
        int bps = static_cast<int>(std::log2(constellation_.size()));
        bits.reserve(syms.size() * bps);
        for (const auto& s : syms) {
            int nearest = 0;
            float min_d = std::norm(s - constellation_[0]);
            for (size_t i = 1; i < constellation_.size(); ++i) {
                float d = std::norm(s - constellation_[i]);
                if (d < min_d) { min_d = d; nearest = static_cast<int>(i); }
            }
            for (int b = bps - 1; b >= 0; --b)
                bits.push_back((nearest >> b) & 1);
        }
        return bits;
    }

    std::vector<std::complex<float>>
    modulate(const std::vector<uint8_t>& bits) const {
        int bps = static_cast<int>(std::log2(constellation_.size()));
        std::vector<std::complex<float>> syms;
        for (int i = 0; i + bps <= static_cast<int>(bits.size()); i += bps) {
            int idx = 0;
            for (int b = 0; b < bps; ++b)
                idx = (idx << 1) | bits[i + b];
            syms.push_back(constellation_[idx % constellation_.size()]);
        }
        return syms;
    }

    const std::vector<std::complex<float>>& constellation() const {
        return constellation_;
    }

    // Factory: 16-APSK (4+12)  – DVB-S2 like, R1/R2 ≈ 2.53
    static APSKModulator make_16APSK() {
        return APSKModulator({
            {4,  1.0f,  static_cast<float>(M_PI / 4.0)},   // inner ring: 4 points
            {12, 2.53f, static_cast<float>(M_PI / 12.0)}   // outer ring: 12 points
        });
    }

    // Factory: 32-APSK (4+12+16)
    static APSKModulator make_32APSK() {
        return APSKModulator({
            {4,  1.0f,  static_cast<float>(M_PI / 4.0)},
            {12, 2.53f, static_cast<float>(M_PI / 12.0)},
            {16, 4.30f, 0.0f}
        });
    }

private:
    std::vector<Ring> rings_;
    std::vector<std::complex<float>> constellation_;

    void build_constellation() {
        constellation_.clear();
        for (const auto& ring : rings_) {
            for (int k = 0; k < ring.num_points; ++k) {
                float angle = ring.phase_offset
                    + 2.0f * static_cast<float>(M_PI) * k / ring.num_points;
                constellation_.push_back(std::polar(ring.radius, angle));
            }
        }
        normalise();
    }

    void normalise() {
        float avg_pow = 0.0f;
        for (const auto& p : constellation_) avg_pow += std::norm(p);
        avg_pow /= constellation_.size();
        float scale = 1.0f / std::sqrt(avg_pow);
        for (auto& p : constellation_) p *= scale;
    }
};


// ─────────────────────────────────────────────────────────────
//  Extended scheme → ModulationType mapping
//
//  Replaces the ad-hoc if-else chains in modulation_thread()
//  and demodulation_thread().  Call this instead.
// ─────────────────────────────────────────────────────────────
inline ModulationType string_to_mod_type(const std::string& scheme) {
    // QAM / PSK (absolute)
    if (scheme == "BPSK")    return ModulationType::BPSK;
    if (scheme == "QPSK")    return ModulationType::QPSK;
    if (scheme == "8PSK"  ||
        scheme == "8-PSK")   return ModulationType::PSK8;
    if (scheme == "16QAM" ||
        scheme == "16-QAM")  return ModulationType::QAM16;
    if (scheme == "32QAM" ||
        scheme == "32-QAM")  return ModulationType::QAM32;
    if (scheme == "64QAM" ||
        scheme == "64-QAM")  return ModulationType::QAM64;
    if (scheme == "128QAM"||
        scheme == "128-QAM") return ModulationType::QAM128;
    if (scheme == "256QAM"||
        scheme == "256-QAM") return ModulationType::QAM256;
    // Differential
    if (scheme == "DBPSK")   return ModulationType::DBPSK;
    if (scheme == "DQPSK")   return ModulationType::DQPSK;
    if (scheme == "8DPSK" ||
        scheme == "8-DPSK")  return ModulationType::DPSK8;
    if (scheme == "DQAM16"||
        scheme == "D16QAM")  return ModulationType::DQAM16;
    if (scheme == "DQAM32"||
        scheme == "D32QAM")  return ModulationType::DQAM32;
    if (scheme == "DQAM64"||
        scheme == "D64QAM")  return ModulationType::DQAM64;
    if (scheme == "DQAM128"||
        scheme == "D128QAM") return ModulationType::DQAM128;
    if (scheme == "DQAM256"||
        scheme == "D256QAM") return ModulationType::DQAM256;
    // Ring / phase schemes now wired into ModulationType + Modulator
    if (scheme == "16APSK" ||
        scheme == "16-APSK") return ModulationType::APSK16;
    if (scheme == "32APSK" ||
        scheme == "32-APSK") return ModulationType::APSK32;
    if (scheme == "PI4QPSK"  ||
        scheme == "PI4-QPSK" ||
        scheme == "P4QPSK")  return ModulationType::PI4QPSK;

    throw std::invalid_argument("[string_to_mod_type] Unknown scheme: " + scheme);
}

// Convenience factory
inline Modulator make_modulator(const std::string& scheme) {
    return Modulator(string_to_mod_type(scheme));
}

// ─────────────────────────────────────────────────────────────
//  Soft-decision (LLR) demodulation for AWGN channel
//
//  Returns log-likelihood ratios (LLRs) for each bit position.
//  Positive LLR  →  bit is more likely 0.
//  Negative LLR  →  bit is more likely 1.
//
//  Uses the Max-Log approximation for speed.
// ─────────────────────────────────────────────────────────────
inline std::vector<float>
soft_demodulate_llr(const std::vector<std::complex<float>>& syms,
                    const Modulator& mod,
                    float noise_variance = 1.0f)
{
    const auto& C   = mod.get_constellation();
    int bps         = mod.get_bits_per_symbol();
    int M           = static_cast<int>(C.size());
    float inv_2N0   = 1.0f / (2.0f * noise_variance);

    std::vector<float> llrs;
    llrs.reserve(syms.size() * bps);

    for (const auto& r : syms) {
        // For each bit position k, LLR = log( Σ_{c:bit k=0} p(r|c) / Σ_{c:bit k=1} p(r|c) )
        // Max-log: LLR ≈ min_d_over_bit1 - min_d_over_bit0   (with sign)
        for (int k = 0; k < bps; ++k) {
            float min_d0 = std::numeric_limits<float>::max();
            float min_d1 = std::numeric_limits<float>::max();
            for (int m = 0; m < M; ++m) {
                float d = std::norm(r - C[m]);
                int bit_k = (m >> (bps - 1 - k)) & 1;
                if (bit_k == 0) min_d0 = std::min(min_d0, d);
                else            min_d1 = std::min(min_d1, d);
            }
            llrs.push_back(inv_2N0 * (min_d1 - min_d0));
        }
    }
    return llrs;
}
