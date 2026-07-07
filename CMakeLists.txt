cmake_minimum_required(VERSION 3.12)
project(qpsk_test CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ── Build type ───────────────────────────────────────────────
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()
if(APPLE)
    set(CMAKE_CXX_FLAGS_RELEASE "-O2")
else()
    set(CMAKE_CXX_FLAGS_RELEASE "-O2 -march=native")
endif()

# ────────────────────────────────────────────────────────────
#  BOOST
#  The Anaconda cmake intercepts find_package(Boost) and its
#  bundled FindBoost.cmake then forwards to Boost's own
#  BoostConfig.cmake, which internally requests boost_system
#  as a dependency of program_options — even though boost_system
#  has been header-only since 1.69 and has no config file.
#
#  Fix: skip find_package entirely and locate the libraries
#  with find_library / find_path directly.  This is immune to
#  whichever cmake binary is on PATH.
# ────────────────────────────────────────────────────────────
set(BOOST_SEARCH_PREFIXES
    /usr/local
    /opt/local
    /opt/homebrew
    /usr
)

find_path(BOOST_INCLUDE_DIR
    NAMES boost/program_options.hpp
    HINTS ${BOOST_SEARCH_PREFIXES}
    PATH_SUFFIXES include
)

find_library(BOOST_PO_LIB
    NAMES boost_program_options-mt boost_program_options
    HINTS ${BOOST_SEARCH_PREFIXES}
    PATH_SUFFIXES lib
)

if(NOT BOOST_INCLUDE_DIR OR NOT BOOST_PO_LIB)
    message(FATAL_ERROR
        "Could not find Boost headers or boost_program_options library.\n"
        "Searched: ${BOOST_SEARCH_PREFIXES}\n"
        "Set BOOST_ROOT to your Boost installation prefix, e.g.:\n"
        "  cmake .. -DBOOST_ROOT=/usr/local")
endif()

message(STATUS "Boost include: ${BOOST_INCLUDE_DIR}")
message(STATUS "Boost PO lib:  ${BOOST_PO_LIB}")

# ── UHD ─────────────────────────────────────────────────────
find_package(UHD REQUIRED)

# ── Threads ─────────────────────────────────────────────────
find_package(Threads REQUIRED)

# ── FFTW3 single-precision ───────────────────────────────────
find_library(FFTW3F_LIB
    NAMES fftw3f
    HINTS ${BOOST_SEARCH_PREFIXES}
    PATH_SUFFIXES lib
    REQUIRED)

find_library(FFTW3F_THREADS_LIB
    NAMES fftw3f_threads
    HINTS ${BOOST_SEARCH_PREFIXES}
    PATH_SUFFIXES lib)

find_path(FFTW3_INCLUDE_DIR
    NAMES fftw3.h
    HINTS ${BOOST_SEARCH_PREFIXES}
    PATH_SUFFIXES include)

message(STATUS "FFTW3F lib:     ${FFTW3F_LIB}")
message(STATUS "FFTW3F threads: ${FFTW3F_THREADS_LIB}")
message(STATUS "FFTW3 include:  ${FFTW3_INCLUDE_DIR}")

# ── Include paths ────────────────────────────────────────────
include_directories(
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${UHD_INCLUDE_DIRS}
    ${BOOST_INCLUDE_DIR}
    ${FFTW3_INCLUDE_DIR}
)

# ── Sources ──────────────────────────────────────────────────
set(SOURCES
    src/main.cpp
    src/modulator.cpp
    src/filters.cpp
    src/transceiver.cpp
    src/synchronization.cpp
)

# ── Executable ───────────────────────────────────────────────
add_executable(sdr_system ${SOURCES})

set(LINK_LIBS
    ${UHD_LIBRARIES}
    ${BOOST_PO_LIB}
    Threads::Threads
    ${FFTW3F_LIB}
)
if(FFTW3F_THREADS_LIB)
    list(APPEND LINK_LIBS ${FFTW3F_THREADS_LIB})
endif()

target_link_libraries(sdr_system ${LINK_LIBS})

# ── Output directories ───────────────────────────────────────
add_custom_command(TARGET sdr_system POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory
        ${CMAKE_BINARY_DIR}/debugging
        ${CMAKE_BINARY_DIR}/transmit
        ${CMAKE_BINARY_DIR}/received
    COMMENT "Creating output directories")

# ── Auto-generate the Python API from the CLI ────────────────
# Regenerate python/sdr.py from `sdr_system --help` on every build, so the
# Python wrapper always exposes exactly the current C++ options. The generator
# always exits 0, so a hiccup here never breaks the C++ build.
find_program(PYTHON3_EXE NAMES python3 python)
if(PYTHON3_EXE)
    add_custom_command(TARGET sdr_system POST_BUILD
        COMMAND ${PYTHON3_EXE} ${CMAKE_SOURCE_DIR}/tools/gen_python_api.py
                --binary $<TARGET_FILE:sdr_system>
                --out   ${CMAKE_SOURCE_DIR}/python/sdr.py
        COMMENT "Regenerating python/sdr.py from sdr_system --help")
else()
    message(STATUS "python3 not found — skipping python/sdr.py auto-generation")
endif()

# ── Summary ──────────────────────────────────────────────────
message(STATUS "Build type: ${CMAKE_BUILD_TYPE}")
message(STATUS "Compiler:   ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}")
message(STATUS "UHD:        ${UHD_LIBRARIES}")
