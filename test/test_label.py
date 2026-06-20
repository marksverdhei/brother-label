#!/usr/bin/env python3
"""Hardware-free unit tests for bin/label's icon-gen fallback + image validation.

Covers the robustness logic added when the OpenRouter key died and the
image-search fallback kept grabbing junk (captcha / AARCH64 logos):
  - gen_image() falls back to SearXNG on auth/credit failures (401/402/403)
    rather than crashing, and re-raises other HTTP errors.
  - _valid_raster() rejects the obvious junk a fallback can download verbatim
    (HTML error pages, empty bodies, sub-48px pixels).

Run: python3 test/test_label.py   (or pytest test/)
"""

import pathlib
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from importlib.machinery import SourceFileLoader
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
# bin/label has no .py extension; main() is guarded, so loading it is side-effect free.
label = SourceFileLoader("label", str(ROOT / "bin" / "label")).load_module()

_HAS_MAGICK = bool(shutil.which("magick") and shutil.which("identify"))


def _http_error(code):
    return urllib.error.HTTPError("http://openrouter", code, "err", {}, None)


def _png(size):
    return subprocess.run(
        ["magick", "-size", size, "xc:blue", "png:-"], capture_output=True
    ).stdout


@unittest.skipUnless(_HAS_MAGICK, "ImageMagick (magick/identify) not installed")
class ValidRasterTests(unittest.TestCase):
    def test_rejects_empty(self):
        self.assertFalse(label._valid_raster(b""))

    def test_rejects_html_error_page(self):
        self.assertFalse(label._valid_raster(b"<html><body>404 Not Found</body></html>" * 20))

    def test_rejects_truncated_bytes(self):
        self.assertFalse(label._valid_raster(b"\x89PNG\r\n not a real image"))

    def test_rejects_one_by_one_pixel(self):
        self.assertFalse(label._valid_raster(_png("1x1")))

    def test_accepts_real_image(self):
        self.assertTrue(label._valid_raster(_png("100x100")))

    def test_min_side_boundary(self):
        # 48px is the floor; 47 should fail, 48 should pass.
        self.assertFalse(label._valid_raster(_png("47x47")))
        self.assertTrue(label._valid_raster(_png("48x48")))


class GenImageFallbackTests(unittest.TestCase):
    """gen_image must survive a dead/credit-less OpenRouter key by falling back,
    but must NOT swallow unexpected server errors."""

    def _gen(self):
        return label.gen_image("a cable icon", pathlib.Path(tempfile.mkdtemp()) / "o.png")

    def test_auth_credit_errors_fall_back_to_comfy(self):
        for code in (401, 402, 403):
            with mock.patch.object(label, "OPENROUTER_KEY", "k"), \
                 mock.patch.object(label, "_gen_image_openrouter",
                                   side_effect=_http_error(code)), \
                 mock.patch.object(label, "_gen_image_comfy") as gc:
                self._gen()
                self.assertTrue(gc.called, f"HTTP {code} should fall back to comfy")

    def test_other_http_error_reraises(self):
        with mock.patch.object(label, "OPENROUTER_KEY", "k"), \
             mock.patch.object(label, "_gen_image_openrouter",
                               side_effect=_http_error(500)), \
             mock.patch.object(label, "_gen_image_comfy") as gc:
            with self.assertRaises(urllib.error.HTTPError):
                self._gen()
            self.assertFalse(gc.called, "a 500 must NOT silently fall back")

    def test_success_does_not_fall_back(self):
        with mock.patch.object(label, "OPENROUTER_KEY", "k"), \
             mock.patch.object(label, "_gen_image_openrouter") as gen_or, \
             mock.patch.object(label, "_gen_image_comfy") as gc:
            self._gen()
            self.assertTrue(gen_or.called)
            self.assertFalse(gc.called, "no fallback when OpenRouter succeeds")

    def test_no_key_goes_straight_to_comfy(self):
        with mock.patch.object(label, "OPENROUTER_KEY", ""), \
             mock.patch.object(label, "_gen_image_openrouter") as gen_or, \
             mock.patch.object(label, "_gen_image_comfy") as gc:
            self._gen()
            self.assertFalse(gen_or.called, "no key → don't even try OpenRouter")
            self.assertTrue(gc.called)

    def test_comfy_error_falls_back_to_searxng(self):
        with mock.patch.object(label, "OPENROUTER_KEY", ""), \
             mock.patch.object(label, "_gen_image_comfy", side_effect=Exception("boom")), \
             mock.patch.object(label, "_gen_image_searxng") as sx:
            self._gen()
            self.assertTrue(sx.called, "comfy error should fall back to SearXNG")


if __name__ == "__main__":
    unittest.main(verbosity=2)
