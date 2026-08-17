#!/usr/bin/env bash
# =============================================================================
#  initialization.sh — install everything the UnionLabs SDR platform needs.
#
#  Designed to run BOTH on a dev host and inside a container image build (it is
#  root-aware, non-interactive, and idempotent). Point a Dockerfile at it, e.g.:
#      COPY . /opt/sdr/unionlabs
#      RUN /opt/sdr/unionlabs/deploy/initialization.sh --build
#
#  ── Required libraries (installed here) ─────────────────────────────────────
#  SYSTEM / C++  (the USRP driver, drivers/usrp — builds sdr_system + pyphy):
#     build-essential          g++ / make, C++17
#     cmake  pkg-config  git  ca-certificates
#     libuhd-dev  uhd-host      UHD driver + tools (uhd_find_devices, images_downloader)
#     libboost-all-dev          Boost.program_options (CLI) + Boost.system
#     libfftw3-dev              FFTW3 single-precision (fftw3f + fftw3f_threads)
#     libvolk2-dev              VOLK (UHD SIMD kernels; -DUSE_VOLK)
#     python3  python3-dev  python3-pip     Python + headers (needed to build pyphy)
#     pybind11-dev             pybind11 (pyphy pybind11 extension)
#     iproute2  iputils-ping   N210 networking / connectivity checks
#  PYTHON  (the union/ middleware + applications):
#     numpy                    core — every algorithm + all DSP
#     matplotlib               figures / experiments (Agg, headless-safe)
#     torch                    MARL (A2C actor/critic), CLIP real      [--minimal skips]
#     networkx                 MARL consensus graph                    [--minimal skips]
#     opencv-python-headless   CLIP mock image path (cv2)              [--minimal skips]
#     python-pptx              slide generation (docs)
#     pybind11                 pyphy build (pip mirror of the apt pkg)
#  PYTHON, real CLIP weights   (only with --with-clip):
#     open_clip_torch  pillow  ftfy  regex
#  DOCS  (only with --docs):
#     pandoc  texlive-xetex  texlive-fonts-recommended
#
#  ── Usage ───────────────────────────────────────────────────────────────────
#     ./initialization.sh                 # system + full Python stack
#     ./initialization.sh --build         # + compile sdr_system AND pyphy
#     ./initialization.sh --minimal       # PHY only: skip torch/networkx/opencv
#     ./initialization.sh --with-clip     # + real-CLIP weights extras
#     ./initialization.sh --cpu-torch     # install CPU-only torch (smaller image)
#     ./initialization.sh --docs          # + pandoc + xelatex (PDF reference)
#     ./initialization.sh --no-images     # skip uhd_images_downloader (offline/CI)
#
#  Supported package managers (auto-detected): apt (Debian/Ubuntu — the container
#  path), MacPorts (port), Homebrew (brew).
# =============================================================================
set -euo pipefail

WITH_BUILD=0 WITH_DOCS=0 MINIMAL=0 WITH_CLIP=0 CPU_TORCH=0 NO_IMAGES=0
for arg in "$@"; do
    case "$arg" in
        --build)      WITH_BUILD=1 ;;
        --docs)       WITH_DOCS=1 ;;
        --minimal)    MINIMAL=1 ;;
        --with-clip)  WITH_CLIP=1 ;;
        --cpu-torch)  CPU_TORCH=1 ;;
        --no-images)  NO_IMAGES=1 ;;
        -h|--help)    sed -n '2,55p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg (see --help)"; exit 1 ;;
    esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"          # repo root (deploy/ -> repo)
USRP_DIR="$ROOT/drivers/usrp"                 # the USRP PHY driver
say()  { printf '\n\033[1;34m[init]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[init] WARNING:\033[0m %s\n' "$*"; }

# ── root / sudo (containers run as root; dev hosts need sudo) ────────────────
if [ "$(id -u)" -eq 0 ]; then SUDO=""; elif command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else
    warn "not root and no sudo — package installs may fail"; SUDO=""; fi
export DEBIAN_FRONTEND=noninteractive

# ── detect the package manager ──────────────────────────────────────────────
if   command -v apt-get >/dev/null 2>&1; then PM="apt"
elif command -v port    >/dev/null 2>&1; then PM="port"
elif command -v brew    >/dev/null 2>&1; then PM="brew"
else warn "no supported package manager (need apt, MacPorts, or Homebrew)"; exit 1; fi
say "Package manager: $PM   (root=$([ -z "$SUDO" ] && echo yes || echo no))"

# ── system / C++ dependencies ───────────────────────────────────────────────
say "Installing system + C++ dependencies (UHD, Boost, FFTW3f, VOLK, pybind11, CMake)..."
case "$PM" in
    apt)
        $SUDO apt-get update
        $SUDO apt-get install -y --no-install-recommends \
            build-essential cmake pkg-config git ca-certificates \
            libuhd-dev uhd-host \
            libboost-all-dev libfftw3-dev libvolk2-dev \
            python3 python3-dev python3-pip pybind11-dev \
            iproute2 iputils-ping
        ;;
    port)
        $SUDO port selfupdate
        $SUDO port install cmake pkgconfig uhd boost fftw-3-single volk py311-pybind11
        ;;
    brew)
        brew update
        brew install cmake pkg-config uhd boost fftw volk pybind11
        ;;
esac

# ── UHD FPGA / firmware images (needed the first time a radio is opened) ─────
if [ "$NO_IMAGES" -eq 0 ] && command -v uhd_images_downloader >/dev/null 2>&1; then
    say "Downloading UHD FPGA/firmware images..."
    $SUDO uhd_images_downloader || warn "uhd_images_downloader failed (re-run before using a radio)"
fi

# ── Python dependencies ─────────────────────────────────────────────────────
PY="$(command -v python3)"; [ -z "$PY" ] && { warn "python3 not found"; exit 1; }
# pip that tolerates PEP-668 externally-managed environments (Debian/Ubuntu system python)
pip_install() {
    "$PY" -m pip install --upgrade "$@" 2>/dev/null \
      || "$PY" -m pip install --break-system-packages --upgrade "$@" 2>/dev/null \
      || "$PY" -m pip install --user --break-system-packages --upgrade "$@"
}
"$PY" -m pip install --upgrade pip >/dev/null 2>&1 || \
    "$PY" -m pip install --break-system-packages --upgrade pip >/dev/null 2>&1 || true

say "Installing Python: numpy, matplotlib, pybind11, python-pptx..."
pip_install numpy matplotlib pybind11 python-pptx

if [ "$MINIMAL" -eq 0 ]; then
    if [ "$CPU_TORCH" -eq 1 ]; then
        say "Installing torch (CPU-only wheel), networkx, opencv..."
        pip_install --index-url https://download.pytorch.org/whl/cpu torch
    else
        say "Installing torch, networkx, opencv (ML applications: MARL, CLIP)..."
        pip_install torch
    fi
    pip_install networkx opencv-python-headless
else
    say "--minimal: skipping torch / networkx / opencv (PHY-only image)."
fi

if [ "$WITH_CLIP" -eq 1 ]; then
    say "Installing real-CLIP extras (open_clip_torch, pillow, ftfy, regex)..."
    pip_install open_clip_torch pillow ftfy regex
fi

# ── optional: documentation toolchain (pandoc + xelatex) ────────────────────
if [ "$WITH_DOCS" -eq 1 ]; then
    say "Installing docs toolchain (pandoc + xelatex)..."
    case "$PM" in
        apt)  $SUDO apt-get install -y pandoc texlive-xetex texlive-fonts-recommended ;;
        port) $SUDO port install pandoc texlive-xetex texlive-latex-recommended ;;
        brew) brew install pandoc; brew install --cask mactex-no-gui ;;
    esac
fi

# ── optional: build the C++ PHY (sdr_system) + the pyphy extension ──────────
if [ "$WITH_BUILD" -eq 1 ]; then
    CMAKE_BIN="$(command -v cmake || echo cmake)"
    case "$CMAKE_BIN" in *conda*|*anaconda*)
        [ -x /usr/bin/cmake ] && CMAKE_BIN=/usr/bin/cmake || \
            warn "cmake on PATH is Anaconda's (breaks 'make'); install a system cmake" ;;
    esac
    say "Building sdr_system (clean build dir, with $CMAKE_BIN)..."
    rm -rf "$USRP_DIR/build"; mkdir -p "$USRP_DIR/build"
    ( cd "$USRP_DIR/build" && "$CMAKE_BIN" .. \
        && make -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)" )
    say "Built: $USRP_DIR/build/sdr_system"

    say "Building the pyphy extension (WITH_UHD=1)..."
    ( WITH_UHD=1 "$USRP_DIR/bindings/build.sh" ) \
        || warn "pyphy build failed (needs pybind11 + numpy + fftw3f + volk [+ UHD]); --channel pyphy will be unavailable"
fi

# ── best-effort USRP check ──────────────────────────────────────────────────
if command -v uhd_find_devices >/dev/null 2>&1; then
    say "Checking for a connected USRP..."
    uhd_find_devices 2>&1 | grep -iE "type:|serial:|product:" || \
        warn "no USRP found (fine for an image build; plug in on the host at run time)"
fi

# ── summary ─────────────────────────────────────────────────────────────────
say "Done. Verify the Python stack:"
echo "    python3 -c 'import numpy, matplotlib$([ "$MINIMAL" -eq 0 ] && echo ", torch, networkx, cv2")' && echo OK"
[ "$WITH_BUILD" -eq 1 ] && echo "    $USRP_DIR/build/sdr_system --help | head" \
                        || say "Build the PHY with:  $0 --build"
say "Run an algorithm over the PHY:   ./run.sh --algo echo --role loopback"
