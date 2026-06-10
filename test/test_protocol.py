#!/usr/bin/env python3
"""Hardware-free unit tests for the native VC-500W protocol builders.

Run: python3 test/test_protocol.py   (or pytest test/)
"""

import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))
import brother_print as bp  # noqa: E402


class FakeSocket:
    """Scripted socket: returns queued byte-chunks from recv(), records sends."""

    def __init__(self, script):
        self.sent = []
        self._script = list(script)
        self._timeout = None

    def settimeout(self, t):
        self._timeout = t

    def gettimeout(self):
        return self._timeout

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _n):
        if self._script:
            return self._script.pop(0)
        raise socket.timeout()

    def shutdown(self, _how):
        pass

    def close(self):
        pass

    # convenience: did we ever send the image bytes?
    def sent_blob(self):
        return b"".join(self.sent)


class _FakeJpeg:
    def __init__(self, data):
        self._data = data

    def read_bytes(self):
        return self._data


def _reply(op, code, comment, token=None):
    tok = f"<job_token>{token}</job_token>\n" if token else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<status>\n<op>{op}</op>\n'
        f"{tok}<code>{code}</code>\n<comment>{comment}</comment>\n</status>\n"
    ).encode()


class XmlFieldTests(unittest.TestCase):
    def test_extract_str_and_bytes(self):
        self.assertEqual(bp.xml_field("<a>hello</a>", "a"), "hello")
        self.assertEqual(bp.xml_field(b"<a>hi</a>", b"a"), "hi")

    def test_missing_returns_none(self):
        self.assertIsNone(bp.xml_field("<a>x</a>", "b"))

    def test_strips_whitespace_and_multiline(self):
        self.assertEqual(bp.xml_field("<a>\n  v  \n</a>", "a"), "v")

    def test_job_token(self):
        resp = b'<?xml version="1.0"?>\n<lock><job_token>ABC123</job_token></lock>'
        self.assertEqual(bp.xml_field(resp, "job_token"), "ABC123")


class BuilderTests(unittest.TestCase):
    def test_lock_set(self):
        s = bp.build_lock_set()
        self.assertIn("<op>set</op>", s)
        self.assertIn("<job_timeout>99</job_timeout>", s)

    def test_unlock_uses_token(self):
        s = bp.build_unlock("TOK")
        self.assertIn("<op>cancel</op>", s)
        self.assertIn("<job_token>TOK</job_token>", s)

    def test_read_default_has_nokeepawake(self):
        s = bp.build_read("/status.xml")
        self.assertIn("<path>/status.xml</path>", s)
        self.assertIn("<nokeepawake>1</nokeepawake>", s)

    def test_read_keepalive_omits_nokeepawake(self):
        s = bp.build_read("/status.xml", keep_awake=True)
        self.assertNotIn("nokeepawake", s)

    def test_read_with_token(self):
        s = bp.build_read("/status.xml", token="T")
        self.assertIn("<job_token>T</job_token>", s)

    def test_print_header_ends_with_cutmode_then_close(self):
        # This exact ordering matches the cups-proxy injection target.
        h = bp.build_print_header("vivid", 12345, "full")
        self.assertIn("<cutmode>full</cutmode>\n</print>", h)
        self.assertIn("<datasize>12345</datasize>", h)
        self.assertIn("<dataformat>jpeg</dataformat>", h)
        self.assertIn("<autofit>1</autofit>", h)

    def test_print_header_mode_table(self):
        for mode, (speed, lpi) in bp.MODES.items():
            h = bp.build_print_header(mode, 1, "full")
            self.assertIn(f"<mode>{mode}</mode>", h)
            self.assertIn(f"<speed>{speed}</speed>", h)
            self.assertIn(f"<lpi>{lpi}</lpi>", h)

    def test_unknown_mode_falls_back(self):
        # build_print_header tolerates an unknown mode (uses default speed/lpi)
        h = bp.build_print_header("nope", 1, "full")
        speed, lpi = bp.MODES[bp.DEFAULT_MODE]
        self.assertIn(f"<speed>{speed}</speed>", h)


class SendLogicTests(unittest.TestCase):
    """Verify the send() safety properties without hardware: image bytes are
    only transmitted after the printer answers the <print> header with code 0,
    and no <lock> is ever taken for a print job — locking without embedding the
    token in the header made the printer reject our own job as 'busy', and an
    orphaned lock is the classic wedge. (The cut is triggered by the socket
    close that follows; FakeSocket can't observe that, hardware verified it.)"""

    def _run_send(self, script, expect_error=False):
        fake = FakeSocket(script)
        result = None
        with mock.patch.object(bp.socket, "create_connection", return_value=fake), \
             mock.patch.object(bp, "resolve_host", return_value="1.2.3.4"), \
             mock.patch.object(bp, "convert_to_jpeg",
                               return_value=_FakeJpeg(b"JPEGDATA")), \
             mock.patch.object(bp, "log_event"):
            if expect_error:
                with self.assertRaises(bp.PrinterError):
                    bp.send("x.png", wait_idle=False)
            else:
                result = bp.send("x.png", wait_idle=False)
        return fake, result

    def test_busy_printer_sends_no_image(self):
        # Header rejected with code 2 (busy / locked by another client).
        fake, _ = self._run_send([_reply("print", 2, "Printer busy")],
                                 expect_error=True)
        # CRITICAL: no image bytes were ever sent — declaring a datasize and
        # not delivering it is what wedges the firmware.
        self.assertNotIn(b"JPEGDATA", fake.sent_blob())

    def test_no_media_sends_no_image(self):
        # Header rejected with code 3 (no media loaded).
        fake, _ = self._run_send([_reply("print", 3, "No media loaded")],
                                 expect_error=True)
        self.assertNotIn(b"JPEGDATA", fake.sent_blob())

    def test_silent_printer_sends_no_image(self):
        # No reply at all to the header (recv times out) — abort, don't stream.
        fake, _ = self._run_send([], expect_error=True)
        self.assertNotIn(b"JPEGDATA", fake.sent_blob())

    def test_happy_path_order_and_no_lock(self):
        script = [
            _reply("print", 0, "ready to receive"),
            b'<?xml version="1.0"?>\n<status>\n<code>0</code>\n'
            b"<comment>print data received</comment>\n</status>\n",
        ]
        fake, result = self._run_send(script)
        blob = fake.sent_blob()
        # Header first, image after; the data ack is returned to the caller.
        self.assertIn(b"<print>", fake.sent[0])
        self.assertIn(b"JPEGDATA", blob)
        self.assertIn(b"print data received", result)
        hdr_idx = next(i for i, s in enumerate(fake.sent) if b"<print>" in s)
        img_idx = next(i for i, s in enumerate(fake.sent) if b"JPEGDATA" in s)
        self.assertLess(hdr_idx, img_idx)
        # NO lock traffic — print jobs are lockless now.
        self.assertNotIn(b"<lock>", blob)
        self.assertNotIn(b"job_token", blob)

    def test_bare_code0_header_reply_accepted(self):
        # Firmware may answer the header with just <code>0</code>, no comment
        # (Sunburn capture); that must count as ready-to-receive.
        script = [
            b'<?xml version="1.0"?>\n<status>\n<code>0</code>\n</status>\n',
            b'<?xml version="1.0"?>\n<status>\n<code>0</code>\n</status>\n',
        ]
        fake, _ = self._run_send(script)
        self.assertIn(b"JPEGDATA", fake.sent_blob())


class ResolveHostTests(unittest.TestCase):
    """resolve_host walks avahi-resolve -> getaddrinfo -> static IP, caching the
    result briefly. This box has no nss-mdns, so the avahi step is load-bearing."""

    def setUp(self):
        bp._resolved["ip"] = None  # clear the module-level cache between cases
        bp._resolved["at"] = 0.0

    @staticmethod
    def _avahi(rc, out):
        return mock.Mock(returncode=rc, stdout=out)

    def test_avahi_success_wins(self):
        with mock.patch.object(bp.subprocess, "run",
                               return_value=self._avahi(0, "VC-500W3904.local\t192.168.8.42\n")):
            self.assertEqual(bp.resolve_host(), "192.168.8.42")

    def test_falls_back_to_getaddrinfo(self):
        with mock.patch.object(bp.subprocess, "run", return_value=self._avahi(1, "")), \
             mock.patch.object(bp.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("192.168.8.43", 9100))]):
            self.assertEqual(bp.resolve_host(), "192.168.8.43")

    def test_falls_back_to_static_ip_when_all_fail(self):
        with mock.patch.object(bp.subprocess, "run", side_effect=OSError), \
             mock.patch.object(bp.socket, "getaddrinfo", side_effect=OSError):
            self.assertEqual(bp.resolve_host(), bp.PRINTER_IP)

    def test_result_is_cached_within_ttl(self):
        with mock.patch.object(bp.subprocess, "run",
                               return_value=self._avahi(0, "x\t192.168.8.44\n")) as run:
            first = bp.resolve_host()
            second = bp.resolve_host()
        self.assertEqual(first, second)
        self.assertEqual(run.call_count, 1)  # second call served from cache, no re-resolve


class ReadReplyTests(unittest.TestCase):
    """_read_reply must break immediately on EOF (peer close) rather than
    busy-looping recv()->b'' until the full timeout, and must accumulate bytes
    across multiple recvs until a framing terminator arrives."""

    def test_breaks_on_eof_without_spinning(self):
        calls = []

        class EofSocket(FakeSocket):
            def recv(self, _n):
                calls.append(1)
                return b""  # peer closed: always EOF

        out = bp._read_reply(EofSocket([]), timeout=5.0)
        self.assertEqual(out, b"")
        # The bug was treating EOF like a timeout and looping; the fix breaks at
        # once, so recv is called exactly once (not hundreds of times for 5s).
        self.assertEqual(len(calls), 1)

    def test_accumulates_across_recvs_until_terminator(self):
        fake = FakeSocket([b"<status>\n<code>0</code>\n", b"<comment>ok</comment>\n</status>\n"])
        out = bp._read_reply(fake, timeout=5.0)
        self.assertIn(b"</status>", out)
        self.assertIn(b"<code>0</code>", out)


class ModeTableTests(unittest.TestCase):
    def test_known_modes(self):
        self.assertEqual(bp.MODES["vivid"], (0, 317))
        self.assertEqual(bp.MODES["color"], (1, 264))
        self.assertEqual(bp.MODES["bw"], (2, 400))

    def test_default_mode_valid(self):
        self.assertIn(bp.DEFAULT_MODE, bp.MODES)


_HAS_MAGICK = bool(shutil.which("magick") and shutil.which("identify"))


@unittest.skipUnless(_HAS_MAGICK, "ImageMagick (magick/identify) not installed")
class ConvertToJpegMarginTests(unittest.TestCase):
    """Regression coverage for the edge-clipping fix: convert_to_jpeg() must add a
    proportional white border so content isn't clipped by the printable-area inset
    (tape 1.022" wide, printable only 0.978"). Runs only where ImageMagick exists."""

    SIZE = 100  # source square side, in px

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="bp_margin_")
        self.src = pathlib.Path(self._tmp) / "swatch.png"
        # Solid red square — distinct stem so it won't collide in cache/jpeg.
        subprocess.run(
            ["magick", "-size", f"{self.SIZE}x{self.SIZE}", "xc:red", str(self.src)],
            check=True, capture_output=True,
        )
        self._outputs = []

    def tearDown(self):
        for p in self._outputs:
            try:
                pathlib.Path(p).unlink()
            except OSError:
                pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _dims(self, path):
        out = subprocess.run(
            ["identify", "-format", "%w %h", str(path)],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        return int(out[0]), int(out[1])

    def test_margin_adds_proportional_border(self):
        out = bp.convert_to_jpeg(self.src, flip=False, margin_pct=4)
        self._outputs.append(out)
        w, h = self._dims(out)
        # 4% of 100 = 4px per side → +8px total each axis.
        self.assertEqual((w, h), (self.SIZE + 8, self.SIZE + 8))

    def test_zero_margin_leaves_size_unchanged(self):
        out = bp.convert_to_jpeg(self.src, flip=False, margin_pct=0)
        self._outputs.append(out)
        self.assertEqual(self._dims(out), (self.SIZE, self.SIZE))

    def test_output_is_three_channel_jpeg(self):
        out = bp.convert_to_jpeg(self.src, flip=False, margin_pct=4)
        self._outputs.append(out)
        self.assertEqual(pathlib.Path(out).suffix, ".jpg")
        fmt = subprocess.run(
            ["identify", "-format", "%m %[channels]", str(out)],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertIn("JPEG", fmt)
        # sRGB TrueColor → 3 channels (no alpha), regardless of source.
        self.assertIn("srgb", fmt.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
