#pragma once
// ============================================================
//  taps.hpp
//  Filter tap generation: Root Raised Cosine, Raised Cosine,
//  and Low-Pass.  Called by filters.cpp.
// ============================================================
#include <complex>
#include <vector>
#include <cmath>
#include <iostream>

// ── Root Raised Cosine (RRC) ─────────────────────────────────
// out     : pointer to output array of length (2*half_len+1)
// half_len: (num_taps-1)/2
// U, D    : up/down-sampling factors; symbol period Ts = D/U samples
// beta    : roll-off factor [0, 1]
inline void rrc_pulse(std::complex<float>* out, int half_len,
                      int U, int D, double beta)
{
    int num_taps = 2 * half_len + 1;
    double Ts = static_cast<double>(U) / D;   // samples per symbol

    for (int i = 0; i < num_taps; i++) {
        double t = (i - half_len) / Ts;       // time in symbol periods

        double val;
        if (std::abs(t) < 1e-8) {
            // t = 0
            val = (1.0 + beta * (4.0 / M_PI - 1.0)) / Ts;
        } else if (std::abs(std::abs(t) - 1.0 / (4.0 * beta)) < 1e-8 && beta > 0) {
            // t = ±1/(4β) singularity
            val = (beta / (Ts * M_SQRT2))
                  * ((1.0 + 2.0 / M_PI) * std::sin(M_PI / (4.0 * beta))
                     + (1.0 - 2.0 / M_PI) * std::cos(M_PI / (4.0 * beta)));
        } else {
            double num = std::sin(M_PI * t * (1.0 - beta))
                       + 4.0 * beta * t * std::cos(M_PI * t * (1.0 + beta));
            double den = M_PI * t * (1.0 - (4.0 * beta * t) * (4.0 * beta * t));
            val = num / (den * Ts);
        }
        out[i] = static_cast<float>(val);
    }

    // Normalise to unit energy
    double energy = 0.0;
    for (int i = 0; i < num_taps; i++)
        energy += std::norm(out[i]);
    double scale = 1.0 / std::sqrt(energy);
    for (int i = 0; i < num_taps; i++)
        out[i] *= static_cast<float>(scale);
}

// ── Raised Cosine (RC) ───────────────────────────────────────
inline void rc_pulse(std::complex<float>* out, int half_len,
                     int U, int D, double beta)
{
    int num_taps = 2 * half_len + 1;
    double Ts = static_cast<double>(U) / D;

    for (int i = 0; i < num_taps; i++) {
        double t = (i - half_len) / Ts;
        double val;
        if (std::abs(t) < 1e-8) {
            val = 1.0 / Ts;
        } else if (beta > 0 &&
                   std::abs(std::abs(t) - Ts / (2.0 * beta)) < 1e-8) {
            val = std::sin(M_PI * t / Ts)
                  / (M_PI * t / Ts)
                  * (M_PI / 4.0)
                  / Ts;
        } else {
            double sinc = std::sin(M_PI * t / Ts) / (M_PI * t / Ts);
            double cosine = std::cos(M_PI * beta * t / Ts);
            double denom  = 1.0 - (2.0 * beta * t / Ts) * (2.0 * beta * t / Ts);
            val = sinc * cosine / (denom * Ts);
        }
        out[i] = static_cast<float>(val);
    }

    double energy = 0.0;
    for (int i = 0; i < num_taps; i++) energy += std::norm(out[i]);
    double scale = 1.0 / std::sqrt(energy);
    for (int i = 0; i < num_taps; i++) out[i] *= static_cast<float>(scale);
}

// ── Low-Pass (sinc) ──────────────────────────────────────────
// cutoff_norm : normalised cutoff frequency (0–1, where 1 = Nyquist)
inline void lp_pulse(std::complex<float>* out, int half_len,
                     double cutoff_norm = 0.5)
{
    int num_taps = 2 * half_len + 1;
    for (int i = 0; i < num_taps; i++) {
        double t = i - half_len;
        double val = (std::abs(t) < 1e-8)
                         ? 2.0 * cutoff_norm
                         : std::sin(2.0 * M_PI * cutoff_norm * t) / (M_PI * t);
        // Hann window
        double w = 0.5 * (1.0 - std::cos(2.0 * M_PI * i / (num_taps - 1)));
        out[i]   = static_cast<float>(val * w);
    }

    double energy = 0.0;
    for (int i = 0; i < num_taps; i++) energy += std::norm(out[i]);
    double scale = 1.0 / std::sqrt(energy);
    for (int i = 0; i < num_taps; i++) out[i] *= static_cast<float>(scale);
}
