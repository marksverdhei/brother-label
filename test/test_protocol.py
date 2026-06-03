#!/usr/bin/env python3
"""Hardware-free unit tests for the native VC-500W protocol builders.

Run: python3 test/test_protocol.py   (or pytest test/)
"""

import pathlib
import sys
import unittest

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))
import brother_print as bp  # noqa: E402


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


class ModeTableTests(unittest.TestCase):
    def test_known_modes(self):
        self.assertEqual(bp.MODES["vivid"], (0, 317))
        self.assertEqual(bp.MODES["color"], (1, 264))
        self.assertEqual(bp.MODES["bw"], (2, 400))

    def test_default_mode_valid(self):
        self.assertIn(bp.DEFAULT_MODE, bp.MODES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
