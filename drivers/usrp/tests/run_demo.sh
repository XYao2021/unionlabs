#!/usr/bin/env bash
# Hardware-free demos (no UHD / no USRP needed) for both requested changes plus
# the receiver bug fixes. Requires only g++ (C++17).
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CXX="${CXX:-g++}"
# -include atomic/cstdint guards libstdc++ versions that don't pull them
# transitively via the FIFO headers; harmless everywhere. tests/stub holds an
# empty transceiver.hpp so the modulator compiles without UHD (tests don't
# touch the radio).
FLAGS="-std=c++17 -O2 -include atomic -include cstdint -I$HERE/stub -I$ROOT/include"
SRC="$ROOT/src/modulator.cpp"

build(){ echo "== building $1 =="; $CXX $FLAGS -o "$HERE/$1" "$SRC" "$HERE/$1.cpp"; }
build mod_loopback_test
build sync_reorder_demo
build rx_chain_sweep

echo; echo "############ DEMO 1: MODULATION ROUND-TRIP (all schemes) ############"
"$HERE/mod_loopback_test"
echo; echo "############ DEMO 2: FREQ/PHASE AFTER TIME SYNC (QPSK) ############"
"$HERE/sync_reorder_demo" 2>/dev/null | grep -E '^  |----|===='
echo; echo "############ DEMO 3: FULL RX CHAIN ACROSS MODULATIONS ############"
"$HERE/rx_chain_sweep" 2>/dev/null | grep -E '^>>|====|-- |===  |Impair'
