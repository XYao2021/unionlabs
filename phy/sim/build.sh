#!/usr/bin/env bash
# Build the two-terminal TX/RX demo (no UHD / no radio). Requires only g++ (C++17).
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CXX="${CXX:-g++}"
FLAGS="-std=c++17 -O2 -pthread -include atomic -include cstdint -I$HERE/../tests/stub -I$ROOT/include -I$HERE"
echo "building tx_app ..."; $CXX $FLAGS -o "$HERE/tx_app" "$ROOT/src/modulator.cpp" "$HERE/tx_app.cpp"
echo "building rx_app ..."; $CXX $FLAGS -o "$HERE/rx_app" "$ROOT/src/modulator.cpp" "$HERE/rx_app.cpp"
echo "done: $HERE/tx_app  $HERE/rx_app"
