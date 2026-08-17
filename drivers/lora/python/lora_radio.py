#!/usr/bin/env python3
"""
lora_radio.py — the LoRa module API: one interface, three attachments.

This is the bottom of the LoRa driver. It exposes ONE radio interface and three
ways a SX1276 can actually be attached, so everything above it (framing, the
uniform PhyDriver) is written once:

    serial   Arduino/Teensy + SX1276 on USB, running arduino/lora_phy/lora_phy.ino.
             Line protocol, 115200 baud (the firmware is the multipath testbed's).
    spi      Raspberry Pi with the SX1276 wired to its own SPI bus (adafruit_rfm9x).
             No Arduino in the path at all.
    sim      No hardware: a shared in-process medium with the real Semtech airtime
             formula and an SNR/spreading-factor loss model. Develop and test the
             whole stack, then change one flag to move to a radio.

    radio = make_radio("sim", sf=9)                       # radio-free
    radio = make_radio("serial", port="/dev/ttyUSB0")     # Arduino over USB
    radio = make_radio("spi", freq_mhz=915.0)             # Pi + RFM9x on SPI

THE INTERFACE (all three implement exactly this)

    configure(sf, cr, bw_hz, power_dbm, freq_hz)  ->  None
    send(data: bytes)                             ->  time-on-air in ms
    recv(timeout: float)                          ->  (payload, snr_db, rssi_dbm) | None
    stats()                                       ->  dict of counters
    close()                                       ->  None

MTU is 255 bytes — a LoRa PHY payload limit, not a choice. Anything larger is the
framing layer's problem (see framing.py).
"""
import os
import struct
import sys
import time

MTU = 255                    # SX1276 LoRa maximum payload, bytes

# Demodulator SNR floor per spreading factor (Semtech SX1276 datasheet, table 13).
# Higher SF buys ~2.5 dB per step at the cost of exponentially longer airtime.
SNR_FLOOR_DB = {7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}


def time_on_air_ms(payload_len, sf=9, cr=5, bw_hz=125000, preamble=8,
                   explicit_header=True, crc_on=True):
    """Exact Semtech LoRa airtime. Ported from the testbed firmware, including the
    two corrections found there the hard way:

        DE (low-data-rate optimize) is 1 only when the symbol time exceeds 16 ms
        (SF11/SF12 at BW125) — NOT always 1.
        IH is 1 only in IMPLICIT header mode — this PHY uses explicit headers, so 0.

    Getting this wrong under-reports SF12 airtime by ~17%, which lets the next
    transmission start while the air is still busy."""
    t_sym = (2.0 ** sf) / float(bw_hz) * 1000.0                  # ms
    de = 1 if t_sym > 16.0 else 0
    ih = 0 if explicit_header else 1
    t_preamble = (preamble + 4.25) * t_sym
    num = 8.0 * payload_len - 4.0 * sf + 28.0 + (16.0 if crc_on else 0.0) - 20.0 * ih
    den = 4.0 * (sf - 2.0 * de)
    import math
    n_sym = max(math.ceil(num / den) * (cr - 4 + 4), 0)          # (CR denom 5..8) -> +4
    return t_preamble + (8.0 + n_sym) * t_sym


class LoRaRadio:
    """The interface every attachment implements. Subclasses fill in send/recv."""
    kind = "abstract"
    mtu = MTU

    def __init__(self, sf=9, cr=5, bw_hz=125000, power_dbm=14, freq_hz=915_000_000):
        self.sf, self.cr, self.bw_hz = int(sf), int(cr), int(bw_hz)
        self.power_dbm, self.freq_hz = int(power_dbm), int(freq_hz)
        self.n_tx = self.n_rx = self.n_lost = 0
        self.airtime_ms = 0.0

    def configure(self, sf=None, cr=None, bw_hz=None, power_dbm=None, freq_hz=None):
        if sf is not None:        self.sf = int(sf)
        if cr is not None:        self.cr = int(cr)
        if bw_hz is not None:     self.bw_hz = int(bw_hz)
        if power_dbm is not None: self.power_dbm = int(power_dbm)
        if freq_hz is not None:   self.freq_hz = int(freq_hz)

    def toa_ms(self, n):
        return time_on_air_ms(n, self.sf, self.cr, self.bw_hz)

    def send(self, data):
        raise NotImplementedError

    def recv(self, timeout=1.0):
        raise NotImplementedError

    def stats(self):
        return dict(kind=self.kind, sf=self.sf, cr=self.cr, bw_hz=self.bw_hz,
                    tx=self.n_tx, rx=self.n_rx, lost=self.n_lost,
                    airtime_s=round(self.airtime_ms / 1000.0, 3))

    def close(self):
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  sim — a shared medium, no hardware
# ══════════════════════════════════════════════════════════════════════════════
class SimMedium:
    """The air, shared by every SimRadio attached to it.

    A transmission reaches every OTHER attached radio, and is decoded when the link
    SNR clears that spreading factor's demodulator floor; near the floor it is
    probabilistic, so a run at a marginal SNR behaves like a marginal link. Airtime
    is the real Semtech number, so the cost of a large payload at SF12 is honest
    even though no radio is involved."""

    def __init__(self, snr_db=0.0, seed=0, capture=True):
        import numpy as np
        self.np = np
        self.snr_db = float(snr_db)
        self.rng = np.random.RandomState(seed)
        self.capture = capture
        self.radios = []
        self.clock_ms = 0.0                # advances by each transmission's airtime
        self.n_frames = 0
        self.n_dropped = 0

    def attach(self, radio):
        self.radios.append(radio)
        return len(self.radios) - 1

    def _decodes(self, sf):
        """P(decode) from the SNR margin over this SF's floor: a soft edge rather
        than a cliff, which is what a real link looks like."""
        margin = self.snr_db - SNR_FLOOR_DB.get(int(sf), -12.5)
        if margin >= 3.0:
            return True
        if margin <= -3.0:
            return False
        p = (margin + 3.0) / 6.0                       # linear ramp across +-3 dB
        return bool(self.rng.rand() < p)

    def carry(self, src, data):
        """One transmission: charge airtime, then deliver to each peer or drop it."""
        toa = time_on_air_ms(len(data), src.sf, src.cr, src.bw_hz)
        self.clock_ms += toa
        self.n_frames += 1
        for r in self.radios:
            if r is src:
                continue
            if self._decodes(src.sf):
                snr = self.snr_db + float(self.rng.randn() * 0.5)
                r._inbox.append((bytes(data), snr, -120.0 + snr))
            else:
                self.n_dropped += 1
                r.n_lost += 1
        return toa


class SimRadio(LoRaRadio):
    """A LoRa module on a simulated medium. Same interface as the real ones."""
    kind = "sim"

    def __init__(self, medium, **kw):
        super().__init__(**kw)
        self.medium = medium
        self._inbox = []
        self.index = medium.attach(self)

    def send(self, data):
        if len(data) > self.mtu:
            raise ValueError(f"{len(data)} B exceeds the LoRa MTU of {self.mtu} B "
                             f"— fragment it (see framing.py)")
        toa = self.medium.carry(self, data)
        self.n_tx += 1
        self.airtime_ms += toa
        return toa

    def recv(self, timeout=1.0):
        if not self._inbox:
            # timeout=0 is the single-process case: nothing is in flight, say so at once.
            # A positive timeout means someone (another thread, i.e. the other end of a
            # link) may still deliver, so yield briefly instead of spinning on a core.
            if timeout and timeout > 0:
                time.sleep(min(float(timeout), 0.001))
            return None
        payload, snr, rssi = self._inbox.pop(0)
        self.n_rx += 1
        return payload, snr, rssi


# ══════════════════════════════════════════════════════════════════════════════
#  serial — Arduino / Teensy running arduino/lora_phy/lora_phy.ino
# ══════════════════════════════════════════════════════════════════════════════
class SerialRadio(LoRaRadio):
    """Talks the firmware's ASCII line protocol over USB:

        ->  CFG SF=<7..12> CR=<5..8> P=<2..20> BW=<Hz> FQ=<Hz>      <-  OK CFG
        ->  TX ID=<u32> LEN=<n> HEX=<2n chars>                      <-  OK TX ID= TOA_MS=
        ->  RXON / RXOFF / STAT / PING / RESET / REBOOT
        <-  RX LEN=<n> HEX=<...> SNR=<float> RSSI=<int> CRC=<OK|FAIL>

    The firmware only surfaces CRC-valid packets, so a corrupt frame appears here as
    silence rather than as a bad RX — the framing layer's ARQ is what turns that
    silence back into a retransmission."""
    kind = "serial"

    def __init__(self, port="/dev/ttyUSB0", baud=115200, boot_wait=5.0, **kw):
        super().__init__(**kw)
        try:
            import serial
        except ImportError:
            raise SystemExit("pyserial is required for the serial backend: "
                             "pip install pyserial")
        self.port, self.baud = port, baud
        self.ser = serial.Serial(port, baud, timeout=0.5)
        self._tx_id = 0
        self._pending = []                       # RX lines read while awaiting a reply
        # The board resets when the port opens; wait for its PONG banner.
        deadline = time.time() + boot_wait
        while time.time() < deadline:
            if self._readline().startswith("PONG"):
                break
        self.configure()                          # push our config to the firmware
        self._write("RXON")

    def _write(self, line):
        self.ser.write((line + "\n").encode())
        self.ser.flush()

    def _readline(self):
        return self.ser.readline().decode("utf-8", "replace").strip()

    def configure(self, sf=None, cr=None, bw_hz=None, power_dbm=None, freq_hz=None):
        super().configure(sf, cr, bw_hz, power_dbm, freq_hz)
        self._write(f"CFG SF={self.sf} CR={self.cr} P={self.power_dbm} "
                    f"BW={self.bw_hz} FQ={self.freq_hz}")

    def send(self, data):
        if len(data) > self.mtu:
            raise ValueError(f"{len(data)} B exceeds the LoRa MTU of {self.mtu} B")
        self._tx_id = (self._tx_id + 1) & 0xFFFFFFFF
        self._write(f"TX ID={self._tx_id} LEN={len(data)} HEX={data.hex().upper()}")
        toa = float(self.toa_ms(len(data)))
        deadline = time.time() + 30.0
        while time.time() < deadline:             # wait for the firmware's TX report
            line = self._readline()
            if line.startswith("OK TX"):
                kv = dict(t.split("=", 1) for t in line.split()[2:] if "=" in t)
                toa = float(kv.get("TOA_MS", toa))
                break
            if line.startswith("RX "):
                self._pending.append(line)        # a frame arrived mid-transmit
            elif line.startswith("ERR"):
                raise IOError(f"LoRa firmware: {line}")
        self.n_tx += 1
        self.airtime_ms += toa
        return toa

    def _parse_rx(self, line):
        kv = dict(t.split("=", 1) for t in line.split()[1:] if "=" in t)
        if kv.get("CRC", "FAIL") != "OK":
            self.n_lost += 1
            return None
        payload = bytes.fromhex(kv.get("HEX", ""))
        self.n_rx += 1
        return payload, float(kv.get("SNR", "0")), float(kv.get("RSSI", "0"))

    def recv(self, timeout=1.0):
        while self._pending:
            got = self._parse_rx(self._pending.pop(0))
            if got:
                return got
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._readline()
            if line.startswith("RX "):
                got = self._parse_rx(line)
                if got:
                    return got
        return None

    def reset(self):
        self._write("RESET")

    def close(self):
        try:
            self._write("RXOFF")
            self.ser.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  spi — Raspberry Pi with the SX1276 on its own SPI bus (no Arduino)
# ══════════════════════════════════════════════════════════════════════════════
class SpiRadio(LoRaRadio):
    """adafruit_rfm9x on the Pi's SPI bus. Address filtering is disabled (node and
    destination both 0xFF) so the driver's own header does all the addressing."""
    kind = "spi"

    _REG_PKT_SNR = 0x19

    def __init__(self, freq_mhz=915.0, cs="CE0", reset="D25", **kw):
        super().__init__(freq_hz=int(freq_mhz * 1e6), **kw)
        import board, busio, digitalio, adafruit_rfm9x     # noqa: E401
        spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)
        self.rfm = adafruit_rfm9x.RFM9x(spi, digitalio.DigitalInOut(getattr(board, cs)),
                                        digitalio.DigitalInOut(getattr(board, reset)),
                                        float(freq_mhz))
        self.rfm.node = 0xFF
        self.rfm.destination = 0xFF
        self.rfm.enable_crc = True
        self.configure()

    def configure(self, sf=None, cr=None, bw_hz=None, power_dbm=None, freq_hz=None):
        super().configure(sf, cr, bw_hz, power_dbm, freq_hz)
        self.rfm.spreading_factor = self.sf
        self.rfm.coding_rate = self.cr
        self.rfm.signal_bandwidth = self.bw_hz
        self.rfm.tx_power = max(5, min(23, self.power_dbm))
        self.rfm.frequency_mhz = self.freq_hz / 1e6

    def send(self, data):
        if len(data) > self.mtu:
            raise ValueError(f"{len(data)} B exceeds the LoRa MTU of {self.mtu} B")
        t0 = time.monotonic()
        self.rfm.send(bytes(data), keep_listening=True)
        toa = max(1.0, (time.monotonic() - t0) * 1000.0)
        self.n_tx += 1
        self.airtime_ms += toa
        return toa

    def recv(self, timeout=1.0):
        pkt = self.rfm.receive(timeout=timeout, keep_listening=True, with_header=False)
        if pkt is None:
            return None
        snr = getattr(self.rfm, "last_snr", None)
        if snr is None:
            raw = self.rfm._read_u8(self._REG_PKT_SNR)      # noqa: SLF001
            snr = (raw - 256 if raw > 127 else raw) / 4.0
        self.n_rx += 1
        return bytes(pkt), float(snr), float(self.rfm.last_rssi)


def make_radio(backend="sim", medium=None, **kw):
    """One call for all three attachments. `sim` makes its own medium if none given."""
    if backend == "sim":
        if medium is None:
            medium = SimMedium(snr_db=kw.pop("snr_db", 0.0), seed=kw.pop("seed", 0))
        else:
            kw.pop("snr_db", None); kw.pop("seed", None)
        return SimRadio(medium, **kw)
    if backend == "serial":
        kw.pop("snr_db", None); kw.pop("seed", None)
        return SerialRadio(**kw)
    if backend == "spi":
        kw.pop("snr_db", None); kw.pop("seed", None)
        return SpiRadio(**kw)
    raise ValueError(f"LoRa backend must be sim | serial | spi (got {backend!r})")
