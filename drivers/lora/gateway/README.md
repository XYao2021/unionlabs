# LoRa gateway — point-to-point jobs on the Pis, through a web page

The Pis cannot join the UnionLabs platform, so they cannot be reserved or logged into
from it. This gateway runs on one lab machine inside the tailnet and holds the **only**
credential for the Pis (its SSH key). Users get **jobs, not shells**: pick a TX Pi, an RX
Pi and the radio settings, and the gateway runs the test and returns both logs. No
password or key ever reaches a user.

```
 session browser ──http──▶ gateway.py (thinkpad-laptop, tailnet) ──ssh──▶ TX Pi / RX Pi
                            holds the SSH key                         pi_agent.py → Teensy
```

| File | Runs on | Does |
|---|---|---|
| `gateway.py` | the gateway machine | lists the Pis, validates and runs jobs, serves the page and `/api/*` |
| `page.html` | the user's browser | Pi table, job form, live logs, summary, JSON download |
| `pi_agent.py` | each Pi, shipped per job over SSH stdin | drives the Teensy, prints JSON lines, **restores the board's settings afterwards** |

Standard library only on the gateway (python3 ≥ 3.8 + the `tailscale` CLI). The Pis need
python3 + pyserial, which they already have. Nothing is installed on a Pi: the agent is
sent over SSH with each job, so there is no copy there to go stale.

## Run it

```bash
python3 gateway.py --pis raspberrypi-07,raspberrypi-08          # binds this machine's Tailscale IP :8080
python3 gateway.py --host 127.0.0.1 --pis raspberrypi-07,raspberrypi-08   # local only, for development
python3 gateway.py --pis raspberrypi-07,raspberrypi-08 --firmware <LoRa_Sensing>/firmware_backup_2026-11-03   # + role swaps
```

Open `http://<gateway tailscale IP>:8080/`. Results are saved as one JSON per job in
`~/lora-gateway/results/` (`--results` to move them).

`--pis` is the list of Pis the gateway may **touch**. Every other Pi is still listed, shown
as "unavailable for the test", but is never probed or used, because even a `STATUS` opens
its radio and its board may belong to another project. Omit it and every Pi in the tailnet
is eligible.

## The firmware it drives

The boards run the **LoRa_Sensing** firmware, not unionlabs `lora_phy`, and are driven as
they are rather than reflashed:

| Firmware | Role | Used for |
|---|---|---|
| `sx1276_sweep_tx` | transmit only | `SET CFG <sf> <bw> <cr> <pow>`, `SET SIZE`, `SET PERIOD`, `START`/`STOP` |
| `sx1276_sweep_rx` | receive only | `SET CFG <sf> <bw> <cr>`, `START`, one `P …` line per packet |

So a board's role comes from what is flashed on it. (pi07 = rx, pi08 = tx.) Power is
2–17 dBm because the firmware caps it.

**Role swaps.** With `--firmware`, a Pi in `FLASHABLE` (07, 08, 09, 16, 18) can take the other role for one job:
the gateway saves its `STATUS`, flashes the other firmware, runs the job, flashes its own
firmware back and restores its settings, on every way out of the job, Stop included. The
job only ends after that. It adds about 25 s (a swap of both boards: 36 s for 20 packets).

- Only the boards in `FLASHABLE` are flashed, only with the files in `FIRMWARE`, one TX and one
  RX per board type, checked by md5 at startup. Each board is checked by `STATUS` after loading.
- pi09 is a Teensy 4.1 and the rest are 4.0, so a Pi always goes home to the file for its own
  board and role, not to a build of its own.
- The hex files stay in the LoRa_Sensing backup folder, not in this public repo.
- The first loader try usually fails and the second lands; up to four are made.
- Restore covers every setting `STATUS` reports, including `LNA`, `ACC`, `ERRATA` and `TEMP`,
  and compares them afterwards. Only the packet and sequence counters restart from 0.
- If the gateway dies mid-job, the board keeps the swapped firmware. The page shows
  `(own: rx)`, and the next job using that Pi takes it home, without its old settings.

**Leave it as you found it.** Each agent reads the board's `STATUS` first and, whatever
happens, puts back its frequency, PHY config, size/period/rate, gain settings and run
state — then reads
`STATUS` again and reports any field that differs (`boards_restored` in the summary). It
also refuses a serial port another process already has open.

## A job

1. Validate: SF 7–12, BW 62.5/125/250/500 kHz, CR 4/5–4/8, 2–17 dBm, 902–928 MHz,
   6–255 bytes; the interval must exceed one packet's airtime + 20 ms (blank = auto,
   1.5 × airtime + 50 ms); both Pis enabled, online, right firmware, not busy.
   **Stop after** either a **duration** (1–600 s) or a **packet count** (1–5000, at most
   600 s of sending).
2. RX agent starts and reports `ready`; only then the TX agent starts.
3. TX sends until the duration or count is reached — or until **Stop** is pressed —
   then reports the exact number sent from the firmware's sequence counter. The RX keeps
   listening 2 s longer so the last packet in flight is counted, then is stopped.
4. Summary: delivered = unique CRC-ok sequence numbers from this job ÷ sent; CRC failures;
   RSSI / SNR / frequency error (min, mean, max); measured airtime; boards restored.

**The stop channel.** The agent is sent as a command argument (base64), not on stdin, so
stdin stays open as a control line: `stop` ends the job early, and EOF — the gateway or
the SSH connection dying — ends it too. Either way the agent's `finally` restores the
board, so a lost gateway cannot leave a radio transmitting.

## The page, live

While a job runs the page polls once a second and fetches only the new log lines
(`?since=<pi>:<n>,…`), so a 10-minute run stays light.

- **Tiles:** delivered %, CRC failures, mean RSSI and SNR (with range), time elapsed.
- **RSSI** and **SNR** per packet over time — two charts on one shared time axis, never one
  chart with two scales. Hover snaps a crosshair to the nearest packet. CRC-failed
  packets are hollow rings. While running the axis spans the planned time; once finished
  it fits what was actually sent.
- **Packet map:** one cell per sequence number — received ✓, CRC failed !, lost ✕, sent but
  not yet seen — with an icon in each cell so state is never color alone.
- Raw logs per Pi, and a JSON download of the whole job.

API: `GET /api/pis[?refresh=1]`, `POST /api/jobs`, `GET /api/jobs`,
`GET /api/jobs/<id>[?since=<pi>:<n>,…]`, `POST /api/jobs/<id>/stop`,
`GET /api/jobs/<id>/download`, `GET /api/airtime?sf=&bw=&cr=&size=`.

## Reservations

`visible_pis()` in `gateway.py` decides which Pis a user sees and may use. Today it
returns all of them. When the AWS reservation record names the Pis a user booked, that one
function filters to them — nothing else in the gateway changes.
