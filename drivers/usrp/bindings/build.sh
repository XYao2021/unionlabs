#!/usr/bin/env bash
# Build the pyphy pybind11 extension (numpy-in/out PHY blocks).
#   bindings/build.sh              # DSP blocks only (no radio) — builds anywhere
#   WITH_UHD=1 bindings/build.sh    # + the Radio source/sink (needs UHD + Boost)
# Requires: pybind11, numpy, the x86_64 fftw3f + volk this repo uses (and UHD if WITH_UHD).
set -euo pipefail
cd "$(dirname "$0")/.."

PYINC="$(python3-config --includes)"                                  # -> Python.h
PYBIND="-I$(python3 -c 'import pybind11; print(pybind11.get_include())')"
SUF="$(python3-config --extension-suffix)"
OUT="bindings/pyphy${SUF}"

# DSP block sources (no UHD): modulator, RRC filters, ACQ sync. FEC/LDPC/turbo/OFDM
# and the CFO/phase correctors are header-only.
SRCS="bindings/pyphy.cpp src/modulator.cpp src/filters.cpp src/synchronization.cpp"
EXTRA=""

if [ "${WITH_UHD:-0}" = "1" ]; then
  echo ">> WITH_UHD=1 — adding the Radio source/sink (UHD)"
  SRCS="${SRCS} src/transceiver.cpp"
  if pkg-config --exists uhd 2>/dev/null; then
    EXTRA="-DPYPHY_WITH_UHD $(pkg-config --cflags uhd) $(pkg-config --libs uhd)"
  else
    # fall back: UHD_PREFIX (e.g. /usr/local or /opt/local) + Boost on the include path
    EXTRA="-DPYPHY_WITH_UHD -I${UHD_PREFIX:-/usr/local}/include -L${UHD_PREFIX:-/usr/local}/lib -luhd -lboost_system"
  fi
fi

echo ">> Python includes: ${PYINC}"
echo ">> pybind include:  ${PYBIND}"

# OS-aware flags: macOS (clang) wants -arch + -undefined dynamic_lookup and the
# Homebrew/MacPorts /usr/local prefix; Linux (containers) resolves Python symbols
# at load time (no flag) and finds fftw3f/volk on the default library path.
if [ "$(uname)" = "Darwin" ]; then
  ARCHFLAGS="-arch x86_64"; UNDEF="-undefined dynamic_lookup"
  SYSINC="-isystem /usr/local/include"; SYSLIB="-L/usr/local/lib"
else
  ARCHFLAGS=""; UNDEF=""; SYSINC=""; SYSLIB=""
fi

g++ -O2 -std=c++17 -shared -fPIC ${ARCHFLAGS} \
    -include atomic -include cstdint -DUSE_VOLK \
    ${PYINC} ${PYBIND} -Itests/stub -Iinclude ${SYSINC} \
    ${SRCS} ${EXTRA} \
    ${SYSLIB} -lfftw3f -lfftw3f_threads -lvolk \
    ${UNDEF} \
    -o "${OUT}"

echo ">> built ${OUT}"
