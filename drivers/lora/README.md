# `lora/` — the LoRa PHY driver (SX1276)

A second physical layer behind the **same uniform API** as `usrp/`. Every algorithm in
`experiments/` and every application in `experiments/` runs over LoRa **without a single
line changing** — that portability is the point of the two-layer split.

```
./run.sh --algo echo       --channel lora --lora-sf 9        # echo over LoRa
./run.sh --algo fl         --channel lora --lora-sf 9        # federated learning over LoRa
./run.sh --algo dl --role gossip --agents 4 --channel lora   # decentralized learning over LoRa
./run.sh --algo marl_multi --role multi   --channel lora     # random access over LoRa
```

Ported from the LoRa testbed work under `~/Desktop/Project Codes/LoRa/`: the firmware is the
multipath testbed's `lora_phy.ino`, the serial API follows `bridge.py:ArduinoLink`, and the
SPI API follows `pi_bridge.py:Radio`.

## Layout

```
lora/
├── DEVICES.md            WHAT WE HAVE: the 8 Pi + Teensy nodes, and how to reach them
├── arduino/lora_phy/lora_phy.ino   PHY firmware for the Arduino/Teensy + SX1276
├── deploy/
│   ├── inventory.sh          the node map (hostnames, IPs, serial devices) — no secrets
│   ├── credentials.sh.example template; the real credentials.sh is gitignored
│   ├── check_devices.sh      which Pis are up, and is a radio attached?
│   └── push_to_pis.sh        copy the driver to every reachable Pi
├── python/
│   ├── lora_radio.py     the LoRa module API — one interface, three attachments
│   ├── framing.py        fragmentation + stop-and-wait ARQ (the 255-byte MTU)
│   └── lora_driver.py    the uniform API: LoRaChannel · LoRaLink (roles) · LoRaDriver
└── tools/
    ├── role_selftest.py  both link ends on threads over one simulated medium — checks the
    │                     tx/rx protocol, addressing, fragmentation and ARQ with NO hardware
    └── spi_selftest.py   two real nodes, point-to-point link check (SNR/RSSI), no stack
```

```bash
python3 tools/role_selftest.py                     # 3 rounds, SF9, clean link
python3 tools/role_selftest.py --bytes 4000        # force multi-fragment messages
python3 tools/role_selftest.py --sf 12 --snr -18   # a marginal link (losses are expected)
```

## Three ways to attach a radio

All three implement exactly the same interface, so everything above them is written once:

| `--lora-backend` | Hardware | Notes |
|---|---|---|
| `sim` (default) | none | Shared in-process medium, real Semtech airtime, SNR/SF loss model. Develop and test the whole stack, then change one flag. |
| `serial` | Arduino/Teensy + SX1276 on USB | Runs `arduino/lora_phy/lora_phy.ino`; ASCII line protocol at 115200 baud. |
| `spi` | Raspberry Pi, SX1276 on the Pi's SPI bus | `adafruit_rfm9x`, no Arduino in the path. |

```python
configure(sf, cr, bw_hz, power_dbm, freq_hz)
send(data: bytes)      -> time-on-air in ms
recv(timeout: float)   -> (payload, snr_db, rssi_dbm) | None
stats() / close()
```

## The firmware's serial protocol

```
->  CFG SF=<7..12> CR=<5..8> P=<2..20> BW=<Hz> FQ=<Hz>     <-  OK CFG
->  TX ID=<u32> LEN=<n> HEX=<2n chars>                     <-  OK TX ID=<u32> TOA_MS=<int>
->  RXON / RXOFF / STAT / PING / RESET / REBOOT
<-  RX LEN=<n> HEX=<...> SNR=<float> RSSI=<int> CRC=<OK|FAIL>
<-  STAT TX= ACK= RXOK= RXBAD= REINIT= DEAF_MS=
```

The sketch carries the fixes documented in its own header — the RX state machine that made
receive-only nodes go deaf, the non-blocking line reader, the corrected Semtech airtime, and
the `RESET`/`REBOOT`/watchdog recovery path. Read that header before changing it.

## The 255-byte problem

LoRa's MTU is 255 bytes. It is a property of the PHY, not a tunable. A federated-learning
model vector is ~200 kB, so `framing.py` fragments every message, numbers the pieces, and
retransmits what does not decode:

```
magic(1)=0x4C  src(1)  dst(1)  msg_id(1)  frag_idx(2)  n_frags(2)  payload(<=247)
ACK:  magic(1)=0x41  src(1)  dst(1)  msg_id(1)  frag_idx(2)
```

The cost is reported rather than hidden. Every transfer returns `frags`, `retx` and
`airtime_ms`, and a run ends with a PHY summary:

```
[run_algo] lora PHY: {'msgs': 4, 'frags': 416, 'retx': 0, 'lost': 0, 'airtime_s': 165.09, 'sf': 7, ...}
```

Two FL rounds with a 25 kB model: **165 s of airtime at SF7, 3727 s at SF12** — the spreading
factor trade-off, in the units that matter. This is why LoRa is a sensing and telemetry PHY,
not a model-transport PHY, and having the number visible is what makes that argument.

## Roles work here too

Roles belong to the middleware, not to a driver, so every role runs on this PHY. What
changes is one flag:

```bash
./run.sh --algo fl --role loopback --channel lora        # one process
./run.sh --algo dl --role gossip --agents 6 --channel lora
./run.sh --algo fl --role server --channel lora --lora-backend serial --lora-port /dev/ttyUSB0
./run.sh --algo fl --role client --channel lora --lora-backend serial --lora-port /dev/ttyUSB0
```

One simplification over the USRP link: a LoRa module is a **transceiver**, so the reply
comes back over the radio. The USRP rig sends its reply over TCP only because the N210
there is receive-only. The two ends strictly alternate, which is what the uniform API's
request/response shape already does.

`tools/role_selftest.py` runs both ends on threads over one simulated medium, so the
role protocol, addressing, fragmentation and ARQ can be checked with no hardware.

## Uniform where it should be, PHY-specific where it must be

Different physical layers really do have different logic, and the API does not pretend
otherwise. The USRP PHY is assembled from parts we choose — modulation, FEC, the ARQ and
where its acknowledgements travel. A LoRa module is a chip: **CRC and modulation are
embedded**, and it offers its own knobs instead.

| | Uniform (all PHYs) | PHY-specific |
|---|---|---|
| Algorithms see | bytes in, bytes out, `info["crc_ok"]` | nothing |
| Shared knobs | `--freq` (MHz), `--max-attempts` | |
| USRP | | `--scheme/--modulation --fec --samp-rate --symbol-rate --tx-gain --rx-gain --ack-transport --ack-timeout --tx-args --rx-args` |
| LoRa | | `--lora-sf --lora-cr --lora-bw --lora-power --lora-backend --lora-port` |

**ARQ.** Both PHYs run stop-and-wait — it is the only scheme the C++ PHY implements
(`ACQ_stop_and_wait.hpp`) and what `framing.py` does here. What differs is where the
acknowledgement travels, and only the USRP has a choice: `--ack-transport tcp` (a socket)
or `rf` (a second RF path, RF B, needing full duplex). A LoRa module is a transceiver and
acknowledges over the same radio, so there is nothing to configure. `--max-attempts` is
shared but defaults per PHY — 50 for the USRP, 8 for LoRa, because a LoRa retry costs a
whole frame of airtime.

`info` carries a common core (`crc_ok`, `ber`) plus each PHY's own accounting — `frags`,
`retx` and `airtime_ms` here. Passing one PHY's knob while another is selected prints a
NOTE rather than being silently ignored. An algorithm that needs a spreading factor is a
LoRa algorithm, not a portable one.

## What LoRa can and cannot do

| Archetype | Verb | LoRa |
|---|---|---|
| Data transfer | `transfer()` | Yes — fragmentation + ARQ |
| Slotted multi-access | `broadcast()` | Yes — it is genuinely a shared medium, so two nodes in one slot really do collide |
| Over-the-air computation | `superpose()` | **No** — needs coherent superposition of simultaneous transmissions, which an SX1276 packet radio cannot do. Use `usrp`. |

## The physical testbed

**[`DEVICES.md`](DEVICES.md)** is the inventory: 8 nodes, each a Raspberry Pi (`pi01`…`pi08`)
bridged over USB to a Teensy 4.0 + RFM95. It lists every hostname, Tailscale IP and serial
device, and explains how credentials are handled — **no password is stored in this repository**.
The SSH user is each Pi's own hostname; the secret lives in an untracked `deploy/credentials.sh`,
or better, nowhere at all if you use SSH keys.

```bash
cd deploy
./check_devices.sh     # who is up, and is a radio attached?
./push_to_pis.sh       # copy the driver to every reachable Pi (unreachable ones are skipped)
```

## Hardware setup

1. **Flash** `arduino/lora_phy/lora_phy.ino` to each node (Arduino IDE or `arduino-cli`). The
   firmware is identical on every node; the role is assigned by the experiment, not the sketch.
   Pins are at the top of the sketch (Teensy 4.0 + RFM95: SS=10, RST=9, DIO0=2).
2. **Find the port**: `ls /dev/ttyUSB* /dev/ttyACM*`. Make sure your user is in `dialout`.
3. **Check the link before the stack**: `python3 tools/spi_selftest.py --role rx` on one node
   and `--role tx` on the other. If this does not work, nothing above it will.
4. **Run**: `./run.sh --algo echo --channel lora --lora-backend serial --lora-port /dev/ttyUSB0`

## Status

- Built and tested on the `sim` backend: `echo`, `fl`, `dl` and `marl_multi` over every
  group role (`loopback`, `chain`, `gossip`, `multi`), plus the `tx`/`rx` link roles via
  `tools/role_selftest.py`. Loss, ARQ and addressing behave correctly across the SNR range.
- Written against the proven firmware/bridge logic but **not yet exercised on real
  hardware**: the `serial` and `spi` backends, and therefore the on-air role runs.
- Known limitation: the simulated medium is per-process, so peers started in separate
  terminals cannot hear each other on `--lora-backend sim`. Real radios share real air;
  the sim needs one process (`--role gossip`). The run says so if you try.
