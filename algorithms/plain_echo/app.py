#!/usr/bin/env python3
"""
app.py — the ONLY glue between an untouched algorithm and the uniform API.

echo_algo.py is a plain algorithm with no idea the PHY exists. All this binding does
is say "here is how to make one for a given role". No phy_link import, no SdrApp, no
radio code — the framework reads the algorithm's transmit()/receive() and handles the
rest (codec, round-trip, modem, radio).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from echo_algo import EchoAlgo


def make(role):                              # the framework calls this per node/role
    return EchoAlgo(role)
