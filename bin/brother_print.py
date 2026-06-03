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
m7i.org. The exact print-header field set is confirmed against a live capture of
the working zsocket traffic via the cups-proxy journal (see docs/protocol.md).
"""

import pathlib
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

# Set True if prints come out upside-down/mirrored vs. the old pipeline (which
# ran the image through pnmflip). Confirmed on first native print.
FLIP = False

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


def resolve_host():
    """Prefer the mDNS hostname; fall back to the last-known IP."""
    try:
        socket.getaddrinfo(PRINTER_HOST, PORT, proto=socket.IPPROTO_TCP)
        return PRINTER_HOST
    except OSError:
        return PRINTER_IP


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
    """One recv with its own timeout; returns b'' on timeout/close."""
    old = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        return sock.recv(4096)
    except socket.timeout:
        return b""
    finally:
        sock.settimeout(old)


def _exchange(sock, msg, timeout=IO_TIMEOUT):
    """Send a message and read one response."""
    _send(sock, XML_DECL + msg)
    return _recv_some(sock, timeout)


def _wait_for(sock, needles, timeout, required=True):
    """Accumulate responses until one of `needles` (or 'error') appears.

    Returns the accumulated bytes. Raises PrinterError on a reported error, or
    on timeout when `required` is True. When `required` is False, a timeout just
    returns what we have (some firmwares stream without an explicit ack)."""
    if isinstance(needles, str):
        needles = [needles]
    needles = [n.lower().encode() for n in needles]
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = _recv_some(sock, min(5.0, max(0.5, deadline - time.monotonic())))
        if chunk:
            buf += chunk
            low = buf.lower()
            err = xml_field(buf, "print_job_error")
            if (b"error" in low and b"none" not in low) or (err and err not in ("NONE", "0")):
                raise PrinterError(f"printer reported error: {buf[:300]!r}")
            if any(n in low for n in needles):
                return buf
    if required:
        raise PrinterError(f"timed out waiting for {needles}; got {buf[:300]!r}")
    return buf


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


def convert_to_jpeg(src, flip=None):
    """Render the source image to a print-ready JPEG; return its path.

    autofit=1 lets the printer scale to the tape, so we just hand it a clean,
    correctly-oriented JPEG. Reuses ImageMagick (already a project dependency)."""
    src = pathlib.Path(src)
    flip = FLIP if flip is None else flip
    out = CACHE / "jpeg" / (src.stem + ".jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["magick", str(src)]
    if flip:
        cmd += ["-rotate", "180"]
    # Always emit a 3-channel sRGB JPEG so the printer sees a consistent format
    # regardless of whether the source PNG was grayscale or had alpha.
    cmd += ["-background", "white", "-flatten",
            "-colorspace", "sRGB", "-type", "TrueColor",
            "-quality", "92", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def send(image, *, mode=DEFAULT_MODE, cut="full", timeout=IO_TIMEOUT, flip=None):
    """Print an image file natively, with auto-cut, releasing the lock always.

    Returns the printer's final response bytes. Raises PrinterError on failure."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; choose from {sorted(MODES)}")
    jpeg = convert_to_jpeg(image, flip=flip).read_bytes()

    host = resolve_host()
    sock = socket.create_connection((host, PORT), timeout=CONNECT_TIMEOUT)
    sock.settimeout(timeout)
    token = None
    log_event("start", f"{pathlib.Path(image).name} mode={mode} cut={cut} bytes={len(jpeg)}")
    try:
        # 1. acquire the job lock
        resp = _exchange(sock, build_lock_set(), timeout)
        token = xml_field(resp, "job_token")

        # 2. print metadata block (its own write; ends in </print>)
        _send(sock, XML_DECL + build_print_header(mode, len(jpeg), cut))

        # 3. wait for the printer to say it's ready (best-effort)
        _wait_for(sock, ["ready to receive", "ready"], timeout=15.0, required=False)

        # 4. stream the image bytes
        _send(sock, jpeg)

        # 5. wait for completion
        result = _wait_for(
            sock,
            ["print data received", "picture received", "print finished",
             "<print_job_stage>success", "<code>0"],
            timeout=timeout,
            required=True,
        )
        log_event("done", pathlib.Path(image).name)
        if cut and cut != "none":
            log_event("cut", "auto-cut")
        return result
    except Exception as e:
        log_event("error", f"{pathlib.Path(image).name}: {e}")
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
