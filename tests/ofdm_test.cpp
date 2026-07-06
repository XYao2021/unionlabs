// OFDM full-receiver test: lead-in noise + multipath + CFO + AWGN, then
// Schmidl-Cox sync -> CFO correction -> per-subcarrier equalization.
// Shows OFDM recovers dense QAM through multipath+CFO where single-carrier
// no-EQ fails, and that sync/CFO estimation are accurate.
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
    for (size_t n=0;n<x.size();++n) for (size_t k=0;k<h.size()&&k<=n;++k) y[n]+=h[k]*x[n-k];
    return y;
}

static void run(const char* scheme, const std::vector<cf>& h,
                float noise_frac, float cfo_sc, int nsym_data, int pad){
    OFDM ofdm(64, 16);
    Modulator mod(string_to_mod_type(scheme));
    int bps = mod.get_bits_per_symbol();
    int nqam = ofdm.data_per_sym() * nsym_data;

    std::mt19937 g(99); std::uniform_int_distribution<int> db(0,1);
    std::vector<uint8_t> tx(nqam*bps); for(auto&x:tx)x=db(g);
    std::vector<cf> ep{cf(1,0)}; bool add=false;
    auto qam = mod.modulate(tx, ep, add); qam.resize(nqam, cf(0,0));

    // TX frame -> multipath -> CFO (cfo_sc subcarriers) -> AWGN, with lead-in pad.
    auto frame = ofdm.modulate(qam);
    auto ch = multipath(frame, h);
    double fn = (double)cfo_sc / ofdm.fft_size();          // cycles/sample
    for (size_t n=0;n<ch.size();++n) ch[n]*=cf(std::cos(2*M_PI*fn*n), std::sin(2*M_PI*fn*n));
    double pw=0; for(auto&s:ch) pw+=std::norm(s); float rms=std::sqrt(pw/ch.size());
    std::mt19937 gn(1); std::normal_distribution<float> nz(0.f, noise_frac*rms);
    std::uniform_real_distribution<float> lo(-noise_frac*rms, noise_frac*rms);
    std::vector<cf> rx;
    for (int i=0;i<pad;i++) rx.push_back(cf(lo(gn),lo(gn)));      // lead-in noise
    for (auto&s:ch) rx.push_back(s+cf(nz(gn),nz(gn)));

    int start; float cfo_est;
    auto rx_qam = ofdm.receive(rx, nqam, &start, &cfo_est);
    int e = bd(tx, mod.demodulate(rx_qam));
    int nb = (int)tx.size();
    printf("%-7s | start=%d (exp %d)  CFO est=%+.3f sc (inj %+.3f)  |  BER=%.5f %s\n",
        scheme, start, pad + ofdm.cp_len(), cfo_est, cfo_sc,
        (float)e/nb, e==0?"[EXACT]":"");
}

int main(){
    std::vector<cf> h = { cf(1.0f,0.0f), cf(0.5f,0.3f), cf(0.2f,-0.15f), cf(0.1f,0.05f) };
    printf("=== OFDM N=64 CP=16, multipath + CFO + lead-in noise, 6 data symbols ===\n");
    printf("-- clean (noise 0), CFO 0.20 subcarrier --\n");
    for (auto s : {"QPSK","16-QAM","64-QAM","256-QAM"}) run(s, h, 0.0f, 0.20f, 6, 37);
    printf("-- noise 1%% RMS, CFO -0.35 subcarrier --\n");
    for (auto s : {"QPSK","16-QAM","64-QAM"}) run(s, h, 0.01f, -0.35f, 6, 53);
    return 0;
}
