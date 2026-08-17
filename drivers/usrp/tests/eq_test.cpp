// Equalizer test: modulate -> multipath ISI channel -> (train on preamble,
// equalize data) -> demod. Reports BER with vs without equalizer for each
// scheme and equalizer type. Isolates the equalizer from the RF front-end
// (operates on 1-sps aligned symbols, as channel_eq_thread does).
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "messages.hpp"
#include "channel_estimation.hpp"
#include <random>
#include <cstdio>
#include <string>
using cf = std::complex<float>;

static int bd(const std::vector<uint8_t>&a,const std::vector<uint8_t>&b){
    int n=std::min(a.size(),b.size()),e=0;for(int i=0;i<n;i++)e+=(a[i]!=b[i]);return e;}

// Apply an FIR multipath channel h to x (full convolution, keep first x.size()).
static std::vector<cf> multipath(const std::vector<cf>& x, const std::vector<cf>& h){
    std::vector<cf> y(x.size(), cf(0,0));
    for (size_t n=0;n<x.size();++n)
        for (size_t k=0;k<h.size() && k<=n;++k)
            y[n] += h[k]*x[n-k];
    return y;
}

static void run(const char* scheme, const std::vector<cf>& h, float sigma,
                const std::vector<cf>& pre, const char* pretag){
    const int Ndata=400; int P=(int)pre.size();
    Modulator mod(string_to_mod_type(scheme));
    int bps=mod.get_bits_per_symbol();
    std::mt19937 g(123); std::uniform_int_distribution<int> db(0,1);
    std::vector<uint8_t> tx(Ndata*bps); for(auto&x:tx)x=db(g);

    bool add=true; std::vector<cf> pre_m=pre; auto pkt=mod.modulate(tx,pre_m,add);  // [guard|pre|data]
    // strip the guard(10) — the pipeline's ACQ delivers [preamble|data]
    std::vector<cf> block(pkt.begin()+10, pkt.end());

    // channel: multipath + AWGN
    auto rxb = multipath(block, h);
    std::mt19937 gn(7); std::normal_distribution<float> nz(0.f,sigma);
    for(auto&s:rxb) s+=cf(nz(gn),nz(gn));

    std::vector<cf> rx_pre(rxb.begin(), rxb.begin()+P);
    std::vector<cf> rx_dat(rxb.begin()+P, rxb.end());

    // BER with NO equalizer
    int e_no = bd(tx, mod.demodulate(rx_dat));

    int nb = (int)tx.size();
    // Feed [data | D zero-runway] so all data symbols get output, then take the
    // delay-aligned window eq[D .. D+Ndata-1] — recovers EVERY data symbol
    // (including the last ones / the CRC), the way the fixed pipeline must.
    auto full_ber = [&](std::vector<cf> d, int D)->float{
        if ((int)d.size() < D+Ndata) return 1.0f;
        std::vector<cf> aligned(d.begin()+D, d.begin()+D+Ndata);
        auto rb = mod.demodulate(aligned);
        return (float)bd(tx, rb)/nb;
    };
    std::vector<cf> dat5 = rx_dat; dat5.insert(dat5.end(), 5, cf(0,0));
    float b_dd, b_frozen;
    { LMSEqualizer eq(11,0.3f,2); eq.train(rx_pre,pre); b_dd    =full_ber(eq.equalize(dat5,&mod),5); }
    { LMSEqualizer eq(11,0.3f,2); eq.train(rx_pre,pre); b_frozen=full_ber(eq.equalize(dat5,nullptr),5); }
    printf("%-7s [%s] | no-EQ BER=%.4f | EQ-DD=%.4f  EQ-frozen=%.4f\n",
        scheme, pretag, (float)e_no/nb, b_dd, b_frozen);
}

int main(){
    std::vector<cf> h = { cf(1.0f,0.0f), cf(0.35f,0.1f), cf(0.15f,-0.05f) };
    auto bpsk = generate_msequence_preamble(5);        // real, length 31
    auto zc   = generate_zadoff_chu_preamble(25, 63);  // complex, length 63
    printf("=== multipath h=[1, 0.35+0.1j, 0.15-0.05j], sigma=0.02 ===\n");
    printf("-- BPSK m-seq preamble (real) --\n");
    for (auto s : {"QPSK","8-PSK","16-QAM","64-QAM"}) run(s, h, 0.02f, bpsk, "BPSK");
    printf("-- Zadoff-Chu preamble (complex) --\n");
    for (auto s : {"QPSK","8-PSK","16-QAM","64-QAM"}) run(s, h, 0.02f, zc, "ZC");
    return 0;
}
