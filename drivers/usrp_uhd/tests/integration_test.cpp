// Integration test of the REAL modified pipeline threads (no UHD):
//   modulate -> pulse_shaping_filter_thread -> [channel CFO+phase+AWGN + AGC] ->
//   match_filter_thread (single-rate matched filter) ->
//   TimeSync_thread (ACQ joint frame+symbol timing at os) ->
//   CFO -> phase -> strip preamble -> demod
// Runs the actual filters.cpp / synchronization.cpp code paths that were changed.
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "messages.hpp"
#include "filters.hpp"
#include "synchronization.hpp"
#include "frequency_offset.hpp"
#include "phase_offset.hpp"
#include "FIFO.hpp"
#include <random>
#include <thread>
#include <cstdio>
using cf=std::complex<float>;
using Blk=std::pair<size_t,std::vector<cf>>;

static int bd(const std::vector<uint8_t>&a,const std::vector<uint8_t>&b){
    int n=std::min(a.size(),b.size()),e=0;for(int i=0;i<n;i++)e+=(a[i]!=b[i]);return e;}

static int run(double symrate,int U,int D,double cfo_hz,double phase_deg,float sigma){
    const int m=5,Ndata=508,num_taps=151; const double beta=0.25, roll=0.25;
    const double tx_rate = symrate*U/D, rx_rate=tx_rate;
    const int os = (int)std::lround(rx_rate/symrate);
    auto pre=generate_msequence_preamble(m); int P=pre.size();
    Modulator mod(ModulationType::QPSK);
    std::mt19937 g(4242); std::uniform_int_distribution<int> db(0,1);
    std::vector<uint8_t> tx(Ndata*2); for(auto&x:tx)x=db(g);

    std::atomic<bool> stop{false};
    MutexFIFO<Blk> mod_fifo, shaped_fifo, agc_fifo, filtered_fifo, synced_fifo;

    // TX: modulate (inline) then real pulse_shaping_filter_thread
    bool add=true; auto pkt=mod.modulate(tx,pre,add);
    mod_fifo.push({0,pkt});
    std::thread ps(pulse_shaping_filter_thread, std::ref(mod_fifo), std::ref(shaped_fifo),
        std::string("rrc"), symrate, tx_rate, num_taps, U, D, roll, 1, std::ref(stop), std::string("transmitter"));

    // wait for shaped block
    Blk shaped; for(int i=0;i<500 && !shaped_fifo.pop(shaped); ++i) std::this_thread::sleep_for(std::chrono::milliseconds(2));
    stop.store(true); ps.join(); stop.store(false);
    if (shaped.second.empty()){ printf("no shaped output\n"); return 9999; }

    // Channel: pad + CFO ramp (per wire sample) + static phase + AWGN, then AGC (RMS->1)
    double cfo_rad = 2.0*M_PI*cfo_hz/(symrate*os);
    double ph = phase_deg*M_PI/180.0;
    std::normal_distribution<float> nz(0.f,sigma); std::uniform_real_distribution<float> lo(-0.05f,0.05f);
    std::vector<cf> y; int pad=19;
    for(int i=0;i<pad;i++) y.push_back(cf(lo(g),lo(g)));
    for(size_t k=0;k<shaped.second.size();++k){ cf s=shaped.second[k]*std::polar(1.0f,(float)(ph+cfo_rad*(double)k)); s+=cf(nz(g),nz(g)); y.push_back(s);}
    for(int i=0;i<pad;i++) y.push_back(cf(lo(g),lo(g)));
    double p=0; for(auto&s:y)p+=std::norm(s); float rms=std::sqrt(p/y.size());
    for(auto&s:y) s/=rms;   // mimic FeedforwardAGC target_rms=1
    agc_fifo.push({0,y});

    // RX: real match_filter_thread (single-rate matched filter)
    std::thread mf(match_filter_thread, std::ref(agc_fifo), std::ref(filtered_fifo),
        std::string("rrc"), symrate, rx_rate, num_taps, 1, 1, roll, 1, std::ref(stop), std::string("receiver"));
    // real TimeSync_thread (ACQ at os)
    std::thread ts(TimeSync_thread, std::ref(filtered_fifo), std::ref(synced_fifo),
        std::ref(pre), (size_t)U, (size_t)D, os, std::ref(stop), Ndata, 15.0f);

    Blk aligned; bool got=false;
    for(int i=0;i<1000;i++){ if(synced_fifo.pop(aligned)){got=true;break;} std::this_thread::sleep_for(std::chrono::milliseconds(2)); }
    stop.store(true); mf.join(); ts.join();
    if(!got){ printf("os=%d cfo=%.0f ph=%.0f: TimeSync produced nothing (no detect)\n",os,cfo_hz,phase_deg); return 9999; }

    auto al=aligned.second;   // [preamble|data] 1 sps
    CFOCorrector cfo(symrate,1,pre,CFOCorrector::Method::PILOT_AIDED);
    auto c=cfo.correct(al);
    PhaseOffsetCorrector poc(mod,pre,P,true,0.02f,0.707f,PhaseOffsetCorrector::EstimationMethod::PREAMBLE);
    auto pc=poc.correct(c);
    std::vector<cf> data(pc.begin()+P, pc.end());
    int e=bd(tx,mod.demodulate(data));
    printf("os=%d U/D=%d/%d cfo=%6.0fHz ph=%4.0f sig=%.3f: aligned=%zu CFOest=%7.1f BER=%.4f %s\n",
        os,U,D,cfo_hz,phase_deg,sigma,al.size(),cfo.get_last_cfo_hz(),
        (double)e/tx.size(), e==0?"[EXACT]":(e<10?"[NEAR]":"[FAIL]"));
    return e;
}

int main(){
    int bad=0;
    // symbol_rate 0.8e6, U/D=2/1 -> 2 sps (the new default)
    for(double c : {0.0, 2000.0, 8000.0})
        for(double ph : {0.0, 35.0, 120.0})
            if(run(0.8e6,2,1,c,ph,0.01f)>5) bad++;
    // also 4 sps (U/D=4/1)
    if(run(0.8e6,4,1,3000.0,50.0,0.01f)>5) bad++;
    printf("\n%s\n", bad==0?"==== INTEGRATION PASS (real threads) ====":"==== INTEGRATION FAIL ====");
    return bad;
}
