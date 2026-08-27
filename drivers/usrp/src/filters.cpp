#include <iostream>
#include <thread>
#include <chrono>
#include <vector>
#include "filters.hpp"
#include <mutex>
#include "FIFO.hpp"
#include "taps.hpp"
#include "transceiver.hpp"
#include "viz.hpp"
#include <fftw3.h>
#include <fstream>

// OverlapSave filter class
// Set up FFT objects and filter's frequency response
void FilterOverlapSave::setup(int x_len, int h_len, std::complex<float>* h, int nt)
{
    // Set up sizes for the overlap-save algorithm
    nx = x_len;
    Unx = nx*U;
    if (Unx%D != 0)
        printf("[WARNING] For proper continuous filtering, input_length*U must be divisible by D!\n");
    L = h_len;
    double LL = (L<32) ? 32.0 : double(L); // If L too small, use 32
    // Choose FFT size to be 2^m, where m is smallest power that 2^m >= 8*L
    fftsize = int(pow(2.0, ceil(log2(LL)+3)));
    
    M = fftsize-L+1;
    // Work out the number of FFT blocks needed
    nblocks = int(ceil(double(Unx)/M));
    // set number of threads to use 
    nthreads = nt;
    
    // Set up forward and inverse FFT objects
    fwdfft = new fft(nthreads, fftsize, nblocks, M, fftsize, false);
    invfft = new fft(nthreads, fftsize, nblocks, fftsize, fftsize, true, fwdfft->get_out());

    // Calculate and save FFT of h
    
    fft fft1time(1, fftsize, 1, 0, 0, false);
    std::complex<float>* a = fft1time.get_in();
    for (int i=0; i<L; i++) {
        a[i] = h[i];
    }
    for (int i=L; i<fftsize; i++) {
        a[i] = 0.0;
    }
    fft1time.calculate();
    H = (std::complex<float>*) fftwf_malloc(fftsize*sizeof(std::complex<float>));
    std::copy(fft1time.get_out(), fft1time.get_out()+fftsize, H);

    // Set whole fwd->in to 0
    std::fill(fwdfft->get_in(), fwdfft->get_in()+nblocks*fftsize, 0.0);
}

// Set head for next call in continuous filtering
// For continuous filtering U*(length of input) must be divisible by D
void FilterOverlapSave::set_head(bool reset) {
    std::complex<float>* in = fwdfft->get_in();
    if (reset)
        std::fill(in, in+L-1, 0.0);
    else
        std::copy(in+Unx, in+Unx+L-1, in);
}

// Single-rate FilterOverlapSave class constructor (U=D=1)
FilterOverlapSave::FilterOverlapSave(int x_len, int h_len, std::complex<float>* h, int nthreads)
{
    // Set up upsampling and downsampling rates to 1
    U = 1;
    D = 1;
    // Set up FFTs and filter's freq response
    this->setup(x_len, h_len, h, nthreads);
}

// Multi-rate FilterOverlapSave class constructor
FilterOverlapSave::FilterOverlapSave(int up, int down, int x_len, int h_len, std::complex<float>* h, int nthreads)
{
    // Set up upsampling and downsampling rates to specified values 
    U = up;
    D = down;
    // Set up FFTs and filter's freq response
    this->setup(x_len, h_len, h, nthreads);
}

// Class destructor
FilterOverlapSave::~FilterOverlapSave(void) {
    fftwf_free(H);
    delete fwdfft;
    delete invfft;
}

// Calculate output length
// N.B.: The implementation of the filter method below outputs
// floor(U*len(input)/D) samples. The transient in the beginning is not
// included in the output. This is done so that the filter method 
// may be used to filter a continuous stream of samples.
int FilterOverlapSave::out_len(void) {
    return int(floor(double(Unx)/D));
}
    
// Filter the input in and obtain output out
int FilterOverlapSave::filter(std::complex<float>* in, std::complex<float>* out)
{
    // Upsampling in by U
    std::complex<float>* up_in = fwdfft->get_in();
    if (U==1) {
        std::copy(in, in+nx, up_in+L-1);
    } else {
        int idx = L-1;
        for (int i=0; i<nx; i++) {
            up_in[idx] = in[i];
            idx += U;
        }
    }

    // Implement overlap-save
    fwdfft->calculate();
    std::complex<float>* transformed = fwdfft->get_out();
    for (int i=0; i<nblocks; i++) {
        int idx = i*fftsize;
        #ifdef USE_VOLK
            volk_32fc_x2_multiply_32fc(transformed+idx, transformed+idx, H, fftsize);
        #else
            for (int j=0; j<fftsize; j++) {
                transformed[idx++] *= H[j];
            }
        #endif
    }
    invfft->calculate();

    std::complex<float>* y = invfft->get_out();
    // Discard overlap output corrupted by circular conv.
    int idx = L-1;
    int oidx = 0;
    std::complex<float>* outbuf = transformed;
    if (D==1) outbuf = out; // put to output array directly
    for (int i=0; i<nblocks-1; i++) {
        std::copy(y+idx, y+idx+M, outbuf+oidx);
        idx += fftsize;
        oidx += M;
    }
    // Last block
    int nlast = Unx-(nblocks-1)*M;
    std::copy(y+idx, y+idx+nlast, outbuf+oidx);
    int nout = this->out_len();
    if (D>1) { 
        // Downsampling filtered by D
        for (int i=0; i<nout; i++) {
            out[i] = outbuf[i*D];
        }
    }
    return nout;
}

// --------------------------------------------------------------------------------------------------------------- //
// Polyphase filter class
// class constructor
FilterPolyphase::FilterPolyphase(int up, int down, int x_len, int h_len, std::complex<float>* h, int nt) {
    // Set up upsampling and downsampling rates to specified values 
    U = up;
    D = down;

    nx = x_len; // length of input signal
    if (nx%D != 0)
        printf("[WARNING] For proper continuous filtering, input_length must be divisible by D!\n");
    // nxD = int(floor(double(nx)/D))+1; // length of down-sampled input signals
    nxD = (nx+D-1) / D;
    // Set up lengths for the polyphase overlap-save algorithm
    L = h_len; // Length of orginal filter in up-sampled domain
    Lp = int(ceil(double(L)/U/D))+1; // length of polyphase filters
    double LL = (Lp<32) ? 32.0 : double(Lp); // If L too small, use 32
    // Choose FFT size to be 2^m, where m is smallest power that 2^m >= 8*L
    fftsize = int(pow(2.0, ceil(log2(LL)+2)));
    M = fftsize-Lp+1; //Length of data block for each polyphase filter

    // Work out the number of FFT blocks needed per down-sampled branch 
    nblocks = int(ceil(double(nxD)/M));
    // set number of threads to use 
    nthreads = nt;
    
    // Set up forward and inverse FFT objects
    fwdfft = new fft(nthreads, fftsize, nblocks, M, fftsize, D, false);
    invfft = new fft(nthreads, fftsize, nblocks, fftsize, fftsize, U, true);

    // Calculate and save FFTs of polyphase filters' impulse responses
    F = (std::complex<float>*) fftwf_malloc(U*D*fftsize*sizeof(std::complex<float>));
    // Append zeros to the beginning of h to get hh for convenience
    // Also hh[n] = h[n-U*D]

    // std::complex<float> hh[fftsize*U*D];  // Use stack
    // std::copy(h, h+L, hh+U*D);

    std::vector<std::complex<float>> hh(fftsize*U*D);  // ✅ Heap allocation
    std::copy(h, h+L, hh.begin()+U*D);  // Note: changed to hh.begin()

    fft fft1time(1, fftsize, 1, 0, 0, false);
    std::complex<float>* f = fft1time.get_in();
    // Note that f[n] = f_{pq}[n-1] below
    for (int p=0; p<U; p++) {
        for (int q=0; q<D; q++) {
            for (int i=0; i<Lp; i++) {
                f[i] = hh[U*D*i+D*p+U*q];
            }
            for (int i=Lp; i<fftsize; i++) {
                f[i] = 0.0;
            }
            fft1time.calculate();
            std::copy(fft1time.get_out(), fft1time.get_out()+fftsize, F+(p*D+q)*fftsize);
        }
    }

    // Set whole fwd->in to 0
    std::fill(fwdfft->get_in(), fwdfft->get_in()+nblocks*D*fftsize, 0.0);
}

// Class destructor
FilterPolyphase::~FilterPolyphase(void) {
    fftwf_free(F);
    delete fwdfft;
    delete invfft;
}


void FilterPolyphase::set_head(bool reset) {
    std::complex<float>* in = fwdfft->get_in();
    if (reset) {
        // Clear all polyphase branches completely
        std::fill(in, in + nblocks*D*fftsize, 0.0);
    } else {
        // Save overlap from previous block for all branches consistently
        for (int q=0; q<D; q++) {
            int branch_offset = q * nblocks * fftsize;
            // Use consistent overlap length for all branches
            int overlap_len = Lp - 1;
            int src_start = branch_offset + nxD + (q == 0 ? 0 : 1);
            int copy_len = (q == 0) ? overlap_len : Lp;
            
            std::copy(in + src_start, 
                     in + src_start + copy_len, 
                     in + branch_offset);
        }
    }
}

// Calculate output length
// N.B.: The implementation of the filter method below outputs
// floor(len(input)/D)*U samples. The transient in the beginning is not
// included in the output. This is done so that the filter method 
// may be used to filter a continuous stream of samples.
int FilterPolyphase::out_len(void) {
    return int(floor(double(nx)/D))*U;
}

int FilterPolyphase::get_group_delay(void){
    return ((L - 1) / 2) * U / D;
}

// Do polyphase filtering
// N.B.: This implementation introduces an additional delay of U samples at output
int FilterPolyphase::filter(std::complex<float>* in, std::complex<float>* out) {
    
    // Step 1 of freq-domain polyphase filtering algorithm
    std::complex<float>* a = fwdfft->get_in();
    int outidx = Lp-1;
    int idx = 0;
    for (int i=0; i<nxD-1; i++) {
        a[outidx++] = in[idx];
        idx += D;
    }
    for (int q=1; q<D; q++) {
        outidx = q*nblocks*fftsize+Lp;
        idx = (D-q)%D;
        for (int i=0; i<nxD; i++) {
            a[outidx++] = in[idx];
            idx += D;
        }
    }
    // Step 2 of freq-domain polyphase filtering algorithm
    fwdfft->calculate();
    // Step 3 of freq-domain polyphase filtering algorithm
    a = fwdfft->get_out();
    std::complex<float>* b = invfft->get_in();
    std::complex<float>* tmp_result = (std::complex<float>*) fftwf_malloc(fftsize*sizeof(std::complex<float>));
    int Fidx = 0;
    for (int p=0; p<U; p++) {
        int startu = p*nblocks*fftsize;
        int uidx = startu;
        int idx = 0;
        for (int i=0; i<nblocks; i++) {
            #ifdef USE_VOLK
                volk_32fc_x2_multiply_32fc(b+uidx, a+idx, F+Fidx, fftsize);
            #else
                for (int j=0; j<fftsize; j++) {
                    b[uidx+j] = F[Fidx+j] * a[idx+j];
                }        
            #endif
                uidx += fftsize;
                idx += fftsize;
            }
            Fidx += fftsize;
            for (int q=1; q<D; q++) {
                uidx = startu;
                for (int i=0; i<nblocks; i++) {
                    #ifdef USE_VOLK
                        volk_32fc_x2_multiply_32fc(tmp_result, a+idx, F+Fidx, fftsize);
                        volk_32fc_x2_add_32fc(b+uidx, b+uidx, tmp_result, fftsize);
                    #else
                        for (int j=0; j<fftsize; j++) {
                            b[uidx+j] += F[Fidx+j] * a[idx+j];
                        }     
                    #endif
                    uidx += fftsize;
                    idx += fftsize;
                }
            Fidx += fftsize;
        }
    }
    // Step 4 of freq-domain polyphase filtering algorithm
    invfft->calculate();
    // Steps 5 and 6 of freq-domain polyphase filtering algorithm
    b = invfft->get_out();
    outidx = 0;
    int olen = this->out_len();
    for (int i=0; i<nblocks; i++) {
        for (int j=0; j<M and outidx<olen; j++) {
            int uidx = i*fftsize+j+Lp-1;
            for (int p=0; p<U and outidx<olen; p++) {
                out[outidx++] = b[uidx];
                uidx += fftsize*nblocks;
            }
        }
    }
    // Cleanup
    fftwf_free(tmp_result);
    return olen;
}

void pulse_shaping_filter_thread(MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& modulation_fifo,
                                MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& filtered_fifo,
                                const std::string filter_type,
                                const double symbol_rate, const double sample_rate,
                                const int num_taps, const int U, const int D, const double roll_off, const int num_threads,
                                std::atomic<bool>& stop_sign, const std::string role)
{
    std::vector<std::complex<float>> taps(num_taps);   
    
    rrc_pulse(taps.data(), (num_taps-1)/2, U, D, roll_off);
    // save_block_to_txt(taps, 0, "taps");

    std::pair<size_t, std::vector<std::complex<float>>> message;
    size_t processed_blocks = 0;

    std::string MF_type = "Polyphase";
    FilterPolyphase* applied_filter = nullptr;

    // std::string MF_type = "multi-rate";
    // FilterOverlapSave* applied_filter = nullptr;

    int padding_size = 2 * num_taps - 1;
    // int padding_size = (num_taps + U - 1) / U;
    
    int tx_delay_samples = (num_taps - 1) / 2;

    // Track cumulative samples for proper delay removal
    int cumulative_input_samples = 0;
    int samples_to_skip = tx_delay_samples;

    // Track cumulative position for delay compensation
    int cumulative_output_samples = 0;
    int tx_filter_delay = (num_taps - 1) / 2;
    bool first_block = true;

    // Choice of scale_factor  (constant or auto scaling)
    // float scale_factor = 0.5f;
    float target_rms = 0.8;
    float headroom_db = 1.0;  // 1dB for safety margin
    float headroom_linear = std::pow(10.0, headroom_db / 20.0);
    size_t tried_time = 0;
 
    DrainGate gate;
    while (gate.keep_going(stop_sign, modulation_fifo)){
        // Debugging printout
        // std::cout << "[FILTER] Number " << tried_time << " FIFO size: " << modulation_fifo.size() << std::endl;

        if (!modulation_fifo.pop(message)){
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            tried_time++;
            continue;
        }

        size_t block_id = message.first;
        std::vector<std::complex<float>>& symbols = message.second;

        symbols.insert(symbols.end(), padding_size, std::complex<float>(0.0, 0.0));

        // Zero padding to make sure the symbols.size() works for filtering
        if (symbols.size() % D != 0){
            int padding = D - (symbols.size() % D);
            symbols.insert(symbols.end(), padding, std::complex<float>(0.0f, 0.0f));
        }

        int num_symbols = symbols.size();

        // std::cout << "[FILTER] Block " << block_id << " has length " << num_symbols << std::endl;

        // Build the Polyphase filter if there is no one defined.
        if (applied_filter == nullptr) {
            applied_filter = new FilterPolyphase(U, D, num_symbols, num_taps, taps.data(), num_threads);
            // applied_filter = new FilterOverlapSave(U, D, num_symbols, num_taps, taps.data(), num_threads);

            applied_filter->set_head(true);
            first_block = true;
        }

        // Check if input size changed (for different length of block_message) -> U (usually symbol_rate * U) = sample_rate / symbol_rare, D = 1 for transmitter
        if (num_symbols != applied_filter->nx) {
            std::cout << "[FILTER WARNING] Symbol count changed from " << applied_filter->out_len()/U << " to " << num_symbols << std::endl;
            delete applied_filter;
            applied_filter = new FilterPolyphase(U, D, num_symbols, num_taps, taps.data(), num_threads);
            // applied_filter = new FilterOverlapSave(U, D, num_symbols, num_taps, taps.data(), num_threads);

            applied_filter->set_head(true);
            first_block = true;
        }

        // continues handling the header problem
        if (!first_block){
            applied_filter->set_head(false);
        } else {
            first_block = false;
        }

        int output_length = applied_filter->out_len();
        std::vector<std::complex<float>> filtered(output_length);

        int actual_output_length = applied_filter->filter(symbols.data(), filtered.data());
        
        if (actual_output_length != output_length){
            filtered.resize(actual_output_length);
        }

        cumulative_output_samples += actual_output_length;

        // if (samples_to_skip > 0) {
        //     int skip_now = std::min(samples_to_skip, (int)filtered.size());
        //     filtered.erase(filtered.begin(), filtered.begin() + skip_now);
        //     samples_to_skip -= skip_now;
            
        //     std::cout << "[MATCH FILTER] Block " << block_id << ": Removed " << skip_now 
        //               << " delay samples, " << samples_to_skip << " remaining" << std::endl;
        // }

        // float rms=0, peak=0;
        // for (auto& s : filtered) {
        //     float mag = std::abs(s);
        //     rms += mag*mag;
        //     peak = std::max(peak, mag);
        // }
        // rms = std::sqrt(rms / filtered.size());
        // // std::cout << "[FILTER RMS] Block " << block_id << " has FILTER output: RMS=" << rms << " Peak=" << peak << std::endl;

        // // Apply scaling
        // float scale_factor = target_rms / (rms * headroom_linear);
        
        // for (auto& sample : filtered){
        //     sample *= scale_factor;
        // }

        // float rms1=0, peak1=0;
        // for (auto& s : filtered) {
        //     float mag = std::abs(s);
        //     rms1 += mag*mag;
        //     peak1 = std::max(peak1, mag);
        // }
        // rms1 = std::sqrt(rms1 / filtered.size());
        // std::cout << "[CHECK AFTER FILTER SCALE] Stage FILTER SCALE: RMS=" << rms1 << " Peak=" << peak1 << std::endl;

        viz::capture("tx_wave", filtered, 2000);   // TX pulse-shaped waveform
        filtered_fifo.push({block_id, filtered});
        // std::cout << "[FILTER] Output FIFO size: " << filtered_fifo.size() << std::endl;

        processed_blocks++;
    }
    // Clean the memory
    if (applied_filter != nullptr){
        delete applied_filter;
    }

    std::cout << "[FILTER] Filter stopped. Processed " << processed_blocks << " blocks. " << std::endl;
}

void match_filter_thread(MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& modulation_fifo,
                        MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& filtered_fifo,
                        const std::string filter_type,
                        const double symbol_rate, const double sample_rate,
                        const int num_taps, const int U, const int D, const double roll_off, const int num_threads,
                        std::atomic<bool>& stop_sign, const std::string role)
{
    // TRUE matched filter design.
    // The RX front-end is a matched filter, NOT a resampler: it convolves the
    // incoming stream with the RRC pulse at the *actual* RX oversampling
    // (sample_rate/symbol_rate samples per symbol) and does NOT change the
    // sample rate. The previous version designed the RRC with Ts = U/D and then
    // upsampled the stream by U — so the pulse's symbol period (U/D samples) did
    // not match the real symbol spacing at the filter output, the filter was not
    // matched, and the preamble correlation collapsed (~3/31). Here the pulse is
    // designed at the integer RX oversampling `os` and the filter runs
    // single-rate (U=D=1). The incoming (U,D) args are ignored for the RX MF.
    const int os = std::max(1, (int)std::lround(sample_rate / symbol_rate));  // RX samples/symbol
    const int fU = 1, fD = 1;                                                  // single-rate filtering
    std::cout << "[MATCH FILTER] matched RRC at " << os
              << " samples/symbol (single-rate, sample_rate/symbol_rate="
              << (sample_rate/symbol_rate) << ")\n";

    std::vector<std::complex<float>> org_taps(num_taps);
    rrc_pulse(org_taps.data(), (num_taps-1)/2, os, 1, roll_off);
    // save_block_to_txt(taps, 0, "taps");

    // 2) Reverse + conjugate for a true matched filter
    std::vector<std::complex<float>> taps(num_taps);
    for (int i = 0; i < num_taps; ++i)
        taps[i] = std::conj(org_taps[num_taps-1-i]);

    std::pair<size_t, std::vector<std::complex<float>>> message;
    size_t processed_blocks = 0;

    std::string MF_type = "Polyphase";
    FilterPolyphase* applied_filter = nullptr;

    // std::string MF_type = "multi-rate";
    // FilterOverlapSave* applied_filter = nullptr;

    int padding_size = 2 * num_taps;  // no need to padding twice
    // int padding_size = (num_taps * U - 1) / U;

    int tx_delay_samples = (num_taps - 1) / 2;
    int rx_delay_samples = (num_taps - 1) / 2;
    int total_delay_samples = tx_delay_samples + rx_delay_samples;
    int delay_symbol = total_delay_samples * D / U;

    // Track cumulative samples for proper delay removal
    int cumulative_input_samples = 0;
    int samples_to_skip = total_delay_samples;
    bool first_block = true;

    // Choice of scale_factor  (constant or auto scaling)
    // float scale_factor = 0.5f;
    float target_rms = 0.8;
    float headroom_db = 1.0;  // 1dB for safety margin
    float headroom_linear = std::pow(10.0, headroom_db / 20.0);
    size_t tried_time = 0;
 
    DrainGate gate;
    while (gate.keep_going(stop_sign, modulation_fifo)){
        // Debugging printout
        // std::cout << "[FILTER] Number " << tried_time << " FIFO size: " << modulation_fifo.size() << std::endl;

        if (!modulation_fifo.pop(message)){
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            tried_time++;
            continue;
        }

        size_t block_id = message.first;
        std::vector<std::complex<float>>& symbols = message.second;

        // save_block_to_txt(symbols, 0, "modulated");

        // symbols.insert(symbols.end(), padding_size, std::complex<float>(0.0, 0.0));

        // Zero padding to make sure the symbols.size() works for filtering
        if (symbols.size() % fD != 0){
            int padding = fD - (symbols.size() % fD);
            symbols.insert(symbols.end(), padding, std::complex<float>(0.0f, 0.0f));
        }

        int num_symbols = symbols.size();
        cumulative_input_samples += num_symbols;

        // std::cout << "[FILTER] Block " << block_id << " has length " << num_symbols << std::endl;

        // Build the Polyphase filter if there is no one defined.
        if (applied_filter == nullptr) {
            applied_filter = new FilterPolyphase(fU, fD, num_symbols, num_taps, taps.data(), num_threads);
            // applied_filter = new FilterOverlapSave(fU, fD, num_symbols, num_taps, taps.data(), num_threads);

            applied_filter->set_head(true);
            first_block = true;
        }

        // Check if input size changed (for different length of block_message). RX
        // matched filter is single-rate (fU=fD=1), so out_len == num_symbols.
        if (num_symbols != applied_filter->nx) {
            std::cout << "[FILTER WARNING] Symbol count changed from " << applied_filter->out_len()/fU << " to " << num_symbols << std::endl;
            delete applied_filter;
            applied_filter = new FilterPolyphase(fU, fD, num_symbols, num_taps, taps.data(), num_threads);
            // applied_filter = new FilterOverlapSave(fU, fD, num_symbols, num_taps, taps.data(), num_threads);

            applied_filter->set_head(true);
            first_block = true;
        }

        // Every block arriving here is an INDEPENDENT detected burst, not a
        // contiguous continuation of the previous one: the energy detector
        // gates on a rising edge and the bursts are separated in time by an
        // arbitrary idle gap. Carrying the previous burst's overlap state into
        // this one (set_head(false)) splices stale samples onto the head of the
        // new burst and corrupts its first num_taps samples -- which lands
        // squarely on the preamble. It only bit when two consecutive captures
        // happened to have the same length (otherwise the filter is rebuilt
        // below with head=true), so it presented as an intermittent "the first
        // burst decodes, later ones don't".
        applied_filter->set_head(true);
        first_block = false;

        int output_length = applied_filter->out_len();
        std::vector<std::complex<float>> filtered(output_length);

        int actual_output_length = applied_filter->filter(symbols.data(), filtered.data());
        
        if (actual_output_length != output_length){
            filtered.resize(actual_output_length);
        }

        // if (samples_to_skip > 0) {
        //     int skip_now = std::min(samples_to_skip, (int)filtered.size());
        //     filtered.erase(filtered.begin(), filtered.begin() + skip_now);
        //     samples_to_skip -= skip_now;
            
        //     std::cout << "[MATCH FILTER] Block " << block_id << ": Removed " << skip_now 
        //               << " delay samples, " << samples_to_skip << " remaining" << std::endl;
        // }

        // save_block_to_txt(filtered, block_id, "filtered");

        // float rms=0, peak=0;
        // for (auto& s : filtered) {
        //     float mag = std::abs(s);
        //     rms += mag*mag;
        //     peak = std::max(peak, mag);
        // }
        // rms = std::sqrt(rms / filtered.size());
        // // std::cout << "[FILTER RMS] Block " << block_id << " has FILTER output: RMS=" << rms << " Peak=" << peak << std::endl;

        // // Apply scaling
        // float scale_factor = target_rms / (rms * headroom_linear);
        
        // for (auto& sample : filtered){
        //     sample *= scale_factor;
        // }

        // float rms1=0, peak1=0;
        // for (auto& s : filtered) {
        //     float mag = std::abs(s);
        //     rms1 += mag*mag;
        //     peak1 = std::max(peak1, mag);
        // }
        // rms1 = std::sqrt(rms1 / filtered.size());
        // std::cout << "[CHECK AFTER FILTER SCALE] Stage FILTER SCALE: RMS=" << rms1 << " Peak=" << peak1 << std::endl;

        if (filtered.size() > 0) {
            filtered_fifo.push({block_id, filtered});
        }
        // std::cout << "[FILTER] Output FIFO size: " << filtered_fifo.size() << std::endl;

        processed_blocks++;
    }
    // Clean the memory
    if (applied_filter != nullptr){
        delete applied_filter;
    }

    std::cout << "[FILTER] Filter stopped. Processed " << processed_blocks << " blocks. " << std::endl;
}

