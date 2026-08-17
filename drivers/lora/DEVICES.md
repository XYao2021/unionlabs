# LoRa devices — what we have, and how to reach it

The physical LoRa testbed: **Raspberry Pi** nodes, each bridged over USB to a
**Teensy 4.0 + RFM95 (SX1276)** radio. The Pi runs the Python side; the Teensy runs
[`arduino/lora_phy/lora_phy.ino`](arduino/lora_phy/lora_phy.ino).

> **No passwords live in this repository.** Device names are here because you need them and
> they are not secret. The login secret goes in an untracked file — see [Credentials](#credentials).

## Do not trust a hardcoded IP — ask Tailscale

Every static inventory we inherited had gone stale. The sensing project's list and the multipath
testbed's list both name IPs that **no longer exist in the tailnet**, because a Tailscale address
changes when a device is re-registered. So `deploy/inventory.sh` **discovers nodes live** from
`tailscale status` and uses only what is actually online.

```bash
tailscale status | grep raspberrypi        # the ground truth, any time
cd deploy && ./check_devices.sh            # the same thing, plus radio + driver state
```

Two names, and they are not the same:

| | Example | Used for |
|---|---|---|
| Tailscale device name | `raspberrypi-16` | finding the machine (MagicDNS, `tailscale status`) |
| SSH user / short name | `pi16` | logging in — **this is the login name** |

## The fleet

Twelve Pis are registered: `raspberrypi-01` … `raspberrypi-09`, and `-16`, `-17`, `-18`.
Which are up changes hour to hour, so the table below is a snapshot, not a contract —
`./check_devices.sh` is authoritative.

*Snapshot, 2026-08-17:*

| Node | Tailscale name | SSH user | State | Radio attached |
|---|---|---|---|---|
| pi16 | `raspberrypi-16` | `pi16` | **online** | none |
| pi17 | `raspberrypi-17` | `pi17` | **online** | none |
| pi18 | `raspberrypi-18` | `pi18` | **online** | none |
| pi01–pi09 | `raspberrypi-01` … `-09` | `pi01`…`pi09` | offline (last seen 4–7 h) | unknown |

All three online nodes run **Python 3.13.5** and accept this Mac's SSH key already.

**No radio was attached to any online Pi at the time of the snapshot** — `/dev/ttyACM*` and
`/dev/ttyUSB*` were both absent. Software can be pushed and the stack will import, but a run with
`--lora-backend serial` needs a Teensy plugged in first. `check_devices.sh` reports this per node
so you find out before the experiment rather than during it.

## Credentials

**SSH keys are already installed** on the online Pis from this Mac, so nothing is needed for
day-to-day use. If you are on a new machine:

```bash
ssh-keygen -t ed25519                 # once, if you have no key
ssh-copy-id pi16@raspberrypi-16       # once per Pi; asks for that Pi's password
```

If you cannot use keys, the password goes in an untracked file:

```bash
cd drivers/lora/deploy
cp credentials.sh.example credentials.sh   # gitignored — never committed
$EDITOR credentials.sh                     # fill in SSH_PASS
brew install sshpass                       # or: sudo apt install sshpass
```

The password is handed to `sshpass` through the environment (`sshpass -e`), so it never appears in
`ps` output or shell history. `credentials.sh` is covered by three `.gitignore` rules; only the
`.example` template, which contains a `changeme` placeholder, is committed.

> The lab's per-device password follows a fixed pattern documented in the sensing project's
> `setup_pi_keys.sh`. It is deliberately **not** reproduced here — put it in your local
> `credentials.sh`, or better, install keys once and forget it.

## Pushing to every Pi

```bash
cd drivers/lora/deploy
./check_devices.sh          # who is reachable, radio attached, driver installed
./push_to_pis.sh --dry-run  # show what would happen, change nothing
./push_to_pis.sh            # copy the driver to every reachable Pi
./push_to_pis.sh --deps     # ...and install pyserial + numpy there
./push_to_pis.sh --node pi16 --node pi17    # only these
```

It copies the LoRa driver (`lora_radio.py`, `framing.py`, `lora_driver.py`) plus the `union/`
middleware into `~/unionlabs/lora` on each Pi, so the node can run an experiment locally.
**Offline nodes are skipped and listed**, not treated as failures — with a dozen field nodes,
some being down is the normal case. Re-running is harmless.

## Flashing the radios

`arduino-cli` + `teensy_loader_cli`, as in the multipath testbed's `flash_all.sh`:

| Setting | Value |
|---|---|
| FQBN | `teensy:avr:teensy40` |
| MCU (teensy_loader_cli) | `TEENSY40` |
| Board manager URL | `https://www.pjrc.com/teensy/package_teensy_index.json` |
| Serial baud | 115200 (must match `SERIAL_BAUD` in the sketch) |
| Pins | SS=10, RST=9, DIO0=2 |

## Running an experiment on a Pi

```bash
# on pi16, once a Teensy is attached
./run.sh --algo fl --role client --channel lora \
         --lora-backend serial --lora-port /dev/ttyACM0
```

Decentralized, one node per Pi:

```bash
./run.sh --algo dl --node 0 --agents 3 --channel lora --lora-backend serial \
         --peers raspberrypi-16,raspberrypi-17,raspberrypi-18
```

See [`README.md`](README.md) for the driver itself and the three ways a radio can be attached.
