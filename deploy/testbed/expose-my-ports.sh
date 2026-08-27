#!/usr/bin/env bash
# expose-my-ports.sh — publish this container's whole set of ports in ONE Service.
#
#   ./expose-my-ports.sh                 # the platform's default 10 ports
#   ./expose-my-ports.sh --base 31600    # start the node-side block here
#   ./expose-my-ports.sh --ports 5599,5700,5800
#   ./expose-my-ports.sh --remove
#
# expose-my-port.sh (singular) publishes one port and is still the right tool when
# that is all you need. This is for the normal case: a session needs its ACK
# socket AND its algorithm network AND its peer links reachable, which was three
# or ten invocations, each with its own Service to remember and clean up.
#
# WHY THESE PORTS. A block like 4500-4509 would expose nothing: the platform
# listens on --ack-port 5599, --net-port 5700 (relays add +1), and --peer-port
# 5800+k for peer k. Those are the ten below, and they are the ones a sender on
# another machine actually has to reach.
#
# WHAT THIS CANNOT DO. NodePorts are unique across the whole cluster, so two
# sessions cannot hold the same block, and the API rejects the ENTIRE Service if
# even one requested port is taken. The block is therefore requested, not
# reserved: on a collision this walks to the next block and prints the one it won.
# If you need numbers a remote sender can hardcode once and forget, that is a
# per-slot Service that outlives the pod, not this.
#
# Needs the one-time grant in rbac-expose.yaml — the same one expose-my-port.sh
# needs, with no additions. Nothing has to be run on the host.
set -euo pipefail

SA=/var/run/secrets/kubernetes.io/serviceaccount
API="https://kubernetes.default.svc"

# The ports the platform actually listens on (see the note above).
PORTS="5599,5700,5701,5800,5801,5802,5803,5804,5805,5806"
BASE=31500          # first node port to try; inside the default 30000-32767 range
STRIDE=10           # how far to jump when a block is taken
TRIES=40
QUIET=0

while [ $# -gt 0 ]; do
  case "$1" in
    --base)   BASE="$2";  shift 2;;
    --ports)  PORTS="$2"; shift 2;;
    --stride) STRIDE="$2"; shift 2;;
    --quiet)  QUIET=1;    shift;;
    --remove) REMOVE=1;   shift;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done

[ -r "$SA/token" ] || {
  [ "$QUIET" = 1 ] && exit 0
  echo "no ServiceAccount token at $SA — this must run inside a pod" >&2; exit 1; }
TOKEN="$(cat "$SA/token")"
NS="$(cat "$SA/namespace")"
POD="$(hostname)"
SVC="expose-$POD"

api() { # api <METHOD> <path> [body]
  local m="$1" p="$2" b="${3:-}"
  if [ -n "$b" ]; then
    curl -sS --cacert "$SA/ca.crt" -H "Authorization: Bearer $TOKEN" \
         -H "Content-Type: application/json" -X "$m" "$API$p" -d "$b"
  else
    curl -sS --cacert "$SA/ca.crt" -H "Authorization: Bearer $TOKEN" -X "$m" "$API$p"
  fi
}

# The API pretty-prints, so match on parsed JSON rather than a substring.
status_code() {
  python3 -c "
import json, sys
try:    d = json.load(sys.stdin)
except Exception: sys.exit(0)
print(d.get('code', '') if d.get('kind') == 'Status' else '')" <<<"$1"
}

if [ "${REMOVE:-0}" = 1 ]; then
  api DELETE "/api/v1/namespaces/$NS/services/$SVC" >/dev/null
  echo "removed $SVC"; exit 0
fi

body() { # body <base>
  python3 - "$SVC" "$POD" "$PORTS" "$1" <<'PY'
import json, sys
svc, pod, ports, base = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
ps = [int(p) for p in ports.split(",") if p.strip()]
print(json.dumps({
    "apiVersion": "v1", "kind": "Service",
    "metadata": {"name": svc, "labels": {"app": "unionlabs-expose"}},
    "spec": {"type": "NodePort",
             # This pod only. A selector matching several pods would load-balance
             # across them, which addresses none of them.
             "selector": {"statefulset.kubernetes.io/pod-name": pod},
             "ports": [{"name": f"p{p}", "port": p, "targetPort": p,
                        "protocol": "TCP", "nodePort": base + i}
                       for i, p in enumerate(ps)]}}))
PY
}

# Replace any block this pod already holds, so re-running is a no-op rather than a 409.
api DELETE "/api/v1/namespaces/$NS/services/$SVC" >/dev/null 2>&1 || true

OUT=""; WON=""
for ((i = 0; i < TRIES; i++)); do
  TRY=$((BASE + i * STRIDE))
  OUT=$(api POST "/api/v1/namespaces/$NS/services" "$(body "$TRY")")
  CODE=$(status_code "$OUT")
  if [ -z "$CODE" ]; then WON="$TRY"; break; fi
  if [ "$CODE" = "403" ]; then
    [ "$QUIET" = 1 ] && exit 0
    cat >&2 <<TXT
The Kubernetes API refused this (403): the one-time grant is missing.

  Ask an admin to run ONCE on the host that owns this cluster:
    sudo env KUBECONFIG=/etc/rancher/rke2/rke2.yaml /snap/bin/kubectl \\
      apply -f ~/Desktop/unionlabs/deploy/testbed/rbac-expose.yaml

  After that, re-run this script — no host access needed again.
TXT
    exit 1
  fi
  # 422 = some port in this block is already allocated. Try the next one.
done

[ -n "$WON" ] || {
  [ "$QUIET" = 1 ] && exit 0
  echo "could not find a free block after $TRIES tries from $BASE:" >&2
  echo "$OUT" | head -20 >&2; exit 1; }

NODEIP=$(api GET "/api/v1/namespaces/$NS/pods/$POD" | python3 -c "
import json, sys
try:    print(json.load(sys.stdin)['status']['hostIP'])
except Exception: pass" 2>/dev/null || true)
[ -n "$NODEIP" ] || NODEIP="<node-ip>"

# Record it where the rest of the platform looks, so another node can read the
# mapping instead of being told it.
OUTDIR=/workspace/experiments/settings
mkdir -p "$OUTDIR" 2>/dev/null || true
python3 - "$OUTDIR/ports-$POD.json" "$POD" "$NODEIP" "$PORTS" "$WON" <<'PY' 2>/dev/null || true
import json, sys, time
path, pod, ip, ports, base = sys.argv[1:6]
ps = [int(p) for p in ports.split(",") if p.strip()]
json.dump({"pod": pod, "node_ip": ip, "node_base": int(base),
           "map": {str(p): int(base) + i for i, p in enumerate(ps)},
           "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
          open(path, "w"), indent=2)
PY

[ "$QUIET" = 1 ] && exit 0
echo "exposed $POD on $NODEIP  (node block $WON+)"
python3 - "$NODEIP" "$PORTS" "$WON" <<'PY'
import sys
ip, ports, base = sys.argv[1], sys.argv[2], int(sys.argv[3])
names = {5599: "ack", 5700: "net", 5701: "net+1 (relay)"}
for i, p in enumerate(int(x) for x in ports.split(",") if x.strip()):
    label = names.get(p) or (f"peer {p - 5800}" if 5800 <= p <= 5899 else "")
    print(f"  container {p}  ->  {ip}:{base + i}   {label}")
PY
