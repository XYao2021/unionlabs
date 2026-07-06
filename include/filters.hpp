#include "fft.hpp"
#include <mutex>
#include "FIFO.hpp"

// University of Florida EEL6528
// Tan F. Wong
// Feb 2, 2021

#pragma once

#include "fft.hpp"

// Overlap-save filter class
class FilterOverlapSave {
  public:
    FilterOverlapSave(int x_len, int h_len, std::complex<float>* h, int nt=1); // single-rate constructor
    FilterOverlapSave(int up, int down, int x_len, int h_len, std::complex<float>* h, int nt=1); // multi-rate constructor
    ~FilterOverlapSave(void); // destructor
    int filter(std::complex<float>* in, std::complex<float>* out);  //do filtering
    void set_head(bool reset=false); // Set head of input array for continuously filtering
    int out_len(void); //calculate output length
    int nx; // length of input sequence

  private:
    void setup(int x_len, int h_len, std::complex<float>* h, int nt); // utility method to set up FFTs
    int fftsize;
    fft* fwdfft; // forward FFT 
    fft* invfft; // inverse FFT
    int U; // upsampling factor
    int D; // downsampling factor
    int L; // length of filter impulse response
    int M; // length of valid sample block
    int nblocks; // number of blocks to calculate
    int nthreads; // number of threads to use in FFT calculation
    // int nx; // length of input sequence
    int Unx; // length of input sequence after interpolation
    std::complex<float>* H; // FFT of impulse response
};

// Polyphase filter class
class FilterPolyphase {
  public:
    FilterPolyphase(int up, int down, int x_len, int h_len, std::complex<float>* h, int nt); // constructor
    ~FilterPolyphase(void); // destructor
    int filter(std::complex<float>* in, std::complex<float>* out);  // do filtering
    void set_head(bool reset=false); // Set head of input array for continuously filtering
    int out_len(void); //calculate output length
    int get_group_delay(void);
    int nx; // length of input sequence

  private:
    int fftsize;
    fft* fwdfft; // forward FFT 
    fft* invfft; // inverse FFT
    int U; // upsampling factor
    int D; // downsampling factor
    int L; // length of filter impulse response
    int Lp; // length of polyphase filter impulse responses
    int M; // length of valid sample block
    int nblocks; // number of blocks to calculate on each down-sampled input signal (row)
    int nthreads; // number of threads to use in FFT calculation
    // int nx; // length of input sequence
    int nxD; // length of down-sampled signals
    std::complex<float>* F; // FFTs of impulse responses of polyphase filters
};

void pulse_shaping_filter_thread(MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& modulation_fifo,
                                MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& filtered_fifo,
                                const std::string filter_type,
                                const double symbol_rate, const double sample_rate,
                                const int num_taps, const int U, const int D, const double roll_off, const int num_threads,
                                std::atomic<bool>& stop_sign, const std::string role);

void match_filter_thread(MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& modulation_fifo,
                        MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& filtered_fifo,
                        const std::string filter_type,
                        const double symbol_rate, const double sample_rate,
                        const int num_taps, const int U, const int D, const double roll_off, const int num_threads,
                        std::atomic<bool>& stop_sign, const std::string role);