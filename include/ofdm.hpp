#pragma once
// ============================================================
//  ofdm.hpp — OFDM modem core + Schmidl-Cox frame sync & CFO.
//
//  Why OFDM: a cyclic prefix (CP) longer than the channel's delay
//  spread turns multipath *convolution* into a per-subcarrier
//  *multiplication*. Equalization is then one complex divide per
//  subcarrier, estimated from a known preamble — no FIR, no ISI.
//
//  Frame on the wire (each symbol = [CP | IFFT(N)]):
//     [ SC-sync symbol | channel-est symbol | data symbol(s) ]
//   • SC-sync: energy on EVEN subcarriers only → two identical
//     time-domain halves. Autocorrelating the halves gives frame
//     TIMING (metric peak) and FRACTIONAL CFO (angle of the peak).
//   • channel-est: known BPSK on every active subcarrier → H[k].
//   • data: QAM on every active subcarrier, one-tap equalized.
//
//  Timing is forgiving: any residual offset inside the CP becomes a
//  linear phase across subcarriers, which the channel estimate H[k]
//  absorbs. CFO up to ±half a subcarrier spacing (here 12.5 kHz at
//  1.6 Msps/N=64) is corrected from the SC phase before the FFT.
// ============================================================
#include <complex>
#include <vector>
#include <cstdint>
#include <cmath>
#include <fftw3.h>

using ofdm_cf = std::complex<float>;

class OFDM {
public:
    OFDM(int fft_size = 64, int cp_len = 16)
        : N_(fft_size), cp_(cp_len)
    {
        for (int k = 1; k < N_; ++k)
            if (k != N_ / 2) active_.push_back(k);      // skip DC + Nyquist

        // Channel-estimation reference: BPSK on every active subcarrier.
        uint32_t s = 0x2Bu;
        pre_ref_.resize(active_.size());
        for (size_t i = 0; i < active_.size(); ++i) {
            int bit = s & 1; s = (s >> 1) ^ (bit ? 0xB4u : 0u);
            pre_ref_[i] = bit ? ofdm_cf(1, 0) : ofdm_cf(-1, 0);
        }
        // Schmidl-Cox reference: BPSK on EVEN active subcarriers only (→ two
        // identical time halves), scaled by sqrt(2) to keep symbol power ~1.
        sc_freq_.assign(N_, ofdm_cf(0, 0));
        uint32_t s2 = 0x5Fu;
        for (int k : active_) if (k % 2 == 0) {
            int bit = s2 & 1; s2 = (s2 >> 1) ^ (bit ? 0xB4u : 0u);
            sc_freq_[k] = (bit ? ofdm_cf(1, 0) : ofdm_cf(-1, 0)) * std::sqrt(2.0f);
        }

        in_  = fftwf_alloc_complex(N_);
        out_ = fftwf_alloc_complex(N_);
        fwd_ = fftwf_plan_dft_1d(N_, in_, out_, FFTW_FORWARD,  FFTW_ESTIMATE);
        inv_ = fftwf_plan_dft_1d(N_, in_, out_, FFTW_BACKWARD, FFTW_ESTIMATE);
    }
    ~OFDM() {
        fftwf_destroy_plan(fwd_); fftwf_destroy_plan(inv_);
        fftwf_free(in_); fftwf_free(out_);
    }
    OFDM(const OFDM&) = delete;
    OFDM& operator=(const OFDM&) = delete;

    int fft_size()     const { return N_; }
    int cp_len()       const { return cp_; }
    int data_per_sym() const { return (int)active_.size(); }
    int sym_len()      const { return N_ + cp_; }
    // Whole-frame length in wire samples for `nqam` QAM symbols.
    int frame_len(int nqam) const {
        int nsym = (nqam + data_per_sym() - 1) / data_per_sym();
        return (2 + nsym) * sym_len();                  // SC + chest + data
    }

    // ── TX: QAM symbols → time-domain frame [SC | chest | data] ──
    std::vector<ofdm_cf> modulate(const std::vector<ofdm_cf>& qam)
    {
        int D = data_per_sym();
        int nsym = (int)((qam.size() + D - 1) / D);
        std::vector<ofdm_cf> out; out.reserve((2 + nsym) * sym_len());

        emit_freq(sc_freq_, out);                       // SC-sync symbol
        std::vector<ofdm_cf> chest(N_, ofdm_cf(0, 0));
        for (size_t i = 0; i < active_.size(); ++i) chest[active_[i]] = pre_ref_[i];
        emit_freq(chest, out);                          // channel-est symbol

        for (int s = 0; s < nsym; ++s) {
            std::vector<ofdm_cf> f(N_, ofdm_cf(0, 0));
            for (int i = 0; i < D; ++i) {
                size_t idx = (size_t)s * D + i;
                if (idx < qam.size()) f[active_[i]] = qam[idx];
            }
            emit_freq(f, out);                          // data symbol
        }
        return out;
    }

    // ── Schmidl-Cox sync over a received stream ──
    // Finds the SC symbol and returns its useful-part start index and the
    // fractional CFO (in subcarrier-spacing units, range ±1). search_len limits
    // how far to look (0 = whole stream).
    void sync(const std::vector<ofdm_cf>& rx, int& start, float& cfo_norm,
              int search_len = 0) const
    {
        int L = N_ / 2;
        int maxd = (int)rx.size() - N_;
        if (search_len > 0) maxd = std::min(maxd, search_len);
        if (maxd < 0) { start = 0; cfo_norm = 0; return; }

        std::vector<float>   M(maxd + 1, 0.0f), Rv(maxd + 1, 0.0f);
        std::vector<ofdm_cf> Pv(maxd + 1);
        float maxR = 1e-12f;
        for (int d = 0; d <= maxd; ++d) {
            ofdm_cf P(0, 0); float R = 0.0f;
            for (int m = 0; m < L; ++m) {
                P += std::conj(rx[d + m]) * rx[d + m + L];
                R += std::norm(rx[d + m + L]);
            }
            M[d]  = (R > 1e-9f) ? std::norm(P) / (R * R) : 0.0f;
            Rv[d] = R; Pv[d] = P;
            if (R > maxR) maxR = R;
        }
        // Energy gate: the |P|²/R² metric explodes where R→0 (lead-in / tail
        // noise). Only accept peaks whose window actually carries signal energy
        // (R ≥ 30% of the peak window energy), so sync locks onto the frame.
        float bestM = -1.0f; int bestd = 0;
        for (int d = 0; d <= maxd; ++d)
            if (Rv[d] >= 0.30f * maxR && M[d] > bestM) { bestM = M[d]; bestd = d; }
        // The metric plateaus over the CP length: both the CP start and the
        // useful-part start give a max (CP is a cyclic copy). Find the plateau's
        // LEFT edge (= CP start) and add cp to land on the useful-part start, so
        // the FFT window sits fully inside the ISI-free region past the CP.
        // The metric plateaus over the CP length: from the CP start to the
        // useful-part start (the CP is a cyclic copy). Find the plateau's LEFT
        // edge (= CP start) with a bounded scan, then place the FFT window a
        // guard into the CP — past the channel's multipath ISI (a few samples)
        // but before the useful start. Any residual offset inside the CP is a
        // linear phase across subcarriers that the channel estimate absorbs;
        // this avoids both the CP-ISI (being too early) and crossing into the
        // next symbol (being too late).
        int left = bestd, lim = std::max(0, bestd - cp_ - 2);
        while (left - 1 >= lim && M[left - 1] > 0.7f * bestM) --left;
        int guard = cp_ / 2;                            // ~mid-CP: robust to ±jitter
        start    = std::min(left + guard, maxd);
        cfo_norm = std::arg(Pv[bestd]) / float(M_PI);   // ε = angle(P)/π
        if (getenv("OFDM_DBG"))
            fprintf(stderr, "[sync] argmax=%d M=%.3f leftedge=%d -> start=%d\n",
                    bestd, bestM, left, start);
    }

    // ── RX: full receiver. sync → CFO-correct → channel-est → equalize ──
    // rx may contain lead-in noise; the frame is located automatically.
    std::vector<ofdm_cf> receive(const std::vector<ofdm_cf>& rx, int num_qam,
                                 int* out_start = nullptr, float* out_cfo = nullptr)
    {
        int need = frame_len(num_qam);
        int start; float cfo;
        // Full search: only the SC symbol has repeated time-halves, so its metric
        // peak dominates (data/chest symbols don't). The energy detector already
        // isolates the burst, so `rx` is short.
        sync(rx, start, cfo, 0);
        if (out_start) *out_start = start;
        if (out_cfo)   *out_cfo   = cfo;

        // CFO-correct from `start`: phase step = 2π·(ε/N) per sample.
        double step = 2.0 * M_PI * (double)cfo / (double)N_;
        int total = std::min((int)rx.size() - start, need + sym_len());
        std::vector<ofdm_cf> r(std::max(0, total));
        for (int k = 0; k < total; ++k)
            r[k] = rx[start + k] * ofdm_cf(std::cos(step * k), -std::sin(step * k));

        int D = data_per_sym(), L = sym_len();
        std::vector<ofdm_cf> qam; qam.reserve(num_qam);
        if ((int)r.size() < 2 * L) return qam;

        // Symbol i useful part starts at r[i·sym_len]. i=0 SC, i=1 chest, i≥2 data.
        std::vector<ofdm_cf> Ych = fft_useful(r, 1 * L);
        std::vector<ofdm_cf> H(active_.size());
        for (size_t i = 0; i < active_.size(); ++i)
            H[i] = Ych[active_[i]] / pre_ref_[i];

        int nsym = (int)((num_qam + D - 1) / D);
        for (int s = 0; s < nsym && (int)qam.size() < num_qam; ++s) {
            int off = (2 + s) * L;
            if (off + N_ > (int)r.size()) break;
            std::vector<ofdm_cf> Y = fft_useful(r, off);
            for (size_t i = 0; i < active_.size() && (int)qam.size() < num_qam; ++i) {
                ofdm_cf h = H[i];
                qam.push_back(std::abs(h) > 1e-9f ? Y[active_[i]] / h : ofdm_cf(0, 0));
            }
        }
        return qam;
    }

private:
    int N_, cp_;
    std::vector<int>     active_;
    std::vector<ofdm_cf> pre_ref_;   // channel-est reference (all active SC)
    std::vector<ofdm_cf> sc_freq_;   // SC-sync freq-domain symbol (even SC)
    fftwf_complex *in_, *out_;
    fftwf_plan     fwd_, inv_;

    // IFFT a full freq-domain vector (length N), prepend CP, append to out.
    void emit_freq(const std::vector<ofdm_cf>& X, std::vector<ofdm_cf>& out)
    {
        for (int k = 0; k < N_; ++k) { in_[k][0] = X[k].real(); in_[k][1] = X[k].imag(); }
        fftwf_execute(inv_);
        float sc = 1.0f / N_;
        std::vector<ofdm_cf> x(N_);
        for (int n = 0; n < N_; ++n) x[n] = ofdm_cf(out_[n][0] * sc, out_[n][1] * sc);
        for (int n = N_ - cp_; n < N_; ++n) out.push_back(x[n]);   // CP
        for (int n = 0; n < N_; ++n)        out.push_back(x[n]);   // body
    }

    // FFT N samples of the useful part starting at r[off].
    std::vector<ofdm_cf> fft_useful(const std::vector<ofdm_cf>& r, int off)
    {
        for (int n = 0; n < N_; ++n) { in_[n][0] = r[off + n].real(); in_[n][1] = r[off + n].imag(); }
        fftwf_execute(fwd_);
        std::vector<ofdm_cf> Y(N_);
        for (int k = 0; k < N_; ++k) Y[k] = ofdm_cf(out_[k][0], out_[k][1]);
        return Y;
    }
};
