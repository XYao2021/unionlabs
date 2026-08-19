#!/usr/bin/env bash
# expose-pod-port.sh — make a port INSIDE a session pod reachable at the node's own IP.
#
#   ./expose-pod-port.sh 2-gnuradio-0 5599              # ACK socket, auto node port
#   ./expose-pod-port.sh 2-gnuradio-0 5599 30599        # ...pinned, so it never moves
#   ./expose-pod-port.sh --list                         # what is exposed now
#   ./expose-pod-port.sh --remove 2-gnuradio-0 5599
#
# WHY THIS EXISTS
# A container never receives traffic sent to its host's IP: the node gets the packet and
# nothing forwards it inward. That is why cross-machine ARQ fails — the source dials the
# sink's HOST address and nothing is listening there, even though the sink is listening
# happily inside its pod. Pod IPs cannot be used instead, because separate single-node
# clusters each allocate 10.42.0.x independently, so the same address means a different
# pod on each machine.
#
# A NodePort Service is the fix that survives a session restart: it selects the pod by
# its per-pod StatefulSet label, so it keeps pointing at the right container, and the
# pod keeps its own network namespace — unlike hostNetwork, which would put every
# session in one namespace fighting over 5599, 6080 and 5901.
set -euo pipefail
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/rke2/rke2.yaml}"
KUBECTL="${KUBECTL:-$(command -v kubectl || echo /snap/bin/kubectl)}"
NS="${NS:-default}"

k() { $KUBECTL -n "$NS" "$@"; }

if [ "${1:-}" = "--list" ]; then
  k get svc -l unionlabs.exposed=true -o wide
  exit 0
fi
if [ "${1:-}" = "--remove" ]; then
  k delete svc "expose-${2}-${3}" && echo "removed"
  exit 0
fi

POD="${1:?usage: expose-pod-port.sh <pod> <port> [nodePort]   (--list, --remove)}"
PORT="${2:?port inside the pod, e.g. 5599}"
NODEPORT="${3:-}"
SVC="expose-${POD}-${PORT}"

k get pod "$POD" >/dev/null   # fail early and clearly if the pod is not there

# Select THIS pod, not every pod of the set: StatefulSet gives each pod a unique
# statefulset.kubernetes.io/pod-name label, which is exactly what we want here.
{
  echo "apiVersion: v1"
  echo "kind: Service"
  echo "metadata:"
  echo "  name: ${SVC}"
  echo "  labels:"
  echo "    unionlabs.exposed: \"true\""
  echo "spec:"
  echo "  type: NodePort"
  echo "  selector:"
  echo "    statefulset.kubernetes.io/pod-name: ${POD}"
  echo "  ports:"
  echo "    - port: ${PORT}"
  echo "      targetPort: ${PORT}"
  [ -n "$NODEPORT" ] && echo "      nodePort: ${NODEPORT}"
  echo "      protocol: TCP"
} | k apply -f - >/dev/null

ASSIGNED=$(k get svc "$SVC" -o jsonpath='{.spec.ports[0].nodePort}')
NODEIP=$(k get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
echo "exposed ${POD}:${PORT}  ->  ${NODEIP}:${ASSIGNED}"
echo
echo "  the other machine's source should use:"
echo "    --ack-host ${NODEIP} --ack-port ${ASSIGNED}"
echo
echo "  check it from there first:"
echo "    python3 -c \"import socket;socket.create_connection(('${NODEIP}',${ASSIGNED}),timeout=5);print('open')\""
