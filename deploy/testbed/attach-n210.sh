#!/bin/bash
# attach-n210.sh — give every gnuradio session pod on this RKE2 node a macvlan
# interface on the N210's NIC. Run by a systemd timer (see install-attach-n210.sh);
# safe to run repeatedly: pods that already have n210v are skipped.
#
# Why this exists: the website platform creates session pods (default/<N>-gnuradio-0)
# via the cluster API with no radio network attached, and an interface injected by
# hand dies with each pod. This is the node-local fix the platform cannot override.
PARENT="${PARENT:-enx144fd7da6276}"        # host NIC wired to the N210
export KUBECONFIG=/etc/rancher/rke2/rke2.yaml
KUBECTL=/snap/bin/kubectl
CRICTL="/var/lib/rancher/rke2/bin/crictl -r unix:///run/k3s/containerd/containerd.sock"

for POD in $($KUBECTL -n default get pods -o name 2>/dev/null | grep -- -gnuradio- | cut -d/ -f2); do
  CID=$($KUBECTL -n default get pod "$POD" -o jsonpath='{.status.containerStatuses[0].containerID}' 2>/dev/null | sed 's|containerd://||')
  [ -n "$CID" ] || continue
  PID=$($CRICTL inspect "$CID" 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['info']['pid'])" 2>/dev/null)
  [ -n "$PID" ] && [ "$PID" -gt 100 ] || continue
  nsenter -t "$PID" -n ip link show n210v >/dev/null 2>&1 && continue   # already attached

  # session number -> stable IP: <N>-gnuradio-0 gets 192.168.10.(100+N)
  NUM="${POD%%-*}"; case "$NUM" in ''|*[!0-9]*) NUM=0;; esac
  IP="192.168.10.$((100 + NUM))"

  ip link add n210tmp link "$PARENT" type macvlan mode bridge 2>/dev/null || continue
  ip link set n210tmp netns "$PID"
  nsenter -t "$PID" -n ip link set n210tmp name n210v
  nsenter -t "$PID" -n ip addr add "$IP/24" dev n210v
  nsenter -t "$PID" -n ip link set n210v up
  logger -t attach-n210 "attached $PARENT to $POD as n210v ($IP)"
done
