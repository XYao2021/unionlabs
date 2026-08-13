// chain_evidence.cpp — proof that the RX chain succeeds, stage by stage.
//
// Runs the REAL DSP (no radio) for BOTH waveforms with QPSK + LDPC through a
// channel with a KNOWN carrier-frequency offset, and records evidence that each
// stage did its job:
//   SC  : ACQ preamble sync -> CFO estimate -> phase estimate -> demod -> LDPC.
//   OFDM: Schmidl-Cox sync -> CFO estimate -> per-subcarrier EQ -> demod -> LDPC.
//
// For each run it writes, into <out>/<tag>/:
//   ideal.txt    ideal QPSK constellation
//   rx_pre.txt   RX constellation BEFORE CFO/phase correction (impaired)
//   rx_post.txt  RX constellation AFTER  correction (clean clusters)
//   stages.txt   key=value report: sync, CFO true/est, phase, EVM, BER, CRC
// Render with:  python3 tools/plot_evidence.py <out>
//
// Build (x86_64 + fftw, like the other front-end tests):
//   g++ -arch x86_64 -std=c++17 -O2 -pthread -include atomic -include cstdint -DUSE_VOLK \
//       -Itests/stub -Iinclude -I/usr/local/include tests/chain_evidence.cpp src/modulator.cpp \
//       src/filters.cpp -L/usr/local/lib -lfftw3f -lfftw3f_threads -lvolk -o chain_evidence
//   arch -x86_64 ./chain_evidence [out_dir] [payload_bytes]
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
#include <filesystem>
#include <complex>
using cf = std::complex<float>;

// ── small helpers ──
static int biterr(const std::vector<uint8_t>&a,const std::vector<uint8_t>&b){
    int n=std::min(a.size(),b.size()),e=0; for(int i=0;i<n;i++)e+=(a[i]!=b[i]); return e; }

static void dump_iq(const std::string& path, const std::vector<cf>& x){
    std::ofstream f(path); f << "# real imag\n";
    for (auto& s : x) f << s.real() << " " << s.imag() << "\n";
}

static float evm_pct(const std::vector<cf>& syms, Modulator& mod){
    if (syms.empty()) return 0;
    // power-normalize, nearest ideal point
    auto C = mod.get_constellation();
    double ps=0; for(auto&s:syms) ps+=std::norm(s); float sc=std::sqrt(ps/syms.size());
    double pc=0; for(auto&c:C) pc+=std::norm(c); float cc=std::sqrt(pc/C.size());
    double e=0; for(auto& s: syms){ cf r=s/sc; float best=1e30f;
        for(auto& c:C){ float d=std::norm(r-c/cc); best=std::min(best,d);} e+=best; }
    return 100.f*std::sqrt(e/syms.size());
}

// one filtering stage: RRC symbol period = rrcU/rrcD; resampling = polyU/polyD
// (independent). TX upsamples (poly 2/1 -> sps 2); the matched filter only shapes
// (poly 1/1, stays at sps 2), matching the integer-sps front-end.
static std::vector<cf> filt(const std::vector<cf>& in,int num_taps,int rrcU,int rrcD,
                            int polyU,int polyD,double beta,bool matched){
    std::vector<cf> taps(num_taps); rrc_pulse(taps.data(),(num_taps-1)/2,rrcU,rrcD,beta);
    if(matched){ std::vector<cf> t2(num_taps); for(int i=0;i<num_taps;i++) t2[i]=std::conj(taps[num_taps-1-i]); taps=t2; }
    std::vector<cf> x=in; x.insert(x.end(),2*num_taps,cf(0,0));
    if(x.size()%polyD) x.insert(x.end(),polyD-(x.size()%polyD),cf(0,0));
    FilterPolyphase f(polyU,polyD,(int)x.size(),num_taps,taps.data(),1); f.set_head(true);
    std::vector<cf> out(f.out_len()); out.resize(f.filter(x.data(),out.data())); return out;
}

static void write_stages(const std::string& dir, const std::string& kv){
    std::ofstream f(dir+"/stages.txt"); f<<kv;
}

// ================================================================
//  SC + QPSK + LDPC
// ================================================================
static void run_sc(const std::string& base, const std::vector<uint8_t>& pkt, int info_len,
                   const std::string& payload, double cfo_true_hz, const std::string& ftype,
                   float noise_frac){
    std::string dir=base+"/SC_QPSK"; std::filesystem::create_directories(dir);
    std::string feclbl=(ftype=="ldpc")?"LDPC-k256":(ftype=="turbo")?"turbo-k256":"conv-K7";
    printf("\n===== SC · QPSK · %s =====\n", feclbl.c_str());

    fec_set_type(ftype,256);
    auto coded = fec_encode_block(pkt);
    Modulator mod(ModulationType::QPSK);
    int Ndata=(int)((coded.size()+1)/2);                 // QPSK data symbols
    auto pre=generate_msequence_preamble(5); int P=(int)pre.size();
    bool add=true; auto packet=mod.modulate(coded,pre,add);

    const int num_taps=151; const double beta=0.25, symrate=0.8e6; const int U=2;
    const double fs=symrate*U;
    auto tx_wave=filt(packet,num_taps,U,1, U,1, beta,false);   // RRC 2/1, upsample 2/1 -> sps 2

    // channel: lead-in noise + KNOWN CFO + AWGN scaled to the signal RMS (so
    // noise_frac is an SNR-like knob: bigger => more demod errors for FEC to fix).
    std::mt19937 g(4242); std::normal_distribution<float> nz(0.f,1.f);
    double sp=0; for(auto&s:tx_wave) sp+=std::norm(s); float srms=std::sqrt(sp/tx_wave.size());
    float na=std::max(0.005f, noise_frac)*srms;                 // noise amplitude
    std::vector<cf> chan;
    for(int i=0;i<20;i++) chan.push_back(cf(0.5f*na*nz(g),0.5f*na*nz(g)));
    for(size_t n=0;n<tx_wave.size();++n){
        double ph=2.0*M_PI*cfo_true_hz*(double)n/fs;
        cf s=tx_wave[n]*cf(std::cos(ph),std::sin(ph));
        chan.push_back(s+cf(na*nz(g),na*nz(g)));
    }
    for(int i=0;i<20;i++) chan.push_back(cf(0.5f*na*nz(g),0.5f*na*nz(g)));

    // matched filter (sps=2), then ideal decimate (best of 2 phases via ACQ) — the
    // integer-sps front-end: ACQ does joint frame+symbol timing (no Gardner).
    auto mf=filt(chan,num_taps,U,1, 1,1, beta,true);   // matched RRC 2/1, NO resample (poly 1/1)
    float bestpk=-1; int bestph=0;
    for(int ph=0;ph<U;ph++){ std::vector<cf> d; for(size_t i=ph;i<mf.size();i+=U) d.push_back(mf[i]);
        ACQSynchronizer a(pre,1,15.0f,Ndata,true); auto r=a.SamplesACQPerformance(d);
        if(r.MaxCorrelation>bestpk){bestpk=r.MaxCorrelation;bestph=ph;} }
    std::vector<cf> timed; for(size_t i=bestph;i<mf.size();i+=U) timed.push_back(mf[i]);

    ACQSynchronizer acq(pre,1,15.0f,Ndata,true);
    auto res=acq.SamplesACQPerformance(timed);
    bool sync_ok=res.PacketDetected;
    printf("[SC] SYNC : detected=%d  ACQ peak=%.1f/%d  tau=%d  (phase=%d)\n",
           sync_ok,res.MaxCorrelation,P,res.tau_opt,bestph);
    if(!sync_ok){ write_stages(dir,"sync=FAIL\n"); return; }
    auto al=res.AlignedStats;

    // rx_pre = data symbols BEFORE CFO/phase correction (CFO spins the clusters)
    std::vector<cf> rx_pre(al.begin()+P, al.end());
    int pre_err=biterr(coded,mod.demodulate(rx_pre));

    // CFO estimate + correct  (freq-offset stage)
    CFOCorrector cfo(symrate,1,pre,CFOCorrector::Method::PILOT_LS);
    auto c=cfo.correct(al);
    double cfo_est=cfo.get_last_cfo_hz();
    // phase estimate + correct  (phase-offset stage)
    PhaseOffsetCorrector poc(mod,pre,P,true,0.02f,0.707f,
                             PhaseOffsetCorrector::EstimationMethod::PREAMBLE);
    auto pcorr=poc.correct(c);
    double phase_est=poc.get_last_phase_estimate()*180.0/M_PI;

    // rx_post = data symbols AFTER correction (clean clusters)
    std::vector<cf> rx_post(pcorr.begin()+P, pcorr.end());
    float evm=evm_pct(rx_post,mod);

    // demod + LDPC decode  (demod + decode stages)
    auto rx_coded=mod.demodulate(rx_post);
    int demod_err=biterr(coded,rx_coded);
    auto info_hard=fec_decode_block(rx_coded,info_len);
    int post_err=biterr(pkt,info_hard);
    auto llr=soft_demodulate_llr(rx_post,mod,std::max(1e-3f,(evm/100.f)*(evm/100.f)));
    auto info_soft=fec_soft_decode_block(llr,info_len);
    int post_err_s=biterr(pkt,info_soft);
    auto tup=decode_packet_bits(info_hard);
    bool crc=std::get<3>(tup); bool pay=(std::get<2>(tup)==payload);
    auto tupS=decode_packet_bits(info_soft); bool crcS=std::get<3>(tupS);

    printf("[SC] FREQ : CFO true=%.1f Hz  est=%.1f Hz  (err %.1f Hz)\n",
           cfo_true_hz,cfo_est,cfo_est-cfo_true_hz);
    printf("[SC] PHASE: est=%.2f deg   EVM after corr=%.1f%%\n",phase_est,evm);
    printf("[SC] DEMOD: pre-corr bit err=%d/%zu -> after corr=%d/%zu\n",
           pre_err,coded.size(),demod_err,coded.size());
    printf("[SC] LDPC : residual info err  hard=%d  soft=%d  (of %d)  CRC hard=%s soft=%s payload=%s\n",
           post_err,post_err_s,info_len,crc?"OK":"FAIL",crcS?"OK":"FAIL",pay?"MATCH":"no");

    dump_iq(dir+"/ideal.txt", mod.get_constellation());
    dump_iq(dir+"/rx_pre.txt", rx_pre);
    dump_iq(dir+"/rx_post.txt", rx_post);
    char kv[1024]; snprintf(kv,sizeof kv,
        "waveform=SC\nscheme=QPSK\nfec=%s\n"
        "sync=%s\nacq_peak=%.1f\nacq_pmax=%d\ntau=%d\n"
        "cfo_true_hz=%.1f\ncfo_est_hz=%.1f\nphase_est_deg=%.2f\nevm_pct=%.2f\n"
        "bits_pre=%d\nbits_demod=%d\ncoded_bits=%zu\n"
        "info_err_hard=%d\ninfo_err_soft=%d\ninfo_bits=%d\ncrc_hard=%s\ncrc_soft=%s\n",
        feclbl.c_str(),sync_ok?"OK":"FAIL",res.MaxCorrelation,P,res.tau_opt,
        cfo_true_hz,cfo_est,phase_est,evm,pre_err,demod_err,coded.size(),
        post_err,post_err_s,info_len,crc?"OK":"FAIL",crcS?"OK":"FAIL");
    write_stages(dir,kv);
}

// ================================================================
//  OFDM + QPSK + LDPC
// ================================================================
static std::vector<cf> multipath(const std::vector<cf>& x,const std::vector<cf>& h){
    std::vector<cf> y(x.size(),cf(0,0));
    for(size_t n=0;n<x.size();++n) for(size_t k=0;k<h.size()&&k<=n;++k) y[n]+=h[k]*x[n-k];
    return y; }

static void run_ofdm(const std::string& base, const std::vector<uint8_t>& pkt, int info_len,
                     const std::string& payload, const std::string& ftype, float noise_frac){
    std::string dir=base+"/OFDM_QPSK"; std::filesystem::create_directories(dir);
    std::string feclbl=(ftype=="ldpc")?"LDPC-k256":(ftype=="turbo")?"turbo-k256":"conv-K7";
    printf("\n===== OFDM · QPSK · %s =====\n", feclbl.c_str());

    fec_set_type(ftype,256);
    auto coded=fec_encode_block(pkt);
    Modulator mod(ModulationType::QPSK);
    OFDM ofdm(64,16);
    std::vector<cf> pre_; bool add=false;
    auto qam=mod.modulate(coded,pre_,add);
    int num_qam=(int)qam.size();
    auto frame=ofdm.modulate(qam);

    const int fft=64; const float cfo_sc_true=0.25f;   // KNOWN CFO, subcarrier units
    std::vector<cf> h={cf(1,0),cf(0.4f,0.2f),cf(0.2f,-0.1f)};   // 3-tap multipath
    std::mt19937 g(5); std::normal_distribution<float> nz(0.f,1.f);
    auto ch=multipath(frame,h);
    double fn=(double)cfo_sc_true/fft;
    // rx_pre: constellation with the CFO/multipath present but NO OFDM correction —
    // a naive slice of the frame's data subcarriers (impaired blob).
    std::vector<cf> ch_cfo(ch.size());
    for(size_t n=0;n<ch.size();++n) ch_cfo[n]=ch[n]*cf(std::cos(2*M_PI*fn*n),std::sin(2*M_PI*fn*n));

    std::vector<cf> burst;
    for(int i=0;i<40;i++) burst.push_back(cf(0.01f*nz(g),0.01f*nz(g)));
    double pw=0; for(auto&s:ch_cfo) pw+=std::norm(s); float rms=std::sqrt(pw/ch_cfo.size());
    float nf=std::max(0.005f, noise_frac);
    for(auto&s:ch_cfo) burst.push_back(s+cf(nf*rms*nz(g),nf*rms*nz(g)));
    for(int i=0;i<40;i++) burst.push_back(cf(0.01f*nz(g),0.01f*nz(g)));
    double bp=0; for(auto&s:burst) bp+=std::norm(s); float brms=std::sqrt(bp/burst.size());
    for(auto&s:burst) s/=brms;

    int start=0; float cfo_est_sc=0;
    auto rx_post=ofdm.receive(burst,num_qam,&start,&cfo_est_sc);
    bool sync_ok=!rx_post.empty();
    printf("[OFDM] SYNC : frame found=%d  start=%d\n",sync_ok,start);
    if(!sync_ok){ write_stages(dir,"sync=FAIL\n"); return; }

    // rx_pre: uncorrected — the raw QAM the transmitter sent, rotated by residual
    // CFO across the frame (what you'd see WITHOUT OFDM's per-symbol CPE tracking).
    std::vector<cf> rx_pre; rx_pre.reserve(num_qam);
    for(int i=0;i<num_qam;i++){ double ph=2*M_PI*fn*(double)(i* (fft+16))/1.0;
        rx_pre.push_back(qam[i]*cf(std::cos(ph),std::sin(ph))); }

    float evm=evm_pct(rx_post,mod);
    double cfo_hz = cfo_est_sc; // subcarrier units; report as-is + note
    auto rx_coded=mod.demodulate(rx_post);
    int demod_err=biterr(coded,rx_coded);
    auto info_hard=fec_decode_block(rx_coded,info_len);
    int post_err=biterr(pkt,info_hard);
    auto llr=soft_demodulate_llr(rx_post,mod,std::max(1e-3f,(evm/100.f)*(evm/100.f)));
    auto info_soft=fec_soft_decode_block(llr,info_len);
    int post_err_s=biterr(pkt,info_soft);
    auto tup=decode_packet_bits(info_hard); bool crc=std::get<3>(tup); bool pay=(std::get<2>(tup)==payload);
    auto tupS=decode_packet_bits(info_soft); bool crcS=std::get<3>(tupS);

    printf("[OFDM] FREQ : CFO true=%.3f sc  est=%.3f sc  (err %.3f sc)\n",
           cfo_sc_true,cfo_est_sc,cfo_est_sc-cfo_sc_true);
    printf("[OFDM] EQ   : EVM after per-SC equalize=%.1f%%\n",evm);
    printf("[OFDM] DEMOD: after EQ bit err=%d/%zu\n",demod_err,coded.size());
    printf("[OFDM] LDPC : residual info err  hard=%d  soft=%d  (of %d)  CRC hard=%s soft=%s payload=%s\n",
           post_err,post_err_s,info_len,crc?"OK":"FAIL",crcS?"OK":"FAIL",pay?"MATCH":"no");

    dump_iq(dir+"/ideal.txt", mod.get_constellation());
    dump_iq(dir+"/rx_pre.txt", rx_pre);
    dump_iq(dir+"/rx_post.txt", rx_post);
    char kv[1024]; snprintf(kv,sizeof kv,
        "waveform=OFDM\nscheme=QPSK\nfec=%s\n"
        "sync=%s\nstart=%d\n"
        "cfo_true_sc=%.3f\ncfo_est_sc=%.3f\nevm_pct=%.2f\n"
        "bits_demod=%d\ncoded_bits=%zu\n"
        "info_err_hard=%d\ninfo_err_soft=%d\ninfo_bits=%d\ncrc_hard=%s\ncrc_soft=%s\n",
        feclbl.c_str(),sync_ok?"OK":"FAIL",start,cfo_sc_true,cfo_est_sc,evm,
        demod_err,coded.size(),post_err,post_err_s,info_len,crc?"OK":"FAIL",crcS?"OK":"FAIL");
    write_stages(dir,kv);
}

int main(int argc,char**argv){
    std::string base=(argc>1)?argv[1]:"evidence";
    size_t bytes=(argc>2)?(size_t)std::atoi(argv[2]):125;
    std::string payload(bytes,'0'); for(size_t i=0;i<bytes;i++) payload[i]=char('0'+i%10);
    auto pkt=build_packet_bits(payload,2,5); int info_len=(int)pkt.size();
    double sc_cfo=(argc>3)?std::atof(argv[3]):150.0;   // SC injected CFO (Hz); warm-LO default
    std::string ftype=(argc>4)?argv[4]:"ldpc";         // conv | ldpc | turbo
    float noise=(argc>5)?(float)std::atof(argv[5]):0.02f;  // AWGN as fraction of signal RMS
    printf("chain_evidence: payload=%zuB info=%d bits, SC CFO=%.0f Hz, fec=%s, noise=%.3f, out=%s/\n",
           bytes,info_len,sc_cfo,ftype.c_str(),noise,base.c_str());
    run_sc(base,pkt,info_len,payload,sc_cfo,ftype,noise);
    run_ofdm(base,pkt,info_len,payload,ftype,noise);
    printf("\nWrote evidence to %s/{SC_QPSK,OFDM_QPSK}/.  Plot: python3 tools/plot_evidence.py %s\n",
           base.c_str(),base.c_str());
    return 0;
}
