#!/usr/bin/env python3
"""brother_print — native AppSocket driver for the Brother VC-500W.

Talks the printer's XML-over-TCP protocol on port 9100 directly, replacing the
old qemu-arm + zsocket + cups-proxy stack. A print job is:

    1. <lock><op>set</op>...</lock>          -> printer returns a job_token
    2. <print>...<cutmode>full</cutmode></print>   (metadata block, own write)
    3. wait for "ready to receive"
    4. raw JPEG bytes (exactly <datasize> of them)
    5. wait for "print data received" / completion
    6. <lock><op>cancel</op><job_token>..</job_token></lock>   <- ALWAYS release

Releasing the lock in a finally block is the fix for the stuck-BUSY state: the
old pipeline left the lock held when the process was killed mid-job, so the
printer stayed BUSY until power-cycled. Auto-cut is native here (<cutmode>full),
so the proxy is no longer needed.

Protocol reference: sgrimee/labelprinter-vc500w, corentin-soriano/vc-500w_autocut,
m7i.org. Confirmed by real end-to-end prints; a byte-level cross-check of the
print-header against the working zsocket traffic (cups-proxy journal) is still
pending — see docs/protocol.md.
"""

import pathlib
import os
import re
import socket
import subprocess
import time

# --- device -----------------------------------------------------------------
PRINTER_HOST = "VC-500W3904.local"   # mDNS name; survives DHCP IP changes
PRINTER_IP = "192.168.8.249"          # fallback if mDNS can't resolve
PORT = 9100

CONNECT_TIMEOUT = 4.0
IO_TIMEOUT = 30.0
DRAIN_TIMEOUT = 2.0

XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'

# mode -> (speed, lpi), from the device's own config.xml media_mode table.
MODES = {
    "vivid": (0, 317),
    "color": (1, 264),
    "bw": (2, 400),
}
DEFAULT_MODE = "vivid"

# Rotate 180° before printing. Orientation vs. the old pnmflip pipeline is
# unverified on the first native print; if labels come out upside-down, set
# BROTHER_FLIP=1 (no code change needed) and, once confirmed, flip this default.
FLIP = os.environ.get("BROTHER_FLIP") == "1"

# White safe-margin (% of each dimension) added around every image before
# printing. The printable media (0.978") is narrower than the tape (1.022"), so
# content at the extreme edges gets clipped; this insets it. Tunable via
# BROTHER_MARGIN (e.g. "0" to disable, "6" for a wider margin).
MARGIN_PCT = float(os.environ.get("BROTHER_MARGIN", "4"))

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
PRINT_LOG = CACHE / "print.log"


class PrinterError(RuntimeError):
    """Raised when the printer reports an error or the protocol goes sideways."""


# --- helpers ----------------------------------------------------------------
def xml_field(blob, tag):
    """Extract the text of <tag>..</tag> from an XML blob (bytes or str)."""
    if isinstance(blob, str):
        blob = blob.encode()
    if isinstance(tag, str):
        tag = tag.encode()
    m = re.search(b"<" + tag + b">(.*?)</" + tag + b">", blob, re.DOTALL)
    return m.group(1).decode("utf-8", "replace").strip() if m else None


_resolved = {"ip": None, "at": 0.0}
_RESOLVE_TTL = 30.0  # seconds; cap how often we spawn avahi-resolve


def resolve_host():
    """Resolve the printer's current IP, tolerating DHCP changes.

    This box has no nss-mdns, so socket.getaddrinfo() can't resolve `.local` —
    avahi-resolve is the working mDNS resolver here. Order: mDNS via avahi
    (picks up a new DHCP IP) → getaddrinfo (in case nss-mdns gets added later)
    → last-known static IP. Result is cached briefly to avoid hammering avahi
    (lazy-brother polls every 2s)."""
    now = time.monotonic()
    if _resolved["ip"] and now - _resolved["at"] < _RESOLVE_TTL:
        return _resolved["ip"]

    ip = None
    try:
        r = subprocess.run(["avahi-resolve", "-4", "-n", PRINTER_HOST],
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            ip = r.stdout.split()[-1].strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    if not ip:
        try:
            info = socket.getaddrinfo(PRINTER_HOST, PORT, proto=socket.IPPROTO_TCP)
            ip = info[0][4][0]
        except OSError:
            pass
    ip = ip or PRINTER_IP

    _resolved["ip"] = ip
    _resolved["at"] = now
    return ip


def log_event(kind, msg=""):
    """Append a structured event for lazy-brother's native event panel."""
    try:
        PRINT_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        with PRINT_LOG.open("a") as fh:
            fh.write(f"{ts}\t{kind}\t{msg}\n")
    except OSError:
        pass


# --- XML builders (pure; unit-tested without hardware) ----------------------
def build_lock_set():
    return (
        "<lock>\n<op>set</op>\n<page_count>-1</page_count>\n"
        "<job_timeout>99</job_timeout>\n</lock>"
    )


def build_unlock(token):
    return f"<lock>\n<op>cancel</op>\n<job_token>{token}</job_token>\n</lock>"


def build_read(path, token=None, keep_awake=False):
    parts = [f"<read>\n<path>{path}</path>\n"]
    if token:
        parts.append(f"<job_token>{token}</job_token>\n")
    if not keep_awake:
        parts.append("<nokeepawake>1</nokeepawake>\n")
    parts.append("</read>\n")
    return "".join(parts)


def build_print_header(mode, datasize, cut):
    speed, lpi = MODES.get(mode, MODES[DEFAULT_MODE])
    return (
        "<print>\n"
        f"<mode>{mode}</mode>\n<speed>{speed}</speed>\n<lpi>{lpi}</lpi>\n"
        "<width>0</width>\n<height>0</height>\n"
        "<dataformat>jpeg</dataformat>\n<autofit>1</autofit>\n"
        f"<datasize>{datasize}</datasize>\n<cutmode>{cut}</cutmode>\n"
        "</print>"
    )


# --- socket I/O -------------------------------------------------------------
def _send(sock, text_or_bytes):
    data = text_or_bytes.encode() if isinstance(text_or_bytes, str) else text_or_bytes
    sock.sendall(data)


def _recv_some(sock, timeout):
    """One recv with its own timeout.

    Returns the bytes read, ``b''`` when the peer has closed the connection
    (EOF), or ``None`` on timeout (connection still open, just no data yet).
    Callers must distinguish these: treating EOF like a timeout busy-loops."""
    old = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        return sock.recv(4096)
    except socket.timeout:
        return None
    finally:
        sock.settimeout(old)


def _read_reply(sock, timeout, terminators=(b"</status>", b"</lock>", b"</config>")):
    """Accumulate bytes until a full XML reply (one of `terminators`) arrives.

    The printer frames every command reply in <status>/<lock>/<config>…; reading
    to the terminator avoids the partial-recv races that left jobs half-sent."""
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = _recv_some(sock, min(5.0, max(0.3, deadline - time.monotonic())))
        if chunk is None:        # timeout: still open, no data — keep waiting
            if buf:
                break
            continue
        if chunk == b"":         # EOF: peer closed — stop (don't spin on a dead socket)
            break
        buf += chunk
        if any(t in buf for t in terminators):
            break
    return buf


def _exchange(sock, msg, timeout=IO_TIMEOUT):
    """Send a message and read one complete reply."""
    _send(sock, XML_DECL + msg)
    return _read_reply(sock, timeout)


def _close(sock):
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    try:
        sock.settimeout(DRAIN_TIMEOUT)
        while sock.recv(4096):
            pass
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# --- public API -------------------------------------------------------------
def query(path="/status.xml", timeout=5.0, keep_awake=False):
    """Read an XML resource (e.g. /status.xml, /config.xml). One-shot connect."""
    host = resolve_host()
    sock = socket.create_connection((host, PORT), timeout=CONNECT_TIMEOUT)
    try:
        sock.settimeout(timeout)
        return _exchange(sock, build_read(path, keep_awake=keep_awake), timeout)
    finally:
        _close(sock)


def convert_to_jpeg(src, flip=None, margin_pct=None):
    """Render the source image to a print-ready JPEG; return its path.

    autofit=1 lets the printer scale to the tape, so we hand it a clean,
    correctly-oriented JPEG with a white safe-margin so edge content isn't
    clipped by the printable-area inset. Reuses ImageMagick."""
    src = pathlib.Path(src)
    flip = FLIP if flip is None else flip
    margin = MARGIN_PCT if margin_pct is None else margin_pct
    out = CACHE / "jpeg" / (src.stem + ".jpg")
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["magick", str(src)]
    if flip:
        cmd += ["-rotate", "180"]
    cmd += ["-background", "white", "-flatten"]
    if margin > 0:
        # Proportional white border on all sides so no content sits at the very
        # edge (the printable media is narrower than the tape).
        dims = subprocess.run(
            ["identify", "-format", "%w %h", f"{src}[0]"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        w, h = int(dims[0]), int(dims[1])
        bx, by = round(w * margin / 100), round(h * margin / 100)
        if bx or by:
            cmd += ["-bordercolor", "white", "-border", f"{bx}x{by}"]
    # Always emit a 3-channel sRGB JPEG so the printer sees a consistent format
    # regardless of whether the source PNG was grayscale or had alpha.
    cmd += ["-colorspace", "sRGB", "-type", "TrueColor", "-quality", "92", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def send(image, *, mode=DEFAULT_MODE, cut="full", timeout=IO_TIMEOUT, flip=None):
    """Print an image file natively, with auto-cut, releasing the lock always.

    Returns the printer's final response bytes. Raises PrinterError on failure."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; choose from {sorted(MODES)}")
    jpeg = convert_to_jpeg(image, flip=flip).read_bytes()

    name = pathlib.Path(image).name
    host = resolve_host()
    sock = socket.create_connection((host, PORT), timeout=CONNECT_TIMEOUT)
    sock.settimeout(timeout)
    token = None
    image_sent = False
    log_event("start", f"{name} mode={mode} cut={cut} bytes={len(jpeg)}")
    try:
        # 1. acquire the job lock — we MUST get a token so we can release it.
        resp = _exchange(sock, build_lock_set(), timeout)
        token = xml_field(resp, "job_token")
        if not token:
            raise PrinterError(f"could not acquire print lock: {resp[:200]!r}")

        # 2. send the print metadata block (ends in </print>) and read the reply.
        hdr_resp = _exchange(sock, build_print_header(mode, len(jpeg), cut), timeout=15.0)
        code = xml_field(hdr_resp, "code")
        comment = (xml_field(hdr_resp, "comment") or "").lower()
        ready = "ready" in comment or "receive" in comment
        # CRITICAL: never stream image bytes unless the printer asked for them.
        # Declaring a datasize then not delivering is what wedges the firmware.
        if not ready:
            if code not in (None, "0"):
                raise PrinterError(f"printer not ready (code {code}: {comment or hdr_resp[:160]!r})")
            # No explicit error but no "ready" either — give it one more read.
            more = _read_reply(sock, 8.0)
            if "ready" not in (xml_field(more, "comment") or "").lower():
                raise PrinterError(f"printer did not signal ready: {(hdr_resp + more)[:200]!r}")

        # 3. stream the full image (one write; all datasize bytes).
        _send(sock, jpeg)
        image_sent = True

        # 4. wait for completion.
        result = _read_reply(
            sock, timeout,
            terminators=(b"received", b"success", b"finished", b"</status>"),
        )
        log_event("done", name)
        if cut and cut != "none":
            log_event("cut", "auto-cut")
        return result
    except Exception as e:
        log_event("error", f"{name}: {e} (image_sent={image_sent})")
        raise
    finally:
        # 6. ALWAYS release the lock — this is what prevents stuck-BUSY.
        if token:
            try:
                _send(sock, XML_DECL + build_unlock(token))
                _recv_some(sock, 3.0)
            except OSError:
                pass
        _close(sock)


def reset():
    """Best-effort clear of a stuck job: grab the lock and immediately release.

    If the printer is wedged such that this can't help, the caller should
    power-cycle. Returns the status after the attempt."""
    host = resolve_host()
    sock = socket.create_connection((host, PORT), timeout=CONNECT_TIMEOUT)
    try:
        sock.settimeout(8.0)
        resp = _exchange(sock, build_lock_set(), 8.0)
        token = xml_field(resp, "job_token")
        if token:
            _exchange(sock, build_unlock(token), 8.0)
    finally:
        _close(sock)
    return query("/status.xml")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] not in ("status", "reset"):
        send(sys.argv[1])
    elif len(sys.argv) > 1 and sys.argv[1] == "reset":
        print(reset().decode("utf-8", "replace")[:500])
    else:
        print(query().decode("utf-8", "replace")[:500])
