# `searching/` — what a testbed measured about its own RF environment

One file per radio, written by `./prepare.sh` (which runs
`drivers/usrp/python/prepare_phy.py`) and read back automatically by `run.sh` and
`radio.sh`:

    phy-<key>-<band>-<subdev>-<ant>.json
                        the usable carriers, the noise floor, and the detector
                        thresholds measured through that one signal path

The name identifies a **signal path**, not a radio. One X310 can carry a VERT900
on one port and a VERT2450 on another, or two antennas of the same band on
different connectors — and each hears a different noise floor over a different
usable span. Keyed on the serial alone, each new survey silently replaced the
last, and the survivor was whichever ran most recently.

Selection reads the record, not the filename, so profiles written under older
names still resolve. `run.sh` and `radio.sh` narrow by the RF channel and
connector they are about to use, which is usually enough on its own: `radio.sh
rx` finds the RX2 survey and `radio.sh tx` the TX/RX one without being told.
Otherwise name it with `--band` / `--ant` / `--subdev` / `$UNION_BAND`, or type a
`--freq` inside the band you mean. With no way to tell, the ambiguity is reported
and the candidates listed, rather than one being picked and right half the time.

`<key>` is the radio's **serial** — stable and physical. Not the hostname: inside
a session that is the pod id and changes every session, so a profile filed under
it is orphaned the next time the session restarts. `$UNION_SITE` overrides, for a
site whose radios get swapped.

## Why this is separate from `settings/`

`settings/` holds records that describe **the session** — which devices this
account has reserved right now, which ports it published — and they churn: they
are rewritten at every session start and are meaningless once the pod is gone.

What is measured here is the opposite. A survey of a band takes minutes, costs a
radio, and stays true for as long as the antenna and the room do. Mixing the two
puts a months-lived measurement in the same folder as records that are expected
to be reaped, where it is one cleanup away from being deleted and one glance away
from being mistaken for something temporary.

## What is in a file (schema 3)

Every value appears **once**. What was measured once sits at the top; the
carriers you can choose between sit in `options`.

    radio           what was measured with: device, antenna, subdev, gain, band
    noise           the floor, its burstiness, and the receiver's own floor
                    beside it -- measured once, at the recommended carrier
    det_mult        derived from that noise. A RATIO above the floor, so it does
    sync_threshold  not change with gain or distance
    options[]       each one a complete parameter combination: a carrier, the
                    quiet band around it, its width, and whether the default
                    2 MS/s link fits. Only the recommended one is marked
                    `measured_here`; the rest inherit the thresholds
    use             which option is recommended (an index into `options`)

Read it with `python3 union/phy_profile.py --list`, or choose another option with
`--pick 2` (rank) or `--pick 5725` (carrier in MHz). `--max-options` on
`prepare.sh` changes how many are kept; anything dropped is named, never
silently truncated.

Schema 2 files still resolve — a measurement that cost a radio is not thrown away
over a layout change.

## This is a receive-side calibration

Run `./prepare.sh` on the **receiver**. Everything in the file is a property of
what that radio hears: the noise floor, which stretches are quiet, the energy
detector's margin above the floor, the correlator threshold, the receive gain.
A transmitter's own noise says nothing about the link.

A transmitter needs one number from it — the carrier, and it must be the
receiver's:

    ./radio.sh tx --device x310 --phy-node <the receiver's key>

which takes the carrier and nothing else. `radio.sh rx` applies the receive-side
values; `radio.sh tx` never does, because `--det-mult` and `--sync-threshold`
configure a receive chain and a transmitter has no use for either.

## Two things a receive-only survey cannot settle

**The transmit gain.** The survey records the gain it *listened* at, and that is
offered as `rx_gain` only. How hard to transmit depends on the path loss to the
other radio, which no receive-only measurement sees — and transmit and receive
gains are different quantities with different ranges anyway (a B210 transmits
over ~89 dB and receives over ~76; a UBX does both in 31.5). A transmit run keeps
the per-device default until a link test says otherwise.

**`sync_threshold`.** The survey can only say where the noise is. Where a *real*
preamble scores is a property of the link, and no receive-only measurement sees
one. When noise never triggers ACQ the saved value is a placeholder — the file
records `sync_threshold_measured: false` and every consumer says so when it
applies it. Check the `[ACQ] Peak correlation` on your first successful decode and
set the threshold between that and the noise.

**The carrier both ends use.** Each profile recommends the widest quiet region
*that node* measured, and on two testbeds that is two different frequencies. Two
ends left to their own profiles tune apart and hear nothing — no error, just
silence. Ask for the overlap instead:

    python3 union/phy_profile.py --common

and pin the result for both ends, in the topology or with `--freq`. A run that
takes its carrier from its own survey in a role that has a partner on the air
says so rather than letting you find out from a dead link.

## Precedence

    explicit flag  >  topology file  >  this measurement  >  built-in default

The first two are someone stating an intent; this is only a measurement of where
the radio happens to be standing, so it never overrides a choice anyone made.
