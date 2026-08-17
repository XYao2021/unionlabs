# `drivers/` — the driver layer (one per PHY × testbed)

Each subfolder is a concrete backend that implements the **`PhyDriver`** interface in
[`../union/driver.py`](../union/driver.py) (`transfer` / `broadcast` / `superpose`). The
middleware in `../union/` and the experiments in `../experiments/` + `../experiments/` call
that interface and never import a specific PHY — so the **same experiment runs on any driver**.
Adding a PHY or a testbed = adding a folder here; nothing above the seam changes. This portable
seam is what UnionLabs has and POWDER / AERPAW do not.

| Driver | PHY × testbed | Status |
|---|---|---|
| `usrp/` | USRP over UHD (`sdr_system` C++ + `pyphy` blocks + `RadioRoundTrip`) — our bench | **built** |
| `sim/` | radio-free: `ideal` (lossless) / `pyphy` (real modem + AWGN) | **built** (channels live in `../union/phy_link.py`) |
| `lora/` | SX1276 LoRa (sim / Arduino-serial / Pi-SPI) | **built** | |
| `usrp_powder/`, … | same USRP PHY on a remote testbed — a different driver | planned |
