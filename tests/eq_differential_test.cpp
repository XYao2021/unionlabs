// Differential + equalizer path test: drive the REAL channel_eq_thread through
// FIFOs with a differential burst over a multipath channel, then differential-
// demodulate its output. Verifies the eq path keeps the last-preamble-symbol
// reference so the decode returns exactly N symbols (no off-by-one) and the bits
// round-trip. A coherent scheme is included as a control.
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "messages.hpp"
#include "channel_estimation.hpp"
#include "FIFO.hpp"
#include <random>
#include <thread>
#include <cstdio>
#include <string>
using cf = std::complex<float>;
using Blk = std::pair<size_t, std::vector<cf>>;

static int bd(const std::vector<uint8_t>& a, const std::vector<uint8_t>& b){
    int n = std::min(a.size(), b.size()), e = 0;
    for (int i = 0; i < n; i++) e += (a[i] != b[i]);
    return e;
}
static std::vector<cf> multipath(const std::vector<cf>& x, const std::vector<cf>& h){
    std::vector<cf> y(x.size(), cf(0,0));
    for (size_t n=0;n<x.size();++n) for (size_t k=0;k<h.size()&&k<=n;++k) y[n]+=h[k]*x[n-k];
    return y;
}

static void run(const char* scheme, const std::vector<cf>& h, float sigma,
                const std::vector<cf>& pre){
    const int Ndata = 400; int P = (int)pre.size();
    Modulator mod(string_to_mod_type(scheme));
    int bps = mod.get_bits_per_symbol();

    std::mt19937 g(123); std::uniform_int_distribution<int> db(0,1);
    std::vector<uint8_t> tx(Ndata*bps); for(auto&x:tx)x=db(g);

    bool add=true; std::vector<cf> pre_m=pre; auto pkt=mod.modulate(tx,pre_m,add); // [guard|pre|data]
    std::vector<cf> block(pkt.begin()+10, pkt.end());                              // strip guard(10)
    auto rxb = multipath(block, h);
    std::mt19937 gn(7); std::normal_distribution<float> nz(0.f,sigma);
    for(auto&s:rxb) s+=cf(nz(gn),nz(gn));

    // Run the REAL channel_eq_thread on this one burst.
    MutexFIFO<Blk> in_fifo, out_fifo;
    in_fifo.push({0, rxb});
    std::atomic<bool> stop{false};
    std::thread th(channel_eq_thread, std::ref(in_fifo), std::ref(out_fifo),
                   std::ref(pre), std::ref(mod), EqType::LMS, 11, 0.3f,
                   /*decision_directed=*/false, std::ref(stop));
    Blk out; bool got=false;
    for(int i=0;i<500;i++){ if(out_fifo.pop(out)){got=true;break;}
        std::this_thread::sleep_for(std::chrono::milliseconds(2)); }
    stop.store(true); th.join();
    if(!got){ printf("%-8s no output from channel_eq_thread\n", scheme); return; }

    // channel_eq_thread output: coherent -> N data syms; differential -> [ref | N].
    int out_n = (int)out.second.size();
    auto rb = mod.demodulate(out.second);   // differential_decode happens inside
    int errs = bd(tx, rb);
    printf("%-8s | eq_out=%d syms (expect %d) | decoded_bits=%zu (expect %d) | BER=%.4f %s\n",
        scheme, out_n, mod.is_differential_scheme()?Ndata+1:Ndata,
        rb.size(), Ndata*bps, (float)errs/tx.size(),
        (errs==0 && (int)rb.size()==Ndata*bps) ? "[OK]" : "[BAD]");
}

int main(){
    // Complex Zadoff-Chu preamble → the equalizer trains by exact LS/MMSE (the
    // NLMS path used for a real preamble converges poorly, which would mask the
    // differential decode). Mild multipath so the equalizer has real work to do.
    std::vector<cf> h = { cf(1.0f,0.0f), cf(0.25f,0.08f) };
    auto pre = generate_zadoff_chu_preamble(25, 63);          // complex, length 63
    printf("=== channel_eq_thread through multipath h=[1, 0.25+0.08j], sigma=0.01, ZC preamble ===\n");
    printf("-- differential schemes (must keep the last-preamble reference) --\n");
    for (auto s : {"DBPSK","DQPSK","8-DPSK"}) run(s, h, 0.01f, pre);
    printf("-- coherent control (unchanged path) --\n");
    for (auto s : {"QPSK","16-QAM"}) run(s, h, 0.01f, pre);
    return 0;
}
