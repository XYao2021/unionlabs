#!/bin/bash
# Is a USRP reachable from THIS container/host?   usage: ./check-radio.sh [addr]
# The radio's network must be attached by whoever LAUNCHES the container (pod spec /
# docker run) — no image can do it; see deploy/PLATFORM_RADIO_REQUEST.md.
ADDR="${1:-192.168.10.2}"
echo "── interfaces here ──"
ip -4 addr 2>/dev/null | grep -E '^[0-9]+:|inet ' | sed 's/^/  /' || ifconfig | sed 's/^/  /'
echo "── route to ${ADDR} ──"
ip route get "${ADDR}" 2>&1 | sed 's/^/  /'
echo "── ping ${ADDR} ──"
ping -c 2 -W 2 "${ADDR}" >/dev/null 2>&1 && echo "  reachable" \
  || echo "  NOT reachable — the radio's subnet is not attached here."
echo "── UHD ──"
uhd_find_devices --args "addr=${ADDR}" 2>&1 | tail -8 | sed 's/^/  /'
