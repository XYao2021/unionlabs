#!/usr/bin/env bash
# expose-my-port.sh — run INSIDE a session container to publish one of its own ports
# on the node's IP, so a container on another machine can reach it.
#
#   ./expose-my-port.sh 5599            # ACK socket, node port chosen for you
#   ./expose-my-port.sh 5599 30599      # ...pinned, so it survives re-runs unchanged
#   ./expose-my-port.sh --remove 5599
#
# A container never receives traffic sent to its host's IP — the node gets the packet
# and nothing carries it inward. That is why cross-machine ARQ fails. This asks the
# Kubernetes API for a NodePort Service pointing at THIS pod, using the ServiceAccount
# token already mounted here, so no host login is needed.
#
# Requires the one-time grant in rbac-expose.yaml. Without it the API answers 403 and
# this says so rather than failing obscurely.
set -euo pipefail
SA=/var/run/secrets/kubernetes.io/serviceaccount
API="https://kubernetes.default.svc"

[ -r "$SA/token" ] || { echo "no ServiceAccount token at $SA — this must run inside a pod, with automountServiceAccountToken enabled" >&2; exit 1; }
TOKEN="$(cat "$SA/token")"
NS="$(cat "$SA/namespace")"
POD="$(hostname)"

api() { # api <METHOD> <path> [body]
  local m="$1" p="$2" b="${3:-}"
  if [ -n "$b" ]; then
    curl -sS --cacert "$SA/ca.crt" -H "Authorization: Bearer $TOKEN" \
         -H "Content-Type: application/json" -X "$m" "$API$p" -d "$b"
  else
    curl -sS --cacert "$SA/ca.crt" -H "Authorization: Bearer $TOKEN" -X "$m" "$API$p"
  fi
}

if [ "${1:-}" = "--remove" ]; then
  api DELETE "/api/v1/namespaces/$NS/services/expose-$POD-${2:?port}" >/dev/null
  echo "removed"; exit 0
fi

PORT="${1:?usage: expose-my-port.sh <port> [nodePort]   (--remove <port>)}"
NODEPORT="${2:-}"
SVC="expose-$POD-$PORT"

BODY=$(python3 - "$SVC" "$POD" "$PORT" "$NODEPORT" <<'PY'
import json, sys
svc, pod, port, nodeport = sys.argv[1:5]
p = {"port": int(port), "targetPort": int(port), "protocol": "TCP"}
if nodeport:
    p["nodePort"] = int(nodeport)
print(json.dumps({
    "apiVersion": "v1", "kind": "Service",
    "metadata": {"name": svc, "labels": {"unionlabs.exposed": "true"}},
    # Select THIS pod: a StatefulSet gives each pod a unique pod-name label.
    "spec": {"type": "NodePort",
             "selector": {"statefulset.kubernetes.io/pod-name": pod},
             "ports": [p]}}))
PY
)

# Read the reply as JSON. Matching on a substring like '"code":403' is wrong: the API
# pretty-prints with a space after the colon, so the check silently never fired and a
# plain 403 surfaced as "unexpected API reply".
status_code() {
  python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(d.get('code', '') if d.get('kind') == 'Status' else '')" <<<"$1"
}

OUT=$(api POST "/api/v1/namespaces/$NS/services" "$BODY")
if [ "$(status_code "$OUT")" = "409" ]; then         # already there: replace it
  api DELETE "/api/v1/namespaces/$NS/services/$SVC" >/dev/null
  OUT=$(api POST "/api/v1/namespaces/$NS/services" "$BODY")
fi
if [ "$(status_code "$OUT")" = "403" ]; then
  echo "The Kubernetes API refused this (403): the one-time grant is missing." >&2
  echo "" >&2
  echo "  Ask an admin to run ONCE on the host that owns this cluster:" >&2
  echo "    sudo env KUBECONFIG=/etc/rancher/rke2/rke2.yaml /snap/bin/kubectl \\" >&2
  echo "      apply -f ~/Desktop/unionlabs/deploy/testbed/rbac-expose.yaml" >&2
  echo "" >&2
  echo "  After that, re-run this script — no host access needed again." >&2
  exit 1
fi

ASSIGNED=$(echo "$OUT" | python3 -c "import json,sys; print(json.load(sys.stdin)['spec']['ports'][0]['nodePort'])" 2>/dev/null) \
  || { echo "unexpected API reply:"; echo "$OUT" | head -20; exit 1; }

# The node's own address — what the other machine must dial. Ask this pod which node it
# is on (.status.hostIP); listing cluster nodes would need a wider, cluster-scoped grant
# and answers the same question.
NODEIP=$(api GET "/api/v1/namespaces/$NS/pods/$POD" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin)['status']['hostIP'])
except Exception:
    pass" 2>/dev/null || true)
if [ -z "$NODEIP" ]; then
  NODEIP=$(api GET "/api/v1/nodes" | python3 -c "
import json, sys
try:
    a = json.load(sys.stdin)['items'][0]['status']['addresses']
    print(next(x['address'] for x in a if x['type'] == 'InternalIP'))
except Exception:
    pass" 2>/dev/null || true)
fi
if [ -z "$NODEIP" ]; then
  NODEIP="<this machine's IP>"
  echo "note: could not read this node's address (the grant may predate pods:get)." >&2
  echo "      The Service IS created — use this machine's own IP with the port below." >&2
fi

echo "exposed  $POD:$PORT  ->  $NODEIP:$ASSIGNED"
echo
echo "  on the OTHER machine's container:"
echo "    --ack-host $NODEIP --ack-port $ASSIGNED"
echo "  check it there first:"
echo "    python3 -c \"import socket;socket.create_connection(('$NODEIP',$ASSIGNED),timeout=5);print('open')\""
