#!/usr/bin/env python3
"""check-novnc.py — say WHICH layer is breaking a noVNC desktop, instead of guessing.

The browser reports "Failed to connect to server" for every possible cause: a dead
X server, a proxy that will not forward a WebSocket upgrade, a client asking for the
wrong path. This walks the same route the browser walks and names the layer.

Standard library only — nothing to install beyond python3.

    # on the machine running the container
    python3 check-novnc.py

    # from your laptop, against the portal URL you actually open
    python3 check-novnc.py --url https://portal.example.com/lab/6080/vnc.html
"""
import argparse, base64, json, os, re, socket, ssl, subprocess, sys
from urllib.parse import urlparse, urljoin
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from http.client import BadStatusLine

OK, BAD, INFO = "  ok  ", " FAIL ", "  ..  "

def say(mark, msg):
    print(f"[{mark}] {msg}")

def rule(title):
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))

# ── the WebSocket half: handshake, then read one frame ──────────────────────────
def ws_probe(host, port, path, use_tls, timeout=8):
    """Return (status_line, first_frame_bytes|None, error|None)."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        return None, None, f"cannot reach {host}:{port} ({e})"
    try:
        if use_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False          # portals often sit behind odd certs;
            ctx.verify_mode = ssl.CERT_NONE     # we are diagnosing reachability, not trust
            sock = ctx.wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path.lstrip('/')} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Sec-WebSocket-Protocol: binary\r\n\r\n")
        sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return None, None, "connection closed during handshake"
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n")[0].decode(errors="replace")
        if "101" not in status:
            return status, None, None
        # A 101 alone proves nothing: websockify answers it BEFORE dialing the VNC
        # server, so a dead desktop still upgrades cleanly. The first frame is the
        # proof — a live x11vnc sends the RFB greeting unprompted.
        sock.settimeout(5)
        try:
            while len(rest) < 2:
                rest += sock.recv(4096)
            ln = rest[1] & 0x7F
            payload = rest[2:2 + ln]
            while len(payload) < ln:
                payload += sock.recv(4096)
        except socket.timeout:
            return status, b"", None
        return status, payload, None
    finally:
        try: sock.close()
        except Exception: pass

def verdict_for(status, frame, err, where):
    if err:
        say(BAD, f"{where}: {err}")
        return False
    if status is None or "101" not in (status or ""):
        say(BAD, f"{where}: no WebSocket upgrade — got {status!r}")
        # The two 404s mean opposite things, and the wording tells them apart:
        # websockify says "File not found" because a request that reaches it WITHOUT
        # the upgrade headers is just a GET for a file of that name. Anything else
        # 404ing means the request never reached websockify at all.
        if status and "File not found" in status:
            print("       That is websockify answering a PLAIN GET: the request arrived,")
            print("       but stripped of its Upgrade/Connection headers. The hop in")
            print("       front is not forwarding the WebSocket upgrade — an AWS ALB")
            print("       does by default, CloudFront needs the right policy, and an")
            print("       API Gateway HTTP API cannot do it at all.")
        elif status and "404" in status:
            print("       Nothing is routed at this path. The proxy serves the page but")
            print("       not the socket, or the client is asking for the wrong path.")
        elif status and ("502" in status or "503" in status):
            print("       The proxy could not reach the container behind it.")
        return False
    if not frame:
        say(BAD, f"{where}: upgraded (101) but the desktop sent nothing")
        print("       websockify is alive; whatever is behind it on 5900 is not.")
        return False
    if frame.startswith(b"RFB "):
        say(OK, f"{where}: 101 + {frame.strip().decode(errors='replace')} — a real desktop")
        return True
    say(BAD, f"{where}: 101 but the first frame is not RFB: {frame[:24]!r}")
    return False

# ── mode 1: the container on this machine ───────────────────────────────────────
def check_container(name, port):
    rule(f"container '{name}'")
    def dk(*a):
        return subprocess.run(["docker", *a], capture_output=True, text=True)
    r = dk("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}} {{.Config.Image}}", name)
    if r.returncode != 0:
        say(BAD, f"no container named '{name}' ({r.stderr.strip()})")
        return False
    status, code, image = r.stdout.split()
    say(OK if status == "running" else BAD, f"state: {status} (exit {code}), image {image}")
    if status != "running":
        print("       the startup script refuses to serve a desktop it cannot back;")
        print("       the reason is the FATAL line below.")

    logs = dk("logs", name).stdout + dk("logs", name).stderr
    fatal = [l for l in logs.splitlines() if "FATAL" in l]
    if fatal:
        say(BAD, "the container reported why it gave up:")
        for l in fatal[:3]:
            print("       " + l)
        return False
    say(OK, "no FATAL in the logs")

    # Is this actually the fixed image, or an older one still cached under the tag?
    r = dk("exec", name, "grep", "-c", "window.location.pathname",
           "/usr/share/novnc/app/ui.js")
    if r.returncode == 0 and r.stdout.strip() not in ("", "0"):
        say(OK, "noVNC derives its socket path from the page location (fix present)")
    else:
        say(BAD, "this image predates the path fix — a prefixed portal will 404")
        print("       docker load -i unionlabs-amd64.tar   and start it again")

    r = dk("exec", name, "git", "-C", "/opt/unionlabs", "log", "--oneline", "-1")
    if r.returncode == 0:
        say(INFO, f"platform commit inside the image: {r.stdout.strip()}")

    st, fr, er = ws_probe("127.0.0.1", port, "websockify", use_tls=False)
    return verdict_for(st, fr, er, f"localhost:{port}")

# ── mode 2: the URL you actually open in a browser ──────────────────────────────
def check_url(page_url, cookie=None):
    rule("portal URL")
    u = urlparse(page_url)
    if not u.scheme.startswith("http"):
        say(BAD, "give a full http(s):// URL to the vnc.html page")
        return False
    tls = u.scheme == "https"
    host = u.hostname
    port = u.port or (443 if tls else 80)
    say(INFO, f"page: {page_url}")
    hdrs = {"User-Agent": "check-novnc"}
    if cookie:
        hdrs["Cookie"] = cookie

    ctx = ssl.create_default_context()
    ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        with urlopen(Request(page_url, headers=hdrs),
                     timeout=15, context=ctx) as resp:
            body = resp.read().decode("utf-8", "replace")
            final = resp.geturl()
    except HTTPError as e:
        say(BAD, f"the page itself returned HTTP {e.code}")
        print("       nothing is serving noVNC at that URL; fix that before the socket.")
        return False
    except URLError as e:
        say(BAD, f"cannot fetch the page ({e.reason})")
        if isinstance(getattr(e, "reason", None), socket.timeout) or "timed out" in str(e.reason):
            print("       A timeout means the packets are being DROPPED — a firewall or")
            print("       security group. A port with nothing behind it refuses the")
            print("       connection immediately instead.")
        return False
    except BadStatusLine as e:
        # Something answered, but not in HTTP. Its first line usually says what it is.
        banner = str(e.line if hasattr(e, "line") else e).strip().strip("'\"")
        say(BAD, f"that port is not HTTP — it answered with: {banner!r}")
        if banner.startswith("SSH-"):
            print("       That is an SSH server. The noVNC desktop is on a different port;")
            print("       this one cannot serve a web page no matter what image is running.")
        return False
    except Exception as e:
        say(BAD, f"cannot fetch the page ({type(e).__name__}: {e})")
        return False

    if final != page_url:
        say(INFO, f"redirected to: {final}")
        if re.search(r"login|signin|auth", final, re.I):
            say(BAD, "that is a login page — this script has no session.")
            print("       Run the container-local mode on the host instead, or export")
            print("       your session cookie and re-run with --cookie.")
            return False
    if "noVNC" not in body and "vnc.html" not in body and "rfb" not in body.lower():
        say(BAD, "the page does not look like noVNC — is this the portal's own client?")
        print("       If the portal supplies its own viewer it talks to raw VNC (5900),")
        print("       not our 6080, and the port to expose is different.")
        return False
    say(OK, "the page is a noVNC client")

    # Which upload is this? Platforms that allocate a port per image make it very easy
    # to keep testing the previous deployment.
    try:
        with urlopen(Request(urljoin(final, "unionlabs-version.txt"), headers=hdrs),
                     timeout=10, context=ctx) as resp:
            for line in resp.read().decode("utf-8", "replace").strip().splitlines():
                say(INFO, "build: " + line.strip())
    except Exception:
        say(INFO, "no /unionlabs-version.txt — this image predates the build stamp")

    # What path will THIS page's javascript ask for?
    page_dir = re.sub(r"[^/]*$", "", urlparse(final).path).lstrip("/")
    ui_url = urljoin(final, "app/ui.js")
    derived, patched = "websockify", None
    try:
        with urlopen(Request(ui_url, headers=hdrs),
                     timeout=15, context=ctx) as resp:
            ui = resp.read().decode("utf-8", "replace")
        patched = "window.location.pathname" in ui and "initSetting('path'" in ui
        derived = (page_dir + "websockify") if patched else "websockify"
        say(OK if patched else INFO,
            f"served noVNC {'derives its path (fixed image)' if patched else 'uses the bare default (unpatched)'}")
    except Exception:
        say(INFO, "could not read app/ui.js; assuming the stock default")

    forced = re.search(r"[?&]path=([^&]+)", page_url)
    if forced:
        derived = forced.group(1)
        say(INFO, f"?path= in your URL overrides everything: {derived}")

    print()
    good = verdict_for(*ws_probe(host, port, derived, tls), where=f"/{derived}")
    if not good and derived != "websockify":
        say(INFO, "trying the root path, to see which one the portal routes:")
        verdict_for(*ws_probe(host, port, "websockify", tls), where="/websockify")
    elif not good and page_dir:
        say(INFO, "trying the prefixed path:")
        if verdict_for(*ws_probe(host, port, page_dir + "websockify", tls),
                       where=f"/{page_dir}websockify"):
            print(f"\n       Workaround right now:  {final}?path={page_dir}websockify")
    return good

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--container", default="unionlabs", help="container name (default: unionlabs)")
    ap.add_argument("--port", type=int, default=6080, help="noVNC port on the host (default: 6080)")
    ap.add_argument("--url", help="the vnc.html URL you open in the browser")
    ap.add_argument("--cookie", help="Cookie header, if the portal needs a logged-in session")
    a = ap.parse_args()

    ok = check_url(a.url, a.cookie) if a.url else check_container(a.container, a.port)
    rule("verdict")
    if ok:
        print("The desktop is reachable over the route tested. If the BROWSER still fails,")
        print("the difference is the browser: check devtools -> Network -> WS for the URL")
        print("it requested, and compare it with the path proved working above.")
    else:
        print("The failure is at the layer marked FAIL above — fix that one, not the others.")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
