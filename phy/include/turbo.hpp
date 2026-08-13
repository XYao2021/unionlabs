#pragma once
// ============================================================
//  turbo.hpp — rate-1/2 punctured parallel-concatenated turbo code (PCCC).
//
//  Two identical (7,5) recursive systematic convolutional (RSC) encoders — one
//  on the info bits, one on an interleaved copy — with the two parity streams
//  punctured (alternate bits) to reach rate 1/2, matching the conv/LDPC codecs.
//
//  Decoder: the "typical" turbo iterative decoder — two soft-in/soft-out (SISO)
//  max-log-MAP (BCJR) decoders exchanging EXTRINSIC LLRs through the interleaver
//  for a few iterations, then a final hard decision on the combined LLR.
//
//  Trellis: RSC(7,5) — feedback g0 = 1+D+D² (7 octal), feedforward g1 = 1+D²
//  (5 octal), memory 2 → 4 states. Open (unterminated) trellis.
//
//  LLR convention (I/O): positive = bit 0 (matches soft_demodulate_llr). The
//  BCJR runs internally in the textbook convention (positive = bit 1) and flips
//  at the boundary.
//
//  k info bits → n = 2k coded bits (rate 1/2). Both ends MUST use the same
//  (k, iters, seed) so the interleaver matches.
// ============================================================

#include <cstdint>
#include <vector>
#include <array>
#include <random>
#include <algorithm>
#include <cmath>
#include <iostream>

class TurboCode {
public:
    // iters : max turbo (BCJR) iterations — the main tuning knob; raise (8-12) if
    //         a marginal link won't converge. scale : extrinsic-LLR scaling for
    //         max-log-MAP (0.7-0.8 typical).
    explicit TurboCode(int k = 256, int iters = 6, uint32_t seed = 0x7EED7EEDu,
                       float scale = 0.75f)
        : k_(std::max(2, ((k + 1) / 2) * 2)),   // force even K (clean puncturing)
          iters_(std::max(1, iters)),
          scale_(scale > 0.f ? scale : 0.75f)
    {
        build_trellis();
        build_interleaver(seed);
        std::cout << "[TurboCode] K=" << k_ << "  n=" << n()
                  << "  rate=1/2 (punctured)  iters=" << iters_ << "  scale=" << scale_
                  << "  RSC(7,5) 4-state\n";
    }

    int k() const { return k_; }
    int n() const { return 2 * k_; }          // coded bits per block

    // Systematic encode: k info bits → 2k coded bits
    //   [ systematic(k) | parity1 even(k/2) | parity2 even(k/2) ].
    std::vector<uint8_t> encode(const std::vector<uint8_t>& info) const {
        std::vector<uint8_t> u(k_, 0);
        for (int i = 0; i < k_ && i < (int)info.size(); ++i) u[i] = info[i] & 1u;

        std::vector<uint8_t> p1(k_), p2(k_), ui(k_);
        rsc_encode(u, p1);                       // encoder 1 on u
        for (int i = 0; i < k_; ++i) ui[i] = u[pi_[i]];
        rsc_encode(ui, p2);                      // encoder 2 on interleaved u

        std::vector<uint8_t> c; c.reserve(2 * k_);
        for (int i = 0; i < k_; ++i)      c.push_back(u[i]);        // systematic
        for (int i = 0; i < k_; i += 2)   c.push_back(p1[i]);       // p1 punctured
        for (int j = 0; j < k_; j += 2)   c.push_back(p2[j]);       // p2 punctured
        return c;                                                   // size = 2k_
    }

    // Iterative decode: n channel LLRs (positive = bit 0) → k info bits.
    std::vector<uint8_t> decode(const std::vector<float>& llr,
                                int* iters_out = nullptr) const {
        // To internal convention (positive = bit 1) and de-puncture the parity.
        std::vector<float> Ls(k_, 0), Lp1(k_, 0), Lp2(k_, 0);
        for (int i = 0; i < k_; ++i) Ls[i] = -get(llr, i);
        int idx = k_;
        for (int i = 0; i < k_; i += 2) Lp1[i] = -get(llr, idx++);
        for (int j = 0; j < k_; j += 2) Lp2[j] = -get(llr, idx++);

        std::vector<float> Ls_il(k_);
        for (int j = 0; j < k_; ++j) Ls_il[j] = Ls[pi_[j]];

        std::vector<float> La1(k_, 0), Le1(k_, 0), La2(k_, 0), Le2(k_, 0);
        std::vector<uint8_t> hard(k_, 0), prev(k_, 0);
        int used = iters_;
        for (int it = 0; it < iters_; ++it) {
            bcjr(Ls,    Lp1, La1, Le1);
            for (int j = 0; j < k_; ++j) La2[j] = Le1[pi_[j]];       // interleave
            bcjr(Ls_il, Lp2, La2, Le2);
            for (int j = 0; j < k_; ++j) La1[pi_[j]] = Le2[j];       // deinterleave

            // Early stop: decision stable across an iteration.
            for (int i = 0; i < k_; ++i) {
                float L = Ls[i] + La1[i] + Le1[i];
                hard[i] = (L > 0.f) ? 1u : 0u;
            }
            if (it > 0 && hard == prev) { used = it + 1; break; }
            prev = hard;
        }
        if (iters_out) *iters_out = used;
        return hard;
    }

private:
    int k_, iters_;
    float scale_ = 0.75f;           // extrinsic-LLR scale (max-log-MAP)
    int ns_[4][2], par_[4][2];      // trellis: next-state and parity per (state,input)
    std::vector<int> pi_;           // interleaver permutation

    static float get(const std::vector<float>& v, int i) {
        return i < (int)v.size() ? v[i] : 0.f;
    }

    void build_trellis() {
        // state s = (r1<<1)|r2, r1 = newest delay. a = u^r1^r2 (feedback 1+D+D²);
        // parity p = a^r2 (feedforward 1+D²); next = (a<<1)|r1.
        for (int s = 0; s < 4; ++s) {
            int r1 = (s >> 1) & 1, r2 = s & 1;
            for (int u = 0; u < 2; ++u) {
                int a = u ^ r1 ^ r2;
                ns_[s][u]  = ((a & 1) << 1) | r1;
                par_[s][u] = a ^ r2;
            }
        }
    }

    void build_interleaver(uint32_t seed) {
        pi_.resize(k_);
        for (int i = 0; i < k_; ++i) pi_[i] = i;
        std::mt19937 g(seed);
        for (int i = k_ - 1; i > 0; --i) {
            std::uniform_int_distribution<int> d(0, i);
            std::swap(pi_[i], pi_[d(g)]);
        }
    }

    void rsc_encode(const std::vector<uint8_t>& u, std::vector<uint8_t>& p) const {
        int s = 0;
        for (int i = 0; i < k_; ++i) { int ui = u[i] & 1; p[i] = par_[s][ui]; s = ns_[s][ui]; }
    }

    // max-log-MAP SISO. Ls/Lp/La length k_ (internal, positive = bit 1). Fills Le
    // with the (scaled) extrinsic LLR. Open trellis: alpha[0]=state0, beta[K]=uniform.
    void bcjr(const std::vector<float>& Ls, const std::vector<float>& Lp,
              const std::vector<float>& La, std::vector<float>& Le) const {
        const float NINF = -1e30f;
        const int K = k_;
        std::vector<std::array<float, 4>> A(K + 1), B(K + 1);
        for (int s = 0; s < 4; ++s) { A[0][s] = NINF; B[K][s] = 0.f; }
        A[0][0] = 0.f;

        auto gam = [&](int k, int s, int u) -> float {
            float xu = u ? 1.f : -1.f;
            float xp = par_[s][u] ? 1.f : -1.f;
            return 0.5f * (xu * (La[k] + Ls[k]) + xp * Lp[k]);
        };

        for (int k = 0; k < K; ++k) {                 // forward (alpha)
            std::array<float, 4> na; na.fill(NINF);
            for (int s = 0; s < 4; ++s) {
                if (A[k][s] <= NINF / 2) continue;
                for (int u = 0; u < 2; ++u) {
                    int nx = ns_[s][u]; float v = A[k][s] + gam(k, s, u);
                    if (v > na[nx]) na[nx] = v;
                }
            }
            A[k + 1] = na;
        }
        for (int k = K - 1; k >= 0; --k) {            // backward (beta)
            std::array<float, 4> nb; nb.fill(NINF);
            for (int s = 0; s < 4; ++s)
                for (int u = 0; u < 2; ++u) {
                    int nx = ns_[s][u]; float v = B[k + 1][nx] + gam(k, s, u);
                    if (v > nb[s]) nb[s] = v;
                }
            B[k] = nb;
        }
        for (int k = 0; k < K; ++k) {                 // LLR + extrinsic
            float m1 = NINF, m0 = NINF;
            for (int s = 0; s < 4; ++s)
                for (int u = 0; u < 2; ++u) {
                    int nx = ns_[s][u];
                    float v = A[k][s] + gam(k, s, u) + B[k + 1][nx];
                    if (u) { if (v > m1) m1 = v; } else { if (v > m0) m0 = v; }
                }
            Le[k] = scale_ * ((m1 - m0) - La[k] - Ls[k]);   // scaled (max-log-MAP)
        }
    }
};
