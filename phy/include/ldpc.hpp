#pragma once
// ============================================================
//  ldpc.hpp
//
//  Self-contained systematic rate-1/2 LDPC codec (encoder + BP
//  decoder). No external dependencies — the parity-check matrix is
//  constructed deterministically from a fixed seed, so the TX and RX
//  build the SAME code from the same (k, col_weight, seed) triple.
//
//  Structure (IRA / "staircase"): codeword c = [ info | parity ],
//  with the parity part checked by a lower-bidiagonal (accumulator)
//  matrix. That gives O(n) systematic encoding (a prefix-XOR over the
//  info syndrome) and parity variable nodes of degree 2 — a genuinely
//  good short LDPC code, unlike an H=[P|I] identity-parity code whose
//  degree-1 parity bits waste the check structure.
//
//  Decoder: flooding normalized min-sum belief propagation on the
//  Tanner graph, with an early stop when the syndrome clears. Works
//  from hard bits (mapped to ±LLR) or from soft channel LLRs.
//
//  LLR sign convention MATCHES soft_demodulate_llr():
//      positive LLR  →  bit 0 more likely
//      negative LLR  →  bit 1 more likely
//
//  Rate 1/2: k info bits → n = 2k coded bits (m = k parity checks).
// ============================================================

#include <cstdint>
#include <vector>
#include <random>
#include <cmath>
#include <algorithm>
#include <limits>
#include <iostream>

// ─────────────────────────────────────────────────────────────
//  LdpcCode — one fixed-size block code (k info bits, n = 2k coded).
// ─────────────────────────────────────────────────────────────
class LdpcCode {
public:
    // k          : info bits per block (must be >= 1).
    // col_weight : number of check rows each INFO column touches
    //              (info variable-node degree; 3 is a solid default).
    // seed       : construction seed — MUST be identical on both ends.
    // max_iter : max belief-propagation iterations (early-stops on clear syndrome).
    // scale    : normalized min-sum attenuation (0.7-0.9 typical; tune if a
    //            marginal link won't converge). Higher col_weight = denser code —
    //            CHANGES H, so both ends must use the same col_weight.
    explicit LdpcCode(int k = 256, int col_weight = 3, uint32_t seed = 0xC0DEC0DEu,
                      int max_iter = 50, float scale = 0.75f)
        : k_(std::max(1, k)),
          m_(std::max(1, k)),          // rate 1/2 → one parity bit per info bit
          n_(k_ + m_),
          max_iter_(std::max(1, max_iter)),
          scale_(scale > 0.f ? scale : 0.75f)
    {
        build(std::max(1, col_weight), seed);
        std::cout << "[LdpcCode] k=" << k_ << "  n=" << n_
                  << "  rate=1/2  col_weight=" << col_weight
                  << "  edges=" << num_edges_
                  << "  max_iter=" << max_iter_ << "  scale=" << scale_ << "\n";
    }

    int k() const { return k_; }
    int m() const { return m_; }
    int n() const { return n_; }

    // Systematic encode: k info bits → n codeword bits [ info | parity ].
    // parity is the prefix-XOR of the info syndrome (bidiagonal accumulator).
    std::vector<uint8_t> encode(const std::vector<uint8_t>& info) const {
        std::vector<uint8_t> cw(n_, 0);
        for (int i = 0; i < k_; ++i) cw[i] = (i < (int)info.size()) ? (info[i] & 1u) : 0u;

        // s[r] = XOR of info bits connected to check row r (info part only).
        std::vector<uint8_t> s(m_, 0);
        for (int r = 0; r < m_; ++r) {
            uint8_t acc = 0;
            for (int v : check_info_vars_[r]) acc ^= cw[v];
            s[r] = acc;
        }
        // Bidiagonal solve: p[0]=s[0]; p[r]=s[r]^p[r-1].
        uint8_t running = 0;
        for (int r = 0; r < m_; ++r) {
            running ^= s[r];
            cw[k_ + r] = running;
        }
        return cw;
    }

    // Belief-propagation decode.
    //   llr : n channel LLRs (positive = bit 0). Returns k info bits.
    // Normalized min-sum with early syndrome-based termination.
    // iters_out (optional) receives the number of BP iterations actually run
    // before the syndrome cleared (or max_iter if it never did) — BP has
    // early termination, so decode cost is data/SNR dependent.
    std::vector<uint8_t> decode(const std::vector<float>& llr,
                                int* iters_out = nullptr) const
    {
        const int   max_iter = max_iter_;
        const float scale    = scale_;
        // Per-edge messages, laid out parallel to the adjacency lists:
        //   Mvc[e] = variable→check message on edge e
        //   Mcv[e] = check→variable message on edge e
        std::vector<float> Mvc(num_edges_, 0.0f);
        std::vector<float> Mcv(num_edges_, 0.0f);

        // Channel LLR per variable (clip to keep min-sum well-behaved).
        std::vector<float> ch(n_);
        for (int v = 0; v < n_; ++v) ch[v] = clip(v < (int)llr.size() ? llr[v] : 0.0f);

        std::vector<uint8_t> hard(n_, 0);

        for (int it = 0; it < max_iter; ++it) {
            // ── Variable-node update ──
            // total[v] = channel + Σ incoming check messages; message back on
            // edge e is total minus that edge's own incoming check message.
            for (int v = 0; v < n_; ++v) {
                float total = ch[v];
                for (int e : var_edges_[v]) total += Mcv[e];
                hard[v] = (total < 0.0f) ? 1u : 0u;
                for (int e : var_edges_[v]) Mvc[e] = total - Mcv[e];
            }

            // ── Early termination: syndrome all-zero ⇒ valid codeword ──
            bool ok = true;
            for (int r = 0; r < m_ && ok; ++r) {
                uint8_t par = 0;
                for (int e : check_edges_[r]) par ^= hard[edge_var_[e]];
                if (par) ok = false;
            }
            if (ok) { if (iters_out) *iters_out = it + 1; break; }
            if (iters_out) *iters_out = it + 1;   // updated each iter; final value if never converges

            // ── Check-node update (normalized min-sum) ──
            // For each edge, combine the OTHER edges of the check: sign =
            // product of signs, magnitude = smallest |Mvc|, scaled.
            for (int r = 0; r < m_; ++r) {
                const auto& edges = check_edges_[r];
                // Track the two smallest magnitudes + overall sign product so
                // each edge's "exclude self" min is O(1).
                float min1 = std::numeric_limits<float>::max();
                float min2 = std::numeric_limits<float>::max();
                int   argmin1 = -1;
                int   signprod = 1;
                for (int e : edges) {
                    float a = std::fabs(Mvc[e]);
                    if (Mvc[e] < 0.0f) signprod = -signprod;
                    if (a < min1) { min2 = min1; min1 = a; argmin1 = e; }
                    else if (a < min2) { min2 = a; }
                }
                for (int e : edges) {
                    float mag = (e == argmin1) ? min2 : min1;
                    int   sgn = signprod;
                    if (Mvc[e] < 0.0f) sgn = -sgn;   // exclude this edge's sign
                    Mcv[e] = scale * (float)sgn * mag;
                }
            }
        }

        // Final hard decision on the info part.
        std::vector<uint8_t> info(k_);
        for (int v = 0; v < k_; ++v) {
            float total = ch[v];
            for (int e : var_edges_[v]) total += Mcv[e];
            info[v] = (total < 0.0f) ? 1u : 0u;
        }
        return info;
    }

private:
    int k_, m_, n_;
    int   max_iter_ = 50;
    float scale_    = 0.75f;
    int num_edges_ = 0;

    // Adjacency by edge index. Each edge connects one variable and one check.
    std::vector<int>              edge_var_;      // edge → variable node
    std::vector<int>              edge_check_;    // edge → check node
    std::vector<std::vector<int>> var_edges_;     // variable → its edge indices
    std::vector<std::vector<int>> check_edges_;   // check → its edge indices
    // Info-only variable list per check row, for the systematic encoder.
    std::vector<std::vector<int>> check_info_vars_;

    static float clip(float x) {
        const float L = 20.0f;
        return std::max(-L, std::min(L, x));
    }

    void add_edge(int v, int c) {
        int e = num_edges_++;
        edge_var_.push_back(v);
        edge_check_.push_back(c);
        var_edges_[v].push_back(e);
        check_edges_[c].push_back(e);
    }

    void build(int col_weight, uint32_t seed) {
        var_edges_.assign(n_, {});
        check_edges_.assign(m_, {});
        check_info_vars_.assign(m_, {});
        std::mt19937 rng(seed);

        // ── Info part: distribute k*col_weight edges across the m check rows
        // as evenly as possible (a balanced "socket" list), with no duplicate
        // (row, column) pair inside a single info column. ──
        long total = (long)k_ * col_weight;
        std::vector<int> sockets;
        sockets.reserve(total);
        for (long e = 0; e < total; ++e) sockets.push_back((int)(e % m_));
        std::shuffle(sockets.begin(), sockets.end(), rng);

        for (int col = 0; col < k_; ++col) {
            for (int w = 0; w < col_weight; ++w) {
                int pos = col * col_weight + w;
                int row = sockets[pos];
                // Ensure this row is distinct from the ones already used in
                // THIS column: if it collides, swap with a later socket that
                // carries a non-colliding row.
                if (row_used_in_col(col, col_weight, w, sockets, row)) {
                    for (int j = pos + 1; j < (int)sockets.size(); ++j) {
                        if (!row_used_in_col(col, col_weight, w, sockets, sockets[j])) {
                            std::swap(sockets[pos], sockets[j]);
                            row = sockets[pos];
                            break;
                        }
                    }
                }
                // Fallback (rare tail case): pick any unused row directly.
                if (row_used_in_col(col, col_weight, w, sockets, row)) {
                    for (int r = 0; r < m_; ++r) {
                        if (!row_used_in_col(col, col_weight, w, sockets, r)) {
                            sockets[pos] = r; row = r; break;
                        }
                    }
                }
                add_edge(col, row);
                check_info_vars_[row].push_back(col);
            }
        }

        // ── Parity part: lower-bidiagonal accumulator. Check row r is
        // connected to parity variable (k_+r) and, for r>0, (k_+r-1). ──
        for (int r = 0; r < m_; ++r) {
            add_edge(k_ + r, r);
            if (r > 0) add_edge(k_ + r - 1, r);
        }
    }

    // True if `row` already appears among the first `w` sockets of `col`.
    static bool row_used_in_col(int col, int col_weight, int w,
                                const std::vector<int>& sockets, int row) {
        for (int u = 0; u < w; ++u)
            if (sockets[col * col_weight + u] == row) return true;
        return false;
    }
};
