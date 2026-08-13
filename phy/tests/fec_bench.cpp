// FEC benchmark: OFDM+QPSK soft-vs-hard coding gain, LDPC block-size sweep, and
// a decode-time comparison of LDPC (hard/soft) vs conv+Viterbi (hard/soft).
//
//   Section A : OFDM+QPSK end-to-end. From the SAME equalized symbols, decode 4
//               ways (LDPC hard/soft, conv hard/soft) → CRC-OK rate vs channel noise.
//   Section B : LDPC info-block k sweep → coding gain AND decode speed per k.
//   Section C : Pure decoder speed (no channel in the timed region), fixed Eb/N0,
//               many repeats → us/packet and Mbit/s(info) for all four decoders.
//
// LLR convention (soft_demodulate_llr): positive = bit 0.
// Usage: fec_bench [N_packets] [payload_bytes]
#include "messages.hpp"
#include "fec.hpp"
#include "ofdm.hpp"
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include <random>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <fstream>
using cf  = std::complex<float>;
using clk = std::chrono::steady_clock;
static double ms(clk::duration d){ return std::chrono::duration<double,std::milli>(d).count(); }

static std::vector<cf> multipath(const std::vector<cf>& x, const std::vector<cf>& h){
    std::vector<cf> y(x.size(), cf(0,0));
    for (size_t n=0;n<x.size();++n) for (size_t k=0;k<h.size()&&k<=n;++k) y[n]+=h[k]*x[n-k];
    return y;
}

// Decision-directed noise-variance estimate (per I/Q), so LLR magnitudes are
// well-scaled for both min-sum and soft Viterbi.
// Modulate bits → QAM symbols with NO preamble (payload symbols only).
static std::vector<cf> qam_only(Modulator& mod, const std::vector<uint8_t>& bits){
    std::vector<cf> pre; bool add=false;
    return mod.modulate(bits, pre, add);
}

static float est_nvar(const std::vector<cf>& qam, Modulator& mod){
    auto bits = mod.demodulate(qam);
    auto ref  = qam_only(mod, bits);
    double e=0; size_t n=std::min(qam.size(),ref.size());
    for(size_t i=0;i<n;i++) e += std::norm(qam[i]-ref[i]);
    return std::max(1e-3f, (float)(e/std::max<size_t>(1,n))/2.0f);
}

// One OFDM+QPSK round trip: coded bits -> QAM -> OFDM frame -> channel -> receive.
// Returns equalized QAM symbols (empty if sync failed).
static std::vector<cf> ofdm_channel(const std::vector<uint8_t>& coded, Modulator& mod,
                                    OFDM& ofdm, const std::vector<cf>& h,
                                    float noise_frac, float cfo_sc, std::mt19937& g){
    int fft = ofdm.fft_size();
    auto qam   = qam_only(mod, coded);
    int num_qam = (int)qam.size();
    auto frame = ofdm.modulate(qam);

    std::normal_distribution<float> nz(0.f,1.f);
    auto ch = multipath(frame, h);
    double fn=(double)cfo_sc/fft;
    for(size_t n=0;n<ch.size();++n) ch[n]*=cf(std::cos(2*M_PI*fn*n),std::sin(2*M_PI*fn*n));
    std::vector<cf> burst;
    for(int i=0;i<40;i++) burst.push_back(cf(0.01f*nz(g),0.01f*nz(g)));
    double pw=0; for(auto&s:ch) pw+=std::norm(s); float rms=std::sqrt(pw/ch.size());
    for(auto&s:ch) burst.push_back(s + cf(noise_frac*rms*nz(g), noise_frac*rms*nz(g)));
    for(int i=0;i<40;i++) burst.push_back(cf(0.01f*nz(g),0.01f*nz(g)));
    double bp=0; for(auto&s:burst) bp+=std::norm(s); float brms=std::sqrt(bp/burst.size());
    for(auto&s:burst) s/=brms;
    return ofdm.receive(burst, num_qam);
}

int main(int argc, char** argv){
    int    N     = (argc>1)? std::atoi(argv[1]) : 200;
    size_t bytes = (argc>2)? (size_t)std::atoi(argv[2]) : 125;

    const std::string scheme="QPSK";
    Modulator mod(string_to_mod_type(scheme));
    OFDM ofdm(64,16);
    std::vector<cf> h = { cf(1,0), cf(0.4f,0.2f), cf(0.2f,-0.1f) };
    const float cfo_sc = 0.25f;

    std::string payload(bytes,'0'); for(size_t i=0;i<bytes;i++) payload[i]=char('0'+i%10);
    auto pkt = build_packet_bits(payload, 2, 5);
    int info_len = (int)pkt.size();

    printf("############  FEC benchmark  (N=%d packets, payload=%zuB, info=%d bits, QPSK/OFDM) ############\n\n",
           N, bytes, info_len);

    // ===================================================================
    //  Section A — OFDM+QPSK coding gain: soft vs hard, LDPC vs conv
    // ===================================================================
    printf("=== A. OFDM+QPSK end-to-end CRC-OK rate (%d pkts/pt) — multipath+CFO+AWGN ===\n", N);
    printf("%-8s | LDPC-hard  LDPC-soft | conv-hard  conv-soft | uncoded\n","noise");
    fec_set_type(FecType::CONV);  auto conv_coded = fec_encode_block(pkt);
    fec_set_type(FecType::LDPC, 256); auto ldpc_coded = fec_encode_block(pkt);

    for(float nf : {0.06f, 0.10f, 0.14f, 0.18f}){
        std::mt19937 g(11);
        int lh=0,ls=0,ch_=0,cs=0,un=0;
        for(int p=0;p<N;++p){
            // LDPC path (its own coded frame / num_qam)
            fec_set_type(FecType::LDPC, 256);
            auto q = ofdm_channel(ldpc_coded, mod, ofdm, h, nf, cfo_sc, g);
            if(!q.empty()){
                float nv = est_nvar(q, mod);
                auto hb  = mod.demodulate(q);
                auto llr = soft_demodulate_llr(q, mod, nv);
                { auto d=fec_decode_block(hb, info_len);
                  auto t=decode_packet_bits(d); if(std::get<3>(t)&&std::get<2>(t)==payload) ++lh; }
                { auto d=fec_soft_decode_block(llr, info_len);
                  auto t=decode_packet_bits(d); if(std::get<3>(t)&&std::get<2>(t)==payload) ++ls; }
                // uncoded reference: slice the payload region of the hard bits
                { auto t=decode_packet_bits(hb); if(std::get<3>(t)&&std::get<2>(t)==payload) ++un; }
            }
            // conv path
            fec_set_type(FecType::CONV);
            auto q2 = ofdm_channel(conv_coded, mod, ofdm, h, nf, cfo_sc, g);
            if(!q2.empty()){
                float nv = est_nvar(q2, mod);
                auto hb  = mod.demodulate(q2);
                auto llr = soft_demodulate_llr(q2, mod, nv);
                { auto d=fec_decode_block(hb, info_len);
                  auto t=decode_packet_bits(d); if(std::get<3>(t)&&std::get<2>(t)==payload) ++ch_; }
                { auto d=fec_soft_decode_block(llr, info_len);
                  auto t=decode_packet_bits(d); if(std::get<3>(t)&&std::get<2>(t)==payload) ++cs; }
            }
        }
        printf("%-8.2f | %6.1f%%    %6.1f%%  | %6.1f%%    %6.1f%%  | %6.1f%%\n", nf,
               100.0*lh/N,100.0*ls/N,100.0*ch_/N,100.0*cs/N,100.0*un/N);
    }

    // ===================================================================
    //  Section B — LDPC block-size k sweep (soft path): gain + speed
    // ===================================================================
    printf("\n=== B. LDPC info-block k sweep (soft decode, noise=0.14, %d pkts) ===\n", N);
    printf("%-6s | coded bits | CRC-OK | decode us/pkt | decoder Mbit/s(info)\n","k");
    for(int k : {128,256,512,1024}){
        fec_set_type(FecType::LDPC, k);
        auto coded = fec_encode_block(pkt);
        std::mt19937 g(11);
        clk::duration td{}; int ok=0, got=0;
        for(int p=0;p<N;++p){
            auto q = ofdm_channel(coded, mod, ofdm, h, 0.14f, cfo_sc, g);
            if(q.empty()) continue; ++got;
            float nv=est_nvar(q,mod);
            auto llr=soft_demodulate_llr(q,mod,nv);
            auto a=clk::now();
            auto d=fec_soft_decode_block(llr, info_len);
            td += clk::now()-a;
            auto t=decode_packet_bits(d); if(std::get<3>(t)&&std::get<2>(t)==payload) ++ok;
        }
        double us = got? ms(td)*1000.0/got : 0;
        double mbps = ms(td)>0? (double)info_len*got/1e3/ms(td) : 0;
        printf("%-6d | %10zu | %5.1f%% | %11.1f   | %8.2f\n",
               k, coded.size(), 100.0*ok/std::max(1,got), us, mbps);
    }

    // ===================================================================
    //  Section C — Pure decode SPEED vs Eb/N0 (no channel in the timed region)
    // ===================================================================
    // For each decoder, add fresh BPSK-AWGN to the codeword every rep, then time
    // ONLY the decode call. LDPC=1 block of k=256 info; conv=1 packet of info_len.
    // Throughput normalized to Mbit/s(info). LDPC also reports avg BP iterations —
    // BP early-terminates, so its cost is SNR-dependent, unlike the fixed Viterbi trellis.
    printf("\n=== C. Decoder speed vs Eb/N0 — decode-call timing only (%d reps/pt) ===\n", 2000);
    const int REPS=2000;

    LdpcCode lc(256,3);
    TurboCode tcode(256,6);
    std::vector<uint8_t> linfo(lc.k());
    { std::mt19937 gi(9); std::bernoulli_distribution c(0.5); for(auto&b:linfo)b=c(gi); }
    auto lcw = lc.encode(linfo);
    auto tcw = tcode.encode(linfo);             // turbo block (same 256-bit info)

    fec_set_type(FecType::CONV);
    auto ccw = fec_encode_block(pkt);           // conv codeword for the packet

    printf("%-8s | %-22s | %-22s | %-22s | %-22s\n",
           "Eb/N0", "LDPC hard", "LDPC soft", "conv hard (Vit)", "conv soft (Vit)");
    printf("  (dB)   |  us/blk  it  ok%%     |  us/blk  it  ok%%     |  us/pkt      ok%%     |  us/pkt      ok%%\n");

    // CSV for plot_fec_compare.py. Decode time normalized to us per 1000 info bits
    // so LDPC (256-bit blocks) and conv (info_len-bit packets) are comparable.
    std::string csv = (argc>3)? argv[3] : "fec_compare.csv";
    std::ofstream cf(csv);
    cf << "ebn0_db,ldpc_hard_crc,ldpc_hard_us,ldpc_hard_it,"
          "ldpc_soft_crc,ldpc_soft_us,ldpc_soft_it,"
          "conv_hard_crc,conv_hard_us,conv_soft_crc,conv_soft_us,"
          "turbo_hard_crc,turbo_hard_us,turbo_hard_it,"
          "turbo_soft_crc,turbo_soft_us,turbo_soft_it\n";
    auto us_per_kbit=[&](double total_ms,int reps,int info_bits){
        return total_ms*1000.0/reps / info_bits * 1000.0; };  // us per 1000 info bits

    for(double ebn0_db : {0.0,1.0,2.0,3.0,4.0,5.0,6.0,7.0}){
        double rate=0.5, ebn0=std::pow(10.0,ebn0_db/10.0);
        double sigma=std::sqrt(1.0/(2.0*rate*ebn0));
        std::mt19937 g(3); std::normal_distribution<float> noise(0.f,(float)sigma);
        auto awgn=[&](const std::vector<uint8_t>& cw, std::vector<uint8_t>& hard, std::vector<float>& llr){
            hard.resize(cw.size()); llr.resize(cw.size());
            for(size_t i=0;i<cw.size();++i){ float s=cw[i]?-1.f:1.f, y=s+noise(g);
                hard[i]=(y<0.f)?1:0; llr[i]=2.f*y/(float)(sigma*sigma); }
        };
        std::vector<uint8_t> hard; std::vector<float> llr;

        // LDPC hard (block): map hard bits -> ±8 LLR, decode, count iters.
        clk::duration lh_t{}; long lh_it=0; int lh_ok=0;
        for(int r=0;r<REPS;++r){ awgn(lcw,hard,llr);
            std::vector<float> hl(hard.size()); for(size_t i=0;i<hard.size();++i) hl[i]=hard[i]?-8.f:8.f;
            int it=0; auto a=clk::now(); auto d=lc.decode(hl,&it); lh_t+=clk::now()-a;
            lh_it+=it; if(d==linfo)++lh_ok; }
        // LDPC soft (block)
        clk::duration ls_t{}; long ls_it=0; int ls_ok=0;
        for(int r=0;r<REPS;++r){ awgn(lcw,hard,llr);
            int it=0; auto a=clk::now(); auto d=lc.decode(llr,&it); ls_t+=clk::now()-a;
            ls_it+=it; if(d==linfo)++ls_ok; }
        // conv hard (packet)
        fec_set_type(FecType::CONV);
        clk::duration chd_t{}; int ch_ok=0;
        for(int r=0;r<REPS;++r){ awgn(ccw,hard,llr);
            auto a=clk::now(); auto d=fec_decode_block(hard,info_len); chd_t+=clk::now()-a;
            auto t=decode_packet_bits(d); if(std::get<3>(t))++ch_ok; }
        // conv soft (packet)
        clk::duration cs_t{}; int cs_ok=0;
        for(int r=0;r<REPS;++r){ awgn(ccw,hard,llr);
            auto a=clk::now(); auto d=fec_soft_decode_block(llr,info_len); cs_t+=clk::now()-a;
            auto t=decode_packet_bits(d); if(std::get<3>(t))++cs_ok; }
        // turbo hard (block): map hard bits -> ±8 LLR, iterative BCJR, count iters.
        clk::duration th_t{}; long th_it=0; int th_ok=0;
        for(int r=0;r<REPS;++r){ awgn(tcw,hard,llr);
            std::vector<float> hl(hard.size()); for(size_t i=0;i<hard.size();++i) hl[i]=hard[i]?-8.f:8.f;
            int it=0; auto a=clk::now(); auto d=tcode.decode(hl,&it); th_t+=clk::now()-a;
            th_it+=it; if(d==linfo)++th_ok; }
        // turbo soft (block)
        clk::duration ts_t{}; long ts_it=0; int ts_ok=0;
        for(int r=0;r<REPS;++r){ awgn(tcw,hard,llr);
            int it=0; auto a=clk::now(); auto d=tcode.decode(llr,&it); ts_t+=clk::now()-a;
            ts_it+=it; if(d==linfo)++ts_ok; }

        printf("%-8.1f | %6.1f %4.1f %5.1f%%    | %6.1f %4.1f %5.1f%%    | %6.1f      %5.1f%%    | %6.1f      %5.1f%%   | turbo-soft %5.1f%% %4.1fit\n",
            ebn0_db,
            ms(lh_t)*1000.0/REPS,(double)lh_it/REPS,100.0*lh_ok/REPS,
            ms(ls_t)*1000.0/REPS,(double)ls_it/REPS,100.0*ls_ok/REPS,
            ms(chd_t)*1000.0/REPS,100.0*ch_ok/REPS,
            ms(cs_t)*1000.0/REPS,100.0*cs_ok/REPS,
            100.0*ts_ok/REPS,(double)ts_it/REPS);
        cf << ebn0_db << ","
           << 100.0*lh_ok/REPS << "," << us_per_kbit(ms(lh_t),REPS,lc.k()) << "," << (double)lh_it/REPS << ","
           << 100.0*ls_ok/REPS << "," << us_per_kbit(ms(ls_t),REPS,lc.k()) << "," << (double)ls_it/REPS << ","
           << 100.0*ch_ok/REPS << "," << us_per_kbit(ms(chd_t),REPS,info_len) << ","
           << 100.0*cs_ok/REPS << "," << us_per_kbit(ms(cs_t),REPS,info_len) << ","
           << 100.0*th_ok/REPS << "," << us_per_kbit(ms(th_t),REPS,tcode.k()) << "," << (double)th_it/REPS << ","
           << 100.0*ts_ok/REPS << "," << us_per_kbit(ms(ts_t),REPS,tcode.k()) << "," << (double)ts_it/REPS << "\n";
    }
    cf.close();
    printf("[csv] wrote %s\n", csv.c_str());
    printf("\nNotes: LDPC us/blk is per 256-info-bit block; conv us/pkt is per %d-info-bit packet\n", info_len);
    printf("       (conv packet = %d-bit trellis; LDPC packet = 5 such blocks).\n", info_len);
    printf("       'it' = avg BP iterations (cap 50). Viterbi has FIXED cost; LDPC cost falls as SNR rises.\n");
    return 0;
}
