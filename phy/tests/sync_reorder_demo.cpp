// DEMO 2 — frequency & phase offset AFTER time sync (no hardware).
// Builds a QPSK packet, injects a carrier frequency offset + static phase offset
// + noise + a packet-position offset, then runs the reordered receive flow:
//   TimeSync(ACQ) -> CFO(pilot) -> phase(preamble ML) -> strip preamble -> demod
// and reports BER with vs without correction.
//
// NOTE: this uses the GLOBAL-MAX search (SamplesACQPerformance) so alignment is
// unambiguous for the demo. The real pipeline uses PerformACQOptimized, whose
// first-threshold-crossing exit means --sync_threshold must be tuned above the
// guard correlation sidelobe (see CHANGES.md).
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "messages.hpp"
#include "synchronization.hpp"
#include "frequency_offset.hpp"
#include "phase_offset.hpp"
#include <random>
#include <cstdio>
using cf=std::complex<float>;
static std::vector<uint8_t> rand_bits(int n,unsigned s){std::mt19937 g(s);
    std::uniform_int_distribution<int> d(0,1);std::vector<uint8_t> b(n);for(auto&x:b)x=d(g);return b;}
static int bd(const std::vector<uint8_t>&a,const std::vector<uint8_t>&b){int n=std::min(a.size(),b.size()),e=0;
    for(int i=0;i<n;i++)e+=(a[i]!=b[i]);return e;}
static int run(float sigma){
    const int m=5,Ndata=508; const float symrate=0.8e6f,dphi=0.03f,theta=0.6f;
    auto pre=generate_msequence_preamble(m); int P=pre.size();
    Modulator mod(ModulationType::QPSK);
    auto tx=rand_bits(Ndata*mod.get_bits_per_symbol(),2025); bool add=true;
    auto pkt=mod.modulate(tx,pre,add);
    std::mt19937 g(7); std::normal_distribution<float> nz(0.f,sigma); std::uniform_real_distribution<float> lo(-0.2f,0.2f);
    std::vector<cf> rx; for(int i=0;i<20;i++)rx.push_back(cf(lo(g),lo(g)));
    rx.insert(rx.end(),pkt.begin(),pkt.end()); for(int i=0;i<20;i++)rx.push_back(cf(lo(g),lo(g)));
    for(size_t n=0;n<rx.size();n++){ cf s=rx[n]*std::polar(1.0f,theta+dphi*(float)n); s+=cf(nz(g),nz(g)); rx[n]=s; }
    ACQSynchronizer ACQ(pre,1,15.0f,Ndata,true);
    auto res=ACQ.SamplesACQPerformance(rx);
    if(!res.PacketDetected){ printf("  *** ACQ failed ***\n"); return 99; }
    auto al=res.AlignedStats;
    std::vector<cf> raw(al.begin()+P,al.end()); int e0=bd(tx,mod.demodulate(raw));
    CFOCorrector cfo(symrate,1,pre,CFOCorrector::Method::PILOT_AIDED);
    PhaseOffsetCorrector poc(mod,pre,P,true,0.02f,0.707f,PhaseOffsetCorrector::EstimationMethod::PREAMBLE);
    auto out=poc.correct(cfo.correct(al));
    std::vector<cf> data(out.begin()+P,out.end()); int e1=bd(tx,mod.demodulate(data));
    printf("  noise sigma=%.2f | tau=%d aligned=%zu | BER no-corr=%.3f  ->  BER corrected=%.4f  %s\n",
        sigma,res.tau_opt,al.size(),(float)e0/tx.size(),(float)e1/tx.size(),
        e1==0?"[EXACT]":(e1<=2?"[OK]":"[FAIL]"));
    return e1;
}
int main(){
    printf("--- reordered flow: TimeSync -> CFO -> phase -> strip -> demod ---\n");
    int a=run(0.0f);      // deterministic, should be EXACT (0 errors)
    int b=run(0.05f);     // realistic AWGN, should be near-zero
    printf("\n==== %s ====\n",(a==0 && b<=2)?"RECOVERED (reorder works)":"PROBLEM");
    return (a==0 && b<=2)?0:1;
}
