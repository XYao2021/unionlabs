// Front-end reproduction test (no radio, no threads):
//   modulate -> RRC pulse-shape -> [clean channel] -> matched filter ->
//   Gardner timing recovery -> ACQ -> CFO -> phase -> strip preamble -> demod
// This is the ONE path the sim/ and sweep tests skip: the RF front-end
// (match filter + timing recovery).  Prints BER.
//
// Usage: frontend_repro <config>
//   real  : TX rrc/poly 5/4, MF rrc 4/1 poly 4/1, Gardner sps=5  (current pipeline)
//   clean : TX rrc/poly 2/1, MF rrc 2/1 poly 1/1, Gardner sps=2  (integer 2 sps wire)
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "messages.hpp"
#include "filters.hpp"
#include "taps.hpp"
#include "timing_recovery.hpp"
#include "synchronization.hpp"
#include "frequency_offset.hpp"
#include "phase_offset.hpp"
#include <random>
#include <cstdio>
#include <string>
using cf = std::complex<float>;

// One filtering stage. rrcU/rrcD set the RRC symbol period (Ts=rrcU/rrcD);
// polyU/polyD set the resampling of the polyphase filter (independent).
static std::vector<cf> filt(const std::vector<cf>& in, int num_taps,
                            int rrcU, int rrcD, int polyU, int polyD,
                            double beta, bool matched)
{
    std::vector<cf> taps(num_taps);
    rrc_pulse(taps.data(), (num_taps-1)/2, rrcU, rrcD, beta);
    if (matched) {
        std::vector<cf> t2(num_taps);
        for (int i=0;i<num_taps;i++) t2[i]=std::conj(taps[num_taps-1-i]);
        taps=t2;
    }
    std::vector<cf> x = in;
    x.insert(x.end(), 2*num_taps, cf(0,0));
    if (x.size()%polyD) x.insert(x.end(), polyD-(x.size()%polyD), cf(0,0));
    FilterPolyphase f(polyU, polyD, (int)x.size(), num_taps, taps.data(), 1);
    f.set_head(true);
    std::vector<cf> out(f.out_len());
    out.resize(f.filter(x.data(), out.data()));
    return out;
}

static int bd(const std::vector<uint8_t>&a,const std::vector<uint8_t>&b){
    int n=std::min(a.size(),b.size()),e=0; for(int i=0;i<n;i++)e+=(a[i]!=b[i]); return e; }

int main(int argc,char**argv){
    std::string cfg = (argc>1)?argv[1]:"real";
    int txU,txD,mfRrcU,mfRrcD,mfPolyU,mfPolyD,gsps;
    if (cfg=="clean"){ txU=2;txD=1; mfRrcU=2;mfRrcD=1; mfPolyU=1;mfPolyD=1; gsps=2; }
    else if (cfg=="clean4"){ txU=4;txD=1; mfRrcU=4;mfRrcD=1; mfPolyU=1;mfPolyD=1; gsps=4; }
    else            { txU=5;txD=4; mfRrcU=4;mfRrcD=1; mfPolyU=4;mfPolyD=1; gsps=5; }
    printf("=== config '%s': TX rrc/poly %d/%d | MF rrc %d/%d poly %d/%d | Gardner sps=%d ===\n",
           cfg.c_str(),txU,txD,mfRrcU,mfRrcD,mfPolyU,mfPolyD,gsps);

    const int m=5, Ndata=508, num_taps=151; const double beta=0.25, symrate=0.8e6;
    auto pre=generate_msequence_preamble(m); int P=pre.size();
    Modulator mod(ModulationType::QPSK);
    std::mt19937 g(4242); std::uniform_int_distribution<int> db(0,1);
    std::vector<uint8_t> tx(Ndata*2); for(auto&x:tx)x=db(g);
    bool add=true; auto pkt=mod.modulate(tx,pre,add);
    printf("[TEST] packet=%zu sym (guard10+pre%d+data%d)\n",pkt.size(),P,Ndata);

    auto tx_wave = filt(pkt, num_taps, txU,txD, txU,txD, beta, false);
    printf("[TEST] TX RRC out  = %zu samples\n", tx_wave.size());

    std::normal_distribution<float> nz(0.f,0.01f);
    std::uniform_real_distribution<float> lo(-0.02f,0.02f);
    std::vector<cf> chan;
    for(int i=0;i<20;i++) chan.push_back(cf(lo(g),lo(g)));
    for(auto s:tx_wave) chan.push_back(s+cf(nz(g),nz(g)));
    for(int i=0;i<20;i++) chan.push_back(cf(lo(g),lo(g)));

    auto mf = filt(chan, num_taps, mfRrcU,mfRrcD, mfPolyU,mfPolyD, beta, true);
    printf("[TEST] MF out      = %zu samples\n", mf.size());

    bool nogard = (argc>2 && std::string(argv[2])=="nogard");
    std::vector<cf> timed;
    if (nogard) {
        // Ideal decimation: pick every gsps-th sample, best of gsps phases by ACQ.
        float bestpk=-1; int bestph=0;
        for (int ph=0; ph<gsps; ph++){
            std::vector<cf> d; for(size_t i=ph;i<mf.size();i+=gsps) d.push_back(mf[i]);
            ACQSynchronizer a(pre,1,15.0f,Ndata,true); auto r=a.SamplesACQPerformance(d);
            if(r.MaxCorrelation>bestpk){bestpk=r.MaxCorrelation;bestph=ph;}
        }
        for(size_t i=bestph;i<mf.size();i+=gsps) timed.push_back(mf[i]);
        printf("[TEST] IDEAL decimate phase=%d (peak %.1f)  timed=%zu\n",bestph,bestpk,timed.size());
    } else {
        GardnerTED ted(0.015f,0.707f,gsps); ted.reset();
        timed = ted.process(mf);
        printf("[TEST] timed out   = %zu symbols\n", timed.size());
    }

    ACQSynchronizer acq(pre,1,15.0f,Ndata,true);
    auto res=acq.SamplesACQPerformance(timed);
    if(!res.PacketDetected){ printf("[TEST] *** ACQ NOT DETECTED (peak %.2f/%d) ***\n",res.MaxCorrelation,P); return 2; }
    printf("[TEST] ACQ peak=%.2f/%d  tau=%d\n",res.MaxCorrelation,P,res.tau_opt);
    auto al=res.AlignedStats;

    // BER with NO CFO/phase correction (just strip preamble + demod) — isolates
    // whether the timing-recovered symbols are already clean.
    { std::vector<cf> d0(al.begin()+P, al.end()); int e0=bd(tx,mod.demodulate(d0));
      printf("[TEST] BER no-corr = %.4f (%d/%zu)\n",(double)e0/tx.size(),e0,tx.size()); }

    CFOCorrector cfo(symrate,1,pre,CFOCorrector::Method::PILOT_AIDED);
    auto c=cfo.correct(al);
    printf("[TEST] CFO est = %.1f Hz\n", cfo.get_last_cfo_hz());
    PhaseOffsetCorrector poc(mod,pre,P,true,0.02f,0.707f,PhaseOffsetCorrector::EstimationMethod::PREAMBLE);
    auto p=poc.correct(c);
    printf("[TEST] phase est = %.2f deg\n", poc.get_last_phase_estimate()*180.0/M_PI);
    { std::vector<cf> dC(c.begin()+P, c.end()); int eC=bd(tx,mod.demodulate(dC));
      printf("[TEST] BER after CFO only = %.4f\n",(double)eC/tx.size()); }
    std::vector<cf> data(p.begin()+P, p.end());
    auto rx=mod.demodulate(data);
    int be=bd(tx,rx);
    printf("[TEST] BER=%.4f (%d/%zu)  %s\n",(double)be/tx.size(),be,tx.size(),
           be==0?"[EXACT]":(be<10?"[NEAR]":"[FAIL]"));
    return be==0?0:1;
}
