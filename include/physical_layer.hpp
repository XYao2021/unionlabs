#pragma once
// ============================================================
//  physical_layer.hpp
//  Wraps the complete transmit and receive pipelines into a
//  single PHYSICAL_LAYER object used by main.cpp.
//  Also defines PHYSICAL_CONFIG (all parameters in one struct).
// ============================================================

#include <string>
#include <vector>
#include <complex>
#include <thread>
#include <atomic>
#include <iostream>
#include <csignal>

#include <uhd/usrp/multi_usrp.hpp>
#include <uhd/utils/safe_main.hpp>
#include <uhd/utils/thread.hpp>

#include "FIFO.hpp"
#include "messages.hpp"
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "transceiver.hpp"
#include "filters.hpp"
#include "synchronization.hpp"
#include "timing_recovery.hpp"
#include "frequency_offset.hpp"
#include "phase_offset.hpp"
#include "channel_estimation.hpp"
#include "ofdm_pipeline.hpp"

// ─────────────────────────────────────────────────────────────
//  Global stop signal (Ctrl-C handler sets this)
// ─────────────────────────────────────────────────────────────
inline std::atomic<bool> global_stop_signal{false};

inline void global_sig_int_handler(int) {
    global_stop_signal.store(true);
    std::cout << "\n[SIGNAL] Ctrl-C caught — stopping...\n";
}

// ─────────────────────────────────────────────────────────────
//  PHYSICAL_CONFIG
// ─────────────────────────────────────────────────────────────
struct PHYSICAL_CONFIG {
    // Message
    std::string data_type     = "float";
    std::string preamble_type = "m-sequence";
    int         preamble_length = 5;       // m for m-sequence
    bool        add_preamble  = true;
    int         ack_length    = 0;

    // Filter — wire samples/symbol = U/D. Integer sps required (e.g. 2/1).
    int         U             = 2;
    int         D             = 1;
    std::string filter_type   = "rrc";
    double      symbol_rate   = 0.8e6;
    int         num_taps      = 151;
    double      roll_off      = 0.25;
    int         num_threads   = 1;

    // TX hardware
    std::string tx_args       = "";
    double      tx_rate       = 1.6e6;   // = symbol_rate * U/D (integer sps)
    double      tx_freq       = 2.412e9;
    double      tx_gain       = 20.0;
    double      tx_bw         = 1.0e6;   // cover ~symbol_rate*(1+rolloff) occupied BW
    std::string tx_ant        = "TX/RX";
    int         tx_channel    = 0;
    std::string tx_subdev     = "A:A";

    // RX hardware
    std::string rx_args       = "";
    double      rx_rate       = 1.6e6;   // = tx_rate (integer samples/symbol)
    double      rx_freq       = 2.412e9;
    double      rx_gain       = 30.0;
    double      rx_bw         = 1.0e6;   // cover ~symbol_rate*(1+rolloff) occupied BW
    std::string rx_ant        = "RX2";
    int         rx_channel    = 0;
    std::string rx_subdev     = "A:A";

    // Common
    std::string ref           = "internal";
    std::string otw           = "sc16";
    double      settling_time = 0.2;
    double      uhd_timeout   = 1000.0;

    // Role: "tx" = transmit only (open TX device + TX pipeline),
    //       "rx" = receive only (open RX device + RX pipeline),
    //       "both" = original full-duplex/loopback (open both, run both),
    //       "source_arq"/"sink_arq" = stop-and-wait ARQ (data over RF).
    std::string role          = "both";

    // ARQ ACK transport: "tcp" (ACK over a socket — default; no reverse RF
    // needed, ideal when both radios are on one host) or "rf" (ACK over the
    // second RF path, RF B — needs the reverse cable/antenna + full-duplex).
    std::string ack_transport = "tcp";
    std::string ack_host      = "127.0.0.1";  // TCP: sink's address (source connects here)
    int         ack_port      = 5599;         // TCP: ACK socket port

    // Energy detection  (alpha: larger = more IIR smoothing; see main.cpp)
    float       alpha                  = 0.95f;
    float       energy_threshold       = 1e-7f;
    size_t      energy_packet_size     = 3300;
    size_t      IIR_window_size        = 20;
    bool        IIR_threshold_adaptive = true;
    float       IIR_threshold_multiplier = 5.0f;
    // Continuously re-track the noise floor during idle (default), instead of a
    // single one-shot calibration at startup — far more robust on real links
    // where the ambient level drifts.
    bool        det_continuous_track   = true;

    // Synchronisation
    int         sps_sync        = 2;   // unused by RX (ACQ times internally at os)
    float       sync_threshold  = 15.0f;
    int         message_length  = 508;      // data symbols only

    // Receiver buffering
    int         samps_per_buff    = 10000;
    int         num_recv_request  = 0;      // 0 = continuous

    // AGC
    std::string AGC_type        = "Feed";
    bool        dc_block        = false;  // per-burst DC-block high-pass (experimental; off by default)
    float       tx_dc_i         = 0.0f;   // manual TX LO-leakage null (normalized I)
    float       tx_dc_q         = 0.0f;   // manual TX LO-leakage null (normalized Q)

    // Modulation
    std::string scheme          = "QPSK";
    int         sps             = 2;

    // Forward Error Correction (rate-1/2 K=7 convolutional + Viterbi). When on,
    // packet bits are FEC-encoded before modulation and Viterbi-decoded after
    // demodulation, so the receiver CORRECTS bit errors instead of dropping the
    // frame — ~3-4 dB coding gain (hard decision). Applied in main.cpp, so it
    // works for both single-carrier and OFDM.
    bool        fec             = false;

    // Waveform: "sc" = single-carrier (RRC + match filter + timing + eq),
    //           "ofdm" = OFDM (IFFT/CP; OFDM does its own sync/CFO/equalize).
    std::string waveform        = "sc";
    int         ofdm_fft        = 64;      // FFT size (subcarriers)
    int         ofdm_cp         = 16;      // cyclic prefix length
    int         lora_sf         = 8;       // LoRa/CSS spreading factor (waveform=lora): 2^SF chips/sym
    float       ofdm_tx_peak    = 0.5f;    // TX scaling (OFDM high PAPR → avoid clip)

    // Timing recovery loop
    float       timing_loop_bw  = 0.015f;
    float       timing_damping  = 0.707f;

    // Phase offset correction
    float       phase_loop_bw   = 0.02f;
    float       phase_damping   = 0.707f;

    // Equaliser
    int         eq_taps         = 11;
    float       eq_mu           = 0.3f;      // NLMS step (fallback DD tracking)
    std::string eq_type         = "None";   // LMS diverges on real signal; see main.cpp
    // With a complex (Zadoff-Chu) preamble the LS-trained equalizer is exact when
    // frozen, so decision-directed tracking is OFF by default (it can diverge on
    // noisy dense QAM). Turn on with --eq_dd for slowly time-varying channels.
    bool        eq_decision_directed = false;
};

// ─────────────────────────────────────────────────────────────
//  PHYSICAL_LAYER
//  Owns all FIFOs and threads for TX and RX pipelines.
// ─────────────────────────────────────────────────────────────
class PHYSICAL_LAYER {
public:
    // Input FIFO: push bit-vectors here to transmit
    MutexFIFO<std::vector<uint8_t>>                              tx_bits_fifo;
    // Output FIFO: pop {block_id, bits} here after reception
    MutexFIFO<std::pair<size_t, std::vector<uint8_t>>>           rx_bits_fifo;

    explicit PHYSICAL_LAYER(const PHYSICAL_CONFIG& cfg) : cfg_(cfg) {
        preamble_ = generate_preamble(cfg_.preamble_type, cfg_.preamble_length);
        std::cout << "[PHY] Preamble: " << preamble_.size() << " symbols\n";
    }

    ~PHYSICAL_LAYER() { stop(); }

    // monitor_only: configure the radios but do NOT launch the modulate/demod
    // pipelines (used by the raw tone monitor, which streams samples itself).
    void start(bool monitor_only = false) {
        stop_flag_.store(false);
        std::signal(SIGINT, global_sig_int_handler);

        // ARQ role pipelines. Data always uses RF; the ACK uses RF only when
        // ack_transport == "rf" (then the box is full-duplex: data on one RF
        // front-end, ACK on the other). With TCP ACKs, each ARQ box needs only
        // its DATA pipeline (source: TX, sink: RX) — a single RF path.
        const bool ack_rf = (cfg_.ack_transport == "rf");
        bool do_tx, do_rx;
        if (cfg_.role == "source_arq") {          // data TX; ACK RX only over RF
            do_tx = true;  do_rx = ack_rf;
        } else if (cfg_.role == "sink_arq") {     // data RX; ACK TX only over RF
            do_rx = true;  do_tx = ack_rf;
        } else {
            do_tx = (cfg_.role == "tx" || cfg_.role == "both");
            do_rx = (cfg_.role == "rx" || cfg_.role == "both"
                     || cfg_.role == "sense");            // sense = RX energy only
        }
        std::cout << "[PHY] Role: " << cfg_.role
                  << (do_tx ? "  [TX pipeline]" : "")
                  << (do_rx ? "  [RX pipeline]" : "") << "\n";

        // ── Build USRP objects ─────────────────────────────────
        // A USB device can be opened only once, so when TX and RX are the SAME
        // serial (full-duplex on one box, e.g. ARQ over RF A + RF B) we make() a
        // single multi_usrp and share the sptr for both directions. setup_tx_usrp
        // and setup_rx_usrp then configure the two directions (different subdev /
        // antenna / frequency) on that one device.
        const bool one_device = do_tx && do_rx && !cfg_.tx_args.empty()
                                && cfg_.tx_args == cfg_.rx_args;
        if (one_device) {
            std::cout << "[PHY] Full-duplex on one device (" << cfg_.tx_args
                      << "): TX subdev " << cfg_.tx_subdev << " / RX subdev "
                      << cfg_.rx_subdev << "\n";
            tx_usrp_ = uhd::usrp::multi_usrp::make(cfg_.tx_args);
            rx_usrp_ = tx_usrp_;
            setup_tx_usrp();
            setup_rx_usrp();
        } else {
            if (do_tx) {
                tx_usrp_ = uhd::usrp::multi_usrp::make(cfg_.tx_args);
                setup_tx_usrp();
            }
            if (do_rx) {
                rx_usrp_ = uhd::usrp::multi_usrp::make(cfg_.rx_args);
                setup_rx_usrp();
            }
        }

        std::this_thread::sleep_for(
            std::chrono::milliseconds(long(cfg_.settling_time * 1000)));

        // ── TX pipeline ────────────────────────────────────
        // tx_bits_fifo → modulation → pulse_shape → transmit
        if (do_tx && !monitor_only) launch_tx_pipeline();

        // ── RX pipeline ────────────────────────────────────
        // receive → energy_det → AGC → match_filter →
        // timing_recovery → TimeSync(ACQ) → CFO → phase_offset →
        // channel_eq (strips preamble) → demodulation → rx_bits_fifo
        //
        // Note the ordering: time synchronisation comes BEFORE the frequency and
        // phase offset estimators. Both are data-aided (they use the known
        // preamble), so they can only run once ACQ has located the preamble and
        // produced an aligned [preamble | data] burst.
        if (do_rx && !monitor_only) launch_rx_pipeline();

        std::cout << (monitor_only ? "[PHY] Radios configured (monitor mode)\n"
                                   : "[PHY] All threads launched\n");
    }

    // ── Raw tone monitor ─────────────────────────────────────────────────
    // Stream samples straight from the RX radio, FFT each block, and report the
    // dominant tone's frequency + power ~once/second. Bypasses the data pipeline
    // entirely (no energy detector / AGC / demod), so it measures the received
    // spectrum directly — the right tool for a sine/cosine test tone. Call after
    // start(monitor_only=true). Skips a ±band around DC so the (cable) carrier
    // leakage doesn't masquerade as the tone.
    void run_tone_monitor(std::atomic<bool>& stop, double skip_hz = 8e3) {
        auto rx = rx_usrp_->get_rx_stream(uhd::stream_args_t("fc32", "sc16"));
        uhd::rx_metadata_t md;
        const size_t N = 4096;
        std::vector<std::complex<float>> buf(N);
        fftwf_complex* in  = fftwf_alloc_complex(N);
        fftwf_complex* out = fftwf_alloc_complex(N);
        fftwf_plan plan = fftwf_plan_dft_1d((int)N, in, out, FFTW_FORWARD, FFTW_ESTIMATE);
        const double fs = cfg_.rx_rate;
        const int skip = std::max(1, (int)(skip_hz / (fs / N)));
        rx_usrp_->issue_stream_cmd(uhd::stream_cmd_t::STREAM_MODE_START_CONTINUOUS);
        std::cout << "[MONITOR] Streaming raw samples at " << fs/1e6
                  << " Msps — reporting the dominant tone once/second. Ctrl-C to stop.\n";
        auto last = std::chrono::steady_clock::now();
        while (!stop.load()) {
            size_t got = rx->recv(&buf.front(), N, md, 1.0);
            if (md.error_code == uhd::rx_metadata_t::ERROR_CODE_TIMEOUT || got < N) continue;
            double pwr = 0.0;
            for (size_t i = 0; i < N; ++i) {
                pwr += std::norm(buf[i]);
                in[i][0] = buf[i].real(); in[i][1] = buf[i].imag();
            }
            pwr /= N;
            fftwf_execute(plan);
            double best = 0.0; int bestf = 0;
            for (size_t k = 0; k < N; ++k) {
                int f = (k < N/2) ? (int)k : (int)k - (int)N;   // signed bin
                if (std::abs(f) <= skip) continue;               // ignore DC leakage
                double m = out[k][0]*out[k][0] + out[k][1]*out[k][1];
                if (m > best) { best = m; bestf = f; }
            }
            auto now = std::chrono::steady_clock::now();
            if (std::chrono::duration<double>(now - last).count() >= 1.0) {
                last = now;
                double peak_db = 10.0 * std::log10(best / (double(N)*N) + 1e-12);
                std::printf("[MONITOR] tone f = %+8.1f kHz   avg power = %.4f   peak = %6.1f dB\n",
                            bestf * fs / N / 1e3, pwr, peak_db);
                std::fflush(stdout);
            }
        }
        rx_usrp_->issue_stream_cmd(uhd::stream_cmd_t::STREAM_MODE_STOP_CONTINUOUS);
        fftwf_destroy_plan(plan); fftwf_free(in); fftwf_free(out);
        std::cout << "[MONITOR] stopped.\n";
    }

    // Channel-occupancy sensing: integrate received power over a window and report
    // whether the band is busy (avg power above a threshold). Prints one machine-
    // parseable line per window ("[SENSE] busy=.. power_db=.."). No decode pipeline —
    // configure the radio with start(monitor_only=true) before calling.
    void run_channel_sense(double window_s, double threshold_db, int count) {
        auto rx = rx_usrp_->get_rx_stream(uhd::stream_args_t("fc32", "sc16"));
        uhd::rx_metadata_t md;
        const size_t N = 4096;
        std::vector<std::complex<float>> buf(N);
        const double fs   = cfg_.rx_rate;
        const size_t need = std::max<size_t>(N, (size_t)(window_s * fs));  // samples/window
        rx_usrp_->issue_stream_cmd(uhd::stream_cmd_t::STREAM_MODE_START_CONTINUOUS);
        for (int c = 0; (count <= 0 || c < count) && !global_stop_signal.load(); ++c) {
            double sum = 0.0, peak = 0.0; size_t n = 0;
            while (n < need && !global_stop_signal.load()) {
                size_t got = rx->recv(&buf.front(), N, md, 1.0);
                if (md.error_code == uhd::rx_metadata_t::ERROR_CODE_TIMEOUT || got == 0) continue;
                for (size_t i = 0; i < got; ++i) {
                    double p = std::norm(buf[i]);
                    sum += p; if (p > peak) peak = p;
                }
                n += got;
            }
            double avg     = (n > 0) ? sum / n : 0.0;
            double avg_db  = 10.0 * std::log10(avg  + 1e-12);
            double peak_db = 10.0 * std::log10(peak + 1e-12);
            int    busy    = (avg_db > threshold_db) ? 1 : 0;
            std::printf("[SENSE] busy=%d power_db=%.2f peak_db=%.2f power=%.6g "
                        "window_ms=%.1f samples=%zu threshold_db=%.2f\n",
                        busy, avg_db, peak_db, avg, window_s * 1e3, n, threshold_db);
            std::fflush(stdout);
        }
        rx_usrp_->issue_stream_cmd(uhd::stream_cmd_t::STREAM_MODE_STOP_CONTINUOUS);
    }

    // Capture up to `n` raw baseband samples from the RX (no decode pipeline) — for
    // custom receivers (LoRa/CSS) that do their own sync. Configure start(monitor=true).
    std::vector<std::complex<float>> capture_raw(size_t n) {
        auto rx = rx_usrp_->get_rx_stream(uhd::stream_args_t("fc32", "sc16"));
        uhd::rx_metadata_t md;
        std::vector<std::complex<float>> out; out.reserve(n);
        std::vector<std::complex<float>> buf(4096);
        rx_usrp_->issue_stream_cmd(uhd::stream_cmd_t::STREAM_MODE_START_CONTINUOUS);
        while (out.size() < n && !global_stop_signal.load()) {
            size_t got = rx->recv(&buf.front(), buf.size(), md, 1.0);
            if (md.error_code == uhd::rx_metadata_t::ERROR_CODE_TIMEOUT || got == 0) continue;
            for (size_t i = 0; i < got && out.size() < n; ++i) out.push_back(buf[i]);
        }
        rx_usrp_->issue_stream_cmd(uhd::stream_cmd_t::STREAM_MODE_STOP_CONTINUOUS);
        return out;
    }

    void stop() {
        stop_flag_.store(true);
        global_stop_signal.store(true);
        for (auto& t : threads_)
            if (t.joinable()) t.join();
        threads_.clear();
        std::cout << "[PHY] All threads stopped\n";
    }

    // Send a pre-built bit-vector
    void transmit(const std::vector<uint8_t>& bits) {
        tx_bits_fifo.push(bits);
    }

    // Push raw complex baseband samples straight to the USRP TX, bypassing the
    // modulator and pulse-shaper. Used by the sine/cosine test-tone message type.
    void transmit_samples(const std::vector<std::complex<float>>& samples) {
        shaped_fifo_.push({tx_raw_id_++, samples});
    }
    // Samples still queued for the USRP (lets the tone generator pace itself so
    // it streams continuously without over-filling the FIFO).
    size_t tx_pending() { return shaped_fifo_.size(); }

    const std::vector<std::complex<float>>& preamble() const {
        return preamble_;
    }

private:
    PHYSICAL_CONFIG cfg_;
    std::vector<std::complex<float>> preamble_;
    std::atomic<bool> stop_flag_{false};

    uhd::usrp::multi_usrp::sptr tx_usrp_;
    uhd::usrp::multi_usrp::sptr rx_usrp_;

    std::vector<std::thread> threads_;
    size_t tx_raw_id_ = 0;   // block id for transmit_samples()

    // Internal FIFOs
    MutexFIFO<std::pair<size_t,std::vector<std::complex<float>>>> mod_fifo_;
    MutexFIFO<std::pair<size_t,std::vector<std::complex<float>>>> shaped_fifo_;
    MutexFIFO<std::pair<size_t,std::vector<std::complex<float>>>> recv_fifo_;
    MutexFIFO<std::pair<size_t,std::vector<std::complex<float>>>> detected_fifo_;
    MutexFIFO<std::pair<size_t,std::vector<std::complex<float>>>> agc_fifo_;
    MutexFIFO<std::pair<size_t,std::vector<std::complex<float>>>> filtered_fifo_;
    MutexFIFO<std::pair<size_t,std::vector<std::complex<float>>>> timed_fifo_;
    MutexFIFO<std::pair<size_t,std::vector<std::complex<float>>>> cfo_fifo_;
    MutexFIFO<std::pair<size_t,std::vector<std::complex<float>>>> synced_fifo_;
    MutexFIFO<std::pair<size_t,std::vector<std::complex<float>>>> phase_fifo_;
    MutexFIFO<std::pair<size_t,std::vector<std::complex<float>>>> eq_fifo_;

    // ── USRP setup ─────────────────────────────────────────
    void setup_tx_usrp() {
        if (!cfg_.tx_subdev.empty())
            tx_usrp_->set_tx_subdev_spec(cfg_.tx_subdev);
        tx_usrp_->set_clock_source(cfg_.ref);
        tx_usrp_->set_tx_rate(cfg_.tx_rate);
        uhd::tune_request_t req(cfg_.tx_freq);
        tx_usrp_->set_tx_freq(req);
        tx_usrp_->set_tx_gain(cfg_.tx_gain);
        tx_usrp_->set_tx_antenna(cfg_.tx_ant);
        tx_usrp_->set_tx_bandwidth(cfg_.tx_bw);
        // Manual TX LO-leakage null. The B210 can't self-calibrate DC offset
        // (no cal antenna), so on a direct cable — where TX carrier leakage
        // couples strongly into the RX and blocks dense QAM — inject a baseband
        // DC that cancels the LO feedthrough. Tune --tx-dc-i / --tx-dc-q
        // (normalized, [-1,1]) to minimize the RX DC spike in the viz figure.
        if (cfg_.tx_dc_i != 0.0f || cfg_.tx_dc_q != 0.0f) {
            tx_usrp_->set_tx_dc_offset(std::complex<double>(cfg_.tx_dc_i, cfg_.tx_dc_q));
            std::cout << "[PHY] TX DC-offset null: (" << cfg_.tx_dc_i << ", "
                      << cfg_.tx_dc_q << ")\n";
        }
        std::cout << "[PHY] TX: " << cfg_.tx_freq/1e9 << " GHz  "
                  << cfg_.tx_rate/1e6 << " Msps  gain=" << cfg_.tx_gain << "\n";
    }

    void setup_rx_usrp() {
        if (!cfg_.rx_subdev.empty())
            rx_usrp_->set_rx_subdev_spec(cfg_.rx_subdev);
        rx_usrp_->set_clock_source(cfg_.ref);
        rx_usrp_->set_rx_rate(cfg_.rx_rate);
        uhd::tune_request_t req(cfg_.rx_freq);
        rx_usrp_->set_rx_freq(req);
        rx_usrp_->set_rx_gain(cfg_.rx_gain);
        rx_usrp_->set_rx_antenna(cfg_.rx_ant);
        rx_usrp_->set_rx_bandwidth(cfg_.rx_bw);
        rx_usrp_->set_rx_dc_offset(true);
        std::cout << "[PHY] RX: " << cfg_.rx_freq/1e9 << " GHz  "
                  << cfg_.rx_rate/1e6 << " Msps  gain=" << cfg_.rx_gain << "\n";
    }

    // ── TX pipeline threads ────────────────────────────────
    void launch_tx_pipeline() {
        // ── OFDM waveform: bits → OFDM frame → transmit (no RRC pulse shaper) ──
        if (cfg_.waveform == "ofdm") {
            threads_.emplace_back(ofdm_modulation_thread,
                std::ref(tx_bits_fifo), std::ref(shaped_fifo_),
                std::ref(cfg_.scheme), cfg_.ofdm_fft, cfg_.ofdm_cp,
                cfg_.ofdm_tx_peak, std::ref(stop_flag_));

            std::vector<unsigned long> tx_ch = {(unsigned long)cfg_.tx_channel};
            threads_.emplace_back(transmit_thread,
                tx_usrp_, std::ref(shaped_fifo_),
                cfg_.tx_rate, tx_ch, cfg_.uhd_timeout / 1000.0,
                std::ref(stop_flag_));
            return;
        }

        auto preamble_copy = preamble_;

        // NOTE: scheme and add_preamble are passed by reference into the thread,
        // which outlives this function, so they must NOT be locals — bind them to
        // the cfg_ members (cfg_ lives as long as this object and every thread is
        // joined in stop() before destruction). Passing std::ref() to a local
        // here dangles the moment this function returns and the thread then reads
        // garbage (e.g. string_to_mod_type sees a corrupted scheme string).
        threads_.emplace_back(modulation_thread,
            std::ref(tx_bits_fifo), std::ref(mod_fifo_),
            std::ref(cfg_.scheme), std::ref(stop_flag_),
            preamble_copy, std::ref(cfg_.add_preamble));

        threads_.emplace_back(pulse_shaping_filter_thread,
            std::ref(mod_fifo_), std::ref(shaped_fifo_),
            cfg_.filter_type, cfg_.symbol_rate, cfg_.tx_rate,
            cfg_.num_taps, cfg_.U, cfg_.D, cfg_.roll_off,
            cfg_.num_threads, std::ref(stop_flag_), "transmitter");

        std::vector<unsigned long> tx_ch = {(unsigned long)cfg_.tx_channel};
        threads_.emplace_back(transmit_thread,
            tx_usrp_, std::ref(shaped_fifo_),
            cfg_.tx_rate, tx_ch, cfg_.uhd_timeout / 1000.0,
            std::ref(stop_flag_));
    }

    // ── RX pipeline threads ────────────────────────────────
    void launch_rx_pipeline() {
        std::vector<unsigned long> rx_ch = {(unsigned long)cfg_.rx_channel};

        // 1. USRP receive
        threads_.emplace_back(receive_thread,
            rx_usrp_, rx_ch, cfg_.rx_rate, cfg_.settling_time,
            std::ref(recv_fifo_),
            cfg_.num_recv_request, cfg_.samps_per_buff,
            std::ref(stop_flag_));

        // 2. Energy detection
        threads_.emplace_back([this]() {
            EnergyDetectorIIR det(cfg_.alpha, cfg_.energy_threshold,
                cfg_.energy_packet_size, cfg_.IIR_window_size,
                cfg_.IIR_threshold_adaptive, cfg_.IIR_threshold_multiplier,
                cfg_.det_continuous_track);
            EnergyDetection_thread(recv_fifo_, detected_fifo_,
                                   det, stop_flag_);
        });

        // 3. AGC  (with per-burst DC-block high-pass to kill cable TX leakage)
        threads_.emplace_back(AGC_thread,
            std::ref(detected_fifo_), std::ref(agc_fifo_),
            std::ref(stop_flag_), std::ref(cfg_.AGC_type), cfg_.dc_block);

        // ── OFDM waveform: energy/AGC burst → OFDM demod → bits.
        //    OFDM::receive() does frame sync, CFO and per-subcarrier equalization
        //    itself, so the single-carrier front-end below is skipped entirely.
        if (cfg_.waveform == "ofdm") {
            threads_.emplace_back(ofdm_demodulation_thread,
                std::ref(agc_fifo_), std::ref(rx_bits_fifo),
                std::ref(cfg_.scheme), cfg_.ofdm_fft, cfg_.ofdm_cp,
                cfg_.message_length, std::ref(stop_flag_));
            return;
        }

        // RX integer oversampling (samples/symbol on the wire). The whole RX
        // front-end now runs at this integer rate — no fractional resampling.
        const int os = std::max(1, (int)std::lround(cfg_.rx_rate / cfg_.symbol_rate));

        // 4. Matched filter (single-rate). It convolves the incoming `os`-sps
        //    stream with the RRC pulse designed at `os` samples/symbol and does
        //    NOT change the sample rate. (The old code upsampled by cfg_.D with a
        //    mismatched pulse, which was not a matched filter and destroyed the
        //    preamble correlation.) The U,D args are ignored by the RX matched
        //    filter; pass 1,1 for clarity.
        threads_.emplace_back(match_filter_thread,
            std::ref(agc_fifo_), std::ref(filtered_fifo_),
            cfg_.filter_type, cfg_.symbol_rate, cfg_.rx_rate,
            cfg_.num_taps, 1, 1, cfg_.roll_off,
            cfg_.num_threads, std::ref(stop_flag_), "receiver");

        auto preamble_copy = preamble_;

        // 5. Time synchronisation (ACQ) — joint frame + symbol timing.
        //    The matched-filter output is at `os` samples/symbol; ACQ correlates
        //    the known preamble across every sample offset, so the peak lands on
        //    the optimal sub-symbol sampling instant. It then extracts the aligned
        //    burst [preamble | data] at exactly ONE sample/symbol (stride = os).
        //    This replaces the separate Gardner timing-recovery loop: for
        //    packet-mode bursts the radios' ppm clock drift over one packet is
        //    negligible, so a per-burst correlation-chosen phase is both simpler
        //    and far more robust than a decision-directed loop. Estimating
        //    frequency/phase still happens AFTER this (they need the aligned
        //    preamble at the front of the block).
        threads_.emplace_back([this, preamble_copy, os]() mutable {
            TimeSync_thread(filtered_fifo_, synced_fifo_,
                            preamble_copy,
                            cfg_.U, cfg_.D, os,
                            stop_flag_,
                            cfg_.message_length, cfg_.sync_threshold);
        });

        // 7. Carrier frequency offset (CFO) correction — AFTER time sync.
        //    Data-aided (pilot) estimation on the aligned burst; the preamble is
        //    now genuinely at the front, so the estimate is meaningful. The burst
        //    is at 1 sample/symbol here, so sps=1 and rate=symbol_rate.
        threads_.emplace_back(CFO_correction_thread,
            std::ref(synced_fifo_), std::ref(cfo_fifo_),
            preamble_copy, cfg_.symbol_rate, 1,
            CFOCorrector::Method::PILOT_AIDED,
            std::ref(stop_flag_));

        // 8. Carrier phase offset correction (required for QPSK / QAM) — AFTER
        //    time sync and CFO. Preamble-ML bulk estimate + optional PLL tracking.
        //    Differential schemes (DBPSK/DQPSK/8-DPSK/pi4-QPSK) carry data in the
        //    phase *transition*, so the decision-directed PLL is turned OFF for
        //    them (it would pin the absolute phase to fixed constellation points
        //    and scramble the transitions). The one-shot preamble de-rotation
        //    still runs (a constant rotation, harmless for differential), and the
        //    differential decoder + CFO stage handle the rest.
        Modulator mod(string_to_mod_type(cfg_.scheme));
        const bool differential   = mod.is_differential_scheme();
        const bool use_phase_track = !differential;
        threads_.emplace_back([this, mod, preamble_copy, use_phase_track]() mutable {
            phase_offset_thread(cfo_fifo_, phase_fifo_,
                mod, preamble_copy,
                (int)preamble_copy.size(),
                use_phase_track,
                cfg_.phase_loop_bw, cfg_.phase_damping,
                PhaseOffsetCorrector::EstimationMethod::PREAMBLE,
                stop_flag_);
        });

        // 9. Channel equaliser
        EqType et = EqType::LMS;
        if (cfg_.eq_type == "RLS") et = EqType::RLS;
        else if (cfg_.eq_type == "DFE") et = EqType::DFE;

        if (cfg_.eq_type != "None") {
            Modulator mod2(string_to_mod_type(cfg_.scheme));
            threads_.emplace_back([this, mod2, preamble_copy, et]() mutable {
                // Signature: input, output, preamble, mod, eq_type,
                //            num_taps, step_size, decision_directed, stop
                channel_eq_thread(phase_fifo_, eq_fifo_,
                    preamble_copy, mod2,
                    et, cfg_.eq_taps, cfg_.eq_mu,
                    cfg_.eq_decision_directed,   // DD tracking after training
                    stop_flag_);
            });
        } else {
            // No equaliser: still strip the leading preamble here (the equaliser
            // path does this internally) so the demodulator receives data only.
            // Differential schemes keep the LAST preamble symbol as the decoder's
            // phase reference (the TX encoder used preamble.back() as its
            // reference), so the differential decode returns exactly N symbols
            // instead of N-1 — otherwise the 1-symbol shortfall shifts the
            // FEC/CRC framing and every packet fails (verified OTA).
            int plen = static_cast<int>(preamble_copy.size()) - (differential ? 1 : 0);
            threads_.emplace_back([this, plen]() {
                std::pair<size_t, std::vector<std::complex<float>>> msg;
                while (!stop_flag_ || phase_fifo_.size() > 0) {
                    if (phase_fifo_.pop(msg)) {
                        auto& s = msg.second;
                        if (static_cast<int>(s.size()) > plen)
                            s.erase(s.begin(), s.begin() + plen);   // drop preamble
                        else
                            s.clear();
                        eq_fifo_.push(std::move(msg));
                    } else {
                        std::this_thread::sleep_for(std::chrono::milliseconds(5));
                    }
                }
            });
        }

        // 10. Demodulation
        // scheme is read by the thread by reference; bind to the cfg_ member (see
        // the note in launch_tx_pipeline). A local + std::ref() dangles once this
        // function returns, which made string_to_mod_type() receive a corrupted
        // scheme string and abort with "Unknown scheme".
        threads_.emplace_back(demodulation_thread,
            std::ref(eq_fifo_), std::ref(rx_bits_fifo),
            std::ref(cfg_.scheme), std::ref(stop_flag_));
    }
};
