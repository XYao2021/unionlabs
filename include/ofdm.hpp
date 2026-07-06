#pragma once
// ============================================================
//  ofdm.hpp — OFDM modem core (single block, perfect timing).
//
//  Why OFDM: a cyclic prefix (CP) longer than the channel's
//  delay spread turns the multipath *convolution* into a
//  per-subcarrier *multiplication*. Channel equalization is then
//  one complex divide per subcarrier, estimated from a known
//  preamble OFDM symbol — no FIR equalizer, no ISI, no delay
//  bookkeeping. This is why dense QAM survives multipath under
//  OFDM where single-carrier struggles.
//
//  Frame:  [ preamble OFDM symbol | data OFDM symbol(s) ]
//  Each symbol on the wire: [ CP (last cp_len of the IFFT) | IFFT(N) ].
//  Active subcarriers: all except DC (0) and Nyquist (N/2).
//  Preamble carries a known BPSK sequence on every active
//  subcarrier → full-band channel estimate H[k].
//
//  This is the DSP core (assumes symbol timing is known). Frame
//  sync / CFO for the RF pipeline are a later integration step.
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

        // Known preamble reference: BPSK ±1 from a simple LFSR, one per active SC.
        uint32_t s = 0x2Bu;
        pre_ref_.resize(active_.size());
        for (size_t i = 0; i < active_.size(); ++i) {
            int bit = s & 1;
            s = (s >> 1) ^ (bit ? 0xB4u : 0u);          // 8-bit LFSR
            pre_ref_[i] = bit ? ofdm_cf(1, 0) : ofdm_cf(-1, 0);
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

    int fft_size()      const { return N_; }
    int cp_len()        const { return cp_; }
    int data_per_sym()  const { return (int)active_.size(); }
    int sym_len()       const { return N_ + cp_; }          // wire samples/OFDM symbol

    // ── Modulate QAM symbols → time-domain OFDM stream ──
    // Prepends one preamble symbol; packs `qam` across data symbols (last one
    // zero-padded). Returns [preamble | data...] with CP on every symbol.
    std::vector<ofdm_cf> modulate(const std::vector<ofdm_cf>& qam)
    {
        int D = data_per_sym();
        int nsym = (int)((qam.size() + D - 1) / D);
        std::vector<ofdm_cf> out;
        out.reserve((1 + nsym) * sym_len());

        // preamble symbol
        emit_symbol(pre_ref_, out);

        // data symbols
        for (int s = 0; s < nsym; ++s) {
            std::vector<ofdm_cf> vals(D, ofdm_cf(0, 0));
            for (int i = 0; i < D; ++i) {
                size_t idx = (size_t)s * D + i;
                if (idx < qam.size()) vals[i] = qam[idx];
            }
            emit_symbol(vals, out);
        }
        return out;
    }

    // ── Demodulate time-domain OFDM stream → QAM symbols ──
    // Assumes `rx` starts exactly at the preamble symbol (perfect timing).
    // Estimates H[k] from the preamble, then one-tap equalizes each data symbol.
    // Returns exactly `num_qam` equalized QAM symbols.
    std::vector<ofdm_cf> demodulate(const std::vector<ofdm_cf>& rx, int num_qam)
    {
        int D = data_per_sym(), L = sym_len();
        std::vector<ofdm_cf> qam; qam.reserve(num_qam);
        if ((int)rx.size() < L) return qam;

        // Preamble → channel estimate H[k] on active subcarriers.
        std::vector<ofdm_cf> Ypre = fft_symbol(rx, 0);
        std::vector<ofdm_cf> H(active_.size());
        for (size_t i = 0; i < active_.size(); ++i)
            H[i] = Ypre[active_[i]] / pre_ref_[i];

        int nsym = (int)((num_qam + D - 1) / D);
        for (int s = 0; s < nsym && (int)qam.size() < num_qam; ++s) {
            int off = (s + 1) * L;                          // +1: skip preamble
            if (off + L > (int)rx.size()) break;
            std::vector<ofdm_cf> Y = fft_symbol(rx, off);
            for (size_t i = 0; i < active_.size() && (int)qam.size() < num_qam; ++i) {
                ofdm_cf h = H[i];
                ofdm_cf x = (std::abs(h) > 1e-9f) ? Y[active_[i]] / h : ofdm_cf(0, 0);
                qam.push_back(x);
            }
        }
        return qam;
    }

private:
    int N_, cp_;
    std::vector<int>      active_;
    std::vector<ofdm_cf>  pre_ref_;
    fftwf_complex *in_, *out_;
    fftwf_plan     fwd_, inv_;

    // Map `vals` onto active subcarriers, IFFT, prepend CP, append to `out`.
    void emit_symbol(const std::vector<ofdm_cf>& vals, std::vector<ofdm_cf>& out)
    {
        for (int k = 0; k < N_; ++k) { in_[k][0] = 0; in_[k][1] = 0; }
        for (size_t i = 0; i < active_.size(); ++i) {
            in_[active_[i]][0] = vals[i].real();
            in_[active_[i]][1] = vals[i].imag();
        }
        fftwf_execute(inv_);                                // out_ = IFFT (unnormalised)
        float scale = 1.0f / N_;
        std::vector<ofdm_cf> x(N_);
        for (int n = 0; n < N_; ++n) x[n] = ofdm_cf(out_[n][0] * scale, out_[n][1] * scale);
        for (int n = N_ - cp_; n < N_; ++n) out.push_back(x[n]);   // cyclic prefix
        for (int n = 0; n < N_; ++n)        out.push_back(x[n]);   // symbol body
    }

    // FFT the N samples of the OFDM symbol at `rx[off+cp_ .. off+cp_+N-1]`.
    std::vector<ofdm_cf> fft_symbol(const std::vector<ofdm_cf>& rx, int off)
    {
        for (int n = 0; n < N_; ++n) {
            ofdm_cf s = rx[off + cp_ + n];
            in_[n][0] = s.real(); in_[n][1] = s.imag();
        }
        fftwf_execute(fwd_);
        std::vector<ofdm_cf> Y(N_);
        for (int k = 0; k < N_; ++k) Y[k] = ofdm_cf(out_[k][0], out_[k][1]);
        return Y;
    }
};
