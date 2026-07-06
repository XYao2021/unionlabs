// OFDM round-trip through a multipath channel (with CP >= channel length).
// Shows OFDM decodes dense QAM through multipath at BER ~0 — where single-
// carrier (no equalizer) fails — because the cyclic prefix makes each
// subcarrier a flat one-tap channel, equalized from the preamble.
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "ofdm.hpp"
#include <random>
#include <cstdio>
using cf = std::complex<float>;

static int bd(const std::vector<uint8_t>&a,const std::vector<uint8_t>&b){
    int n=std::min(a.size(),b.size()),e=0;for(int i=0;i<n;i++)e+=(a[i]!=b[i]);return e;}

static std::vector<cf> multipath(const std::vector<cf>& x, const std::vector<cf>& h){
    std::vector<cf> y(x.size(), cf(0,0));
    for (size_t n=0;n<x.size();++n)
        for (size_t k=0;k<h.size() && k<=n;++k) y[n]+=h[k]*x[n-k];
    return y;
}

static void run(const char* scheme, const std::vector<cf>& h, float sigma, int nsym_data){
    OFDM ofdm(64, 16);                 // N=64, CP=16 (channel len must be <= 17)
    Modulator mod(string_to_mod_type(scheme));
    int bps = mod.get_bits_per_symbol();
    int nqam = ofdm.data_per_sym() * nsym_data;

    std::mt19937 g(99); std::uniform_int_distribution<int> db(0,1);
    std::vector<uint8_t> tx(nqam*bps); for(auto&x:tx)x=db(g);

    // bits -> QAM (no single-carrier preamble)
    std::vector<cf> empty_pre{cf(1,0)}; bool add=false;
    auto qam = mod.modulate(tx, empty_pre, add);
    qam.resize(nqam, cf(0,0));

    // OFDM modulate -> multipath + AWGN -> OFDM demodulate.
    // Scale noise to the OFDM signal's own RMS so the per-symbol SNR matches the
    // single-carrier baseline (the IFFT spreads energy, lowering time-domain RMS).
    auto txwave = ofdm.modulate(qam);
    auto rx = multipath(txwave, h);
    double pw=0; for(auto&s:rx) pw+=std::norm(s); float rms=std::sqrt(pw/rx.size());
    std::mt19937 gn(1); std::normal_distribution<float> nz(0.f,sigma*rms);
    for(auto&s:rx) s+=cf(nz(gn),nz(gn));
    auto rx_qam = ofdm.demodulate(rx, nqam);

    // demod QAM -> bits, BER (OFDM). Also single-carrier no-EQ baseline.
    auto rx_bits = mod.demodulate(rx_qam);
    int e_ofdm = bd(tx, rx_bits);

    auto sc = multipath(qam, h);
    for(auto&s:sc) s+=cf(nz(gn),nz(gn));
    int e_sc = bd(tx, mod.demodulate(sc));

    int nb = (int)tx.size();
    printf("%-7s | single-carrier no-EQ BER=%.3f  |  OFDM BER=%.5f  %s\n",
        scheme, (float)e_sc/nb, (float)e_ofdm/nb, e_ofdm==0?"[EXACT]":"");
}

int main(){
    // multipath: 3 taps within the CP; main + echoes
    std::vector<cf> h = { cf(1.0f,0.0f), cf(0.5f,0.3f), cf(0.2f,-0.15f), cf(0.1f,0.05f) };
    printf("=== OFDM N=64 CP=16, multipath (4 taps), 6 data symbols ===\n");
    printf("-- zero noise (core correctness) --\n");
    for (auto s : {"QPSK","16-QAM","64-QAM","256-QAM"}) run(s, h, 0.0f, 6);
    printf("-- relative noise 1%% of signal RMS --\n");
    for (auto s : {"QPSK","16-QAM","64-QAM","256-QAM"}) run(s, h, 0.01f, 6);
    return 0;
}
