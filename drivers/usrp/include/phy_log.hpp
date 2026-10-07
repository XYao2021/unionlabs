// phy_log.hpp — one switch for the pipeline's per-block chatter.
//
// The modem prints two very different kinds of line and they were indistinguishable
// because both went to std::cout unconditionally:
//
//   DIAGNOSIS   [ACQ] Peak correlation, [CRC OK], [SOURCE] ACK received,
//               [USRP TX] clip guard, [USRP RX] TIMEOUT, [ERROR], [BER]
//               — answers "is this link working, and if not, which layer broke"
//
//   CHATTER     [MODULATION] Number, [DEMODULATION] Output FIFO size,
//               [FILTER] Block, [DETECTOR] Input FIFO size, [AGC] Block,
//               [DIFF_ENCODE] Input symbol[0], [PSD] FFT size
//               — says a block passed through a stage, and nothing else
//
// The chatter was written while the pipeline's threading was being brought up, when
// FIFO depths were the thing in question. It answers no question anyone asks of a
// working radio, and there is a lot of it: one or more lines per block PER STAGE, so
// an 8000-byte message at 125-byte chunks pays for it 64 times over on every stage it
// crosses. Some of those prints are synchronous writes inside the hot loop, so at
// large payloads this is not only unreadable, it is slow.
//
// --quiet-phy silences the CHATTER only. Diagnosis always prints, because a flag that
// can hide an error is worse than a noisy log: this codebase has twice lost an
// afternoon to a message that named the wrong layer, and hiding the right one would
// be the same mistake with a switch on it.
//
// Default is OFF, so existing output is unchanged for anyone reading it.
#pragma once

namespace phylog {
// A function-local static rather than an inline variable: the build is C++17, but an
// inline variable makes every tool that opens this header at a lower default warn
// about an extension, and this needs to be includable from anywhere without noise.
// Set once from main() before the pipeline threads start and read-only thereafter,
// so no synchronisation is needed -- the threads only ever see what main() wrote.
inline bool& quiet() { static bool q = false; return q; }
}

// The `if/else` shape, not `if (!quiet)`, so the macro is safe in an unbraced
// if/else — `if (c) PHY_CHATTER << x; else y;` would otherwise bind `else` to the
// macro's own `if` and silently change what the surrounding code means.
#define PHY_CHATTER if (phylog::quiet()) {} else std::cout
