#pragma once
// Empty stand-in for the real UHD-backed transceiver.hpp, used ONLY to build the
// hardware-free unit demos below. modulator.cpp includes "transceiver.hpp" but
// uses no UHD symbols, so an empty header lets the modulator compile on its own.
