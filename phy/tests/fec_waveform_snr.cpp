// fec_waveform_snr.cpp — FAIR SC-vs-OFDM comparison at matched Eb/N0.
//
// The earlier chain_evidence used an "injected-noise fraction" that was NOT the
// same effective SNR for SC and OFDM (SC has RRC matched-filter processing gain).
// Here we stop trusting the injected number and MEASURE the delivered Eb/N0
// data-aided: after equalization we compare the received symbols to the KNOWN
// transmitted symbols → noise variance → Es/N0 = Eb/N0 (QPSK, rate 1/2).
// We sweep the injected noise per waveform and plot results vs the MEASURED
// Eb/N0, so SC and OFDM land on the same x-axis. If the RF chain is fair, their
// coded-CRC and uncoded-BER curves should nearly overlap.
//
// CFO is set to 0 here to isolate the AWGN behaviour.
// Build (x86_64 + fftw); run: arch -x86_64 ./fec_waveform_snr [fec] [csv] [pkts]
#include "messages.hpp"
#include "fec.hpp"
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "filters.hpp"
#include "taps.hpp"
#include "synchronization.hpp"
#include "frequency_offset.hpp"
#include "phase_offset.hpp"
#include "ofdm.hpp"
#include <random>
#include <cstdio>
#include <cmath>
#include <string>
#include <fstream>
#include <complex>
#include <algorithm>
#include <vector>
using cf = std::complex<float>;

struct Res { bool ok; double noise_var; int demod_err; int nbits; bool crc; };

static std::vector<cf> filt(const std::vector<cf>& in,int num_taps,int rrcU,int rrcD,
                            int polyU,int polyD,double beta,bool matched){
    std::vector<cf> taps(num_taps); rrc_pulse(taps.data(),(num_taps-1)/2,rrcU,rrcD,beta);
    if(matched){ std::vector<cf> t2(num_taps); for(int i=0;i<num_taps;i++) t2[i]=std::conj(taps[num_taps-1-i]); taps=t2; }
    std::vector<cf> x=in; x.insert(x.end(),2*num_taps,cf(0,0));
    if(x.size()%polyD) x.insert(x.end(),polyD-(x.size()%polyD),cf(0,0));
    FilterPolyphase f(polyU,polyD,(int)x.size(),num_taps,taps.data(),1); f.set_head(true);
    std::vector<cf> out(f.out_len()); out.resize(f.filter(x.data(),out.data())); return out;
}
static int biterr(const std::vector<uint8_t>&a,const std::vector<uint8_t>&b){
    int n=std::min(a.size(),b.size()),e=0; for(int i=0;i<n;i++)e+=(a[i]!=b[i]); return e; }

// Data-aided noise/signal ratio via an LS-fit complex gain a = <r,s>/<s,s>, so
// residual scale/phase is removed and the estimate is unbiased at low SNR:
//   noise = mean|r - a·s|²,  signal = |a|²·mean|s|²,  return noise/signal.
// For QPSK rate-1/2, Eb/N0 = Es/N0 = signal/noise = 1/(that ratio).
static double da_noise(const std::vector<cf>& r, const std::vector<cf>& s){
    int n=std::min(r.size(),s.size()); if(n==0) return 1;
    cf num(0,0); double den=0;
    for(int i=0;i<n;i++){ num+=r[i]*std::conj(s[i]); den+=std::norm(s[i]); }
    if(den<1e-12) return 1;
    cf a=num/(float)den;
    double noise=0, sig=0;
    for(int i=0;i<n;i++){ noise+=std::norm(r[i]-a*s[i]); sig+=std::norm(a*s[i]); }
    if(sig<1e-12) return 1;
    return noise/sig;
}

static Res sc_once(const std::vector<uint8_t>& pkt, int info_len, Modulator& mod,
                   float noise_frac, uint32_t seed){
    auto coded=fec_encode_block(pkt);
    int Ndata=(int)((coded.size()+1)/2);
    auto pre=generate_msequence_preamble(5); int P=(int)pre.size();
    bool add=true; auto packet=mod.modulate(coded,pre,add);
    std::vector<cf> npre; bool na=false; auto s_true=mod.modulate(coded,npre,na);  // data QAM
    const int nt=151; const double beta=0.25; const int U=2;
    auto tx=filt(packet,nt,U,1,U,1,beta,false);
    std::mt19937 g(seed); std::normal_distribution<float> nz(0.f,1.f);
    double sp=0; for(auto&s:tx) sp+=std::norm(s); float srms=std::sqrt(sp/tx.size());
    float amp=std::max(0.003f,noise_frac)*srms;
    std::vector<cf> chan;
    for(int i=0;i<20;i++) chan.push_back(cf(0.5f*amp*nz(g),0.5f*amp*nz(g)));
    for(auto&s:tx) chan.push_back(s+cf(amp*nz(g),amp*nz(g)));
    for(int i=0;i<20;i++) chan.push_back(cf(0.5f*amp*nz(g),0.5f*amp*nz(g)));
    auto mf=filt(chan,nt,U,1,1,1,beta,true);
    float bp=-1; int bph=0;
    for(int ph=0;ph<U;ph++){ std::vector<cf> d; for(size_t i=ph;i<mf.size();i+=U) d.push_back(mf[i]);
        ACQSynchronizer a(pre,1,15.0f,Ndata,true); auto r=a.SamplesACQPerformance(d);
        if(r.MaxCorrelation>bp){bp=r.MaxCorrelation;bph=ph;} }
    std::vector<cf> timed; for(size_t i=bph;i<mf.size();i+=U) timed.push_back(mf[i]);
    ACQSynchronizer acq(pre,1,15.0f,Ndata,true); auto res=acq.SamplesACQPerformance(timed);
    if(!res.PacketDetected) return {false,1,0,0,false};
    auto al=res.AlignedStats;
    CFOCorrector cfo(0.8e6,1,pre,CFOCorrector::Method::PILOT_LS); auto c=cfo.correct(al);
    PhaseOffsetCorrector poc(mod,pre,P,true,0.02f,0.707f,PhaseOffsetCorrector::EstimationMethod::PREAMBLE);
    auto pc=poc.correct(c);
    std::vector<cf> rx(pc.begin()+P, pc.end());
    double nv=da_noise(rx,s_true);
    auto rxc=mod.demodulate(rx); int de=biterr(coded,rxc);
    auto llr=soft_demodulate_llr(rx,mod,std::max(1e-3f,(float)nv));
    auto info=fec_soft_decode_block(llr,info_len);
    auto t=decode_packet_bits(info);
    return {true,nv,de,(int)coded.size(),std::get<3>(t)};
}

static std::vector<cf> multipath(const std::vector<cf>& x,const std::vector<cf>& h){
    std::vector<cf> y(x.size(),cf(0,0));
    for(size_t n=0;n<x.size();++n) for(size_t k=0;k<h.size()&&k<=n;++k) y[n]+=h[k]*x[n-k];
    return y; }

static Res ofdm_once(const std::vector<uint8_t>& pkt, int info_len, Modulator& mod,
                     OFDM& ofdm, float noise_frac, uint32_t seed){
    auto coded=fec_encode_block(pkt);
    std::vector<cf> npre; bool na=false; auto qam=mod.modulate(coded,npre,na);
    int nq=(int)qam.size(); auto frame=ofdm.modulate(qam);
    std::vector<cf> h={cf(1,0)};                       // flat channel (AWGN-only, no CFO)
    std::mt19937 g(seed); std::normal_distribution<float> nz(0.f,1.f);
    auto ch=multipath(frame,h);
    std::vector<cf> burst;
    for(int i=0;i<40;i++) burst.push_back(cf(0.01f*nz(g),0.01f*nz(g)));
    double pw=0; for(auto&s:ch) pw+=std::norm(s); float rms=std::sqrt(pw/ch.size());
    float nf=std::max(0.003f,noise_frac);
    for(auto&s:ch) burst.push_back(s+cf(nf*rms*nz(g),nf*rms*nz(g)));
    for(int i=0;i<40;i++) burst.push_back(cf(0.01f*nz(g),0.01f*nz(g)));
    double bp=0; for(auto&s:burst) bp+=std::norm(s); float brms=std::sqrt(bp/burst.size());
    for(auto&s:burst) s/=brms;
    auto rx=ofdm.receive(burst,nq);
    if(rx.empty()) return {false,1,0,0,false};
    double nv=da_noise(rx,qam);
    auto rxc=mod.demodulate(rx); int de=biterr(coded,rxc);
    auto llr=soft_demodulate_llr(rx,mod,std::max(1e-3f,(float)nv));
    auto info=fec_soft_decode_block(llr,info_len);
    auto t=decode_packet_bits(info);
    return {true,nv,de,(int)coded.size(),std::get<3>(t)};
}

int main(int argc,char**argv){
    std::string fec=(argc>1)?argv[1]:"turbo";
    std::string csv=(argc>2)?argv[2]:"fec_waveform_snr.csv";
    int PK=(argc>3)?std::atoi(argv[3]):80;
    size_t bytes=125;
    std::string payload(bytes,'0'); for(size_t i=0;i<bytes;i++) payload[i]=char('0'+i%10);
    auto pkt=build_packet_bits(payload,2,5); int info_len=(int)pkt.size();
    fec_set_type(fec,256);
    Modulator mod(ModulationType::QPSK); OFDM ofdm(64,16);

    std::ofstream cf(csv); cf<<"waveform,ebn0_db,crc_pct,uncoded_ber,npkt\n";
    printf("fair SC-vs-OFDM @ matched Eb/N0, fec=%s, %d pkts/point, CFO=0 (AWGN)\n", fec.c_str(), PK);
    printf("%-6s | inj  | meas Eb/N0 | coded CRC | uncoded BER\n","wave");

    // Per-waveform injected-noise sweeps chosen to span the same measured Eb/N0.
    std::vector<float> sc_nz ={0.20f,0.32f,0.45f,0.55f,0.62f,0.66f,0.70f,0.75f,0.82f};
    std::vector<float> of_nz ={0.08f,0.14f,0.20f,0.26f,0.30f,0.33f,0.36f,0.40f,0.46f};

    auto sweep=[&](const char* name, std::vector<float>& nzs, bool is_ofdm){
        for(float nf : nzs){
            std::vector<double> nvs; long de=0, nb=0; int crc=0, got=0;
            for(int p=0;p<PK;++p){
                Res r = is_ofdm ? ofdm_once(pkt,info_len,mod,ofdm,nf,1000+p*7)
                                : sc_once(pkt,info_len,mod,nf,1000+p*7);
                if(!r.ok) continue; ++got;
                nvs.push_back(r.noise_var); de+=r.demod_err; nb+=r.nbits; if(r.crc)++crc;
            }
            if(!got) continue;
            // MEDIAN per-packet noise/signal — robust to the odd mis-synced packet
            // whose huge nv would wreck a mean and misplace the SNR point.
            std::sort(nvs.begin(),nvs.end());
            double nv=nvs[nvs.size()/2];
            double ebn0=-10.0*std::log10(std::max(1e-6,nv));
            double crcp=100.0*crc/got, uber=(double)de/std::max<long>(1,nb);
            printf("%-6s | %.2f | %8.2f   | %6.1f%%   | %.4f\n", name, nf, ebn0, crcp, uber);
            cf<<name<<","<<ebn0<<","<<crcp<<","<<uber<<","<<got<<"\n";
        }
    };
    sweep("SC",   sc_nz, false);
    sweep("OFDM", of_nz, true);
    cf.close();
    printf("[csv] wrote %s\n", csv.c_str());
    return 0;
}
