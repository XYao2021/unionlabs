# `sim/` — the radio-free driver

No hardware. Two backends, used for every radio-free validation in the repo:

- **`ideal`** — lossless byte pipe (round-trips exactly).
- **`pyphy`** — the *real* modem DSP + AWGN (framing · FEC · modulate · noise · demod · decode),
  so an algorithm meets true corruption without a radio.

They are the reference implementations of `PhyDriver.transfer()` and today live as
`IdealChannel` / `PyphyChannel` in [`../../union/phy_link.py`](../../union/phy_link.py)
(`make_channel("ideal"|"pyphy")`). Select with `./run.sh --algo <name> --channel ideal|pyphy`.
