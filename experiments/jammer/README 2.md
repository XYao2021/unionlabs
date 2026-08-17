# Active RF jammer (`jammer.py`)

A configurable interferer built on the B210 SDR PHY, for random-access / MARL
experiments where you want a **controllable channel contender** — to make the
`channel-busy` observation meaningful, create real collisions, or stress a policy.

It only transmits (no decode), driving `sdr_system --role tx` with the chosen
waveform and timing. Runs on its own radio; point `--tx-args` at the jammer's B210.

## Waveforms

| `--waveform` | what | key knobs |
|---|---|---|
| **chirp** (default) | LoRa/CSS linear frequency sweep — wideband energy across the band, the most effective broadband jammer | `--chirp-bw` (sweep Hz, 0=full band), `--chirp-sf` (7–12), `--chirp-down` |
| **tone** | a single sine/cosine carrier — narrowband | `--tone-freq` (baseband Hz) |
| **scheme** | a real modulated signal (random data of `--scheme`) — looks like a legitimate transmitter | `--scheme` (QPSK, DQPSK, …), `--num-bits` |

## Timing

| `--mode` | behavior |
|---|---|
| **continuous** | unbroken transmission for `--duration` s |
| **burst** | repeated bursts with `--interval` ms gaps for `--duration` s (a pulsed / duty-cycled jammer) |

## Examples

```bash
# broadband chirp sweep, full band, 10 s, high power
python3 jammer.py --waveform chirp --chirp-bw 1.6e6 --tx-gain 85 --duration 10

# pulsed QPSK interferer: bursts every 100 ms for 30 s
python3 jammer.py --waveform scheme --scheme QPSK --mode burst \
    --interval 100 --duration 30 --tx-gain 80

# narrowband tone at +200 kHz for 5 s
python3 jammer.py --waveform tone --tone-freq 200e3 --tx-gain 75 --duration 5

# print the command without transmitting
python3 jammer.py --dry-run --waveform chirp --duration 10
```

All knobs: `--waveform --scheme --freq --tx-gain --tx-rate --tx-args --mode
--interval --burst-ms --duration --chirp-bw --chirp-sf --chirp-down --tone-freq
--num-bits`. `--duration` bounds the run (the jammer stops the TX process after it).

## Using it with the multi-agent experiments

Run the jammer on a spare radio alongside the agents to add real contention:

```bash
# jammer on its radio — pulsed, so agents see intermittent busy channel
python3 jammer.py --tx-args serial=<JAMMER> --freq 915e6 --mode burst \
    --interval 150 --duration 300 --tx-gain 80 &
# then run the agents (see ../../README.md — decentralized multi-node)
```

The jammer's energy raises the agents' carrier-sense `busy` and causes their bursts
to fail (no ACK) when it overlaps — exactly the interference the MARL policy must
learn to work around. Match `--freq` to the link frequency; a **chirp** sweeping the
band hits every burst, while **burst** mode leaves gaps the policy can learn to exploit.

## Notes

- The **chirp** waveform is the new LoRa/CSS raw sweep added to the PHY
  (`--message-type chirp`, `--chirp-bw`, `--chirp-sf`). It is transmit-only here;
  LoRa as a *decodable data* modulation is a separate PHY addition.
- Verify a waveform is on the air with the tone monitor (another radio):
  `python3 ../../drivers/usrp/python/run.py configs/rx_tone_monitor.json` — a chirp shows as a
  frequency that sweeps.
- Jamming affects any receiver on that frequency. Use only on your own link /
  authorized test setup.

## See also

- `../EXPERIMENT_GUIDE.pdf` (or `.md`) — step-by-step commands to run every application
  (radio-free + hardware); the jammer appears under §1A as an optional contention source.
