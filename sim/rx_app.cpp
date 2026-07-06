// ============================================================
//  rx_app — RECEIVE terminal (no radio).
//  Listens for the TX stream, applies a simple channel (AWGN +
//  a carrier frequency/phase offset), then runs the real receive
//  chain and logs every stage:
//     [CHANNEL]  injected noise / CFO / phase
//     [SYNC]     which correlation sample is the peak
//     [CFO]      estimated frequency offset  (vs injected)
//     [PHASE]    estimated carrier phase      (vs injected)
//     [DEMOD]    recovered symbol/bit pattern
//     [DECODE]   decoded chunk text
//  and finally reassembles + prints the whole decoded message.
//
//  Usage:
//    ./rx_app --scheme QPSK [--m 5] [--port 5555]
//             [--snr-db 30] [--noise-sigma S]
//             [--cfo-hz 3820] [--cfo-rad R] [--phase-deg 35]
//             [--symbol-rate 0.8e6]
// ============================================================
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "messages.hpp"
#include "synchronization.hpp"
#include "frequency_offset.hpp"
#include "phase_offset.hpp"
#include "net.hpp"
#include <iostream>
#include <iomanip>
#include <string>
#include <vector>
#include <complex>
#include <cmath>
#include <random>

using cf = std::complex<float>;

static std::string arg_str(int c,char**v,const std::string&k,const std::string&d){
    for(int i=1;i<c-1;i++) if(k==v[i]) return v[i+1]; return d; }
static int   arg_int(int c,char**v,const std::string&k,int d){
    std::string s=arg_str(c,v,k,""); return s.empty()?d:std::stoi(s); }
static double arg_dbl(int c,char**v,const std::string&k,double d){
    std::string s=arg_str(c,v,k,""); return s.empty()?d:std::stod(s); }
static bool  has_arg(int c,char**v,const std::string&k){
    for(int i=1;i<c;i++) if(k==v[i]) return true; return false; }

static int bit_errors(const std::vector<uint8_t>&a,const std::vector<uint8_t>&b){
    size_t n=std::min(a.size(),b.size()); int e=0; for(size_t i=0;i<n;i++) e+=(a[i]!=b[i]); return e; }
static std::string bits_str(const std::vector<uint8_t>&b,int n){
    std::string s; for(int i=0;i<n && i<(int)b.size();i++) s+=char('0'+(b[i]&1)); return s; }

int main(int argc, char** argv){
    std::string scheme = arg_str(argc,argv,"--scheme","QPSK");
    int    m           = arg_int(argc,argv,"--m",5);
    int    port        = arg_int(argc,argv,"--port",5555);
    double symrate     = arg_dbl(argc,argv,"--symbol-rate",0.8e6);
    double phase_deg   = arg_dbl(argc,argv,"--phase-deg",35.0);

    // channel noise: --noise-sigma overrides --snr-db
    double snr_db   = arg_dbl(argc,argv,"--snr-db",30.0);
    double sigma    = has_arg(argc,argv,"--noise-sigma")
                        ? arg_dbl(argc,argv,"--noise-sigma",0.0)
                        : std::sqrt(1.0/(2.0*std::pow(10.0, snr_db/10.0)));
    // channel CFO: --cfo-rad (rad/symbol) overrides --cfo-hz
    double cfo_hz   = arg_dbl(argc,argv,"--cfo-hz",3820.0);
    double cfo_rad  = has_arg(argc,argv,"--cfo-rad")
                        ? arg_dbl(argc,argv,"--cfo-rad",0.0)
                        : 2.0*M_PI*cfo_hz/symrate;
    if (has_arg(argc,argv,"--cfo-rad")) cfo_hz = cfo_rad*symrate/(2.0*M_PI);
    double phase_rad = phase_deg*M_PI/180.0;

    ModulationType mt;
    try { mt = string_to_mod_type(scheme); }
    catch(const std::exception& e){ std::cerr<<"[RX] "<<e.what()<<"\n"; return 1; }
    bool diff = (mt==ModulationType::DBPSK||mt==ModulationType::DQPSK||mt==ModulationType::DPSK8);

    Modulator mod(mt);
    auto preamble = generate_msequence_preamble(m);
    int  P   = (int)preamble.size();
    int  bps = mod.get_bits_per_symbol();
    const int GUARD = 10;

    std::cout << "\n================ RX ================\n";
    std::cout << "[RX] Scheme        : " << mod.get_modulation_name()
              << "  (" << bps << " bits/symbol, C=" << mod.get_constellation_size()
              << (diff? ", differential":"") << ")\n";
    std::cout << "[RX] Preamble      : m-sequence m=" << m << " (" << P << " symbols)\n";
    std::cout << "[RX] Channel       : AWGN sigma=" << std::fixed << std::setprecision(4) << sigma
              << " (~" << std::setprecision(1) << snr_db << " dB), CFO=" << std::setprecision(1)
              << cfo_hz << " Hz (" << std::setprecision(4) << cfo_rad << " rad/sym), phase="
              << std::setprecision(1) << phase_deg << " deg\n";
    std::cout << "[RX] Listening on  : 0.0.0.0:" << port << " (waiting for TX)...\n";

    int fd;
    try { fd = net::accept_one(port); }
    catch(const std::exception& e){ std::cerr<<"[RX] "<<e.what()<<"\n"; return 1; }
    std::cout << "[RX] TX connected. Receiving...\n";

    std::mt19937 rng(12345);
    std::normal_distribution<float> nz(0.f,(float)sigma);
    std::uniform_real_distribution<float> lo(-(float)sigma,(float)sigma);

    std::vector<std::string> parts;   // reassembled payload by chunk index
    int total_chunks = 0;
    long tot_bits=0, tot_biterr=0;

    while (true) {
        int tag;
        try { tag = net::recv_i32(fd); } catch(...) { break; }
        if (tag == 0) break;                     // end-of-stream

        int idx        = net::recv_i32(fd);
        int tot         = net::recv_i32(fd);
        std::string sch = net::recv_str(fd);
        int tx_m        = net::recv_i32(fd);
        std::string truth = net::recv_str(fd);
        auto pkt        = net::recv_symbols(fd); // clean [guard|preamble|data]

        total_chunks = tot;
        if ((int)parts.size() < tot) parts.resize(tot);
        if (sch != scheme)
            std::cout << "\n[RX][WARN] scheme mismatch: TX=" << sch << " RX=" << scheme
                      << " (demod will fail)\n";
        if (tx_m != m)
            std::cout << "[RX][WARN] preamble m mismatch: TX=" << tx_m << " RX=" << m << "\n";

        int num_data = (int)pkt.size() - GUARD - P;
        std::cout << "\n========== PACKET (chunk " << idx << "/" << (tot-1) << ") ==========\n";
        std::cout << "[RX] Received " << pkt.size() << " clean symbols (guard " << GUARD
                  << " + preamble " << P << " + data " << num_data << ")\n";

        // ---- CHANNEL: pad + CFO ramp + static phase + AWGN ----
        int pad_pre = 13, pad_post = 8;
        std::vector<cf> y; y.reserve(pad_pre + pkt.size() + pad_post);
        for (int i=0;i<pad_pre;i++)  y.push_back(cf(lo(rng),lo(rng)));        // noise before burst
        for (size_t k=0;k<pkt.size();++k){
            cf s = pkt[k] * std::polar(1.0f,(float)(phase_rad + cfo_rad*(double)k));
            s += cf(nz(rng),nz(rng));
            y.push_back(s);
        }
        for (int i=0;i<pad_post;i++)  y.push_back(cf(lo(rng),lo(rng)));
        std::cout << "[CHANNEL] burst placed at sample " << pad_pre
                  << " (after " << pad_pre << " noise samples); applied CFO="
                  << std::setprecision(1) << cfo_hz << " Hz, phase="
                  << phase_deg << " deg, AWGN sigma=" << std::setprecision(4) << sigma << "\n";

        // ---- TIME SYNC (ACQ): find the preamble ----
        ACQSynchronizer acq(preamble, /*sps*/1, /*threshold*/12.0f, num_data, /*use_last*/true);
        auto res = acq.SamplesACQPerformance(y);
        if (!res.PacketDetected){
            std::cout << "[SYNC] *** packet NOT detected (peak "
                      << res.MaxCorrelation << ") *** skipping chunk\n";
            continue;
        }
        int expected_peak = pad_pre + GUARD;   // preamble starts after pad + guard
        std::cout << "[SYNC] correlation PEAK at sample index " << res.tau_opt
                  << " (expected " << expected_peak << " = " << pad_pre << " pad + "
                  << GUARD << " guard)\n";
        std::cout << "[SYNC]   peak correlation = " << std::setprecision(2) << res.MaxCorrelation
                  << " / " << P << " (m-seq length)\n";
        auto aligned = res.AlignedStats;   // [preamble | data]
        std::cout << "[SYNC]   aligned block = " << aligned.size() << " symbols [preamble "
                  << P << " | data " << num_data << "]\n";

        // ---- CFO (pilot-aided, data-aided on the aligned burst) ----
        CFOCorrector cfo(symrate, /*sps*/1, preamble, CFOCorrector::Method::PILOT_AIDED);
        auto cfo_out = cfo.correct(aligned);
        std::cout << "[CFO]  estimated = " << std::setprecision(1) << cfo.get_last_cfo_hz()
                  << " Hz  (" << std::setprecision(4) << (cfo.get_last_cfo_hz()*2.0*M_PI/symrate)
                  << " rad/sym)   |  injected " << std::setprecision(1) << cfo_hz << " Hz\n";

        // ---- PHASE (absolute schemes only; differential is phase-robust) ----
        std::vector<cf> data;
        if (!diff){
            PhaseOffsetCorrector poc(mod, preamble, P, /*tracker*/true, 0.02f, 0.707f,
                                     PhaseOffsetCorrector::EstimationMethod::PREAMBLE);
            auto ph = poc.correct(cfo_out);
            double eff = phase_deg + (cfo_rad*(double)GUARD)*180.0/M_PI; // ramp over guard
            std::cout << "[PHASE] estimated carrier phase = " << std::setprecision(2)
                      << poc.get_last_phase_estimate()*180.0/M_PI << " deg  |  injected "
                      << phase_deg << " deg + guard-ramp = " << eff << " deg\n";
            data.assign(ph.begin()+P, ph.end());              // strip whole preamble
        } else {
            std::cout << "[PHASE] skipped (differential demod is phase-robust; keeps last "
                         "preamble symbol as reference)\n";
            data.assign(cfo_out.begin()+(P-1), cfo_out.end()); // keep reference symbol
        }

        // ---- DEMOD ----
        auto rx_bits = mod.demodulate(data);
        const auto& C = mod.get_constellation();
        std::cout << "[DEMOD] first data symbols -> constellation indices: ";
        for (int i=0;i<12 && i<(int)data.size();++i){
            int best=0; float md=std::norm(data[i]-C[0]);
            for (int j=1;j<(int)C.size();++j){ float d=std::norm(data[i]-C[j]); if(d<md){md=d;best=j;} }
            std::cout << best << " ";
        }
        std::cout << "\n[DEMOD] recovered bit pattern (first 48): " << bits_str(rx_bits,48) << "\n";

        // ---- BER vs demo ground-truth ----
        auto truth_bits = build_packet_bits(truth,(uint8_t)idx,(uint8_t)tot);
        std::cout << "[DEMOD] expected  bit pattern (first 48): " << bits_str(truth_bits,48) << "\n";
        int be = bit_errors(truth_bits, rx_bits);
        tot_bits += (long)truth_bits.size(); tot_biterr += be;
        std::cout << "[DEMOD] bit errors this chunk = " << be << " / " << truth_bits.size()
                  << "  (BER " << std::setprecision(4)
                  << (double)be/std::max<size_t>(1,truth_bits.size()) << ")\n";

        // ---- DECODE (header + payload + CRC) ----
        auto [ridx, rtot, payload, rcrc] = decode_packet_bits(rx_bits);
        std::cout << "[DECODE] header: chunk_index=" << (int)ridx << " total_chunks=" << (int)rtot
                  << "  CRC=" << (rcrc ? "OK" : "FAIL") << "\n";
        std::cout << "[DECODE] payload text: \"" << payload << "\"\n";
        if (idx>=0 && idx<(int)parts.size()) parts[idx] = payload;
    }

    // ---- reassemble + report ----
    std::string full; for (auto& p : parts) full += p;
    std::cout << "\n================ RESULT ================\n";
    std::cout << "[RX] Reassembled " << total_chunks << " chunk(s).\n";
    std::cout << "[RX] Total bit errors: " << tot_biterr << " / " << tot_bits
              << "  (overall BER " << std::setprecision(6)
              << (tot_bits? (double)tot_biterr/tot_bits : 0.0) << ")\n";
    std::cout << "---------------- DECODED MESSAGE ----------------\n";
    std::cout << full << "\n";
    std::cout << "-------------------------------------------------\n";
    ::close(fd);
    return 0;
}
