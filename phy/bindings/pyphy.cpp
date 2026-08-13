// pyphy.cpp — GNU-Radio-style block API over the sdr_system DSP.
//
// Exposes the PHY's building blocks as pure numpy-in / numpy-out functions so a
// Python script can COMPOSE the chain and insert its own operations between any
// two stages (e.g. a digital gain between modulate and the RRC pulse shaper):
//
//     import pyphy, numpy as np
//     bits  = pyphy.frame("hello", 0, 1)
//     syms  = pyphy.modulate(bits, "QPSK")     # -> complex64[]
//     syms  = syms * 0.8                        # <-- YOUR op, plain numpy
//     wave  = pyphy.rrc_tx(syms, sps=2)         # RRC pulse shape
//     # ... hand `wave` to the radio (pyphy.Radio, built where UHD is present)
//
// Same C++ code the monolithic sdr_system uses — no DSP is re-implemented.
// Build: see bindings/build.sh (pybind11 + fftw/volk; no UHD needed for these blocks).
#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include <vector>
#include <complex>
#include <string>

#include "messages.hpp"
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "filters.hpp"
#include "taps.hpp"
#include "fec.hpp"
#include "synchronization.hpp"
#include "frequency_offset.hpp"
#include "phase_offset.hpp"
#include "ofdm.hpp"
#ifdef PYPHY_WITH_UHD
#include "physical_layer.hpp"
#include <thread>
#include <chrono>
#endif

namespace py = pybind11;
using cf = std::complex<float>;

// ---- numpy <-> std::vector converters ----
static std::vector<cf> tocv(py::array_t<std::complex<float>, py::array::c_style | py::array::forcecast> a){
    auto b = a.request(); auto p = static_cast<cf*>(b.ptr);
    return std::vector<cf>(p, p + b.size);
}
static py::array_t<std::complex<float>> fromcv(const std::vector<cf>& v){
    py::array_t<std::complex<float>> a(v.size());
    std::copy(v.begin(), v.end(), static_cast<cf*>(a.request().ptr));
    return a;
}
static std::vector<uint8_t> tou8(py::array_t<uint8_t, py::array::c_style | py::array::forcecast> a){
    auto b = a.request(); auto p = static_cast<uint8_t*>(b.ptr);
    return std::vector<uint8_t>(p, p + b.size);
}
static py::array_t<uint8_t> fromu8(const std::vector<uint8_t>& v){
    py::array_t<uint8_t> a(v.size());
    std::copy(v.begin(), v.end(), static_cast<uint8_t*>(a.request().ptr));
    return a;
}
static std::vector<float> tof32(py::array_t<float, py::array::c_style | py::array::forcecast> a){
    auto b = a.request(); auto p = static_cast<float*>(b.ptr);
    return std::vector<float>(p, p + b.size);
}
static py::array_t<float> fromf32(const std::vector<float>& v){
    py::array_t<float> a(v.size());
    std::copy(v.begin(), v.end(), static_cast<float*>(a.request().ptr));
    return a;
}

// ---- one RRC filtering stage (shared by rrc_tx / rrc_rx) ----
// rrc period = U/1 ; resampling = polyU/polyD ; matched = time-reversed conj taps.
static std::vector<cf> rrc(const std::vector<cf>& in, int ntaps, int U,
                           int polyU, int polyD, double beta, bool matched){
    std::vector<cf> taps(ntaps);
    rrc_pulse(taps.data(), (ntaps - 1) / 2, U, 1, beta);
    if (matched){ std::vector<cf> t(ntaps);
        for (int i = 0; i < ntaps; ++i) t[i] = std::conj(taps[ntaps - 1 - i]); taps = t; }
    std::vector<cf> x = in; x.insert(x.end(), 2 * ntaps, cf(0, 0));
    if (x.size() % polyD) x.insert(x.end(), polyD - (x.size() % polyD), cf(0, 0));
    FilterPolyphase f(polyU, polyD, (int)x.size(), ntaps, taps.data(), 1);
    f.set_head(true);
    std::vector<cf> out(f.out_len());
    out.resize(f.filter(x.data(), out.data()));
    return out;
}

// ================= the blocks =================

// framing
static py::array_t<uint8_t> frame(const std::string& payload, int idx, int tot){
    return fromu8(build_packet_bits(payload, (uint8_t)idx, (uint8_t)tot));
}
static py::tuple unframe(py::array_t<uint8_t> bits){
    auto [i, t, p, ok] = decode_packet_bits(tou8(bits));
    return py::make_tuple((int)i, (int)t, py::bytes(p), ok);
}

// modulation  (data symbols only — no preamble/guard)
static py::array_t<std::complex<float>> modulate(py::array_t<uint8_t> bits, const std::string& scheme){
    Modulator mod(string_to_mod_type(scheme));
    std::vector<cf> pre; bool add = false;
    return fromcv(mod.modulate(tou8(bits), pre, add));
}
static py::array_t<uint8_t> demodulate(py::array_t<std::complex<float>> syms, const std::string& scheme){
    Modulator mod(string_to_mod_type(scheme));
    return fromu8(mod.demodulate(tocv(syms)));
}
// soft demapper -> one LLR per coded bit (positive = bit 0)
static py::array_t<float> soft_llr(py::array_t<std::complex<float>> syms,
                                   const std::string& scheme, float noise_var){
    Modulator mod(string_to_mod_type(scheme));
    return fromf32(soft_demodulate_llr(tocv(syms), mod, noise_var));
}

// pulse shaping / matched filter
static py::array_t<std::complex<float>> rrc_tx(py::array_t<std::complex<float>> syms,
                                               int sps, double beta, int ntaps){
    return fromcv(rrc(tocv(syms), ntaps, sps, sps, 1, beta, false));   // upsample to sps
}
static py::array_t<std::complex<float>> rrc_rx(py::array_t<std::complex<float>> wave,
                                               int sps, double beta, int ntaps){
    return fromcv(rrc(tocv(wave), ntaps, sps, 1, 1, beta, true));      // matched, no resample
}

// FEC
static py::array_t<uint8_t> fec_encode(py::array_t<uint8_t> bits, const std::string& type, int k){
    fec_set_type(type, k);
    return fromu8(fec_encode_block(tou8(bits)));
}
static py::array_t<uint8_t> fec_decode(py::array_t<uint8_t> coded, const std::string& type,
                                       int k, int info_len){
    fec_set_type(type, k);
    return fromu8(fec_decode_block(tou8(coded), info_len));
}
static py::array_t<uint8_t> fec_decode_soft(py::array_t<float> llr, const std::string& type,
                                            int k, int info_len){
    fec_set_type(type, k);
    return fromu8(fec_soft_decode_block(tof32(llr), info_len));
}
static int fec_encoded_length(int nbits, const std::string& type, int k){
    fec_set_type(type, k); return fec_encoded_len(nbits);
}

// ---- preamble ----
static py::array_t<std::complex<float>> preamble(int m){
    return fromcv(generate_msequence_preamble(m));
}

// ---- sync: frame + symbol timing (ACQ) ----
// Returns (aligned [preamble|data] symbols, detected, peak, tau).
static py::tuple acq(py::array_t<std::complex<float>> syms, py::array_t<std::complex<float>> pre,
                     int ndata, int sps, float threshold){
    auto p = tocv(pre);
    ACQSynchronizer a(p, sps, threshold, ndata, true);
    auto r = a.SamplesACQPerformance(tocv(syms));
    return py::make_tuple(fromcv(r.AlignedStats), r.PacketDetected, r.MaxCorrelation, r.tau_opt);
}

// ---- CFO estimate + correct ----  returns (corrected, cfo_hz)
static py::tuple cfo_correct(py::array_t<std::complex<float>> block, py::array_t<std::complex<float>> pre,
                             double symbol_rate, int sps, const std::string& method){
    auto meth = CFOCorrector::Method::PILOT_LS;
    if (method == "pilot_aided") meth = CFOCorrector::Method::PILOT_AIDED;
    else if (method == "schmidl_cox") meth = CFOCorrector::Method::SCHMIDL_COX;
    CFOCorrector c(symbol_rate, sps, tocv(pre), meth);
    auto out = c.correct(tocv(block));
    return py::make_tuple(fromcv(out), c.get_last_cfo_hz());
}

// ---- carrier phase estimate + correct ----  returns (corrected, phase_deg)
static py::tuple phase_correct(py::array_t<std::complex<float>> block, py::array_t<std::complex<float>> pre,
                               const std::string& scheme, float loop_bw, float damping){
    Modulator mod(string_to_mod_type(scheme));
    auto p = tocv(pre);
    PhaseOffsetCorrector poc(mod, p, (int)p.size(), true, loop_bw, damping,
                             PhaseOffsetCorrector::EstimationMethod::PREAMBLE);
    auto out = poc.correct(tocv(block));
    return py::make_tuple(fromcv(out), poc.get_last_phase_estimate() * 180.0 / M_PI);
}

// ---- OFDM modulate/demodulate ----
static py::array_t<std::complex<float>> ofdm_mod(py::array_t<std::complex<float>> qam, int fft, int cp){
    OFDM o(fft, cp);
    return fromcv(o.modulate(tocv(qam)));
}
// returns (equalized qam, frame_start, cfo_subcarriers)
static py::tuple ofdm_demod(py::array_t<std::complex<float>> burst, int num_qam, int fft, int cp){
    OFDM o(fft, cp);
    int start = -1; float cfo = 0.f;
    auto qam = o.receive(tocv(burst), num_qam, &start, &cfo);
    return py::make_tuple(fromcv(qam), start, cfo);
}
static int ofdm_data_per_sym(int fft, int cp){ OFDM o(fft, cp); return o.data_per_sym(); }

// ================= radio source / sink (needs UHD) =================
#ifdef PYPHY_WITH_UHD
// A thin source/sink block over the real USRP: transmit a prepared baseband
// buffer, or capture raw samples. Wraps PHYSICAL_LAYER::transmit_samples /
// capture_raw. role "tx" launches the TX pipeline; "rx" runs monitor mode.
class Radio {
    PHYSICAL_CONFIG cfg_;
    PHYSICAL_LAYER  phy_;
    std::string     role_;
    static PHYSICAL_CONFIG make(const std::string& role, const std::string& args,
                                double freq, double rate, double symbol_rate, double gain,
                                const std::string& subdev, const std::string& ant){
        PHYSICAL_CONFIG c;
        c.symbol_rate = symbol_rate;
        if (role == "tx"){ c.tx_args=args; c.tx_freq=freq; c.tx_rate=rate; c.tx_gain=gain;
                           c.tx_subdev=subdev; c.tx_ant=ant; c.rx_args=""; }
        else            { c.rx_args=args; c.rx_freq=freq; c.rx_rate=rate; c.rx_gain=gain;
                          c.rx_subdev=subdev; c.rx_ant=ant; c.tx_args=""; }
        return c;
    }
public:
    Radio(const std::string& role, const std::string& args, double freq, double rate,
          double symbol_rate, double gain, const std::string& subdev, const std::string& ant)
        : cfg_(make(role, args, freq, rate, symbol_rate, gain, subdev, ant)),
          phy_(cfg_), role_(role)
    {
        phy_.start(/*monitor_only=*/ role == "rx");   // rx: raw monitor; tx: pipeline
    }
    // sink: push a prepared complex baseband buffer to the USRP TX, block until drained.
    void transmit(py::array_t<std::complex<float>> samples){
        phy_.transmit_samples(tocv(samples));
        while (phy_.tx_pending() > 0)
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    // source: capture n raw baseband samples from the USRP RX.
    py::array_t<std::complex<float>> capture(size_t n){ return fromcv(phy_.capture_raw(n)); }
    void close(){ phy_.stop(); }
};
#endif

PYBIND11_MODULE(pyphy, m){
    m.doc() = "GNU-Radio-style block API over the sdr_system PHY (numpy in/out).";

    m.def("frame", &frame, py::arg("payload"), py::arg("idx") = 0, py::arg("tot") = 1,
          "payload(str) -> framed bits (header+payload+CRC16), uint8[]");
    m.def("unframe", &unframe, py::arg("bits"),
          "framed bits -> (idx, tot, payload_bytes, crc_ok)");

    m.def("modulate", &modulate, py::arg("bits"), py::arg("scheme") = "QPSK",
          "bits -> data symbols (complex64[]); scheme e.g. QPSK/BPSK/16-QAM/DQPSK");
    m.def("demodulate", &demodulate, py::arg("syms"), py::arg("scheme") = "QPSK");
    m.def("soft_llr", &soft_llr, py::arg("syms"), py::arg("scheme") = "QPSK",
          py::arg("noise_var") = 1.0f, "equalized symbols -> per-bit LLRs (positive=bit0)");

    m.def("rrc_tx", &rrc_tx, py::arg("syms"), py::arg("sps") = 2, py::arg("beta") = 0.25,
          py::arg("ntaps") = 151, "RRC pulse-shape: symbols -> waveform at sps samples/symbol");
    m.def("rrc_rx", &rrc_rx, py::arg("wave"), py::arg("sps") = 2, py::arg("beta") = 0.25,
          py::arg("ntaps") = 151, "matched RRC filter: waveform -> filtered (still at sps)");

    m.def("fec_encode", &fec_encode, py::arg("bits"), py::arg("type") = "conv", py::arg("k") = 256,
          "type = conv | ldpc | turbo");
    m.def("fec_decode", &fec_decode, py::arg("coded"), py::arg("type") = "conv",
          py::arg("k") = 256, py::arg("info_len") = -1, "hard-decision decode");
    m.def("fec_decode_soft", &fec_decode_soft, py::arg("llr"), py::arg("type") = "conv",
          py::arg("k") = 256, py::arg("info_len") = -1, "soft-decision decode (LLRs in)");
    m.def("fec_encoded_len", &fec_encoded_length, py::arg("nbits"), py::arg("type") = "conv",
          py::arg("k") = 256);

    // ---- sync / carrier recovery ----
    m.def("preamble", &preamble, py::arg("m") = 5, "m-sequence preamble (2^m-1 symbols)");
    m.def("acq", &acq, py::arg("syms"), py::arg("preamble"), py::arg("ndata"),
          py::arg("sps") = 1, py::arg("threshold") = 15.0f,
          "frame + symbol-timing sync -> (aligned [preamble|data], detected, peak, tau)");
    m.def("cfo_correct", &cfo_correct, py::arg("block"), py::arg("preamble"),
          py::arg("symbol_rate"), py::arg("sps") = 1, py::arg("method") = "pilot_ls",
          "estimate & remove carrier frequency offset -> (corrected, cfo_hz)");
    m.def("phase_correct", &phase_correct, py::arg("block"), py::arg("preamble"),
          py::arg("scheme") = "QPSK", py::arg("loop_bw") = 0.02f, py::arg("damping") = 0.707f,
          "estimate & remove carrier phase -> (corrected, phase_deg)");

    // ---- OFDM ----
    m.def("ofdm_mod", &ofdm_mod, py::arg("qam"), py::arg("fft") = 64, py::arg("cp") = 16,
          "QAM symbols -> OFDM time-domain frame (adds SC/chest/pilots + CP)");
    m.def("ofdm_demod", &ofdm_demod, py::arg("burst"), py::arg("num_qam"),
          py::arg("fft") = 64, py::arg("cp") = 16,
          "OFDM burst -> (equalized QAM, frame_start, cfo_subcarriers); does sync/CFO/EQ");
    m.def("ofdm_data_per_sym", &ofdm_data_per_sym, py::arg("fft") = 64, py::arg("cp") = 16,
          "data subcarriers per OFDM symbol (excludes pilots + DC/Nyquist)");

#ifdef PYPHY_WITH_UHD
    py::class_<Radio>(m, "Radio",
        "Real USRP source/sink block. role='tx' transmits a prepared baseband buffer; "
        "role='rx' captures raw samples. Wraps transmit_samples() / capture_raw().")
        .def(py::init<std::string, std::string, double, double, double, double,
                      std::string, std::string>(),
             py::arg("role"), py::arg("args"), py::arg("freq"), py::arg("rate"),
             py::arg("symbol_rate") = 1e6, py::arg("gain") = 30.0,
             py::arg("subdev") = "A:0", py::arg("ant") = "")
        .def("transmit", &Radio::transmit, py::arg("samples"),
             "sink: push a complex64 baseband buffer to the USRP TX (blocks until drained)")
        .def("capture", &Radio::capture, py::arg("n"),
             "source: capture n raw complex64 baseband samples from the USRP RX")
        .def("close", &Radio::close);
    m.attr("HAS_RADIO") = true;
#else
    m.attr("HAS_RADIO") = false;   // built without UHD; Radio unavailable
#endif
}
