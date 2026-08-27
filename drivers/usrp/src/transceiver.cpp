# include "transceiver.hpp"
# include "FIFO.hpp"
# include "messages.hpp"

# include <uhd/utils/safe_main.hpp>  // for UHD_SAFE_MAIN function -> catch UHD exceptions / diagnostic infos / cleanup purpose
# include <uhd/utils/thread.hpp>  // increase the thread's scheduling priority
# include <uhd/usrp/multi_usrp.hpp>  // main UHD class to control a usrp device -< gateway to radio hardware
# include <uhd/exception.hpp>  // handle UHD errors

# include <iostream>
# include <string>
# include <vector>
# include <mutex>
# include <csignal>
# include <thread>
# include <chrono>
# include <fftw3.h>
# include <fstream>
# include <sstream>
# include "viz.hpp"

// ---------------------------------------------------- Helper functions for transceiver -------------------------------------------//
void save_block_to_txt(const std::vector<std::complex<float>>& recv_block,
                       int recv_block_id, std::string role)
{
    // Build filename: "received_<id>.txt"
    std::ostringstream oss;
    if (role == "transmitter"){
        oss << "transmit/" << role << "_" << recv_block_id << ".txt";
    } else if (role == "receiver"){
        oss << "received/" << role << "_" << recv_block_id << ".txt";  
    } else {
        oss << "debugging/" << role << "_" << recv_block_id << ".txt";
    }
        
    std::string filename = oss.str();

    std::ofstream saved_file(filename);
    if (!saved_file.is_open()) {
        std::cerr << "Error: cannot open file " << filename << std::endl;
        return;
    }

    // Write samples as "real imag"
    for (const auto& s : recv_block)
        saved_file << s.real() << " " << s.imag() << "\n";

    saved_file.close();
    std::cout << "[INFO] Saved block " << recv_block_id
              << " with " << recv_block.size()
              << " samples to " << filename << std::endl;
}

void save_bits_to_txt(const std::vector<uint8_t>& bits,
                      int block_id, std::string role)
{
    // Build filename: e.g. "received_<id>.txt" or "debugging_<id>.txt"
    std::ostringstream oss;
    if (role == "transmitter") {
        oss << "transmit/" << role << "_" << block_id << "_bits.txt";
    } else if (role == "receiver") {
        oss << "received/" << role << "_" << block_id << "_bits.txt";
    } else {
        oss << "debugging/" << role << "_" << block_id << "_bits.txt";
    }
    
    std::string filename = oss.str();

    std::ofstream saved_file(filename);
    if (!saved_file.is_open()) {
        std::cerr << "Error: cannot open file " << filename << std::endl;
        return;
    }

    // Write bits, one per line
    for (const auto& b : bits)
        saved_file << static_cast<int>(b) << "\n";

    saved_file.close();

    std::cout << "[INFO] Saved bit block " << block_id
              << " with " << bits.size()
              << " bits to " << filename << std::endl;
}

void compute_instant_energy(size_t num_recv_samples, size_t recv_block_id, std::vector<std::complex<float>>& recv_samples)
{
    float avg_power = 0.0f;
    float max_mag = 0.0f;
    for (size_t i = 0; i < num_recv_samples; i++) {
        float mag = std::abs(recv_samples[i]);
        max_mag = std::max(max_mag, mag);
        avg_power += std::norm(recv_samples[i]);
    }
    avg_power /= num_recv_samples;
    float avg_power_db = 10.0f * std::log10(avg_power + 1e-20f);

    // Short, one-line printout
    std::cout << "[AGC] Block " << recv_block_id << ": Power=" << avg_power_db 
            << " dB, Max=" << max_mag << std::endl;
}

// Clipping: Multiplying all samples with a constant (<1) to meet the requirement of USRP
// Clipping prevent the waveform distortion but increase BER
// The peak of amplitude exceed [-1, 1] caused by RRC filter
void clipping_checking(const std::vector<std::complex<float>>& samples,
                       const std::string& filename = "Clipping_histgoram.txt")
{
    std::vector<float> magnitude;
    magnitude.reserve(samples.size());

    for (const auto& sample : samples){
        magnitude.push_back(std::abs(sample));
    }

    // Find max and min value in a vector by go over the pointers
    float max_val = *std::max_element(magnitude.begin(), magnitude.end());
    float min_val = *std::min_element(magnitude.begin(), magnitude.end());

    // Create histogram bins
    const int num_bins = 100;
    std::vector<int> histogram(num_bins, 0);
    float bin_width = (max_val - min_val) / num_bins;

    // Fill histogram
    for (float mag : magnitude) {
        int bin = static_cast<int>((mag - min_val) / bin_width);
        if (bin >= num_bins) bin = num_bins - 1;
        if (bin < 0) bin = 0;
        histogram[bin]++;
    }

    // Save to file
    std::ofstream outfile(filename);
    outfile << "# Bin_Center Magnityde Count" << std::endl;  // "outfle << ": write a line to outfile
    for (int i = 0; i < num_bins; i++){
        float bin_center = min_val + (i + 0.5f) * bin_width;
        outfile << bin_center << " " << histogram[i] << std::endl;
    }
    outfile.close();

    // Print Statistics
    // std::cout << "\nHistogram Statistics:" << std::endl;
    // std::cout << " Min Magnitude: " << min_val << std::endl;
    // std::cout << " Max Magnitude: " << max_val << std::endl;
    // std::cout << " Total Samples: " << samples.size() << std::endl;

    // Count clipped samples
    int num_clipped = 0;
    for (const auto& sample : samples){
        if (std::abs(sample.real()) > 1.0f || std::abs(sample.imag()) > 1.0f){
            num_clipped++;
        }
    }

    float clipped_rate = 100.0f * num_clipped / samples.size();
    std::cout << " Clipped samples: " << num_clipped << " (" << clipped_rate << "%)" << std::endl;
}

void PSD(const std::vector<std::complex<float>>& samples,
         double sample_rate, 
         int fft_size,
         const std::string& filename = "psd.txt")
{
    int N = samples.size();
    
    std::cout << "[PSD] Calculating PSD..." << std::endl;
    std::cout << "[PSD]   Input samples: " << N << std::endl;
    std::cout << "[PSD]   FFT size: " << fft_size << std::endl;
    std::cout << "[PSD]   Sample rate: " << sample_rate / 1e6 << " MHz" << std::endl;

    if (N < fft_size) {
        std::cerr << "[PSD] ERROR: Not enough samples (" << N 
                  << ") for FFT size (" << fft_size << ")" << std::endl;
        return;
    }

    // Allocate FFT buffers
    fftwf_complex* fft_in = fftwf_alloc_complex(fft_size);
    fftwf_complex* fft_out = fftwf_alloc_complex(fft_size);
    fftwf_plan plan = fftwf_plan_dft_1d(fft_size, fft_in, fft_out, 
                                        FFTW_FORWARD, FFTW_ESTIMATE);

    // Welch's method parameters
    int num_blocks = N / fft_size;
    std::cout << "[PSD]   Number of blocks: " << num_blocks << std::endl;
    
    if (num_blocks == 0) {
        std::cerr << "[PSD] ERROR: No complete blocks!" << std::endl;
        fftwf_destroy_plan(plan);
        fftwf_free(fft_in);
        fftwf_free(fft_out);
        return;
    }

    std::vector<double> psd(fft_size, 0.0);

    // CRITICAL FIX 1: Pre-calculate window and normalization factors
    std::vector<float> window(fft_size);
    double window_power = 0.0;
    
    for (int i = 0; i < fft_size; i++) {
        // Hann window
        window[i] = 0.5f * (1.0f - std::cos(2.0f * M_PI * i / (fft_size - 1)));
        window_power += window[i] * window[i];
    }
    
    std::cout << "[PSD]   Window power sum: " << window_power << std::endl;

    // Process each block
    for (int block = 0; block < num_blocks; block++) {
        // Apply window to block
        for (int i = 0; i < fft_size; i++) {
            int index = block * fft_size + i;
            if (index < N) {
                fft_in[i][0] = samples[index].real() * window[i];
                fft_in[i][1] = samples[index].imag() * window[i];
            } else {
                fft_in[i][0] = 0.0f;
                fft_in[i][1] = 0.0f;
            }
        }

        // Execute FFT
        fftwf_execute(plan);

        // Accumulate power
        for (int i = 0; i < fft_size; i++) {
            double power = fft_out[i][0] * fft_out[i][0] + 
                          fft_out[i][1] * fft_out[i][1];
            psd[i] += power;
        }
    }

    // CRITICAL FIX 2: Proper normalization
    // The PSD needs to be normalized by:
    // 1. Number of blocks (averaging)
    // 2. FFT size squared (FFT scaling)
    // 3. Window power (window correction)
    // 4. Sample rate (to get power spectral DENSITY in power/Hz)
    
    double scale_factor = 1.0 / (window_power * sample_rate);
    
    std::cout << "[PSD]   Scale factor: " << scale_factor << std::endl;

    // Find max PSD for normalization to 0 dB
    double max_psd = 0.0;
    for (int i = 0; i < fft_size; i++) {
        psd[i] *= scale_factor;
        if (psd[i] > max_psd) {
            max_psd = psd[i];
        }
    }
    
    std::cout << "[PSD]   Max PSD (linear): " << max_psd << std::endl;
    
    if (max_psd <= 0.0) {
        std::cerr << "[PSD] ERROR: Max PSD is zero or negative!" << std::endl;
        fftwf_destroy_plan(plan);
        fftwf_free(fft_in);
        fftwf_free(fft_out);
        return;
    }

    // CRITICAL FIX 3: Normalize to 0 dB at peak and convert to dB
    for (int i = 0; i < fft_size; i++) {
        psd[i] = psd[i] / max_psd;  // Normalize so peak = 1.0
        psd[i] = 10.0 * std::log10(psd[i] + 1e-20);  // Convert to dB
    }

    // Write to file
    std::ofstream outfile(filename);
    if (!outfile.is_open()) {
        std::cerr << "[PSD] ERROR: Could not open file " << filename << std::endl;
        fftwf_destroy_plan(plan);
        fftwf_free(fft_in);
        fftwf_free(fft_out);
        return;
    }
    
    outfile << "# Frequency (MHz) PSD (dB)" << std::endl;
    
    double frequency_resolution = sample_rate / fft_size;
    std::cout << "[PSD]   Freq resolution: " << frequency_resolution / 1e3 
              << " kHz" << std::endl;

    if (frequency_resolution <= 0.0) {
        std::cerr << "[PSD] ERROR: Invalid frequency resolution!" << std::endl;
        outfile.close();
        fftwf_destroy_plan(plan);
        fftwf_free(fft_in);
        fftwf_free(fft_out);
        return;
    }

    // CRITICAL FIX 4: Correct frequency ordering for baseband
    // Write negative frequencies first (from -Fs/2 to 0)
    for (int i = fft_size / 2; i < fft_size; i++) {
        double freq = (i - fft_size) * frequency_resolution;
        outfile << freq / 1e6 << " " << psd[i] << std::endl;
    }
    
    // Then positive frequencies (from 0 to Fs/2)
    for (int i = 0; i < fft_size / 2; i++) {
        double freq = i * frequency_resolution;
        outfile << freq / 1e6 << " " << psd[i] << std::endl;
    }
    
    outfile.close();

    std::cout << "[PSD] ✓ PSD saved to: " << filename << std::endl;
    std::cout << "[PSD]   Frequency range: " 
              << -sample_rate/2e6 << " to " << sample_rate/2e6 << " MHz" << std::endl;

    // Cleanup
    fftwf_destroy_plan(plan);
    fftwf_free(fft_in);
    fftwf_free(fft_out);
}

// Alternative: Simpler PSD with better defaults
void PSD_Simple(const std::vector<std::complex<float>>& samples,
                double sample_rate,
                const std::string& filename = "psd.txt")
{
    // Auto-select FFT size (power of 2, reasonable for signal length)
    int N = samples.size();
    int fft_size = 1024;
    
    // Adjust FFT size based on signal length
    if (N < 1024) {
        fft_size = 256;
    } else if (N > 8192) {
        fft_size = 2048;
    }
    
    std::cout << "[PSD_Simple] Using FFT size: " << fft_size << std::endl;
    
    PSD(samples, sample_rate, fft_size, filename);
}

float calculate_rms(const std::vector<std::complex<float>>& samples){
        if (samples.empty()) {return 1e-10f;}

        float power = 0.0f;
        for (const auto& sample : samples){
            float mag = std::abs(sample);
            power += mag * mag;
        }
        power /= samples.size();
        
        return std::sqrt(power);
}

// --------------------------------------------- THREADS for transmitter and / or receiver ---------------------------------------------//
bool validate_tx_samples(const std::vector<std::complex<float>>& samples,
                        size_t block_id)
{
    if (samples.empty()) {
        std::cerr << "[TX VALIDATE] ERROR: Block " << block_id << " is empty!" << std::endl;
        return false;
    }

    // Check for invalid values (NaN, Inf)
    size_t invalid_count = 0;
    for (size_t i = 0; i < samples.size(); i++) {
        if (!std::isfinite(samples[i].real()) || !std::isfinite(samples[i].imag())) {
            if (invalid_count < 5) {  // Only print first 5
                std::cerr << "[TX VALIDATE] ERROR: Invalid sample at index " << i 
                          << ": (" << samples[i].real() << ", " 
                          << samples[i].imag() << ")" << std::endl;
            }
            invalid_count++;
        }
    }
    
    if (invalid_count > 0) {
        std::cerr << "[TX VALIDATE] ERROR: Block " << block_id 
                  << " has " << invalid_count << " invalid samples!" << std::endl;
        return false;
    }

    // Calculate statistics
    float max_mag = 0.0f;
    float avg_power = 0.0f;
    for (const auto& s : samples) {
        float mag = std::abs(s);
        max_mag = std::max(max_mag, mag);
        avg_power += std::norm(s);
    }
    avg_power /= samples.size();
    float rms = std::sqrt(avg_power);

    std::cout << "[TX VALIDATE] Block " << block_id << ":" << std::endl;
    std::cout << "  Samples: " << samples.size() << std::endl;
    std::cout << "  Max magnitude: " << max_mag << std::endl;
    std::cout << "  RMS: " << rms << std::endl;

    // Sanity checks
    // fc32 full scale is 1.0: anything above it is hard-clipped by the DAC, not
    // merely "large". The old threshold of 5.0 let 25% overshoot pass silently.
    if (max_mag > 1.0f) {
        std::cerr << "[TX VALIDATE] WARNING: Block " << block_id << " peaks at "
                  << max_mag << " (> 1.0 full scale) — the DAC is CLIPPING. "
                     "Back off with --tx-scale " << (0.9f / max_mag)
                  << " or lower." << std::endl;
    }
    if (max_mag < 0.001f) {
        std::cerr << "[TX VALIDATE] WARNING: Very small magnitude (" << max_mag 
                  << ")! Signal may be too weak!" << std::endl;
        return false;
    }

    return true;
}

void transmit_thread(uhd::usrp::multi_usrp::sptr usrp,
                     MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& filtered_fifo,
                     double tx_rate, std::vector<unsigned long> channel, double UHD_timeout,
                     std::atomic<bool>& stop_sign, float tx_scale)
{
    uhd::set_thread_priority_safe(1.0, true);

    // create transmit streamer
    uhd::stream_args_t stream_args("fc32", "sc16");  // (cpu_format, wire_format) -> can be change if needed
    stream_args.channels = channel;
    uhd::tx_streamer::sptr tx_stream = usrp->get_tx_stream(stream_args);

    // Set stream parameters
    size_t samps_per_buff = tx_stream->get_max_num_samps();

    std::cout << "[USRP TX] Configuration: " << "TX rate = " << tx_rate/1e6 << " MHz, " 
              << " Sampes per buffer = " << samps_per_buff << std::endl;

    // Initial the Metadata for transmission
    uhd::tx_metadata_t md;
    md.start_of_burst = false;
    md.end_of_burst = false;
    md.has_time_spec = false;  // start based on the device clock if true

    std::pair<size_t, std::vector<std::complex<float>>> message;
    size_t total_transmitted = 0;
    size_t total_blocks = 0;
    bool first_transmission = true;
    size_t idle_count = 0;
    size_t consecutive_errors = 0;
    const size_t MAX_CONSECUTIVE_ERRORS = 10;

    DrainGate gate;
    while (gate.keep_going(stop_sign, filtered_fifo)){

        if (!filtered_fifo.pop(message)){

            idle_count++;
            //  // Print waiting message periodically
            // if (idle_count == 1 || idle_count % 100 == 0) {
            //     std::cout << "[USRP TX] Waiting for data (FIFO empty, idle: " 
            //               << idle_count << ")" << std::endl;
            // }
            

            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            continue;
        }

        idle_count = 0;
        size_t block_id = message.first;
        // Digital back-off before the DAC. The single-carrier chain has no
        // amplitude control of its own (unlike --ofdm-tx-peak / --tone-amp), so a
        // pulse-shaped burst can overshoot full scale and be hard-clipped by the
        // converter. Clipping distorts the constellation while leaving the strong
        // preamble correlating perfectly -- sync locks, the payload decodes to
        // garbage. Default 1.0 keeps the previous behaviour exactly.
        if (tx_scale != 1.0f) {
            for (auto& s : message.second) s *= tx_scale;
        }
        // Clip guard. fc32 full scale is 1.0; anything beyond it is hard-clipped
        // by the DAC, which distorts the constellation while leaving the strong
        // periodic preamble correlating perfectly -- sync locks and the payload
        // decodes to garbage. This only ever scales a block DOWN, and only when it
        // would otherwise clip, so a compliant signal passes through untouched.
        {
            float peak = 0.0f;
            for (const auto& s : message.second) peak = std::max(peak, std::abs(s));
            if (peak > 1.0f) {
                const float g = 0.9f / peak;
                for (auto& s : message.second) s *= g;
                static std::atomic<int> warned{0};
                if (warned.fetch_add(1) < 3)
                    std::cout << "[USRP TX] clip guard: block #" << block_id
                              << " peaked at " << peak
                              << " (> 1.0 full scale) — scaled by " << g
                              << " to keep the DAC out of clipping. Set --tx-scale "
                              << (0.9f / peak) << " to do this up front."
                              << std::endl;
            }
        }
        const std::vector<std::complex<float>>& samples = message.second;
        // save_block_to_txt(samples, 0, "transmit");

        std::cout << "\n[USRP TX] Processing Block #" << block_id << std::endl;

        // Validate samples
        if (!validate_tx_samples(samples, block_id)) {
            std::cerr << "[USRP TX] Skipping invalid block #" << block_id << std::endl;
            consecutive_errors++;
            if (consecutive_errors >= MAX_CONSECUTIVE_ERRORS) {
                std::cerr << "[USRP TX] ERROR: Too many consecutive errors (" 
                          << consecutive_errors << "), stopping!" << std::endl;
                break;
            }
            continue;
        }

        consecutive_errors = 0;

        // Set timing for first transmission
        if (first_transmission) {
            md.start_of_burst = true;
            md.has_time_spec = true;
            md.end_of_burst = false;
            
            // Schedule transmission to start 0.5 seconds from now
            uhd::time_spec_t now = usrp->get_time_now();
            md.time_spec = now + uhd::time_spec_t(0.5);
            
            std::cout << "[USRP TX] First transmission scheduled:" << std::endl;
            std::cout << "  Current time: " << now.get_real_secs() << " s" << std::endl;
            std::cout << "  Start time: " << md.time_spec.get_real_secs() << " s" << std::endl;
            std::cout << "  Delay: 0.5 s" << std::endl;
            
            first_transmission = false;
        } else {
            md.start_of_burst = false;
            md.has_time_spec = false;
        }

        // Transmit in chunks if needed
        size_t samples_sent = 0;
        size_t chunks_sent = 0;
        const std::complex<float>* buff_ptr = samples.data();
        bool transmission_failed = false;

        // // In your TX transmission loop
        // std::cout << "========== TX DEBUG ==========" << std::endl;
        // std::cout << "[TX] Samples per packet: " << samples.size() << std::endl;
        // std::cout << "[TX] Time per packet: " << (samples.size() / tx_rate * 1000) << " ms" << std::endl;

        // Check timing between packet transmissions
        // auto tx_start = std::chrono::high_resolution_clock::now();

        while (samples_sent < samples.size() && !stop_sign && !transmission_failed){
            size_t samples_remaning = samples.size() - samples_sent;
            size_t samples_to_send = std::min(samps_per_buff, samples_remaning);

            try{
                size_t num_sent_samples = tx_stream->send(buff_ptr + samples_sent, 
                                                      samples_to_send, md, 
                                                      UHD_timeout);  // UHD_timeout in second is the maximum wait time for UHD to finish transferring

                if (num_sent_samples == 0){
                    std::cerr << "[USRP TX] ERROR: send() returned 0 (timeout or error)!" 
                              << std::endl;
                    transmission_failed = true;
                    break;
                }

                if (num_sent_samples != samples_to_send) {
                    std::cerr << "[USRP TX] WARNING: Partial send - sent " << num_sent_samples 
                              << " of " << samples_to_send << " samples" << std::endl;
                }

                samples_sent += num_sent_samples;
                chunks_sent++;

                if (md.start_of_burst) {
                    md.start_of_burst = false;
                    md.has_time_spec = false;
                }
            } catch (const std::exception& e) {
                std::cerr << "[USRP TX] Exception during send: " << e.what() << std::endl;
                transmission_failed = true;
                break;
            }
        }

        // auto tx_end = std::chrono::high_resolution_clock::now();
        // std::chrono::duration<double> tx_duration = tx_end - tx_start;

        // std::cout << "[TX] Transmission took: " << tx_duration.count() * 1000 << " ms" << std::endl;
        // std::cout << "[TX] Expected duration: " << (samples.size() / tx_rate * 1000) << " ms" << std::endl;
        
        md.start_of_burst = false;
        md.has_time_spec = false;
        md.end_of_burst = true;
        tx_stream->send("", 0, md, UHD_timeout);

        total_transmitted += samples_sent;
        total_blocks++;
    }

    // Send end-of-burst signal
    std::cout << "\n[USRP TX] Sending end-of-burst..." << std::endl;
    md.end_of_burst = true;
    md.start_of_burst = false;
    md.has_time_spec = false;
    
    try {
        tx_stream->send("", 0, md, UHD_timeout);
        std::cout << "[USRP TX] End-of-burst sent successfully" << std::endl;
    } catch (const std::exception& e) {
        std::cerr << "[USRP TX] Error sending end-of-burst: " << e.what() << std::endl;
    }

    // Wait for transmission to complete and check for errors
    std::cout << "[USRP TX] Waiting for transmission to complete..." << std::endl;
    std::this_thread::sleep_for(std::chrono::milliseconds(500));

    // Final statistics
    std::cout << "\n" << std::string(60, '=') << std::endl;
    std::cout << "[USRP TX] Transmission Complete" << std::endl;
    std::cout << std::string(60, '=') << std::endl;
    std::cout << "Statistics:" << std::endl;
    std::cout << "  Total blocks: " << total_blocks << std::endl;
    std::cout << "  Total samples: " << total_transmitted << std::endl;
}

void receive_thread(uhd::usrp::multi_usrp::sptr usrp,
                    std::vector<unsigned long> channel, double rx_rate, double setting_time,
                    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& recv_fifo,
                    int num_requested_samples, size_t samps_per_buff, std::atomic<bool>& stop_sign)
{
    uhd::set_thread_priority_safe(1.0, true);
    
    uhd::stream_args_t stream_args("fc32", "sc16");  // (cpu format, wire format)
    stream_args.channels = channel;
    uhd::rx_streamer::sptr rx_stream = usrp->get_rx_stream(stream_args);
    uhd::rx_metadata_t md;
    
    // Prepare buffers for received samples
    std::vector<std::vector<std::complex<float>>> buffs(channel.size(),
                                          std::vector<std::complex<float>>(samps_per_buff));

    // Create a vector of pointers to point to each of the channel buffers
    std::vector<std::complex<float>*> buff_ptrs;
    for (size_t i = 0; i < buffs.size(); i++){
        buff_ptrs.push_back(&buffs[i].front());  // put two buffers into one vector
    }

    // Check the number of buffers equals to the numver of active receive channels
    UHD_ASSERT_THROW(buffs.size() == channel.size());

    bool overflow_message = true;
    size_t num_total_recv_samps = 0;  // record the number of received samples
    size_t recv_block_id = 0;
    size_t overflow_count = 0;
    size_t timeout_count = 0;

    double initial_timeout = 3.0;
    double timeout = initial_timeout;

    uhd::stream_cmd_t stream_cmd(((num_requested_samples == 0)  // N_samples * U
                                   ? uhd::stream_cmd_t::STREAM_MODE_START_CONTINUOUS  // continues 
                                   : uhd::stream_cmd_t::STREAM_MODE_NUM_SAMPS_AND_DONE));  // burst mode
    
    stream_cmd.num_samps = (num_requested_samples == 0) ? 0 : num_requested_samples;
    stream_cmd.stream_now = false;
    // Setting_time ensures the hardware to stablize
    stream_cmd.time_spec = usrp->get_time_now() + uhd::time_spec_t(setting_time); 
    rx_stream->issue_stream_cmd(stream_cmd);

    size_t blocks_received = 0;
    auto last_print = std::chrono::steady_clock::now();

    while (!stop_sign && (num_requested_samples > num_total_recv_samps || num_requested_samples == 0)){
        // get receive samples from receive buffer and push them to host buffer (buff_ptrs)
        size_t num_recv_samples = rx_stream->recv(buff_ptrs, samps_per_buff, md, timeout);

        if (timeout == initial_timeout && num_recv_samples > 0) {
            timeout = 0.1;
            std::cout << "[USRP RX] First samples received, reducing timeout to " 
                      << timeout << " s" << std::endl;
        }

        if (md.error_code == uhd::rx_metadata_t::ERROR_CODE_TIMEOUT) {
            timeout_count++;
            
            if (timeout_count == 1) {
                std::cerr << "[USRP RX] TIMEOUT waiting for samples!" << std::endl;
                std::cerr << "  Possible causes:" << std::endl;
                std::cerr << "    - No signal present (check TX is running)" << std::endl;
                std::cerr << "    - Frequency mismatch (TX freq ≠ RX freq)" << std::endl;
                std::cerr << "    - Gain too low (try increasing RX gain)" << std::endl;
                std::cerr << "    - Hardware issue" << std::endl;
            }
            
            if (timeout_count >= 10) {
                std::cerr << "[USRP RX] Too many timeouts (" << timeout_count 
                          << "), stopping..." << std::endl;
                break;
            }
            continue;
        }

        if (md.error_code == uhd::rx_metadata_t::ERROR_CODE_OVERFLOW) {
            overflow_count++;
            if (overflow_message) {
                overflow_message = false;
                std::cerr << "[USRP RX] OVERFLOW detected!" << std::endl;
                std::cerr << "  Samples being dropped!" << std::endl;
                std::cerr << "  Solutions:" << std::endl;
                std::cerr << "    - Reduce RX rate" << std::endl;
                std::cerr << "    - Increase buffer size" << std::endl;
                std::cerr << "    - Process samples faster" << std::endl;
            }
            continue;
        }

        if (md.error_code != uhd::rx_metadata_t::ERROR_CODE_NONE) {
            std::cerr << "[USRP RX] ERROR: " << md.strerror() << std::endl;
            throw std::runtime_error("[USRP RX] Receiver error: " + md.strerror());
        }

        // CRITICAL FIX 5: Validate received samples
        if (num_recv_samples == 0) {
            std::cerr << "[USRP RX] WARNING: Received 0 samples (no error code)" << std::endl;
            continue;
        }

        timeout_count = 0;
        num_total_recv_samps += num_recv_samples;
        blocks_received++;

        for (size_t i = 0; i < buff_ptrs.size(); i++) {  
            // 2-D vector with only one vector inside if only using one antenna port for receiving
            // Every time called recv() function, the buff_ptrs will be overwritten by new coming samples
            // Therefore, the pointer is always the same.
            std::vector<std::complex<float>> recv_block(buff_ptrs[i], buff_ptrs[i] + num_recv_samples);
            recv_fifo.push({recv_block_id, recv_block});  // same as std::make_pair()
            // save_block_to_txt(recv_block, recv_block_id, "RX");
            // compute_instant_energy(recv_block.size(), recv_block_id, recv_block);
            // std::cout << "[RECEIVER] The receiver's FIFO size is " << recv_fifo.size() << std::endl;
        }
        recv_block_id++;
    }

    // Stop streaming
    std::cout << "\n[USRP RX] Stopping stream..." << std::endl;
    uhd::stream_cmd_t stop_cmd(uhd::stream_cmd_t::STREAM_MODE_STOP_CONTINUOUS);
    stop_cmd.stream_now = true; // Excute the command immediately on the device's internal time clock
    rx_stream->issue_stream_cmd(stop_cmd);

    // Drain any remaining samples
    std::cout << "[USRP RX] Draining receive buffers..." << std::endl;
    size_t drained = 0;
    // Bounded drain: the stream is already stopped, so the on-device backlog
    // clears in a handful of blocks. Cap iterations so a still-delivering radio
    // can never wedge shutdown (a single Ctrl-C must always tear down cleanly).
    while (drained < 2000 && rx_stream->recv(buff_ptrs, samps_per_buff, md, 0.1) > 0) {
        drained++;
    }
    if (drained > 0) {
        std::cout << "[USRP RX] Drained " << drained << " additional blocks" << std::endl;
    }
}

void EnergyDetection_thread(MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& input_fifo,
                            MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& output_fifo,
                            EnergyDetectorIIR& detector, std::atomic<bool>& stop_sign)
{
    size_t wait_times = 0;
    size_t pushed_packets = 0;
    std::pair<size_t, std::vector<std::complex<float>>> message;

    DrainGate gate;
    while (gate.keep_going(stop_sign, input_fifo)){
        // std::cout << "[DETECTOR] Input FIFO size: " << input_fifo.size() << std::endl;
        if (!input_fifo.pop(message)){
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            wait_times++;
            continue;
        }
        // std::cout << "[DETECTOR] Input FIFO size: " << input_fifo.size() << std::endl;
        std::vector<std::complex<float>> symbols = message.second;
        std::vector<std::complex<float>> output;
        // compute_instant_energy(symbols.size(), pushed_packets, symbols);
        if (detector.process(symbols, output)){

            std::cout << "[DETECTION THREAD] Packet detected in block #" << message.first << std::endl;

            output_fifo.push({message.first, output});
            pushed_packets++;
            // save_block_to_txt(output, message.first, "detected");

            float rms=0, peak=0;
            for (auto& s : output) {
                float mag = std::abs(s);
                rms += mag*mag;
                peak = std::max(peak, mag);
            }
            rms = std::sqrt(rms / output.size());
            std::cout << "[AFTER ENERGY DETECTION] Stage ENERGY DETECTION: Number= " << pushed_packets << " RMS=" << rms << " Peak=" << peak << std::endl;

        }
        // std::cout << "[DETECTOR] Output FIFO size: " << output_fifo.size() << std::endl;
    }
    std::cout << "[DETECTION THREAD] Stopped" << std::endl;
}

void AGC_thread(MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& received_fifo,
                MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& agc_fifo,
                std::atomic<bool>& stop_sign, const std::string& AGC,
                bool dc_block)
{
    // Setup the AGC
    FeedforwardAGC FF_AGC(1.0f);  // target_rms
    ClosedLoopAGC CL_AGC(1.0f, 0.1f, 0.01f);  // target_rms, kp, ki
    size_t tried_time = 0;

    std::pair<size_t, std::vector<std::complex<float>>> message;

    DrainGate gate;
    while (gate.keep_going(stop_sign, received_fifo)){
        // std::cout << "[AGC] The input FIFO size: " << received_fifo.size() << std::endl;

        if (!received_fifo.pop(message)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            tried_time++;
            continue;
        }

        // ── DC-block high-pass (per burst) ────────────────────────────────
        // On a direct cable the TX carrier/LO leakage couples into the RX as a
        // strong tone near DC (it beats at the free-running CFO, a few hundred Hz
        // to ~1.5 kHz). Left in, it dominates the AGC and corrupts the CFO
        // estimate, smearing dense QAM into rings/blobs (QPSK survives, 16-QAM+
        // does not). A static mean subtraction can't remove a *tone*, so use a
        // first-order DC-blocker  y[n] = x[n] - x[n-1] + a*y[n-1]  (a=0.98,
        // ~5 kHz cutoff): it kills everything below ~5 kHz (the leakage) while
        // passing the signal — single-carrier is broadband and OFDM's lowest
        // data subcarrier sits at 25 kHz (fs/N = 1.6 MHz/64), so the loss is
        // negligible. The DC subcarrier is already nulled.
        if (dc_block) {
            auto& v = message.second;
            const float a = 0.999f;
            std::complex<float> xprev(0.f, 0.f), yprev(0.f, 0.f);
            for (auto& s : v) {
                std::complex<float> x = s;
                std::complex<float> y = x - xprev + a * yprev;
                xprev = x; yprev = y;
                s = y;
            }
        }
        // compute_instant_energy(message.second.size(), message.first, message.second);
        if (AGC == "Feed"){
            // std::cout << "[AGC] FeedFoward AGC applied!"<< std::endl;
            std::vector<std::complex<float>> agc_message = FF_AGC.process(message.second);
            viz::capture("rx_wave", agc_message, 2000);   // RX burst waveform
            agc_fifo.push({message.first, agc_message});

            float rms=0, peak=0;
            for (auto& s : agc_message) {
                float mag = std::abs(s);
                rms += mag*mag;
                peak = std::max(peak, mag);
            }
            rms = std::sqrt(rms / agc_message.size());
            // std::cout << "[CHECK AFTER AGC] Stage AGC: RMS=" << rms << " Peak=" << peak << std::endl;

            // std::cout << std::endl;
        }
        else if (AGC == "Closed"){
            // std::cout << "[AGC] ClosedLoop AGC applied!" << std::endl;
            std::vector<std::complex<float>> agc_message = CL_AGC.process(message.second);
            agc_fifo.push({message.first, agc_message});
        } 
        else {
            std::cerr << "[AGC] Unrecognized AGC type";
        }
        // std::cout << "[AGC] The AGCed FIFO size is " << agc_fifo.size() << std::endl;
    }
}
