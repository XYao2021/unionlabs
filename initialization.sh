#!/usr/bin/env bash
# =============================================================================
#  initialization.sh — install everything the USRP B210 SDR link needs.
#
#  Sets up both toolchains used by this repo:
#    • C++ PHY (build/sdr_system) : CMake + UHD(+VOLK) + Boost + FFTW3f + pthreads
#    • Python layer (python/, tools/, slides/) : numpy, matplotlib, python-pptx
#    • (optional) docs toolchain  : pandoc + xelatex, for tools/build_reference_pdf.sh
#
#  Supported package managers (auto-detected):
#    macOS  : MacPorts (port)  or  Homebrew (brew)      ← UHD 3.15 validated on MacPorts
#    Linux  : apt (Debian/Ubuntu)
#
#  Usage:
#    ./initialization.sh                 # install C++ + Python dependencies
#    ./initialization.sh --docs          # also install pandoc + xelatex (PDF reference)
#    ./initialization.sh --build         # also configure + compile build/sdr_system
#    ./initialization.sh --docs --build  # everything
#
#  Re-runnable: installing an already-present package is a no-op.
# =============================================================================
set -euo pipefail

WITH_DOCS=0
WITH_BUILD=0
for arg in "$@"; do
    case "$arg" in
        --docs)  WITH_DOCS=1 ;;
        --build) WITH_BUILD=1 ;;
        -h|--help)
            sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg (see --help)"; exit 1 ;;
    esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
say()  { printf '\n\033[1;34m[init]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[init] WARNING:\033[0m %s\n' "$*"; }

# ── Detect the package manager ───────────────────────────────────────────────
PM=""
if   command -v port  >/dev/null 2>&1; then PM="port"
elif command -v brew  >/dev/null 2>&1; then PM="brew"
elif command -v apt-get >/dev/null 2>&1; then PM="apt"
else
    warn "No supported package manager found (need MacPorts, Homebrew, or apt)."
    warn "Install one, or install these manually: cmake uhd boost fftw(single) volk."
    exit 1
fi
say "Package manager: $PM"

# ── C++ system dependencies ──────────────────────────────────────────────────
say "Installing C++ build dependencies (UHD, Boost, FFTW3f, VOLK, CMake)..."
case "$PM" in
    port)
        sudo port selfupdate
        sudo port install cmake uhd boost fftw-3-single volk
        # UHD's FPGA/firmware images (needed the first time a B210 is opened):
        command -v uhd_images_downloader >/dev/null 2>&1 && \
            uhd_images_downloader || warn "run 'uhd_images_downloader' once before using a radio"
        ;;
    brew)
        brew update
        brew install cmake uhd boost fftw volk
        command -v uhd_images_downloader >/dev/null 2>&1 && \
            uhd_images_downloader || warn "run 'uhd_images_downloader' once before using a radio"
        ;;
    apt)
        sudo apt-get update
        sudo apt-get install -y build-essential cmake \
            libuhd-dev uhd-host \
            libboost-program-options-dev libboost-system-dev \
            libfftw3-dev libvolk2-dev python3-pip
        # UHD images (Debian/Ubuntu ships the downloader with uhd-host):
        command -v uhd_images_downloader >/dev/null 2>&1 && \
            sudo uhd_images_downloader || warn "run 'sudo uhd_images_downloader' once before using a radio"
        ;;
esac

# ── Python dependencies ──────────────────────────────────────────────────────
say "Installing Python dependencies (numpy, matplotlib, python-pptx)..."
PY="$(command -v python3 || true)"
[ -z "$PY" ] && { warn "python3 not found"; exit 1; }
"$PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
# --user keeps it out of the system site-packages; drop it inside a venv.
if "$PY" -c 'import sys; sys.exit(0 if sys.prefix!=sys.base_prefix else 1)' 2>/dev/null; then
    "$PY" -m pip install --upgrade numpy matplotlib python-pptx        # in a venv
else
    "$PY" -m pip install --user --upgrade numpy matplotlib python-pptx # system python
fi

# ── Optional: documentation toolchain (pandoc + xelatex) ─────────────────────
if [ "$WITH_DOCS" -eq 1 ]; then
    say "Installing docs toolchain (pandoc + xelatex)..."
    case "$PM" in
        port) sudo port install pandoc texlive-xetex texlive-latex-recommended ;;
        brew) brew install pandoc; brew install --cask mactex-no-gui ;;
        apt)  sudo apt-get install -y pandoc texlive-xetex texlive-fonts-recommended ;;
    esac
fi

# ── Optional: build the C++ PHY ──────────────────────────────────────────────
if [ "$WITH_BUILD" -eq 1 ]; then
    say "Configuring + building build/sdr_system..."
    mkdir -p "$HERE/build"
    ( cd "$HERE/build" && cmake .. && make -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)" )
    say "Built: $HERE/build/sdr_system"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
say "Done. Verify:"
echo "    uhd_find_devices           # should list your B210(s)"
echo "    python3 -c 'import numpy, matplotlib' && echo 'python OK'"
if [ "$WITH_BUILD" -eq 0 ]; then
    echo
    say "To build the C++ PHY:"
    echo "    mkdir -p build && cd build && cmake .. && make -j4"
fi
say "Then run, e.g.:  cd python && python3 run.py --list"
