#pragma once
// The header for USRP operation threads
# include "FIFO.hpp"

# include <vector>
# include <fstream>
# include <deque>
# include <mutex>
# include <uhd/usrp/multi_usrp.hpp>

//-------------------------------------------------- AGC classes include FeedForward and ClosedLoop AGC --------------------------------------------//
class FeedforwardAGC{

private:
    float target_rms;
    float min_gain;
    float max_gain;
    float last_gain;  // store the last gain for debugging

public:
    FeedforwardAGC(float target_rms = 1.0f, float min_gain = 0.01f, float max_gain = 100.0f)
        : target_rms(target_rms), min_gain(min_gain), max_gain(max_gain), last_gain(1.0f){}
    
    float calculate_rms(const std::vector<std::complex<float>>& samples){
        if (samples.empty()){
            return 1e-10f;
        }

        float power = 0.0f;
        for (const auto& sample : samples){
            // float mag = std::abs(sample);
            // power += mag * mag;
            power += std::norm(sample);
        }
        power /= samples.size();

        return std::sqrt(power);
    }

    std::vector<std::complex<float>> process(std::vector<std::complex<float>>& received_message){
        
        float rms = calculate_rms(received_message);

        // Gain = target_RMS / Measured_RMS
        float gain = (rms > 1e-10f) ? target_rms / rms : 1.0f;
        gain = std::max(min_gain, std::min(max_gain, gain));
        last_gain = gain;
        // float gain = 1.0;

        std::vector<std::complex<float>> output(received_message.size());
        for (size_t i = 0; i < received_message.size(); i++){
            output[i] = gain * received_message[i];
        }
        
        float rms_agc = calculate_rms(output);

        //Print for debugging
        // std::cout << "[FEEDFORWARD AGC] Received message's RMS: " << rms << " | Gain: " << gain << " | Output size: " << output.size() << std::endl;
        std::cout << "[FEEDFORWARD AGC] Received message's RMS: " << rms << " | Gain: " << gain << " | Output RMS: " << rms_agc << std::endl;

        return output;
    }
    
    void set_target_rms(float target) {target_rms = target;}
    float get_last_gain() const {return last_gain;}
};

class ClosedLoopAGC{

private:
    float target_rms;
    float kp;  // Proportional gain (0 to 1, e.g. 0.05-0.2) -> how fast response to errors
    float ki;  // Integral gain (0 to 1, e.g. 0.01-0.1) -> corrects steady-state error (response to the historical errors)
    float error_integral;  // Accumulate the integral of errors
    float min_gain;
    float max_gain;
    float current_gain;  // current overall gain
    float last_error;

public:
    ClosedLoopAGC(float target_rms = 1.0f, float kp = 0.1f, float ki = 0.01f, float min_gain = 0.01f, float max_gain = 100.0f)
        : target_rms(target_rms), kp(kp), ki(ki), min_gain(min_gain), max_gain(max_gain), current_gain(1.0f), last_error(0.0f) {}
    
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

    std::vector<std::complex<float>> process(std::vector<std::complex<float>>& received_message) {
        
        float rms = calculate_rms(received_message);
        float error = target_rms - rms;
        last_error = error;

        float proportional_adjustment = kp * error;
        error_integral *= ki * error;
        error_integral = std::max(-1.0f, std::min(1.0f, error_integral));

        // Gain[n+1] = Gain[n] + kp * error + ki * error_history
        current_gain = current_gain + proportional_adjustment + error_integral;
        current_gain = std::max(min_gain, std::max(max_gain, current_gain));

        std::vector<std::complex<float>> output(received_message.size());

        for (int i = 0; i < received_message.size(); i++){
            output[i] = current_gain * received_message[i];
        }

        //  std::cout << "[Closed-Loop AGC] Measured RMS: " << rms 
        //           << " | Error: " << error
        //           << " | Gain: " << current_gain 
        //           << " | Output size: " << output.size() << std::endl;
        
        return output;
    }

    void reset(){
        error_integral = 0.0;
        current_gain = 1.0f;
        last_error = 0.0f;
    }

    void set_target_rms (float target_rms) {target_rms = target_rms;}
    void set_kp (float kp) {kp = kp;}
    void set_ki (float ki) {ki = ki;}

    float get_current_gain() const {return current_gain;}
    float get_last_error() const {return last_error;}
    float get_error_integral() const {return error_integral;}
};

//--------------------------------------------------------- Energy Detection classe--------------------------------------------------------//
class EnergyDetectorIIR{

private:
    // IIR filter parameters
    float alpha;
    float filtered_energy;
    float threshold;

    // Adaptive choice
    bool use_adaptive_threshold;
    float noise_floor;
    float threshold_multiplier;
    std::vector<float> noise_samples;
    size_t noise_samples_max;
    bool noise_floor_calibrated;
    bool continuous_track_;   // keep tracking the noise floor during IDLE

    // Sliding window for sample-level enery -> use to control the false-alarm probability
    std::vector<std::complex<float>> window_samples;
    std::vector<std::complex<float>> pre_window_samples;
    size_t window_size;

    enum State{
        IDLE,
        COLLECTING,
    };
    State state;

    // Current packet for collecting
    std::vector<std::complex<float>> current_packet;
    size_t packet_size;
    size_t collected_samples;
    size_t post_samples;
    
    // Track previous energy for edge detection
    float pre_filtered_enerrgy;
    bool was_below_threshold;
    int guard_window_num = 2;
    int num_guard_samples = 100; 

public:
    EnergyDetectorIIR(float alpha = 0.1f, float threshold = 0.5f,
                      size_t packet_size = 10000, size_t window_size = 100,
                      bool adaptive = true, float threshold_mult = 5.0f,
                      bool continuous_track = true)
        : alpha(alpha), filtered_energy(0.0f), threshold(threshold), window_size(window_size), use_adaptive_threshold(adaptive),
          noise_floor(0.0f), threshold_multiplier(threshold_mult), noise_samples_max(10000), noise_floor_calibrated(false),
          continuous_track_(continuous_track),
          state(IDLE), packet_size(packet_size), collected_samples(0), post_samples(0), pre_filtered_enerrgy(0.0f), was_below_threshold(true){

            current_packet.reserve(packet_size);
            window_samples.reserve(window_size);
            pre_window_samples.reserve(num_guard_samples);
            noise_samples.reserve(noise_samples_max);
        }

    // Calculate sliding window energy
    float calculate_window_energy(const std::vector<std::complex<float>>& window){
        if (window.empty()){return 0.0f;}

        float energy_sum = 0.0f;
        for (const auto& sample : window){
            float mag = std::abs(sample);
            energy_sum += mag * mag;
        }

        return energy_sum / window.size();  // average window energy
    }

    // Apply IIR filter to compute the energy
    float IIR_filter(float current_energy){
        filtered_energy = (1 - alpha) * current_energy + alpha * filtered_energy;
        return filtered_energy;
    }

    void update_noise_floor(float energy){
        if (state != IDLE) return;

        // Collect noise samples
        if (!noise_floor_calibrated && noise_samples.size() < noise_samples_max){
            noise_samples.push_back(energy);

            if (noise_samples.size() == noise_samples_max){
                // Calculate noise floor (choose the median value for robustness)
                std::sort(noise_samples.begin(), noise_samples.end());
                noise_floor = noise_samples[noise_samples_max / 2];

                // Set adaptive threshold
                threshold = noise_floor * threshold_multiplier;
                noise_floor_calibrated = true;

                // Print out for debugging
                std::cout << "[DETECTOR CALIBRATION] Complete!" << std::endl;
                std::cout << "  Noise floor: " << noise_floor << std::endl;
                std::cout << "  Adaptive threshold: " << threshold << std::endl;
                std::cout << "  Threshold (dB): " << 10*log10(threshold + 1e-20) << " dB" << std::endl;
            } else if (noise_samples.size() % 100 == 0){
                // std::cout << "[DETECTOR CALIBRATION] Progress: " << noise_samples.size() 
                //           << "/" << noise_samples_max << std::endl;
            }
        }
    }

    // Edge detection
    bool detect_energy_rising(float filtered_energy){
        return was_below_threshold && (filtered_energy > threshold);
    }

    bool detect_energy_failing(float filtered_energy){
        return !was_below_threshold && (filtered_energy <= threshold);
    }

    // main process function
    bool process(std::vector<std::complex<float>>& input_vector,
                 std::vector<std::complex<float>>& output_vector){
        
        output_vector.clear();
        if (input_vector.empty()){return false;}

        for (size_t sample_index = 0; sample_index < input_vector.size(); sample_index++){
            
            std::complex<float> sample = input_vector[sample_index];
            
            // window_samples.push_back(sample);
            // // process untill have enough samples for one window and keep the window size constant
            // if (window_samples.size() > window_size){
            //     window_samples.erase(window_samples.begin());
            // }
            // if (window_samples.size() < window_size){continue;}

            // float window_energy = calculate_window_energy(window_samples);
            // float current_filtered_energy = IIR_filter(window_energy);

            float current_filtered_energy = IIR_filter(std::pow(std::abs(sample), 2.0));

            // record the previous samples before the energy rised
            pre_window_samples.push_back(sample);

            if (pre_window_samples.size() > num_guard_samples){
                pre_window_samples.erase(pre_window_samples.begin());
            }
            
            if (use_adaptive_threshold && !noise_floor_calibrated){
                update_noise_floor(current_filtered_energy);
                was_below_threshold = (current_filtered_energy <= threshold);
                pre_filtered_enerrgy = current_filtered_energy;
                continue;
            }

            // Continuous noise-floor tracking: after the initial calibration, keep
            // updating the floor from NOISE-like samples (below threshold, and only
            // while idle) so the threshold follows a drifting ambient level. This
            // fixes the one-shot calibration failing for the whole run when the
            // calibration window happened to be unusually quiet/noisy.
            if (use_adaptive_threshold && continuous_track_ &&
                state == IDLE && current_filtered_energy < threshold) {
                const float beta = 0.003f;               // slow EMA
                noise_floor = (1.0f - beta) * noise_floor + beta * current_filtered_energy;
                threshold   = noise_floor * threshold_multiplier;
            }

            // Detect the rising and failing energy edge
            if (state == IDLE && detect_energy_rising(current_filtered_energy)){
                // std::cout << "[DETECTION] Rising edge at sample: " << sample_index << " | Energy: " << current_filtered_energy << " | Threshold: " << threshold << std::endl;

                state = COLLECTING;
                current_packet.clear();
                collected_samples = 0;
                post_samples = 0;

                current_packet.insert(current_packet.end(), pre_window_samples.begin(), pre_window_samples.end());
                current_packet.push_back(sample);
                collected_samples++;
                pre_window_samples.clear();
                // std::cout << "[COLLECTING] Start collection at " << sample_index << " with energy " << current_filtered_energy << std::endl;
            }
            else if (state == COLLECTING && current_filtered_energy > threshold){
                current_packet.push_back(sample);
                collected_samples++;
                
                // // This can be removed or chose a large value for the packet_size to capture the redundent samples
                // if (collected_samples >= packet_size){
                //     // std::cout << "[COLLECTING] Complete collection at sample " << sample_index << " | Collected " << collected_samples << std::endl;

                //     // // Calculate RMS for debugging
                //     // float rms = 0;
                //     // for (const auto& s : current_packet) {
                //     //     rms += std::norm(s);
                //     // }
                //     // rms = std::sqrt(rms / current_packet.size());
                //     // std::cout << "[DETECTION] Packet READY! Size: " << current_packet.size() 
                //     //           << " samples, RMS: " << rms << std::endl;

                //     output_vector = current_packet;

                //     // Reset the collecting parameters
                //     state = IDLE;
                //     current_packet.clear();
                //     collected_samples = 0;

                //     was_below_threshold = (current_filtered_energy <= threshold);
                //     pre_filtered_enerrgy = current_filtered_energy;
                //     return true;
                // }
            }
            else if (state == COLLECTING && current_filtered_energy <= threshold){
                current_packet.push_back(sample);
                collected_samples++;

                if (post_samples < num_guard_samples){
                    post_samples++;
                } else {
                    if (collected_samples >= packet_size){
                        // Emit the FULL collected burst. Do NOT truncate to
                        // packet_size: the burst length depends on the modulation
                        // (fewer symbols for higher-order schemes), so a fixed
                        // target would cut off data for some schemes and reject
                        // whole bursts for others. packet_size is only a minimum
                        // length gate; downstream sync locates the preamble and
                        // extracts exactly message_length data symbols, so extra
                        // tail samples are harmless.
                        output_vector = current_packet;

                        std::cout << "[COLLECTING END] Collection end at " << sample_index << " | collect " << collected_samples << " | total " << output_vector.size() << std::endl;

                        state = IDLE;
                        current_packet.clear();
                        collected_samples = 0;
                        post_samples = 0;

                        was_below_threshold = (current_filtered_energy <= threshold);
                        pre_filtered_enerrgy = current_filtered_energy;
                        return true;
                    } else {
                        state = IDLE;
                        current_packet.clear();
                        collected_samples = 0;
                        post_samples = 0;

                        was_below_threshold = (current_filtered_energy <= threshold);
                        pre_filtered_enerrgy = current_filtered_energy;
                    }
                    was_below_threshold = (current_filtered_energy <= threshold);
                    pre_filtered_enerrgy = current_filtered_energy;
                }
            }
            was_below_threshold = (current_filtered_energy <= threshold);
            pre_filtered_enerrgy = current_filtered_energy;
            // pre_window_samples = window_samples;
        }

        if (state == COLLECTING){
            // std::cout << "[STILL COLLECTING] [BLOCK END] Progress " << collected_samples << " / " << packet_size << std::endl;
        }
        return false;
    }

    // CONFIGURATION SETTINGS
    void set_threshold(float new_threshold){
        threshold = new_threshold;
        use_adaptive_threshold = false;  // Disable adaptive if manually set
        std::cout << "[DETECTOR CONFIG] Threshold set to: " << threshold << std::endl;
    }

    void set_alpha(float a){
        alpha = a;
        std::cout << "[DETECTIOR CONFIGURATION] Alpha set to: " << threshold << std::endl;
    }

    void set_packet_size(size_t size){
        packet_size = size;
        std::cout << "[DETECTIOR CONFIGURATION] Packet size set to: " << threshold << std::endl;
    }

    void set_window_size(size_t size){
        window_size = size;
        std::cout << "[DETECTIOR CONFIGURATION] Window size set to: " << threshold << std::endl;
    }

    void set_threshold_multiplier(float mult){
        threshold_multiplier = mult;
        if (noise_floor_calibrated){
            threshold = noise_floor * threshold_multiplier;
            std::cout << "[DETECTOR CONFIG] Threshold multiplier set to: " << mult << std::endl;
            std::cout << "[DETECTOR CONFIG] New threshold: " << threshold << std::endl;
        }
    }

    void reset_calibration() {
        noise_samples.clear();
        noise_floor_calibrated = false;
        std::cout << "[DETECTOR CONFIG] Calibration reset" << std::endl;
    }

    // Debugging functions
    float get_filtered_energy() const { return filtered_energy; }
    float get_threshold() const { return threshold; }
    float get_noise_floor() const { return noise_floor; }
    size_t get_collected_samples() const { return collected_samples; }
    bool is_collecting() const { return state == COLLECTING; }
    bool is_calibrated() const { return noise_floor_calibrated; }
};

// ------------------------------------------------- THREADS FUNCTIONS ----------------------------------------------- //
bool validate_tx_samples(const std::vector<std::complex<float>>& samples,
                        size_t block_id);

void transmit_thread(uhd::usrp::multi_usrp::sptr usrp,
                     MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& filtered_fifo,
                     double tx_rate, std::vector<unsigned long> channel, double UHD_timeout,
                     std::atomic<bool>& stop_sign);

void receive_thread(uhd::usrp::multi_usrp::sptr usrp,
                    std::vector<unsigned long> channel, double rx_rate, double setting_time,
                    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& recv_fifo,
                    int num_requested_samples, size_t samps_per_buff, std::atomic<bool>& stop_sign);

void AGC_thread(MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& received_fifo,
                MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& agc_fifo,
                std::atomic<bool>& stop_sign, const std::string& AGC,
                bool dc_block = true);

void EnergyDetection_thread(MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& input_fifo,
                            MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& output_fifo,
                            EnergyDetectorIIR& detector, std::atomic<bool>& stop_sign);

// Generate the histogram for clipping analysis
void clipping_checking(const std::vector<std::complex<float>>& samples,
                       const std::string& filename);

void save_block_to_txt(const std::vector<std::complex<float>>& recv_block,
                       int recv_block_id, std::string role);

void save_bits_to_txt(const std::vector<uint8_t>& bits,
                      int block_id, std::string role);

// void compute_instant_energy(size_t num_recv_samples, size_t recv_block_id);
void compute_instant_energy(size_t num_recv_samples, size_t recv_block_id, std::vector<std::complex<float>>& recv_samples);
// Calculate the Power Spectrum Density to check Bandwidth
void PSD(const std::vector<std::complex<float>>& samples,
         double sample_rate, int fft_size,
         const std::string& filename);

void PSD_Simple(const std::vector<std::complex<float>>& samples,
                double sample_rate,
                const std::string& filename);

float calculate_rms(const std::vector<std::complex<float>>& samples);