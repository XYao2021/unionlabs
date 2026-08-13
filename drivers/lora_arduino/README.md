# `lora_arduino/` — LoRa PHY driver (planned)

Placeholder for the Arduino-based LoRa TX/RX backend (the code under
`~/Desktop/Project Codes/LoRa/`). It will implement the same **`PhyDriver`** interface as
`usrp_uhd/` (`../../union/driver.py`) — a different PHY behind the *same* contract, so every
algorithm in `algorithms/` runs over LoRa unchanged. Nothing here yet.
