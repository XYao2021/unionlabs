#!/usr/bin/env python3
"""
driver.py — the PhyDriver interface: the ONE seam every (PHY × testbed) backend implements.

This is the boundary between the two UnionLabs layers:

    ┌─ ABSTRACTION / MIDDLEWARE (this folder, `union/`) ─ one, shared across all testbeds+PHYs ─┐
    │   algorithms/ + algorithms/  ->  phy_link (SdrApp/PayloadSpec/Codec) + run_algo         │
    │   call ↓ this interface only — never a specific PHY or testbed                             │
    ├─ PhyDriver ─────────────────────────────────────────────────────────────────────────────┤
    │   transfer() · broadcast() · superpose()                                                  │
    └─ DRIVER LAYER (`drivers/<name>/`) ─ many, one per (PHY × testbed) ─────────────────────────┘
        drivers/usrp  ·  drivers/sim  ·  drivers/lora  ·  drivers/usrp_powder …

Adding a new PHY or testbed = implementing this class. Nothing in `algorithms/` or
`algorithms/` changes — that is the portability UnionLabs provides over POWDER/AERPAW.

WHAT IS UNIFORM AND WHAT IS NOT — the line this interface draws
---------------------------------------------------------------
Different physical layers genuinely have different logic, and a uniform API that
pretends otherwise would be a lie that leaks. The USRP PHY is built out of parts we
choose: modulation scheme, FEC, the ARQ and where its acknowledgements travel. A LoRa
module is a chip: CRC and the modulation are embedded, and the knobs it offers are its
own (spreading factor, bandwidth, coding rate, TX power). Neither set belongs in the
other's vocabulary.

So the contract is deliberately narrow. UNIFORM, and all an algorithm may rely on:

    * carry these BYTES to the peer, whatever they are and however many;
    * tell me whether they arrived   -> info["crc_ok"];
    * tell me what it cost           -> info, whose extra keys are the PHY's own
                                        (ber and snr_db for the modem; frags, retx and
                                        airtime_ms for LoRa).

PHY-SPECIFIC, and never promoted into the shared layer:

    usrp   --scheme --fec --tx-args --rx-args --ack-host --ack-port
           (we assemble the waveform, the coding and the acknowledgement path)
    lora   --lora-sf --lora-cr --lora-bw --lora-power --lora-port
           (the chip owns the modulation and the CRC; the driver adds only the
            fragmentation and ARQ that a 255-byte MTU forces on it)

An algorithm that needs a spreading factor is a LoRa algorithm, not a portable one.
Selecting the PHY (--channel) and how it is attached (--<phy>-backend) is the
experimenter's business; everything above the driver only ever sees bytes and crc_ok.

Three verbs cover the three experiment archetypes:

    transfer(payload)  -> (reply_bytes, info)   round-trip link       fl · clip_semcom · echo
    broadcast(bursts)  -> acks / info           slotted multi-access  marl · marl_multi
    superpose(coded)   -> combined              over-the-air compute  stc_aircomp (AJOU)

Reference implementations already exist in `phy_link.py` (this is a formalisation of the seam
that is already there, not new machinery):

    IdealChannel / PyphyChannel  .transfer()   -> drivers/sim        (radio-free)
    RadioRoundTrip               .transfer()   -> drivers/usrp   (real USRP link)
    run_slotted(...)             broadcast()   -> the slotted-medium driver
    stc_core.aircomp_codeword    superpose()   -> drivers/usrp   (capture2, on hardware)
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
    """The drivers shipped today (see drivers/<name>/). `sim`, `usrp` and `lora` are built;
    others are placeholders that only need to implement PhyDriver."""
    return {
        "sim":          "radio-free: IdealChannel (lossless) / PyphyChannel (real modem + AWGN)",
        "usrp":         "USRP over UHD: sdr_system (C++) + pyphy blocks + RadioRoundTrip",
        "lora":         "SX1276 LoRa: sim / Arduino-serial / Pi-SPI, 255 B MTU + ARQ "
                        "(--channel lora). No superpose() — a packet radio cannot do aircomp",
        "usrp_powder":  "(planned) same USRP PHY on the POWDER testbed — a different driver",
    }
