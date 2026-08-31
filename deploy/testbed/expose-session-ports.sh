#!/bin/bash
# expose-session-ports.sh — publish every session pod's port block from the NODE,
# and tell each pod what its peers are CALLED, never where they are.
#
# Run by a systemd timer on the RKE2 node (see install-expose-ports.sh); safe to
# run repeatedly — a pod that already holds a block keeps it.
#
# WHY THIS RUNS ON THE HOST
# The earlier version ran inside the session container, which meant the container
# needed a Kubernetes API grant (services:create, pods:get) and read its own
# node's address out of .status.hostIP in order to report it. Experimenters get a
# root terminal on that desktop; they do NOT get the host. Anything the platform
# hands the container it hands them, so the privileged half moved out here where
# they cannot follow — and the session ServiceAccount now needs no API access at
# all. rbac-expose.yaml is withdrawn, not narrowed.
#
# WHAT CROSSES INTO THE POD
# Dialling a peer needs an address at connect() time, so something must cross.
# What crosses is a NAME:
#
#     /etc/hosts                       10.0.0.7  siteA        resolvable, not printed
#     /workspace/.../ports-<pod>.json  {"site": "siteA", "map": {...}}
#
# so `--ack-host siteA` resolves through libc like any hostname and every tool
# keeps working unchanged, while the shared record — which /workspace hands to
# every session on both testbeds, and which nothing used to reap — carries an
# alias only. An address never lands in a file that outlives the session, gets
# screenshotted, or is pasted into a paper.
#
# CONFIGURE  /etc/unionlabs/sites.conf   (written with a default on first run)
#     self  = siteA          # what THIS node is called
#     siteA =                # blank means this node's own InternalIP
#     siteB = 10.0.0.9       # the other testbed, so `--ack-host siteB` resolves
NS="${NS:-default}"
POD_MATCH="${POD_MATCH:--gnuradio-}"
PORTS="${PORTS:-5599,5700,5701,5800,5801,5802,5803,5804,5805,5806}"
BASE="${BASE:-31500}"
STRIDE="${STRIDE:-10}"
TRIES="${TRIES:-40}"
CONF="${CONF:-/etc/unionlabs/sites.conf}"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/rke2/rke2.yaml}"
KUBECTL="${KUBECTL:-$(command -v kubectl || echo /snap/bin/kubectl)}"

k() { $KUBECTL -n "$NS" "$@"; }
log() { logger -t expose-session-ports "$*"; [ -n "${VERBOSE:-}" ] && echo "$*"; }

# Heredocs inside $( ) do not parse under bash 3.2, and this file is edited on a
# Mac as often as it runs on the node, so the JSON builders are -c strings.
svc_json() { # <svc> <pod> <ports> <base>
  python3 -c '
import json, sys
svc, pod, ports, base = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
ps = [int(p) for p in ports.split(",") if p.strip()]
print(json.dumps({
    "apiVersion": "v1", "kind": "Service",
    "metadata": {"name": svc, "labels": {"app": "unionlabs-expose"}},
    "spec": {"type": "NodePort",
             "selector": {"statefulset.kubernetes.io/pod-name": pod},
             "ports": [{"name": "p%d" % p, "port": p, "targetPort": p,
                        "protocol": "TCP", "nodePort": base + i}
                       for i, p in enumerate(ps)]}}))
' "$1" "$2" "$3" "$4"
}

record_json() { # <pod> <site> <ports> <base>
  python3 -c '
import json, sys, time
pod, site, ports, base = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
ps = [int(p) for p in ports.split(",") if p.strip()]
print(json.dumps({"pod": pod, "site": site, "node_base": base,
                  "map": {str(p): base + i for i, p in enumerate(ps)},
                  "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                 indent=2))
' "$1" "$2" "$3" "$4"
}

NODE_IP="$(k get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null)"
[ -n "$NODE_IP" ] || exit 0        # no API here: not the node this belongs on

# ── sites.conf ────────────────────────────────────────────────────────────────
# A missing file must not mean a missing alias: with no name to use, a pod would
# fall back to reporting raw addresses, which is the thing this exists to stop.
# So the default is written out, named after the node rather than numbered.
if [ ! -f "$CONF" ]; then
  mkdir -p "$(dirname "$CONF")"
  { echo "# What each site is CALLED. These aliases are injected into every"
    echo "# session /etc/hosts, so an experimenter types a name, never an address."
    echo "self  = site-$(hostname -s)"
    echo "site-$(hostname -s) ="
    echo "# siteB = 10.0.0.9      # the other testbed"
  } > "$CONF"
  log "wrote default $CONF"
fi

SELF=""
SITES=""     # newline-separated "alias address"
while IFS= read -r line; do
  line="${line%%#*}"
  case "$line" in *=*) ;; *) continue ;; esac
  key="$(printf '%s' "${line%%=*}" | tr -d '[:space:]')"
  val="$(printf '%s' "${line#*=}"  | tr -d '[:space:]')"
  [ -n "$key" ] || continue
  if [ "$key" = "self" ]; then SELF="$val"; continue; fi
  [ -n "$val" ] || val="$NODE_IP"          # blank address means "this node"
  SITES="${SITES}${key} ${val}
"
done < "$CONF"
[ -n "$SELF" ] || SELF="site-$(hostname -s)"
# The self alias has to resolve even if sites.conf never gave it an address.
printf '%s' "$SITES" | grep -q "^$SELF " || SITES="$SELF $NODE_IP
$SITES"

HOSTS_BLOCK="$(printf '# unionlabs sites BEGIN\n%s# unionlabs sites END' "$SITES")"

LIVE="$(k get pods -o name 2>/dev/null | grep -- "$POD_MATCH" | cut -d/ -f2)"

# ── reap Services whose pod is gone ───────────────────────────────────────────
# NodePorts are cluster-unique and a block is ten of them, so a Service left
# behind by a dead session holds its block against every session that follows.
for SVC in $(k get svc -l app=unionlabs-expose -o name 2>/dev/null | cut -d/ -f2); do
  GONE="${SVC#expose-}"
  printf '%s\n' "$LIVE" | grep -qx "$GONE" && continue
  k delete svc "$SVC" >/dev/null 2>&1 && log "reaped $SVC (pod gone)"
done

[ -n "$LIVE" ] || exit 0

for POD in $LIVE; do
  [ "$(k get pod "$POD" -o jsonpath='{.status.phase}' 2>/dev/null)" = "Running" ] || continue
  SVC="expose-$POD"
  WON="$(k get svc "$SVC" -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null)"

  if [ -z "$WON" ]; then
    i=0
    while [ "$i" -lt "$TRIES" ]; do
      TRY=$((BASE + i * STRIDE))
      i=$((i + 1))
      OUT="$(svc_json "$SVC" "$POD" "$PORTS" "$TRY" | k create -f - 2>&1)"
      case "$OUT" in
        *created*) WON="$TRY"; break ;;
        # 422: some port in this block is taken. The API rejects the WHOLE
        # Service if even one is, so the block is requested, not reserved.
        *"already allocated"*|*"provided port is already"*) continue ;;
        *) log "$POD: $OUT"; break ;;
      esac
    done
  fi
  [ -n "$WON" ] || { log "$POD: no free block from $BASE after $TRIES tries"; continue; }

  # /etc/hosts inside a pod is a BIND MOUNT: `sed -i` renames a temp file over it
  # and fails with EBUSY. Truncate-and-rewrite keeps the inode, so this works.
  # Marker-delimited, so re-running replaces the block instead of growing it.
  printf '%s\n' "$HOSTS_BLOCK" | k exec -i "$POD" -- sh -c '
      new=$(sed "/# unionlabs sites BEGIN/,/# unionlabs sites END/d" /etc/hosts; cat)
      printf "%s\n" "$new" > /etc/hosts' >/dev/null 2>&1 \
    || { log "$POD: could not write /etc/hosts"; continue; }

  record_json "$POD" "$SELF" "$PORTS" "$WON" | k exec -i "$POD" -- sh -c '
      mkdir -p /workspace/experiments/settings
      cat > /workspace/experiments/settings/ports-'"$POD"'.json' >/dev/null 2>&1 \
    || { log "$POD: could not write the ports record"; continue; }

  # Reap records for sessions that no longer exist. Nothing used to, so the
  # shared folder kept one stale file per session ever run — each of which, in
  # the old format, carried a node address.
  printf '%s\n' "$LIVE" | k exec -i "$POD" -- sh -c '
      live=$(cat)                       # read ONCE: a grep per loop would eat it
      cd /workspace/experiments/settings 2>/dev/null || exit 0
      for f in ports-*.json; do
        [ -e "$f" ] || continue
        p=${f#ports-}; p=${p%.json}
        printf "%s\n" "$live" | grep -qx "$p" || rm -f "$f"
      done' >/dev/null 2>&1

  log "exposed $POD as $SELF, node block $WON+"
done
