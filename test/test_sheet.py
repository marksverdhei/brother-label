#!/usr/bin/env python3
"""Hardware-free unit tests for bin/label-sheet's pure composition geometry.

label-sheet packs N square pictogram tiles onto the 1" (320 px @ 320 DPI) tape
and reports tape-saving stats. None of that needs a printer, so the packing
math, the ruler-reserves-width behaviour, the never-zero-columns guard, and the
tile/guide helpers are all unit-testable. Keeps the real-size math honest if the
constants or shelf-packing ever change.

Run: python3 test/test_sheet.py   (or pytest test/)
"""

import math
import pathlib
import unittest
from importlib.machinery import SourceFileLoader

from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parent.parent
# bin/label-sheet has no .py extension; main() is __main__-guarded, so loading
# it just defines the module (no argparse, no printing).
ls = SourceFileLoader("label_sheet", str(ROOT / "bin" / "label-sheet")).load_module()


def squares(n, px=64, color="red"):
    return [Image.new("RGB", (px, px), color) for _ in range(n)]


class ComposeGeometryTests(unittest.TestCase):
    def test_strip_width_is_always_tape_width(self):
        for n, tile in [(1, 0.45), (8, 0.45), (8, 0.3), (5, 0.6)]:
            strip, _ = ls.compose(squares(n), tile)
            self.assertEqual(strip.width, ls.TAPE_W_PX,
                             f"n={n} tile={tile}: width must equal tape width")

    def test_default_tile_layout(self):
        # 8 tiles @ 0.45" with the ruler — the live status-sticker strip shape.
        strip, stats = ls.compose(squares(8), 0.45, ruler=True)
        self.assertEqual((stats["cols"], stats["rows"]), (2, 4))
        self.assertEqual(stats["tiles"], 8)
        tile_px = round(0.45 * ls.PRINT_DPI)            # 144
        self.assertEqual(strip.height, 4 * tile_px + 2 * ls.EDGE_PX)  # 592
        self.assertAlmostEqual(stats["strip_in"], strip.height / ls.PRINT_DPI)

    def test_strip_height_matches_grid(self):
        for n, tile in [(1, 0.45), (3, 0.5), (8, 0.3), (10, 0.4)]:
            strip, stats = ls.compose(squares(n), tile)
            tile_px = round(tile * ls.PRINT_DPI)
            expected = stats["rows"] * tile_px + 2 * ls.EDGE_PX
            self.assertEqual(strip.height, expected, f"n={n} tile={tile}")

    def test_rows_cover_all_tiles(self):
        for n, tile in [(1, 0.45), (7, 0.45), (8, 0.45), (9, 0.45), (13, 0.3)]:
            _, stats = ls.compose(squares(n), tile)
            self.assertGreaterEqual(stats["cols"] * stats["rows"], n)
            self.assertEqual(stats["rows"], math.ceil(n / stats["cols"]))

    def test_ruler_reserves_width(self):
        # tile_px=151 straddles the ruler reservation: 290//151==1 but 304//151==2,
        # so the ruler must cost exactly one column here.
        tile_in = 151 / ls.PRINT_DPI
        _, with_ruler = ls.compose(squares(4), tile_in, ruler=True)
        _, no_ruler = ls.compose(squares(4), tile_in, ruler=False)
        self.assertEqual(with_ruler["cols"], 1)
        self.assertEqual(no_ruler["cols"], 2)

    def test_columns_never_zero(self):
        # A tile wider than the tape must still yield one (clipped) column,
        # never a divide-into-zero grid.
        _, stats = ls.compose(squares(3), 2.0)
        self.assertEqual(stats["cols"], 1)
        self.assertEqual(stats["rows"], 3)

    def test_singly_in_uses_measured_overhead(self):
        n, tile = 6, 0.45
        _, stats = ls.compose(squares(n), tile)
        self.assertAlmostEqual(stats["singly_in"], n * (tile + 0.55))

    def test_saving_is_positive_for_packed_strip(self):
        # The whole point: a packed strip costs less tape than N singles.
        _, stats = ls.compose(squares(8), 0.45)
        packed = stats["strip_in"] + 0.55
        self.assertLess(packed, stats["singly_in"])


class FitTileTests(unittest.TestCase):
    def test_returns_square_rgb_box(self):
        tile = ls.fit_tile(Image.new("RGB", (50, 50), "red"), 100)
        self.assertEqual(tile.size, (100, 100))
        self.assertEqual(tile.mode, "RGB")

    def test_smaller_image_is_centered_on_white(self):
        # A 50px image in a 100px box leaves a white margin → corner is white.
        tile = ls.fit_tile(Image.new("RGB", (50, 50), "red"), 100)
        self.assertEqual(tile.getpixel((0, 0)), (255, 255, 255))

    def test_oversized_image_is_contained(self):
        # Contain-fit must shrink a large image to within the box (minus pad).
        tile = ls.fit_tile(Image.new("RGB", (1024, 1024), "blue"), 120)
        self.assertEqual(tile.size, (120, 120))


class DashedLineTests(unittest.TestCase):
    def test_zero_length_is_noop(self):
        img = Image.new("RGB", (20, 20), "white")
        d = ImageDraw.Draw(img)
        ls.dashed_line(d, (5, 5), (5, 5))  # must not raise / divide by zero
        self.assertEqual(img.getpixel((5, 5)), (255, 255, 255))

    def test_draws_some_ink(self):
        img = Image.new("RGB", (40, 4), "white")
        d = ImageDraw.Draw(img)
        ls.dashed_line(d, (0, 2), (39, 2), color=(0, 0, 0), dash=4, gap=4)
        px = [img.getpixel((x, 2)) for x in range(40)]
        self.assertIn((0, 0, 0), px, "dashed line should lay down some segments")
        self.assertIn((255, 255, 255), px, "and leave gaps between them")


if __name__ == "__main__":
    unittest.main(verbosity=2)
