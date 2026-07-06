// DEMO 3 — full software RECEIVE CHAIN across many modulations (no hardware).
// For each scheme: build a packet, inject a carrier frequency offset + static
// phase offset + AWGN + a packet-position offset, then run the reordered flow
//   TimeSync(ACQ) -> CFO(pilot) -> phase(preamble ML) -> strip preamble -> demod
// and report BER with vs without correction. Absolute schemes strip the whole
// preamble; differential schemes keep the last preamble symbol as their
// differential reference (strip P-1). Uses global-max ACQ so alignment is exact.
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "messages.hpp"
#include "synchronization.hpp"
#include "frequency_offset.hpp"
#include "phase_offset.hpp"
#include <random>
#include <cstdio>
using cf=std::complex<float>;
static std::vector<uint8_t> rb(int n,unsigned s){std::mt19937 g(s);std::uniform_int_distribution<int> d(0,1);
    std::vector<uint8_t> b(n);for(auto&x:b)x=d(g);return b;}
static int bd(const std::vector<uint8_t>&a,const std::vector<uint8_t>&b){int n=std::min(a.size(),b.size()),e=0;
    for(int i=0;i<n;i++)e+=(a[i]!=b[i]);return e;}

// run one scheme through the reordered RX chain; returns bit errors after correction
static int one(const char* name, float sigma, bool& detected){
    const int m=5, Ndata=400; const float symrate=0.8e6f, dphi=0.03f, theta=0.6f;
    auto pre=generate_msequence_preamble(m); int P=pre.size();
    Modulator mod(string_to_mod_type(name));
    // differential schemes (matches Modulator::check_differential): reference-based
    std::string nm(name);
    bool diff = (nm=="DBPSK"||nm=="DQPSK"||nm=="8-DPSK");
    int bps=mod.get_bits_per_symbol();
    auto tx=rb(Ndata*bps, 4242);
    bool add=true; auto pkt=mod.modulate(tx,pre,add);

    // received stream: [pad | packet | pad] with CFO + phase + noise
    std::mt19937 g(11); std::normal_distribution<float> nz(0.f,sigma);
    std::uniform_real_distribution<float> lo(-0.2f,0.2f);
    std::vector<cf> rx; for(int i=0;i<18;i++)rx.push_back(cf(lo(g),lo(g)));
    rx.insert(rx.end(),pkt.begin(),pkt.end()); for(int i=0;i<18;i++)rx.push_back(cf(lo(g),lo(g)));
    for(size_t n=0;n<rx.size();n++){ cf s=rx[n]*std::polar(1.0f,theta+dphi*(float)n); s+=cf(nz(g),nz(g)); rx[n]=s; }

    ACQSynchronizer ACQ(pre,1,15.0f,Ndata,true);
    auto res=ACQ.SamplesACQPerformance(rx);
    detected=res.PacketDetected;
    if(!detected){ printf(">> %-9s  NOT DETECTED\n",name); return 9999; }
    auto al=res.AlignedStats;   // [preamble | data]

    // BER with NO correction (demod the data straight from the aligned block)
    int e0;
    if(diff){ std::vector<cf> rd(al.begin()+(P-1),al.end()); e0=bd(tx,mod.demodulate(rd)); }
    else    { std::vector<cf> rd(al.begin()+P,     al.end()); e0=bd(tx,mod.demodulate(rd)); }

    // reordered correction: CFO then (for absolute) phase
    CFOCorrector cfo(symrate,1,pre,CFOCorrector::Method::PILOT_AIDED);
    auto c = cfo.correct(al);
    std::vector<cf> corr;
    if(diff){
        // differential schemes are phase-robust; differential demod handles the
        // residual static phase. Keep last preamble symbol as the reference.
        corr.assign(c.begin()+(P-1), c.end());
    } else {
        PhaseOffsetCorrector poc(mod,pre,P,true,0.02f,0.707f,
                                 PhaseOffsetCorrector::EstimationMethod::PREAMBLE);
        auto p = poc.correct(c);
        corr.assign(p.begin()+P, p.end());
    }
    int e1=bd(tx,mod.demodulate(corr));
    printf(">> %-9s %-4s bps=%d C=%3d | BER no-corr=%.3f -> corrected=%.4f  %s\n",
        name, diff?"DIFF":"ABS", bps, mod.get_constellation_size(),
        (float)e0/tx.size(), (float)e1/tx.size(),
        e1==0?"[EXACT]":(e1<=2?"[OK]":"[FAIL]"));
    return e1;
}

int main(){
    const char* abs_set[] ={"QPSK","8-PSK","16-QAM","32-QAM","64-QAM","16APSK","32APSK"};
    const char* diff_set[]={"DBPSK","DQPSK","8-DPSK"};
    int bad=0; bool det;
    printf("Impairments per run: CFO=0.03 rad/sym, static phase=0.6 rad, + position offset\n\n");
    for(float sig : {0.0f, 0.02f}){
        printf("=================  AWGN sigma = %.2f  =================\n", sig);
        printf("-- absolute-constellation schemes (full CFO + phase chain) --\n");
        for(auto s:abs_set){ int e=one(s,sig,det); if(e>2)bad++; }
        printf("-- differential schemes (CFO + differential demod; phase-robust) --\n");
        for(auto s:diff_set){ int e=one(s,sig,det); if(e>2)bad++; }
        printf("\n");
    }
    printf("==== %s ====\n", bad==0?"ALL MODULATIONS RECOVERED THROUGH THE REORDERED CHAIN":"SOME FAILED");
    return bad;
}
