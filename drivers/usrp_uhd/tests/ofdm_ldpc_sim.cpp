// End-to-end OFDM + QPSK + LDPC simulation, with timing.
//
//   build_packet_bits -> LDPC encode -> ofdm_modulation_thread
//     -> [multipath + CFO + AWGN + AGC] -> ofdm_demodulation_thread
//     -> LDPC decode -> decode_packet_bits (CRC).
//
// Runs N packets through the REAL OFDM pipeline threads and reports:
//   * per-stage wall time (modulate / channel / demodulate / LDPC decode)
//   * end-to-end throughput (info bits/s)
//   * pure LDPC-decoder throughput (the "how fast does LDPC act" number)
//   * CRC success rate through the channel
//
// Usage: ofdm_ldpc_sim [N_packets] [payload_bytes] [ldpc_k] [noise_frac]
#include "messages.hpp"
#include "fec.hpp"
#include "ofdm_pipeline.hpp"
#include <random>
#include <thread>
#include <chrono>
#include <cstdio>
#include <cstdlib>
using cf  = std::complex<float>;
using Blk = std::pair<size_t, std::vector<cf>>;
using clk = std::chrono::steady_clock;
static double ms(clk::duration d){ return std::chrono::duration<double,std::milli>(d).count(); }

static std::vector<cf> multipath(const std::vector<cf>& x, const std::vector<cf>& h){
    std::vector<cf> y(x.size(), cf(0,0));
    for (size_t n=0;n<x.size();++n) for (size_t k=0;k<h.size()&&k<=n;++k) y[n]+=h[k]*x[n-k];
    return y;
}

int main(int argc, char** argv){
    int    N          = (argc>1)? std::atoi(argv[1]) : 200;
    size_t bytes_len  = (argc>2)? (size_t)std::atoi(argv[2]) : 125;
    int    ldpc_k     = (argc>3)? std::atoi(argv[3]) : 256;
    float  noise_frac = (argc>4)? (float)std::atof(argv[4]) : 0.02f;

    const std::string scheme = "QPSK";
    const int fft=64, cp=16;
    std::vector<cf> h = { cf(1,0), cf(0.4f,0.2f), cf(0.2f,-0.1f) };  // 3-tap multipath
    const float cfo_sc = 0.25f;                                     // CFO, subcarrier units

    // Fixed payload; LDPC-encode once (TX side, deterministic code).
    std::string payload(bytes_len,'0');
    for(size_t i=0;i<bytes_len;i++) payload[i]=char('0'+i%10);
    auto tx_bits  = build_packet_bits(payload, 2, 5);
    int  info_len = (int)tx_bits.size();

    fec_set_type(FecType::LDPC, ldpc_k);
    auto coded    = fec_encode_block(tx_bits);
    Modulator probe(string_to_mod_type(scheme));
    int  bps      = probe.get_bits_per_symbol();
    int  data_syms= ((int)coded.size()+bps-1)/bps;

    printf("=== OFDM+QPSK+LDPC end-to-end  (N=%d packets) ===\n", N);
    printf("payload=%zuB  info=%d bits  LDPC k=%d -> coded=%zu bits  data_syms=%d  "
           "fft=%d cp=%d  noise=%.3f cfo=%.2fsc\n\n",
           bytes_len, info_len, ldpc_k, coded.size(), data_syms, fft, cp,
           noise_frac, cfo_sc);

    std::mt19937 g(5); std::normal_distribution<float> nz(0.f,1.f);
    clk::duration t_mod{}, t_chan{}, t_demod{}, t_ldpc{};
    long ldpc_info_bits=0;
    int crc_ok=0, no_out=0;

    auto t_all0 = clk::now();
    for(int p=0; p<N; ++p){
        std::string sch = scheme;
        std::atomic<bool> stop{false};
        MutexFIFO<std::vector<uint8_t>> bits_fifo;
        MutexFIFO<Blk> frame_fifo, burst_fifo;
        MutexFIFO<std::pair<size_t,std::vector<uint8_t>>> out_fifo;

        // ── TX: OFDM modulate the LDPC-coded bits ──
        auto a=clk::now();
        bits_fifo.push(coded);
        std::thread tx(ofdm_modulation_thread, std::ref(bits_fifo), std::ref(frame_fifo),
                       std::ref(sch), fft, cp, 0.5f, std::ref(stop));
        Blk frame; for(int i=0;i<500 && !frame_fifo.pop(frame); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        stop.store(true); tx.join(); stop.store(false);
        t_mod += clk::now()-a;
        if(frame.second.empty()){ ++no_out; continue; }

        // ── Channel: multipath + CFO + AWGN + AGC (RMS->1) ──
        a=clk::now();
        auto ch = multipath(frame.second, h);
        double fn=(double)cfo_sc/fft;
        for(size_t n=0;n<ch.size();++n) ch[n]*=cf(std::cos(2*M_PI*fn*n),std::sin(2*M_PI*fn*n));
        std::vector<cf> burst;
        for(int i=0;i<40;i++) burst.push_back(cf(0.01f*nz(g),0.01f*nz(g)));
        double pw=0; for(auto&s:ch) pw+=std::norm(s); float rms=std::sqrt(pw/ch.size());
        for(auto&s:ch) burst.push_back(s + cf(noise_frac*rms*nz(g), noise_frac*rms*nz(g)));
        for(int i=0;i<40;i++) burst.push_back(cf(0.01f*nz(g),0.01f*nz(g)));
        double bp=0; for(auto&s:burst) bp+=std::norm(s); float brms=std::sqrt(bp/burst.size());
        for(auto&s:burst) s/=brms;
        t_chan += clk::now()-a;

        // ── RX: OFDM demodulate -> coded bits ──
        a=clk::now();
        burst_fifo.push({(size_t)p,burst});
        std::thread rx(ofdm_demodulation_thread, std::ref(burst_fifo), std::ref(out_fifo),
                       std::ref(sch), fft, cp, data_syms, std::ref(stop));
        std::pair<size_t,std::vector<uint8_t>> res; bool got=false;
        for(int i=0;i<1000;i++){ if(out_fifo.pop(res)){got=true;break;}
            std::this_thread::sleep_for(std::chrono::milliseconds(1)); }
        stop.store(true); rx.join();
        t_demod += clk::now()-a;
        if(!got){ ++no_out; continue; }

        // ── LDPC decode (timed on its own) ──
        std::vector<uint8_t> raw = res.second;
        int clen = fec_encoded_len(info_len);
        if((int)raw.size()>=clen) raw.resize(clen);
        a=clk::now();
        auto dec = fec_decode_block(raw, info_len);
        t_ldpc += clk::now()-a;
        ldpc_info_bits += info_len;

        auto [idx,tot,pl,crc] = decode_packet_bits(dec);
        if(crc && pl==payload) ++crc_ok;
    }
    auto t_all = clk::now()-t_all0;

    // ── Report ──
    double sec = ms(t_all)/1000.0;
    long tot_info = (long)info_len*N;
    printf("results over %d packets:\n", N);
    printf("  CRC OK        : %d/%d (%.1f%%)   no-output: %d\n",
           crc_ok, N, 100.0*crc_ok/N, no_out);
    printf("  wall total    : %8.1f ms  (%.3f ms/packet)\n", ms(t_all), ms(t_all)/N);
    printf("    modulate    : %8.1f ms  (%.3f ms/pkt)\n", ms(t_mod),   ms(t_mod)/N);
    printf("    channel     : %8.1f ms  (%.3f ms/pkt)\n", ms(t_chan),  ms(t_chan)/N);
    printf("    demodulate  : %8.1f ms  (%.3f ms/pkt)\n", ms(t_demod), ms(t_demod)/N);
    printf("    LDPC decode : %8.1f ms  (%.3f ms/pkt)\n", ms(t_ldpc),  ms(t_ldpc)/N);
    printf("  end-to-end throughput : %.2f Mbit/s (info)\n", tot_info/1e6/sec);
    if(ms(t_ldpc)>0)
        printf("  LDPC decoder alone    : %.2f Mbit/s (info)   [%.1f us/block of k=%d]\n",
               ldpc_info_bits/1e3/ms(t_ldpc),
               ms(t_ldpc)*1000.0/((double)ldpc_info_bits/ldpc_k), ldpc_k);
    return 0;
}
