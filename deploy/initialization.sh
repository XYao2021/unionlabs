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
            libfftw3-dev libvolk2-dev python3-pip \
            python3-numpy python3-matplotlib   # apt, not pip: PEP 668 blocks system pip
        # UHD images (Debian/Ubuntu ships the downloader with uhd-host):
        command -v uhd_images_downloader >/dev/null 2>&1 && \
            sudo uhd_images_downloader || warn "run 'sudo uhd_images_downloader' once before using a radio"
        ;;
esac

# ── Python dependencies ──────────────────────────────────────────────────────
say "Installing Python dependencies (numpy, matplotlib, python-pptx)..."
PY="$(command -v python3 || true)"
[ -z "$PY" ] && { warn "python3 not found"; exit 1; }
# Three cases. A venv → pip is unrestricted. Debian/Raspberry Pi OS system python
# → pip is PEP 668 "externally-managed" and refuses to install (the error seen on
# the Pi); numpy+matplotlib were installed via apt above, and only python-pptx
# (slide generation) still needs pip, so try apt then a --break-system-packages
# pip, non-fatal. Any other system python → the old --user path.
if "$PY" -c 'import sys; sys.exit(0 if sys.prefix!=sys.base_prefix else 1)' 2>/dev/null; then
    "$PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
    "$PY" -m pip install --upgrade numpy matplotlib python-pptx        # in a venv
elif [ "$PM" = "apt" ]; then
    say "numpy + matplotlib installed via apt (PEP 668: system pip is blocked)."
    if ! sudo apt-get install -y python3-pptx 2>/dev/null; then
        "$PY" -m pip install --break-system-packages --upgrade python-pptx 2>/dev/null \
            || warn "python-pptx not installed (only used for slide generation) — skipping"
    fi
else
    "$PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
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
    # Pick a cmake that is NOT Anaconda's — its pip cmake bakes a dead path into
    # the Makefile and breaks 'make' (seen on the Dell).
    CMAKE_BIN="$(command -v cmake || true)"
    case "$CMAKE_BIN" in
        *conda*|*anaconda*)
            warn "cmake on PATH is Anaconda's ($CMAKE_BIN) — it breaks 'make'."
            if [ -x /usr/bin/cmake ]; then
                CMAKE_BIN=/usr/bin/cmake; say "using $CMAKE_BIN instead (run 'conda deactivate' to make this permanent)"
            else
                warn "no system cmake found — run: sudo apt-get install -y cmake, then re-run"
            fi ;;
    esac
    [ -z "$CMAKE_BIN" ] && CMAKE_BIN=cmake
    say "Configuring + building build/sdr_system (with $CMAKE_BIN)..."
    # A COPIED build/ caches absolute paths from the source machine and won't
    # reconfigure here — always start from a clean build dir.
    rm -rf "$HERE/../phy/build"
    mkdir -p "$HERE/../phy/build"
    ( cd "$HERE/../phy/build" && "$CMAKE_BIN" .. && make -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)" )
    say "Built: $HERE/../phy/build/sdr_system"
fi

# ── Confirm a connected USRP (best-effort) ───────────────────────────────────
if command -v uhd_find_devices >/dev/null 2>&1; then
    say "Checking for a connected USRP..."
    if uhd_find_devices 2>&1 | grep -qi "serial"; then
        uhd_find_devices 2>&1 | grep -iE "type:|serial:|product:"
        say "USRP detected ✓"
    else
        warn "No USRP found. Plug into a USB 3 port. If you just installed uhd-host,"
        warn "  unplug/replug the radio (udev rules need a re-enumeration), then: uhd_find_devices"
        warn "If a device IS found but firmware fails to load: sudo uhd_images_downloader"
    fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────
say "Done. Verify:"
echo "    python3 -c 'import numpy, matplotlib' && echo 'python OK'"
if [ "$WITH_BUILD" -eq 0 ]; then
    echo
    say "To build the C++ PHY (deactivate Anaconda first so the system cmake is used):"
    echo "    conda deactivate 2>/dev/null; rm -rf build && mkdir build && cd build && cmake .. && make -j4"
fi
say "Then run, e.g.:  cd python && python3 run.py --list"
