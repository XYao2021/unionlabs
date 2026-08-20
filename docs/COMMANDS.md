# Ready-to-run commands

Four levels, in the order you should meet them: **prove the radio works**, then change one
knob at a time, then run a real algorithm over it, then drive the modem directly. Every
option is explained in [`BEGINNER_GUIDE.md §3`](BEGINNER_GUIDE.md); the auto-generated list
of every modem flag is [`PARAMETERS.md`](PARAMETERS.md).

---

## 1. Start here — is the link alive? (`./radio.sh`)

Raw bursts, no protocol: the shortest path from "the radio is plugged in" to "bits crossed
the air". **Start the receiver first**, then transmit.

```bash
# terminal 1 — receiver
./radio.sh rx --device n210 --args addr=192.168.10.2

# terminal 2 — transmitter
./radio.sh tx --device n210 --args addr=192.168.10.2 --gain 30
```

`radio.sh` fills in a working setup so these two lines are complete on their own:
**915 MHz, DQPSK, single-carrier, 2 MS/s, 1 MSym/s, FEC on**, N210 gains tx/rx = 25/25
(B210: 78/20). TX leaves the **TX/RX** port, RX listens on **RX2**.

Two practical notes:

- **`--args` is not optional in practice.** The built-in default for `--device n210` is
  `addr=192.168.20.2`; give the address your radio actually has.
- **The two roles need two radios.** UHD gives a device to one process exclusively, so one
  N210 cannot transmit and receive at once. With a single radio, run them one at a time as a
  smoke test (RX alone shows the noise floor; TX alone confirms the transmit chain).

Print the exact command a wrapper would run, without running it:

```bash
./radio.sh tx --device n210 --args addr=192.168.10.2 --dry-run
```

Cabling instead of antennas? Put a **30–40 dB attenuator** between TX/RX and RX2. Feeding a
transmitter straight into a receiver input damages it.

### ⚠ Check the antennas before transmitting

**Transmitting into a connector with no antenna reflects the power back into the amplifier.**
Only some ports on this rig are cabled, so not every command below is safe to run as-is:

| Radio | Cabled connector | Can therefore |
|---|---|---|
| **N210** `addr=192.168.10.2` | `A:0` **RX2** | **receive only** |
| **B210** `serial=30CD424` | **RF A** (`A:A`) **TX/RX** | **transmit** (RF A) |
| **B210** `serial=30CD3F7` | **RF B** (`A:B`) **RX2** | **receive** (RF B) |

Safe pairs, with the flags that make them match the cabling:

```bash
# TX: B210 30CD424 (RF A, TX/RX — all defaults)
./radio.sh tx --device b210 --args serial=30CD424

# RX option 1: the N210 (its defaults already point at A:0 / RX2)
./radio.sh rx --device n210 --args addr=192.168.10.2

# RX option 2: B210 30CD3F7 — needs --subdev A:B, because the default A:A has no antenna
./radio.sh rx --device b210 --args serial=30CD3F7 --subdev A:B
```

**Do not run these on this rig:**

| Command | Why |
|---|---|
| `./radio.sh tx --device n210 …` | transmits out the N210's TX/RX, which is bare |
| `./radio.sh tx --device b210 --args serial=30CD3F7` | transmits out RF A TX/RX on 3F7, which is bare |
| `./radio.sh rx --device b210 --args serial=30CD3F7` *(no `--subdev`)* | listens on RF A RX2 — bare, so it hears nothing and looks like a dead link |

Recabled since? Update the table above — the commands throughout this file are written to
match it.

### Send a complete message and stop

Nothing to enable — this is the default. `--stop-on-complete` is **`true`**, the payload is
finite (`--message-type bytes`) and `--tx-mode` is `burst`, so the **receiver exits as soon
as every chunk is CRC-verified**. Two other defaults matter here: `--tx-reps` is **20** (it
cycles the whole message 20 times), so pass `--tx-reps 1` for a quick test, and
`--rx-idle-timeout` is **8 s**, which stops a receiver that never hears anything.

```bash
# receiver — B210 30CD3F7 on RF B          |  or the N210:
./radio.sh rx --device b210 \               #  ./radio.sh rx --device n210 \
  --args serial=30CD3F7 --subdev A:B       #    --args addr=192.168.10.2

# transmitter — B210 30CD424, short known message, one pass
./radio.sh tx --device b210 --args serial=30CD424 --message "hello unionlabs" --tx-reps 1
```

`--stop-on-complete false` keeps the receiver running instead — for collecting duplicates or
measuring the link.

### With ACKs: the transmitter waits for confirmation (ARQ)

`--role tx` / `rx` are **one-way — no ACK at all**. Retransmission needs the ARQ roles, which
the wrapper reaches through `--role` while keeping every tuned default:

```bash
# sink: CRC-checks, ACKs, and exits when the message is complete
./radio.sh rx --device b210 --args serial=30CD3F7 --subdev A:B \
  --role sink_arq --ack-port 5599
#   or on the N210:
./radio.sh rx --device n210 --args addr=192.168.10.2 --role sink_arq --ack-port 5599

# source: waits for each ACK and resends until it arrives
./radio.sh tx --device b210 --args serial=30CD424 \
  --role source_arq --ack-host 127.0.0.1 --ack-port 5599 --max-attempts 0
```

- `--ack-host` is `127.0.0.1` only because both processes are on one machine. **On two
  machines it must be the sink's IP** — see §3 for reading that address off the receiver.
- `--max-attempts 0` = never give up, keeping the pair in lockstep on a marginal link; the
  default `50` abandons a chunk and desynchronises them.
- The sink also stops when complete — it exits after one message unless `--serve-forever`.

**Start the receiver first in every case**, and remember only `30CD424` may transmit on this
rig.

---

## 2. Then change one thing at a time

Every flag `radio.sh` accepts — this is the complete list. Anything not given falls back to
a default tuned over the air, so no command needs all of them.

| Flag | What it sets | Default |
|---|---|---|
| `--device b210\|n210\|x310` | which model — picks the subdev, address and gains below | `b210` |
| `--args <uhd args>` | which radio: `serial=…` (USB) or `addr=…` (Ethernet) | b210: auto-pick · n210: `addr=192.168.20.2` · x310: `addr=192.168.40.2` |
| `--freq <Hz>` | carrier frequency | `915e6` |
| `--scheme <NAME>` | `BPSK` `QPSK` `8-PSK` `16-QAM` `DBPSK` `DQPSK` `8-DPSK` | `DQPSK` |
| `--waveform sc\|ofdm` | single-carrier, or OFDM (64 subcarriers, CP 16) | `sc` |
| `--gain <dB>` | TX or RX gain, whichever role is running | b210: tx 78 / rx 20 · n210 & x310: 25 / 25 |
| `--rate <Hz>` | sample rate | `2e6` |
| `--sym <Hz>` | symbol rate | `1e6` |
| `--fec true\|false` | rate-1/2 K=7 convolutional + Viterbi | `true` |
| `--ant TX/RX\|RX2` | which **connector** | `TX/RX` sending · `RX2` receiving |
| `--subdev A:A\|A:B\|A:0` | which **RF channel** | b210: `A:A` (RF A) · n210 & x310: `A:0` |
| `--dry-run` | print the command that would run, and stop | off |
| `-h`, `--help` | usage | — |

**Keep `--scheme`, `--freq`, `--waveform`, `--rate` and `--sym` identical on both ends** — a
mismatch looks exactly like a dead link. If nothing decodes: raise TX gain in steps, try
`--scheme BPSK` (most robust), or `--waveform ofdm` (tolerates frequency offset far better —
set it on both sides). Per-scheme gains tuned over the air are in §5.

### Every modem option is reachable from the wrapper

The table above is shorthand. **Any `sdr_system` option can be appended, and yours replaces
the wrapper's default for that same option** — so `radio.sh` gives you the tuned setup as a
starting point without ever standing between you and the modem:

```bash
./radio.sh tx --device b210 --tx-gain 85 --det-mult 5     # override one, add another
./radio.sh rx --device b210 --rx-subdev A:B               # RF B
./radio.sh rx --role sink_arq --ack-port 5599             # an ARQ role, tuned RX defaults
./radio.sh tx --scheme BPSK --waveform ofdm --fec false
```

This replacement is what makes it work: the modem rejects a repeated option (`option
'--tx-ant' cannot be specified more than once`), so an appended flag used to be an error
rather than an override. The wrapper now drops its own default for anything you name.

Complete lists, all generated from the code so they cannot drift:
[`PARAMETERS.md`](PARAMETERS.md) for the modem's ~100 options, and
[`PARAMETERS_ALGO.md`](PARAMETERS_ALGO.md) for every `./run.sh` flag and every LoRa driver
variable. `drivers/usrp/build/sdr_system --help` prints the modem's list live. Check what will
run with `--dry-run` before committing to it.

### Naming a radio: `serial=` for USB, `addr=` for Ethernet

How you identify a radio depends on how it is attached, and the two forms are not
interchangeable:

| Radio | Attached by | How you name it |
|---|---|---|
| **B210 / B200** | **USB** | `--args serial=30CD424` — a device serial. It has **no IP address**. |
| **N210 / X310** | **Ethernet** | `--args addr=192.168.10.2` — the radio's own IP on its subnet |

`uhd_find_devices` prints whichever applies (`serial:` for a B210, `addr:` for an N210). With
a single B210 attached you can omit `--args` entirely; with more than one, the serial is the
only way to say which.

**Three different addresses are in play — keep them apart:**

| | What it names | Example |
|---|---|---|
| `--args serial=` | the radio itself, over USB | `serial=30CD424` |
| `--args addr=` | the radio itself, over Ethernet | `addr=192.168.10.2` |
| `--ack-host` | the **host machine** running the ARQ sink | `10.0.0.5` |

An Ethernet radio's `addr=` is the *radio's* IP, not the computer's, and never what
`--ack-host` wants. Mixing those two is the most common way a two-machine ARQ run fails.

### Which connector do I actually plug into?

Transmit leaves **TX/RX**; receive listens on **RX2**. Those are the defaults
(`--tx-ant TX/RX`, `--rx-ant RX2`) and there is no reason to change them — so with a normal
one-way link you only populate **two connectors in total**: TX/RX on the sender, RX2 on the
receiver. Everything else can stay bare.

Which *channel* those connectors belong to is `--subdev`:

| Model | Channels | Default | Connectors on that channel |
|---|---|---|---|
| **N210** | one daughterboard | `A:0` | TX/RX, RX2 |
| **B210** | two: **RF A** = `A:A`, **RF B** = `A:B` | `A:A` (RF A) | each channel has its **own** TX/RX and RX2 |
| **X310** | two slots: `A:0`, `B:0` | `A:0` | TX/RX, RX2 per slot |

`radio.sh` sets the right `--subdev` per `--device`, so the basic commands need nothing here.
To choose explicitly:

```bash
./radio.sh rx --device b210 --subdev A:B              # receive on RF B's RX2
./radio.sh tx --device b210 --subdev A:B --ant TX/RX  # transmit on RF B
./radio.sh tx --device n210 --args addr=192.168.10.2 --ant RX2   # send out RX2 instead
```

The antenna name is **`TX/RX`** — with the TX first. `RX/TX` is not a UHD antenna name, and
the wrapper warns if you pass something that is neither `TX/RX` nor `RX2`.

**A B210 has two of everything.** RF A and RF B each carry their own TX/RX and RX2 pair, so
"the TX/RX port" is ambiguous until you know the channel. Default is RF A — plug into the
**RF A** side unless you pass `--tx-subdev A:B` / `--rx-subdev A:B`.

How many antennas you need, by setup:

| Setup | Connectors used |
|---|---|
| one-way link (`tx` → `rx`) | TX/RX on the sender · RX2 on the receiver |
| ARQ with TCP ACKs (default) | same two — the ACK goes over the network, not the air |
| ARQ with `--ack-transport rf` | **four**: RF A TX/RX + RF A RX2 *and* RF B TX/RX + RF B RX2, on one B210 per box — data on one RF path, ACK on the other |

That last row is the reason `--ack-transport rf` demands `--tx-args` and `--rx-args` naming
the *same* radio with different subdevs: it is one B210 running full duplex across both of
its channels, so both channels must actually be cabled.

**Keep `--scheme`, `--freq`, `--waveform`, `--rate` and `--sym` identical on both ends** — a
mismatch looks exactly like a dead link. If nothing decodes: raise TX gain in steps, try
`--scheme BPSK` (most robust), or `--waveform ofdm` (tolerates frequency offset far better —
set it on both sides). Per-scheme gains tuned over the air are in §5.

---

## 3. Run an algorithm over it (`./run.sh`)

```bash
# ── develop: no hardware at all ──────────────────────────────────────────────
./run.sh --algo echo                                    # lossless, is my logic right?
./run.sh --algo fl --steps 20                           # federated learning on MNIST
./run.sh --algo dl --role gossip --agents 6             # decentralized, 6 peers on a ring
./run.sh --algo marl_multi --role multi --steps 300     # random access, real collisions
./run.sh list                                           # every algorithm and its roles

# ── same algorithm, a different PHY ──────────────────────────────────────────
./run.sh --algo fl --channel usrp --sim-snr-db 8            # the real C++ modem + noise
./run.sh --algo fl --channel usrp --modulation 16-QAM --fec ldpc --sim-snr-db 12
./run.sh --algo fl --channel lora --lora-sf 9           # the SX1276 LoRa PHY
./run.sh --algo fl --channel lora --lora-sf 12 --lora-bw 500000 --lora-verbose

# ── deploy: one process per node, real radios ────────────────────────────────
# USRP, two hosts — start the RX first. On hardware you set GAINS; the SNR you get is
# whatever the link gives you, and the receiver reports it.
./run.sh --algo fl --role server --radio addr=192.168.20.2 --rx-gain 30
./run.sh --algo fl --role client --radio serial=30CD424 --tx-gain 70 \
         --ack-host <SERVER_IP> --net-host <SERVER_IP> --steps 20

# LoRa, two hosts — same roles, one flag different:
./run.sh --algo fl --role server --channel lora --lora-backend serial --lora-port /dev/ttyUSB0
./run.sh --algo fl --role client --channel lora --lora-backend serial --lora-port /dev/ttyUSB0

# decentralized, one terminal per node (--node K implies --role peer):
./run.sh --algo dl --node 0 --agents 3
./run.sh --algo dl --node 1 --agents 3
./run.sh --algo dl --node 2 --agents 3
# ...on three different computers:
./run.sh --algo dl --node 0 --agents 3 --peers 10.0.0.1,10.0.0.2,10.0.0.3

# ── a three-node chain through a relay ───────────────────────────────────────
./run.sh --algo fl --role chain --relays 1 --steps 20            # one process
./run.sh --algo fl --role relay --radio serial=<B> --net-port 5700 \
         --down-host <SERVER_IP> --down-port 5701                # the middle node
```

### The whole experiment in one file (`--topology`)

Every command above types one node's wiring on one machine. A topology file holds all of
it — what radio each node owns, which connector and RF channel it uses, which port it
listens on, and whether each link goes over the air or over TCP/IP — and every node reads
the same file, saying only which node it is. The examples live in
`/workspace/experiments/topologies/` (seeded by `deploy/workspace/init-workspace.sh`).

```bash
./run.sh topologies                       # what wirings exist, with descriptions
./run.sh --algo fl --topology fl-star-tcp --node c0 --print-plan   # resolve, run nothing

# federated learning with NO RADIO — every link over plain TCP/IP. Server first.
./run.sh --algo fl --topology fl-star-tcp --node srv          # on the server's machine
./run.sh --algo fl --topology fl-star-tcp --node c0           # on client 0's
./run.sh --algo fl --topology fl-star-tcp --node c1           # on client 1's
./run.sh topology fl-star-tcp                                 # ...or all of them here

# decentralized learning, 3 peers in a ring, one process per node
./run.sh --algo dl --topology dl-ring3-tcp --node n0
./run.sh --algo dl --topology dl-ring3-tcp --node n1
./run.sh --algo dl --topology dl-ring3-tcp --node n2

# the same federated star on the radios: B210 clients transmit, the RX-only N210
# answers over TCP — which is what the file's {"up":"wireless","down":"tcp"} means
./run.sh --algo fl --topology fl-star-radio --node srv
./run.sh --algo fl --topology fl-star-radio --node c0

# anything you type still wins over the file
./run.sh --algo fl --topology fl-star-radio --node c0 --steps 40 --tx-gain 65
```

Without a file, the same two transports are reachable by flag: `--link tcp` runs the
client/server roles over plain TCP/IP with no radio (`fl.py --uplink tcp --downlink tcp`),
and `--clients N` tells a server how many clients to collect from before it aggregates.

### Every PHY variable is reachable here too

The named flags cover what most experiments touch. For anything else, each PHY has a
setter that takes **its own variable names**, so what you read in the PHY's docs is what
you type — the USRP modem alone has ~100 options, and listing them here would only rot:

```bash
./run.sh --algo fl --channel usrp --usrp-backend radio --role rx \
         --usrp-set det_mult=5 --usrp-set viz=true          # any modem option
./run.sh --algo fl --channel lora --lora-set seed=7 --lora-set reply_timeout=60
```

Either spelling works (`det-mult` or `det_mult`), and both setters are repeatable. Two
things they will not do:

- **An unknown variable is refused, not ignored** — with a suggestion:
  `--usrp-set: usrp has no variable 'det_multiplier' — did you mean det_mult…?` A typo that
  silently changes nothing is a wrong experiment that looks like a right one.
- **`--usrp-set` needs `--usrp-backend radio`.** The default `pyphy` backend calls the DSP
  in-process rather than starting the modem, so modem options cannot reach it; the run stops
  and says so instead of pretending.

Keys are checked against each PHY's real surface — `sdr.py`'s auto-generated `OPTIONS` for
the USRP, the driver's own signature for LoRa — so the check stays correct as the PHYs grow.

**Choosing the graph** (`--role gossip`): `ring` (default), `full`, or an explicit edge list
such as `--topology 0-1,1-2,2-3,3-4,4-5`. Set `DL_NONIID=1` to make the topology actually
matter — with IID shards every node learns nearly the same model.

### Two machines: the ACK socket is not optional

ARQ is on by default (`--arq stop-and-wait`) and its ACKs travel over **TCP**, not over the
air. The receiving side listens; the transmitting side connects to it. Those defaults point
at `127.0.0.1`, which quietly works on one box and quietly fails on two — so on separate
machines the sender must be told where the receiver is:

```bash
--ack-host <RX_IP>     # where the ACK socket lives   (default 127.0.0.1, port 5599)
--net-host <RX_IP>     # the payload/control socket
--ack-timeout 3000     # ms to wait for an ACK before resending
```

Port **5599** must be reachable between the two hosts — a firewall or a pod without the right
network attached produces a link that transmits perfectly and never confirms anything.

**There is no listener-IP option, and none is needed.** The receiving side binds
`INADDR_ANY`, so it accepts on *every* interface it has; only the port is yours to choose.
The address that has to be right is the one you hand the **sender** — and on a session pod
that address changes every time the session restarts, so never hard-code it. Read it off the
receiver instead:

```bash
# on the RECEIVER — the address(es) a sender can reach it on
hostname -I
ip -4 -o addr | awk '{print $2, $4}'      # which interface each one belongs to
```

Pick the address on the network the sender shares. On a testbed with a radio subnet that is
usually the radio-side address (e.g. `192.168.10.x`), not the cluster address — nodes in
different clusters cannot reach each other's `10.42.x` pod IPs at all.

Since every live session now publishes its own addresses into the shared workspace, you can
also just look them up from anywhere, without a shell on the other machine:

```bash
python3 - <<'PY'
import json, glob
for f in glob.glob('/workspace/experiments/settings/*.json'):
    d = json.load(open(f))
    print(d['node_id'], [i['cidr'] for i in d['interfaces']])
PY
```

---

## 4. Drive the modem directly (`sdr_system`) — for experimenters

`radio.sh` is a thin wrapper; the binary takes the same ideas with more roles. Build it with
`deploy/initialization.sh --build`, then:

```bash
BIN=drivers/usrp/build/sdr_system
```

| `--role` | What it does |
|---|---|
| `tx` / `rx` | raw bursts, **no ARQ** — what `radio.sh` runs |
| `source_arq` / `sink_arq` | the two ends of stop-and-wait ARQ, with retransmission |
| `sense` | channel occupancy / listen-before-talk (§ "Channel sensing") |
| `both` | legacy single-box routing |

**FEC is OFF in the binary.** `--fec` defaults to `false`; `radio.sh` passes `--fec true`,
which is why the wrapper appears to enable it by default. Pass it explicitly here:

```bash
# raw pair, QPSK, FEC on — RX first
$BIN --role rx --rx-args addr=192.168.10.2 --rx-subdev A:0 --rx-ant RX2 \
     --rx-freq 915e6 --rx-rate 2e6 --tx-rate 2e6 --symbol_rate 1e6 \
     --rx-gain 25 --scheme QPSK --waveform sc --fec true

$BIN --role tx --tx-args addr=192.168.10.2 --tx-subdev A:0 --tx-ant TX/RX \
     --tx-freq 915e6 --tx-rate 2e6 --rx-rate 2e6 --symbol_rate 1e6 \
     --tx-gain 25 --scheme QPSK --waveform sc --fec true
```

### ARQ: the sink is the ACK server

Data always crosses the **air**; the ACK comes back over the transport you choose. With the
default `--ack-transport tcp`:

* **`sink_arq` listens** on `--ack-port` (default **5599**), on **all interfaces** — start it
  first. It has no address to configure.
* **`source_arq` connects** to `--ack-host:--ack-port` (default **127.0.0.1**:5599). This is
  the one that changes whenever the receiver's session does.

So the source on another machine *must* be given the sink's address:

```bash
# ── receiver / ACK server (host A, 10.0.0.5) ──
$BIN --role sink_arq --rx-args addr=192.168.10.2 --rx-subdev A:0 --rx-ant RX2 \
     --rx-freq 915e6 --rx-rate 2e6 --tx-rate 2e6 --symbol_rate 1e6 \
     --rx-gain 25 --scheme QPSK --fec true \
     --ack-transport tcp --ack-port 5599

# ── transmitter (host B) — points at host A's ACK socket ──
$BIN --role source_arq --tx-args serial=30CD424 --tx-subdev A:A --tx-ant TX/RX \
     --tx-freq 915e6 --tx-rate 2e6 --rx-rate 2e6 --symbol_rate 1e6 \
     --tx-gain 78 --scheme QPSK --fec true \
     --ack-transport tcp --ack-host 10.0.0.5 --ack-port 5599 \
     --timeout 3000 --max-attempts 50
```

| Flag | Meaning |
|---|---|
| `--ack-transport tcp\|rf` | ACK over a socket (default), or over a second RF path |
| `--ack-port 5599` | the port. Set it on **both** ends — the sink binds it on all interfaces (`INADDR_ANY`), the source dials it |
| `--ack-host` | **source only**: the sink's address (`127.0.0.1` by default, i.e. same box). There is no matching bind option, because the sink listens everywhere — see §3 for finding the address that changes each session |
| `--timeout 3000` | ms the source waits for an ACK before resending |
| `--max-attempts 50` | give up on a chunk after N sends; **0 = never give up**, which keeps a paired sender/receiver in lockstep on a marginal link |
| `--serve-forever` | `sink_arq` stays up as an access point, re-accepting a new source per session |
| `--on-demand` | `source_arq` keeps the radio warm and fires one packet per stdin line |

`--ack-transport rf` sends ACKs over a second RF path instead of a socket, and therefore
requires `--tx-args` and `--rx-args` to name the **same** radio with different
`--tx-subdev`/`--rx-subdev` (RF A vs RF B on one B210).

---

## 5. Per-scheme reference — every modulation, tuned over the air

Copy-paste TX/RX command pairs with **gains tuned per scheme** from over-the-air
testing @915 MHz (two B210s, VERT900 antennas ~10 cm apart). All use
**stop-and-wait ARQ** (TCP ACK on localhost) + **FEC**, and auto-save plots to
`phy_outputs/<scheme>/figure.png`.

**Higher-order QAM (16-QAM and up) is intentionally omitted** — those need a
cleaner link (SMA cable + attenuator); the ~10 cm OTA link floors at ~28–31 %
EVM, too noisy for them. See the cable-link note at the bottom.

## Fixed setup (this rig)

| Role | UHD device | subdev | antenna |
|---|---|---|---|
| RX / sink   | `serial=30CD3F7` | `A:B` (**RF B**) | `RX2`   |
| TX / source | `serial=30CD424` | `A:A` (RF A) | `TX/RX` |
| RX (alt)    | `addr=192.168.10.2` (N210) | `A:0` | `RX2` |

These are the **cabled** connectors — see the warning in §1. The sink's `A:B` is not the
modem's default (`A:A`), so it is passed explicitly in every command below; if 3F7 is ever
recabled to RF A, change `--rx-subdev A:B` back to `A:A` throughout.

Common: `--rx-freq/--tx-freq 915e6`, `--rx-rate/--tx-rate 1.6e6`,
`--ack-transport tcp --ack-port 5599 --ack-host 127.0.0.1 --det-mult 3 --fec true`.

**Always start the sink (RX) first**, then the source (TX). Run each in its own terminal.

## Tuned gains per scheme (why they differ)

| Scheme | Waveform | `--rx-gain` | `--tx-gain` | extra | OTA status |
|---|---|---|---|---|---|
| BPSK    | sc   | 20 | 78 | — | ✅ very robust |
| QPSK    | sc   | 20 | 78 | — | ✅ solid (5/5) |
| 8-PSK   | sc   | 16 | 86 | — | ✅ usable — a few retransmits (EVM ~16 %) |
| DBPSK   | sc   | 20 | 78 | — | ✅ solid (5/5) |
| DQPSK   | sc   | 20 | 78 | — | ✅ solid (5/5) |
| 8-DPSK  | sc   | 16 | 86 | — | ✅ usable — a few retransmits |
| BPSK    | ofdm | 22 | 80 | `--ofdm-tx-peak 0.5` | ✅ robust |
| QPSK    | ofdm | 22 | 80 | `--ofdm-tx-peak 0.5` | ✅ solid |

The pattern: **8-ary schemes (8-PSK/8-DPSK) need more TX power (86) and lower RX
gain (16)** — a stronger signal buys the SNR their tighter decision regions
demand, without overdriving the front end. The robust schemes are comfortable at
20/78. OFDM keeps TX a touch lower + `--ofdm-tx-peak 0.5` because of its high PAPR.

Differential schemes run on the default `--eq_type None` (single-carrier, flat
link). **Don't combine differential with OFDM** — OFDM's pilots already handle
phase, and differential fights its per-symbol CPE tracking.

---

# Single-carrier (`--waveform sc`, the default)

## BPSK — sc
```bash
# RX / sink  (terminal 1)
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:B --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 \
  --scheme BPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source  (terminal 2)
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 78 \
  --scheme BPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## QPSK — sc
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:B --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 \
  --scheme QPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 78 \
  --scheme QPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## 8-PSK — sc  (more TX power, lower RX gain)
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:B --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 16 \
  --scheme 8-PSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 86 \
  --scheme 8-PSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## DBPSK — sc  (differential; no PLL needed)
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:B --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 \
  --scheme DBPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 78 \
  --scheme DBPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## DQPSK — sc  (differential)
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:B --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 \
  --scheme DQPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 78 \
  --scheme DQPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## 8-DPSK — sc  (differential; more TX power like 8-PSK)
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:B --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 16 \
  --scheme 8-DPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 86 \
  --scheme 8-DPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

---

# OFDM (`--waveform ofdm`, 64 subcarriers, CP 16)

## BPSK — OFDM
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:B --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 22 \
  --waveform ofdm --ofdm-fft 64 --ofdm-cp 16 --scheme BPSK --fec true \
  --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 80 \
  --waveform ofdm --ofdm-fft 64 --ofdm-cp 16 --ofdm-tx-peak 0.5 --scheme BPSK --fec true \
  --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## QPSK — OFDM
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:B --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 22 \
  --waveform ofdm --ofdm-fft 64 --ofdm-cp 16 --scheme QPSK --fec true \
  --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 85 \
  --waveform ofdm --ofdm-fft 64 --ofdm-cp 16 --ofdm-tx-peak 0.5 --scheme QPSK --fec true \
  --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

---

## Tips
- Run from `build/` (the binary is `build/sdr_system`); prefix `./`.
- Watch the RX `Peak=` line: post-AGC PAPR of ~1.2–1.4 is normal (not ADC
  clipping). If chunks never decode *and* the raw front end looks saturated,
  lower `--rx-gain`. If detection never fires (`bursts=0`), raise a gain.
- The sink auto-stops once all chunks are CRC-verified and writes the figure.
- Plots off: add `--viz false`. Separate runs: change `--viz-dir`.
- **One-way (no ACK)** instead of ARQ: use `--role rx` / `--role tx --tx-reps 20`
  (drop the `--ack-*` flags).

## Payload type & transmission mode

`--message-type` selects what is sent; `--tx-mode` (role tx) selects burst (finite,
`--tx-reps` cycles with `--interval` gaps) vs continuous (until Ctrl-C). Works with
any scheme/waveform above.

| `--message-type` | Payload | RX prints | ARQ? |
|---|---|---|---|
| `bytes` (default) | given text — default Star Wars crawl, override with `--message "..."` | the text | yes |
| `random` | `--num_bits` random bits (→ bytes, chunked like text) | byte count + hex, CRC-verified | yes |
| `sine` / `cosine` | raw baseband test tone (`--tone-freq`, `--tone-amp`) | spectrum plot (monitor) | no (role tx/rx only) |

```bash
# Custom text
./sdr_system --role source_arq ... --message-type bytes --message "hello world"

# Random-bit throughput test (2000 bits → 250 bytes → 2 chunks), ARQ terminates normally
./sdr_system --role sink_arq   ... --message-type random --num_bits 2000
./sdr_system --role source_arq ... --message-type random --num_bits 2000

# Continuous data loop (never stops until Ctrl-C)
./sdr_system --role tx ... --scheme QPSK --tx-mode continuous

# Test tone: TX a continuous 200 kHz cosine carrier; monitor it on the other radio
./sdr_system --role tx ... --message-type cosine --tone-freq 200e3 --tone-amp 0.5 --tx-mode continuous
./sdr_system --role rx ... --message-type cosine     # monitor: captures + plots the spectrum
```

Notes: `random` uses a fixed seed (reproducible) and rides the same CRC/FEC/ARQ path
as text, so the payload arrives bit-error-free or not at all. Tones are raw waveforms
(no preamble/CRC) — for a clean RX capture use `--tx-mode burst` so each burst triggers
detection, and set both ends to `--message-type sine|cosine`.

## LoRa / CSS (chirp spread spectrum)

Two chirp features share the same base up-chirp:

**1. Chirp jamming waveform** (`--message-type chirp`) — a raw cyclic frequency sweep,
no decode. The most effective broadband jammer; also a candidate test waveform.

```bash
# continuous full-band up-chirp sweep
./sdr_system --role tx --message-type chirp --chirp-bw 1.6e6 --chirp-sf 8 \
  --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 85 --tx-args serial=30CD424
#   --chirp-bw = sweep Hz (0 = full band), --chirp-sf = 7-12 (2^SF/BW symbol dur),
#   --chirp-down = down-chirp.  See experiments/jammer/jammer.py for a full jammer.
```

**2. Decodable LoRa data link** (`--waveform lora`) — CSS modulation that *carries data*:
bits → cyclically-shifted up-chirps; the RX dechirps + FFTs (peak bin = symbol), giving
the LoRa processing gain (decodes below the noise floor). Reuses the CRC/FEC framing.

```bash
# RX (start first)
./sdr_system --role rx --waveform lora --lora-sf 8 --lora-sync-word 18 \
  --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 --rx-args serial=30CD3F7
#   -> [LoRa RX] chunk 0/1  CRC=OK  payload="..."
# TX
./sdr_system --role tx --waveform lora --lora-sf 8 --lora-sync-word 18 \
  --message "hello LoRa" --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 80 --tx-args serial=30CD424
```

- `--lora-sf` (7–12): spreading factor; higher = more gain/range, longer symbols.
- `--lora-sync-word` (**default 18 = 0x12 private**, 52 = 0x34 public): the network id,
  sent as 2 sync symbols after the preamble. **The RX rejects frames with a different
  word** — must match on both ends. Frame = `preamble(8 up-chirps) + sync word(2 syms) +
  SFD(2 down-chirps) + data`.

The CSS DSP is validated in-repo by `tools/lora_loopback_test.cpp` (clean / timing / CFO
/ −10 dB SNR / sync-word rejection all pass). One-way for now (no ARQ). See
`SYSTEM_REFERENCE.md` §14.

## Channel sensing (occupancy / listen-before-talk)

`--role sense` integrates received power over a `--sense-window` (ms) and prints
`[SENSE] busy=.. power_db=..` per window; `--sense-count 0` streams forever. The
busy threshold is gain-dependent — calibrate it to your idle floor (Python does this
automatically).

```bash
# one 5-window measurement (raw CLI). Busy if avg power > --sense-threshold-db
./sdr_system --role sense --rx-args serial=30CD3F7 --rx-freq 915e6 --rx-rate 1.6e6 \
  --rx-gain 30 --sense-window 10 --sense-count 5 --sense-threshold-db -6 --viz false

# persistent feed (stream until Ctrl-C) — for a live occupancy monitor
./sdr_system --role sense --rx-args serial=30CD3F7 --rx-gain 30 --sense-count 0 --viz false
```

Python wrapper (recommended — auto-calibrates the threshold, importable helpers):

```bash
python3 channel_sense.py --calibrate                     # measure the idle floor
python3 channel_sense.py --count 10                      # calibrate, then sense 10 windows
python3 channel_sense.py --p 0.5 --count 8               # p-persistent sense->decide loop
```

```python
from channel_sense import SenseStream           # persistent feed, one radio init
with SenseStream(rx_args="serial=30CD3F7") as s:
    thr = s.calibrate()
    go, r = s.should_transmit(p=0.5, threshold_db=thr)   # idle -> TX w.p. p; busy -> defer
```

Validated: idle ≈ −12 dB → not busy; an active carrier ≈ −3 dB → busy. See
`SYSTEM_REFERENCE.md` §4.1.

## Link BER diagnostic (`--ber-expected`)

Measure the *real* per-burst bit-error-rate. Give the sink the KNOWN transmitted
payload and it prints, for **every** decoded burst (CRC pass or fail), the pre-FEC
(raw channel) and post-FEC (payload) BER vs ground truth — so you can tell whether
a CRC-failed frame is nearly right (a few residual bits) or garbage (FEC overwhelmed).

```bash
# RX / sink: known payload as ground truth
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 40 --scheme DQPSK --fec true \
  --ack-transport tcp --ack-port 5599 --det-mult 3 --bytes-length 125 --timer_interval 20 \
  --serve-forever --ber-expected known_payload.bin --viz false
#   -> [BER] pre-FEC=1.20% ... post-FEC payload=0.30% ... CRC=FAIL   (marginal: nearly right)
#   -> [BER] pre-FEC=24.0% ... post-FEC payload=42.3% ... CRC=FAIL   (broken: garbage, FEC gave up)

# TX / source: send that same known payload
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 ... \
  --scheme DQPSK --bytes-length 125 --payload-file known_payload.bin --max-attempts 1
```

Python (single-box probe — runs its own warm AP + fires N known packets, reports
min/median/max BER):

```bash
python3 marl_phy.py ber --attempts 20 --scheme DQPSK
```
```python
from marl_phy import ber_probe
rows = ber_probe(n=20, scheme="DQPSK")     # [{pre_fec, post_fec, crc}, ...]
```

Note: **post-FEC > pre-FEC** means the raw BER exceeded the rate-½ code's ~11%
threshold — Viterbi then *amplifies* errors (catastrophic FEC failure) → garbage.
See `SYSTEM_REFERENCE.md` §8.1.

## 16-QAM and higher — blocked without a shared clock
**Validated ceiling on this rig is QPSK / 8-PSK.** 16-QAM+ does **not** decode, and a clean cable
is necessary but not sufficient. On a direct cable the SNR is excellent and QPSK works, but the two
B210s are **free-running** (independent TCXOs) and the TX carrier leakage beats at that CFO — a
drifting near-DC tone that rotates the constellation (16-QAM can't tolerate it; the phase PLL can't
lock 16 points) and dominates the AGC (OFDM → blob). Static DC removal, an RX DC-block high-pass
(`--dc-block`), and a manual TX LO-leakage null (`--tx-dc-i/--tx-dc-q`) were all tried and none
unblock it (details in `SYSTEM_REFERENCE.md` §13).

**The fix is a shared 10 MHz reference:** feed one 10 MHz source (signal generator / GPSDO /
OctoClock) into both radios' `REF IN` and run both with `--ref external`. That makes CFO ≈ 0 and
turns the leakage into a removable static DC, so dense QAM decodes. Once that's in place, start from
`--rx-gain 21 --tx-gain 80`, lower `--rx-gain` / add attenuation until the RX front end isn't
saturated, keep `--fec true`; higher orders need progressively lower EVM (16-QAM ≲ 12 %, 64-QAM ≲
6 %, 256-QAM ≲ 3 %).

---

# Other USRP models — N210 / X310 / X410

The **DSP is device-independent**: every `--scheme / --waveform / --fec / --ack-* / --preamble /
--det-mult / eq / timing / phase` flag is exactly as in the B210 sections above. Only the
**hardware addressing** changes — device args, subdev, antenna, and (importantly) the **gain
range**. The blocks below are **templates adapted from the B210 commands and NOT yet tested on
those models** — confirm the exact subdev / antenna / gain range of your unit first with:
```bash
uhd_usrp_probe --args addr=<ip>      # prints subdev names, antenna ports, gain ranges
```

## What changes per model

| Model | Transport · `--*-args` | `--*-subdev` | `--*-ant` | Gain range* | Reference clock |
|---|---|---|---|---|---|
| **B210** (this rig) | USB · `serial=30CD424` | `A:A` | `TX/RX`, `RX2` | 0–89.8 dB | internal TCXO (no ext ref) |
| **N210** | 1 GbE · `addr=192.168.10.2` | `A:0` | `TX/RX`, `RX2` | ~0–31.5 dB (SBX/WBX/UBX) | `REF IN` 10 MHz + PPS, or MIMO cable |
| **X310** | 10 GbE / PCIe · `addr=192.168.40.2` | `A:0` (or `B:0`; 2 slots) | `TX/RX`, `RX2` | ~0–31.5 dB (UBX/SBX) | `REF IN` + PPS, GPSDO option |
| **X410** | 100 GbE (QSFP28) · `addr=192.168.10.2` | integrated ZBX — use `--rx-channel/--tx-channel` | `TX/RX0`, `RX1` (per ch.) | ~0–60 dB (ZBX) | `REF IN` + PPS, internal, White Rabbit |

\*Gain is **daughterboard-specific**. The classic SBX/WBX/UBX cards top out at **31.5 dB**, so the
B210's tx-gain 78–86 is far out of range there — start mid-to-high and tune by the same EVM /
`Peak=` method. For 915 MHz use a card that covers it (SBX 0.4–4.4 GHz, UBX 10 MHz–6 GHz, X410 ZBX
1 MHz–8 GHz; note **CBX is 1.2–6 GHz and does *not* cover 915 MHz**).

## Sharing a clock is easier on these → dense QAM
An external clock is **not** needed for basic operation and is **not** a per-model requirement —
every USRP runs standalone on its internal oscillator. It is needed only to make **two separate
radios frequency-coherent**: two independent oscillators drift apart (that *is* the CFO), and one
10 MHz reference fed to *both* radios locks them together (CFO → ~0), which is what dense QAM needs.
That's the B210 §13 limitation — not that the B210 can't (it also supports `--ref external`, via
small onboard connectors), but that we have **no 10 MHz source**.

The N210/X310/X410 just make this convenient: dedicated **front-panel SMA `REF IN` + `PPS IN`**,
and the X310/X410 offer an **internal GPSDO** (a self-contained 10 MHz source). Feed one 10 MHz
reference into both units' `REF IN`, add **`--ref external`** to both commands, and
**16-QAM / 64-QAM / 256-QAM decode**. (`--ref external` is a standard UHD option on all models,
including the B210; you always supply the 10 MHz source yourself unless a GPSDO is installed.)

## Template — QPSK single-carrier
Swap `--scheme` (BPSK / 8-PSK / DBPSK / DQPSK / …) and add the OFDM flags exactly as in the B210
sections. Each unit has its own IP: the sink uses radio A's address for both `--rx-args` and
`--tx-args`, the source uses radio B's. Drop `--ref external` if you don't have a shared clock
(then you're limited to QPSK/8-PSK, same as the B210).

**N210** (two units, shared 10 MHz):
```bash
# RX / sink   (radio A = 192.168.10.2)
./sdr_system --role sink_arq --rx-args addr=192.168.10.2 --tx-args addr=192.168.10.2 \
  --rx-subdev A:0 --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 --ref external \
  --scheme QPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3
# TX / source (radio B = 192.168.20.2)
./sdr_system --role source_arq --tx-args addr=192.168.20.2 --rx-args addr=192.168.20.2 \
  --tx-subdev A:0 --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 28 --ref external \
  --scheme QPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

**X310** (two units, shared 10 MHz — subdev `A:0`, 10 GbE addresses):
```bash
# RX / sink   (radio A = 192.168.40.2)
./sdr_system --role sink_arq --rx-args addr=192.168.40.2 --tx-args addr=192.168.40.2 \
  --rx-subdev A:0 --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 --ref external \
  --scheme QPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3
# TX / source (radio B = 192.168.40.3)
./sdr_system --role source_arq --tx-args addr=192.168.40.3 --rx-args addr=192.168.40.3 \
  --tx-subdev A:0 --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 25 --ref external \
  --scheme QPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

**X410** (RFSoC / ZBX — channel-based; confirm antenna names + subdev with `uhd_usrp_probe`):
```bash
# RX / sink   (radio A)
./sdr_system --role sink_arq --rx-args addr=192.168.10.2 --tx-args addr=192.168.10.2 \
  --rx-subdev A:0 --rx-channel 0 --rx-ant RX1 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 30 --ref external \
  --scheme QPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3
# TX / source (radio B)
./sdr_system --role source_arq --tx-args addr=192.168.10.3 --rx-args addr=192.168.10.3 \
  --tx-subdev A:0 --tx-channel 0 --tx-ant TX/RX0 --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 30 --ref external \
  --scheme QPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## Sample-rate note (per master clock)
The pipeline needs `tx-rate == rx-rate` with an integer samples/symbol (default `0.8e6 sym × U/D 2 =
1.6e6`). 1.6 MHz divides the **X310** master clock (200 MHz) exactly, but **N210** (100 MHz) and
**X410** (245.76 / 250 MHz) will snap 1.6 MHz to the nearest valid rate — UHD prints the actual
rate on start-up. If it snaps, set `--symbol_rate` so that `rate = symbol_rate × U/D` holds at the
achieved rate (keeping integer sps), and use the same values on both ends.
