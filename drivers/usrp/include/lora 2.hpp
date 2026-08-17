// lora.hpp — LoRa / CSS (Chirp Spread Spectrum) modulator + DECODABLE receiver.
//
// Data-carrying counterpart to the raw `--message-type chirp` jamming sweep. Each
// symbol is one of N = 2^SF cyclic shifts of a base up-chirp; SF bits/symbol. The
// receiver dechirps (multiply by the conjugate base chirp) and takes an FFT — the
// peak bin is the symbol. That FFT coherently integrates all N chips, giving the
// LoRa processing gain (~SF·... dB) that lets it decode below the noise floor.
//
// Self-contained (own radix-2 FFT), operates on complex baseband samples, and is
// validated by an in-memory loopback (modulate -> +CFO/timing/noise -> demodulate)
// so decodability is proven without a radio. Frame:
//     [ n_pre base up-chirps ][ 2 down-chirps (SFD) ][ data symbols ]
// The up-chirp preamble gives coarse timing; the up/down-chirp pair separates the
// sample-timing offset (STO) from the carrier-frequency offset (CFO).
#pragma once
#include <vector>
#include <complex>
#include <cmath>
#include <cstdint>
#include <algorithm>

namespace lora {

using cf = std::complex<float>;
static constexpr double TWO_PI = 6.283185307179586;

// ── radix-2 iterative FFT (N a power of two). inv=false forward. ──
inline void fft(std::vector<cf>& a, bool inv = false) {
    int n = (int)a.size();
    for (int i = 1, j = 0; i < n; ++i) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) std::swap(a[i], a[j]);
    }
    for (int len = 2; len <= n; len <<= 1) {
        double ang = TWO_PI / len * (inv ? 1 : -1);
        cf wlen((float)std::cos(ang), (float)std::sin(ang));
        for (int i = 0; i < n; i += len) {
            cf w(1, 0);
            for (int k = 0; k < len / 2; ++k) {
                cf u = a[i + k], v = a[i + k + len / 2] * w;
                a[i + k] = u + v;
                a[i + k + len / 2] = u - v;
                w *= wlen;
            }
        }
    }
    if (inv) for (auto& x : a) x /= (float)n;
}

// ── base up-chirp of length N = 2^SF: phase(n) = 2π( n²/2N − n/2 ). ──
inline std::vector<cf> base_chirp(int SF, bool down = false) {
    int N = 1 << SF;
    std::vector<cf> c(N);
    for (int n = 0; n < N; ++n) {
        double ph = TWO_PI * ((double)n * n / (2.0 * N) - (double)n / 2.0);
        if (down) ph = -ph;
        c[n] = cf((float)std::cos(ph), (float)std::sin(ph));
    }
    return c;
}

// ── modulate one symbol s in [0, N): cyclic shift of the base up-chirp. ──
inline void mod_symbol(int s, int SF, const std::vector<cf>& up, std::vector<cf>& out) {
    int N = 1 << SF;
    out.resize(N);
    for (int n = 0; n < N; ++n) out[n] = up[(n + s) % N];
}

// ── demodulate one N-sample symbol: dechirp (× ref) then FFT argmax. `q` returns the
//    peak-to-average power ratio — high for a real chirp (sharp FFT peak), ~1 for
//    noise/constant junk (flat FFT). Used to reject false preamble locks. ──
inline int demod_symbol_q(const cf* r, int SF, const std::vector<cf>& ref, float& q) {
    int N = 1 << SF;
    std::vector<cf> d(N);
    for (int n = 0; n < N; ++n) d[n] = r[n] * ref[n];
    fft(d);
    int best = 0; float bm = -1.0f; double sum = 0.0;
    for (int k = 0; k < N; ++k) {
        float m = std::norm(d[k]);
        sum += m;
        if (m > bm) { bm = m; best = k; }
    }
    q = (sum > 0.0) ? (float)((double)bm / (sum / N)) : 0.0f;   // peak / average
    return best;
}
inline int demod_symbol(const cf* r, int SF, const std::vector<cf>& down) {
    float q;
    return demod_symbol_q(r, SF, down, q);
}

// ── bits (MSB-first) <-> SF-bit symbols. ──
inline std::vector<int> bits_to_symbols(const std::vector<uint8_t>& bits, int SF) {
    std::vector<int> syms;
    for (size_t i = 0; i < bits.size(); i += SF) {
        int s = 0;
        for (int b = 0; b < SF; ++b) {
            s <<= 1;
            if (i + b < bits.size()) s |= (bits[i + b] & 1);
        }
        syms.push_back(s);
    }
    return syms;
}
inline std::vector<uint8_t> symbols_to_bits(const std::vector<int>& syms, int SF, size_t nbits) {
    std::vector<uint8_t> bits;
    for (int s : syms)
        for (int b = SF - 1; b >= 0; --b) bits.push_back((uint8_t)((s >> b) & 1));
    if (nbits && bits.size() > nbits) bits.resize(nbits);
    return bits;
}

// A 1-byte sync word (network id) -> two data symbols, LoRa-style: each nibble × 8
// (so they stay well separated for SF7-12). Default 0x12 = private network.
inline void sync_symbols(int sync_word, int SF, int& s1, int& s2) {
    int N = 1 << SF;
    s1 = (((sync_word >> 4) & 0xF) << 3) % N;
    s2 = ((sync_word & 0xF) << 3) % N;
}

// ── modulate a full frame: preamble up-chirps + SYNC WORD (2 symbols) + SFD
//    down-chirps + data symbols. ──
inline std::vector<cf> modulate(const std::vector<uint8_t>& bits, int SF,
                                int n_preamble = 8, int sync_word = 0x12) {
    int N = 1 << SF;
    auto up = base_chirp(SF, false), down = base_chirp(SF, true);
    std::vector<cf> out;
    out.reserve((n_preamble + 4 + bits.size() / SF + 1) * N);
    for (int p = 0; p < n_preamble; ++p) out.insert(out.end(), up.begin(), up.end());
    std::vector<cf> sym;
    int s1, s2; sync_symbols(sync_word, SF, s1, s2);     // 2 sync-word symbols
    mod_symbol(s1, SF, up, sym); out.insert(out.end(), sym.begin(), sym.end());
    mod_symbol(s2, SF, up, sym); out.insert(out.end(), sym.begin(), sym.end());
    out.insert(out.end(), down.begin(), down.end());     // 2 down-chirps = SFD
    out.insert(out.end(), down.begin(), down.end());
    for (int s : bits_to_symbols(bits, SF)) {
        mod_symbol(s, SF, up, sym);
        out.insert(out.end(), sym.begin(), sym.end());
    }
    return out;
}

// ── find the frame: locate the SFD (down-chirps) after the up-chirp preamble, and
//    estimate an integer CFO from the up/down dechirp peak split. Returns the sample
//    index where DATA begins, or -1 if not found. `cfo_bins` gets the coarse CFO. ──
inline long sync(const std::vector<cf>& r, int SF, int n_preamble, int& cfo_bins,
                 int sync_word = 0x12) {
    int N = 1 << SF;
    auto up = base_chirp(SF, false), down = base_chirp(SF, true);
    cfo_bins = 0;
    // Slide symbol-by-symbol; a run of consistent up-chirp peaks = preamble, then the
    // 2 sync-word symbols, then the SFD down-chirps.
    long maxstart = (long)r.size() - (long)(n_preamble + 6) * N;
    const float QMIN = std::max(6.0f, (float)N / 32.0f);  // sharp dechirp peak (rejects noise/junk)
    for (long off = 0; off <= maxstart; ++off) {
        // (1) COARSE detect: n_preamble up-chirps decode to the same bin AND dechirp
        // sharply (a real chirp gives high peak/avg; noise/junk ~1 -> can't false-lock).
        // A combined CFO/timing offset shifts the common bin — consistency is the cue.
        float q0;
        int b0 = demod_symbol_q(&r[off], SF, down, q0);
        if (q0 < QMIN) continue;
        bool consistent = true;
        for (int p = 1; p < n_preamble; ++p) {
            float qp;
            int bp = demod_symbol_q(&r[off + (long)p * N], SF, down, qp);
            if (bp != b0 || qp < QMIN) { consistent = false; break; }
        }
        if (!consistent) continue;

        // (2) FINE timing: the coarse `off` is only aligned to within a symbol. The
        // SFD (2 down-chirps) begins at frame_start + (n_preamble+2)*N (after the 2
        // sync-word symbols); search the sample shift tau in [0,N) that makes BOTH SFD
        // down-chirps decode sharply and to the same bin (dechirped with the UP ref).
        // That pins the true frame boundary and separates timing from CFO.
        int best_tau = -1; float best_q = -1.0f; int best_d = 0;
        for (int tau = 0; tau < N; ++tau) {
            long sfd = off + tau + (long)(n_preamble + 2) * N;
            if (sfd + 2 * N > (long)r.size()) break;
            float qa, qb;
            int da = demod_symbol_q(&r[sfd], SF, up, qa);
            int db = demod_symbol_q(&r[sfd + N], SF, up, qb);
            if (da == db && qa > QMIN && qb > QMIN && qa + qb > best_q) {
                best_q = qa + qb; best_tau = tau; best_d = da;
            }
        }
        if (best_tau < 0) continue;                       // no clean SFD -> false preamble

        long frame = off + best_tau;                      // true first-preamble-sample index
        // (3) CFO: with timing aligned, a clean preamble up-chirp dechirps to a peak at
        // exactly the CFO (in bins). Fold to [-N/2, N/2).
        float qu;
        int upk = demod_symbol_q(&r[frame + N], SF, down, qu);
        cfo_bins = (upk >= N / 2) ? upk - N : upk;

        // (4) SYNC WORD: verify the 2 sync symbols (right after the preamble) match the
        // expected network id — reject frames from a foreign network. The dechirp peak
        // is shifted by the CFO, so compare against (expected + cfo) mod N.
        int es1, es2; sync_symbols(sync_word, SF, es1, es2);
        int m1 = demod_symbol(&r[frame + (long)n_preamble * N], SF, down);
        int m2 = demod_symbol(&r[frame + (long)(n_preamble + 1) * N], SF, down);
        int x1 = ((es1 + cfo_bins) % N + N) % N, x2 = ((es2 + cfo_bins) % N + N) % N;
#ifdef LORA_SYNC_DEBUG
        std::fprintf(stderr, "[sync] off=%ld tau=%d frame=%ld cfo=%d sync m=(%d,%d) exp=(%d,%d)\n",
                     off, best_tau, frame, cfo_bins, m1, m2, x1, x2);
#endif
        if (m1 != x1 || m2 != x2) continue;               // wrong sync word -> foreign frame

        return frame + (long)(n_preamble + 4) * N;        // data after preamble+sync+SFD
    }
    return -1;
}

// ── full receive: sync, correct integer CFO, demod all data symbols to bits. ──
inline std::vector<uint8_t> demodulate(const std::vector<cf>& rx, int SF, size_t nbits,
                                       int n_preamble = 8, int sync_word = 0x12) {
    int N = 1 << SF;
    int cfo = 0;
    long data = sync(rx, SF, n_preamble, cfo, sync_word);
    if (data < 0) return {};
    auto down = base_chirp(SF, true);
    std::vector<cf> corr = rx;
    if (cfo) {                                            // de-rotate integer CFO
        for (size_t n = 0; n < corr.size(); ++n) {
            double ph = -TWO_PI * (double)cfo / N * (double)n;
            corr[n] *= cf((float)std::cos(ph), (float)std::sin(ph));
        }
    }
    std::vector<int> syms;
    int nsym = (int)((nbits + SF - 1) / SF);
    for (int i = 0; i < nsym; ++i) {
        long p = data + (long)i * N;
        if (p + N > (long)corr.size()) break;
        int s = demod_symbol(&corr[p], SF, down);
        // subtract residual preamble bin offset (b0) is folded into CFO already
        syms.push_back(((s % N) + N) % N);
    }
    return symbols_to_bits(syms, SF, nbits);
}

}  // namespace lora
