#!/usr/bin/env python3
"""
driver.py — the PhyDriver interface: the ONE seam every (PHY × testbed) backend implements.

This is the boundary between the two UnionLabs layers:

    ┌─ ABSTRACTION / MIDDLEWARE (this folder, `union/`) ─ one, shared across all testbeds+PHYs ─┐
    │   algorithms/ + applications/  ->  phy_link (SdrApp/PayloadSpec/Codec) + run_algo         │
    │   call ↓ this interface only — never a specific PHY or testbed                             │
    ├─ PhyDriver ─────────────────────────────────────────────────────────────────────────────┤
    │   transfer() · broadcast() · superpose()                                                  │
    └─ DRIVER LAYER (`drivers/<name>/`) ─ many, one per (PHY × testbed) ─────────────────────────┘
        drivers/usrp_uhd  ·  drivers/sim  ·  drivers/lora_arduino  ·  drivers/usrp_powder …

Adding a new PHY or testbed = implementing this class. Nothing in `algorithms/` or
`applications/` changes — that is the portability UnionLabs provides over POWDER/AERPAW.

Three verbs cover the three experiment archetypes:

    transfer(payload)  -> (reply_bytes, info)   round-trip link       fl · clip_semcom · echo
    broadcast(bursts)  -> acks / info           slotted multi-access  marl · marl_multi
    superpose(coded)   -> combined              over-the-air compute  stc_aircomp (AJOU)

Reference implementations already exist in `phy_link.py` (this is a formalisation of the seam
that is already there, not new machinery):

    IdealChannel / PyphyChannel  .transfer()   -> drivers/sim        (radio-free)
    RadioRoundTrip               .transfer()   -> drivers/usrp_uhd   (real USRP link)
    run_slotted(...)             broadcast()   -> the slotted-medium driver
    stc_core.aircomp_codeword    superpose()   -> drivers/usrp_uhd   (capture2, on hardware)
"""


class PhyDriver:
    """Contract every backend (PHY × testbed) implements. The middleware calls these; it never
    imports a concrete PHY. Implement the verb(s) your PHY supports; leave the rest raising."""

    name = "abstract"

    # ── archetype 1: data-transfer link (fl, clip_semcom, echo) ──
    def transfer(self, payload):
        """Carry `payload` bytes one round-trip; return (reply_bytes, info) where
        info carries at least {'crc_ok': bool}."""
        raise NotImplementedError

    # ── archetype 2: slotted multi-access (marl, marl_multi) ──
    def broadcast(self, bursts):
        """Resolve one slotted-medium round: `bursts` is one payload-or-None per node.
        Return per-node ack/info (0 tx = idle, 1 = delivered iff decoded, ≥2 = collision)."""
        raise NotImplementedError

    # ── archetype 3: over-the-air computation (stc_aircomp / AJOU) ──
    def superpose(self, coded):
        """Fire N nodes' pre-coded symbols simultaneously; return the AP's combined
        (summed) observation — the medium performs the computation."""
        raise NotImplementedError

    def close(self):
        """Release radios / sockets. No-op for radio-free drivers."""
        pass


def available():
    """The drivers shipped today (see drivers/<name>/). `sim` and `usrp_uhd` are built;
    others are placeholders that only need to implement PhyDriver."""
    return {
        "sim":          "radio-free: IdealChannel (lossless) / PyphyChannel (real modem + AWGN)",
        "usrp_uhd":     "USRP over UHD: sdr_system (C++) + pyphy blocks + RadioRoundTrip",
        "lora_arduino": "(planned) Arduino LoRa TX/RX — a different PHY, same contract",
        "usrp_powder":  "(planned) same USRP PHY on the POWDER testbed — a different driver",
    }
