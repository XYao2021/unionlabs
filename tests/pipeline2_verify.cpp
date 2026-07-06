// Verify the proposed integer-sps front-end:
//   modulate -> RRC shape (N sps) -> channel(CFO+phase+AWGN) ->
//   TRUE matched filter (RRC at Ts=N, no resample) ->
//   ACQ at samples_per_symbol=N  (joint frame+symbol timing, no Gardner) ->
//   CFO -> phase -> strip preamble -> demod.
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "messages.hpp"
#include "filters.hpp"
#include "taps.hpp"
#include "synchronization.hpp"
#include "frequency_offset.hpp"
#include "phase_offset.hpp"
#include <random>
#include <cstdio>
using cf=std::complex<float>;

static std::vector<cf> filt(const std::vector<cf>& in,int nt,int rU,int rD,int pU,int pD,double b,bool m){
    std::vector<cf> t(nt); rrc_pulse(t.data(),(nt-1)/2,rU,rD,b);
    if(m){std::vector<cf> t2(nt);for(int i=0;i<nt;i++)t2[i]=std::conj(t[nt-1-i]);t=t2;}
    std::vector<cf> x=in; x.insert(x.end(),2*nt,cf(0,0));
    if(x.size()%pD)x.insert(x.end(),pD-(x.size()%pD),cf(0,0));
    FilterPolyphase f(pU,pD,(int)x.size(),nt,t.data(),1); f.set_head(true);
    std::vector<cf> o(f.out_len()); o.resize(f.filter(x.data(),o.data())); return o;
}
static int bd(const std::vector<uint8_t>&a,const std::vector<uint8_t>&b){
    int n=std::min(a.size(),b.size()),e=0;for(int i=0;i<n;i++)e+=(a[i]!=b[i]);return e;}

static int run(int N, double cfo_hz, double phase_deg, float sigma){
    const int m=5,Ndata=508,nt=151; const double beta=0.25, symrate=0.8e6;
    auto pre=generate_msequence_preamble(m); int P=pre.size();
    Modulator mod(ModulationType::QPSK);
    std::mt19937 g(4242); std::uniform_int_distribution<int> db(0,1);
    std::vector<uint8_t> tx(Ndata*2); for(auto&x:tx)x=db(g);
    bool add=true; auto pkt=mod.modulate(tx,pre,add);

    // TX RRC: N sps wire (rrc Ts=N, resample by N)
    auto txw=filt(pkt,nt,N,1,N,1,beta,false);

    // Channel: pad + CFO ramp (per wire sample) + static phase + AWGN
    double cfo_rad = 2.0*M_PI*cfo_hz/(symrate*N);   // per wire sample
    double ph = phase_deg*M_PI/180.0;
    std::normal_distribution<float> nz(0.f,sigma); std::uniform_real_distribution<float> lo(-0.05f,0.05f);
    std::vector<cf> ch; int pad=17;
    for(int i=0;i<pad;i++)ch.push_back(cf(lo(g),lo(g)));
    for(size_t k=0;k<txw.size();++k){ cf s=txw[k]*std::polar(1.0f,(float)(ph+cfo_rad*(double)k)); s+=cf(nz(g),nz(g)); ch.push_back(s);}
    for(int i=0;i<pad;i++)ch.push_back(cf(lo(g),lo(g)));

    // TRUE matched filter at N sps, NO resample
    auto mf=filt(ch,nt,N,1,1,1,beta,true);

    // ACQ directly on N-sps stream: joint frame+symbol timing.
    ACQSynchronizer acq(pre,/*sps*/N,15.0f,Ndata,true);
    auto res=acq.SamplesACQPerformance(mf);
    if(!res.PacketDetected){ printf("N=%d cfo=%.0f ph=%.0f sig=%.3f: NOT DETECTED (pk %.1f)\n",N,cfo_hz,phase_deg,sigma,res.MaxCorrelation); return 9999; }
    auto al=res.AlignedStats;    // 1 sps [preamble|data] at best phase

    // CFO estimate is per SYMBOL now (aligned block is 1 sps)
    CFOCorrector cfo(symrate,1,pre,CFOCorrector::Method::PILOT_AIDED);
    auto c=cfo.correct(al);
    PhaseOffsetCorrector poc(mod,pre,P,true,0.02f,0.707f,PhaseOffsetCorrector::EstimationMethod::PREAMBLE);
    auto p=poc.correct(c);
    std::vector<cf> data(p.begin()+P,p.end());
    int e=bd(tx,mod.demodulate(data));
    double sym_cfo_est = cfo.get_last_cfo_hz();
    printf("N=%d cfo=%6.0fHz ph=%4.0fdeg sig=%.3f: pk=%.1f/%d tau=%d  CFOest=%7.1f  BER=%.4f %s\n",
        N,cfo_hz,phase_deg,sigma,res.MaxCorrelation,P,res.tau_opt,sym_cfo_est,
        (double)e/tx.size(), e==0?"[EXACT]":(e<10?"[NEAR]":"[FAIL]"));
    return e;
}

int main(){
    int bad=0;
    for(int N : {2,4}){
        for(double c : {0.0, 2000.0, 8000.0})
            for(double ph : {0.0, 35.0, 120.0})
                if(run(N,c,ph,0.01f)>5) bad++;
    }
    printf("\n%s\n", bad==0?"==== ALL PASS (integer-sps + ACQ-timing front-end) ====":"==== SOME FAILED ====");
    return bad;
}
