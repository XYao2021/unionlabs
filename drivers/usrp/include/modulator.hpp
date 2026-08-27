// modulator.hpp — the constellation, and mapping bits on and off it.
//
// One Modulator serves every scheme (BPSK through 64-QAM, absolute and
// differential) by holding a constellation table plus the bits-per-symbol that
// follows from it. Differential variants add an encode/decode pass over the same
// table rather than a second table.
//
// Note the asymmetry that follows: differential_decode returns N-1 symbols for N,
// because the first symbol is the reference the rest are measured against.

#ifndef MODULATOR_HPP
#define MODULATOR_HPP

# include <vector>
# include <complex>
# include <string>
# include <map>
# include <mutex>
# include "FIFO.hpp"

enum class ModulationType {
    BPSK,
    QPSK,
    PSK8,
    QAM16,
    QAM32,
    QAM64,
    QAM128,
    QAM256,
    DBPSK,
    DQPSK,
    DPSK8,
    DQAM16,
    DQAM32,
    DQAM64,
    DQAM128,
    DQAM256,
    // ── Newly wired-in schemes (previously only in modulator_extended.hpp) ──
    APSK16,     // 16-APSK, 4+12 ring layout (DVB-S2 style)
    APSK32,     // 32-APSK, 4+12+16 ring layout
    PI4QPSK     // pi/4-DQPSK (differential; handled specially in the threads)
};

class Modulator
{
private:
    ModulationType mod_type;
    int bps;
    std::vector<std::complex<float>> constellation;
    bool is_differential;  // Flag for differential modulation
    
    // Private helper functions (declarations only)
    // For both regular (absolute) modulation and differential modulation, the constellation map is same
    void create_constellation();
    void create_bpsk_constellation();
    void create_qpsk_constellation();
    void create_8psk_constellation();
    void create_16QAM_constellation();
    void create_32QAM_constellation();
    void create_64QAM_constellation();
    void create_128QAM_constellation();
    void create_256QAM_constellation();
    void create_16APSK_constellation();   // 4+12 ring APSK
    void create_32APSK_constellation();   // 4+12+16 ring APSK
    void normalize_constellation();
    int bits_to_index(const std::vector<uint8_t>& bits, int start_position);

    bool check_differential(){
        return (mod_type == ModulationType::DBPSK ||
                mod_type == ModulationType::DQPSK ||
                mod_type == ModulationType::DPSK8 ||
                mod_type == ModulationType::DQAM16 ||
                mod_type == ModulationType::DQAM32 ||
                mod_type == ModulationType::DQAM64 ||
                mod_type == ModulationType::DQAM128 ||
                mod_type == ModulationType::DQAM256 );
    }

public:
    // Constructor
    Modulator(ModulationType type);

    // Public interface (declaration)
    int get_bits_per_symbol() const;  // const ensure the function won't modify the project (afterward only works for member function)
    std::string get_modulation_name() const;
    int get_constellation_size() const;
    // True for schemes that carry information in the phase *transition* between
    // symbols (differential PSK + pi/4-DQPSK). The RX handles these differently:
    // it keeps the last preamble symbol as the differential reference (so the
    // decoded symbol count is exact) and bypasses the coherent phase PLL (which
    // would pin the absolute phase and destroy the transitions).
    bool is_differential_scheme() const {
        return is_differential || mod_type == ModulationType::PI4QPSK;
    }
    const std::vector<std::complex<float>>& get_constellation() const;
    void print_constellation_info();

    std::vector<std::complex<float>> modulate(const std::vector<uint8_t>& bits, 
                                              std::vector<std::complex<float>>& preamble_sequence, 
                                              bool& add_preamble);
    std::vector<uint8_t> demodulate(const std::vector<std::complex<float>>& symbols);

    std::vector<std::complex<float>> differential_encode(const std::vector<std::complex<float>>& symbols, std::complex<float> pre_symbol);
    std::vector<std::complex<float>> differential_decode(const std::vector<std::complex<float>>& symbols);
};

// Helper functions (declarations)
float calculate_ser(const std::vector<std::complex<float>>& tx_symbols,
                    const std::vector<std::complex<float>>& rx_symbols);  // sample error rate

float calculate_ber(const std::vector<uint8_t>& tx_bits,
                    const std::vector<uint8_t>& rx_bits);  // bit error rate

void modulation_thread(MutexFIFO<std::vector<uint8_t>>& fifo, 
                       MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& fifo_out,
                       std::string& scheme, std::atomic<bool>& stop_sign,
                       std::vector<std::complex<float>> preamble_sequence, bool& add_preamble);

void demodulation_thread(MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& fifo,
                       MutexFIFO<std::pair<size_t, std::vector<uint8_t>>>& fifo_out,
                       std::string& scheme, std::atomic<bool>& stop_sign,
                       // Optional soft-decision output: when non-null, also emit one
                       // LLR per coded bit (soft_demodulate_llr) into *llr_out for the
                       // FEC soft decoder. Differential/pi4 push an empty vector (soft
                       // undefined there) to keep the two FIFOs in lockstep.
                       MutexFIFO<std::pair<size_t, std::vector<float>>>* llr_out = nullptr);

#endif