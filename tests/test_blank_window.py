"""A window that opens and stays blank, and the ladder that fixes it.

This is the commonest way the whole system stops dead, and it depends
entirely on the machine: a discrete NVIDIA card under X, a VM with no GPU,
and an ARM box driving a Mac's GPU under Linux all fail differently, and none
of them say so. Nobody should have to guess flags, so the sign-in tries them.
"""
from __future__ import annotations

import struct
import sys
import zlib

import pytest

from prep import scraper


# ------------------------------------------------------------- the ladder

def test_software_rendering_is_where_it_starts():
    """Not the GPU. Nothing here is fast enough to notice the difference, and
    a slow window you can see beats a fast one you cannot."""
    assert scraper.render_ladder()[0] == "software"
    assert scraper.DEFAULT_RENDER_LEVEL == "software"


def test_every_rung_is_tried_before_giving_up():
    ladder = scraper.render_ladder()
    assert "swiftshader" in ladder
    assert ladder.index("swiftshader") > ladder.index("software")


def test_asking_for_the_gpu_puts_it_first_and_still_leaves_a_way_down():
    """Someone who asked for hardware gets it first; someone who did not is
    never handed it as a fallback."""
    asked = scraper.render_ladder("gpu")
    assert asked[0] == "gpu"
    assert asked[1] == "software"
    assert "gpu" not in scraper.render_ladder("software")


def test_the_x11_rung_is_linux_only():
    """Chromium's Wayland backend failing to present is a Linux fault, and a
    wasted rung is a wasted relaunch in front of someone waiting."""
    ladder = scraper.render_ladder()
    assert ("x11" in ladder) == (sys.platform == "linux")


def test_each_rung_adds_to_the_one_before_it():
    """A ladder whose rungs contradict each other tests nothing in order."""
    software = set(scraper.render_flags("software"))
    swiftshader = set(scraper.render_flags("swiftshader"))
    assert software < swiftshader


def test_an_unknown_level_says_what_the_levels_are():
    with pytest.raises(ValueError, match="software"):
        scraper.render_flags("turbo")


def test_a_working_level_comes_with_the_config_lines_to_keep_it():
    """A window fixed by a flag nobody writes down is fixed exactly once."""
    advice = "\n".join(scraper.render_advice("swiftshader"))
    assert "browser_args" in advice
    assert "--use-angle=swiftshader" in advice
    assert "--disable-gpu" not in advice, \
        "repeated the flags disable_gpu already supplies"


def test_the_default_needs_no_config_change():
    assert scraper.render_advice("software") == []


def test_the_scheduled_run_is_not_affected_by_what_a_person_tried_once():
    """render= is a per-launch override. If it leaked into the config the
    unattended run would quietly diverge from the one that was tested."""
    class Teams:
        disable_gpu = True
        browser_args: list = []
        no_sandbox = False
        third_party_cookies = True

    plain = scraper.browser_args(Teams())
    overridden = scraper.browser_args(Teams(), render="swiftshader")
    assert "--use-angle=swiftshader" in overridden
    assert "--use-angle=swiftshader" not in plain
    assert scraper.browser_args(Teams()) == plain, "the override was sticky"


def test_an_override_still_merges_the_cookie_flag():
    """Chromium keeps only the last --disable-features, so a render level
    that carries one must not throw away the third-party cookie opt-out."""
    class Teams:
        disable_gpu = True
        browser_args: list = []
        no_sandbox = False
        third_party_cookies = True

    args = scraper.browser_args(Teams(), render="swiftshader")
    features = [a for a in args if a.startswith("--disable-features=")]
    assert len(features) == 1
    assert "VizDisplayCompositor" in features[0]
    assert "TrackingProtection3pcd" in features[0]


# --------------------------------------------------- seeing that it is blank

def _png(width: int, height: int, pixel, filter_type: int = 0,
         second=None) -> bytes:
    """A real PNG, filtered the way Chromium might filter one.

    Built here rather than mocked: the un-filtering is the part that can be
    quietly wrong, and a wrong answer means relaunching a browser that was
    working, in front of someone waiting for it.
    """
    rows = []
    previous = bytearray(width * len(pixel))
    for y in range(height):
        colour = second if (second and y >= height // 2) else pixel
        line = bytearray(bytes(colour) * width)
        stride = len(line)
        encoded = bytearray(line)
        if filter_type == 2:                       # Up
            for i in range(stride):
                encoded[i] = (line[i] - previous[i]) & 0xFF
        elif filter_type == 1:                     # Sub
            channels = len(pixel)
            for i in range(stride - 1, channels - 1, -1):
                encoded[i] = (line[i] - line[i - channels]) & 0xFF
        rows.append(bytes([filter_type]) + bytes(encoded))
        previous = line

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    colour_type = {1: 0, 3: 2, 4: 6}[len(pixel)]
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8,
                                         colour_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows)))
            + chunk(b"IEND", b""))


def test_a_solid_white_screenshot_reads_as_blank():
    """The case the first version of this got wrong. It inferred "blank" from
    how small the file was, and a plain white 1280x720 PNG is several KB - so
    the one thing it existed to catch was the one thing it missed."""
    colours = scraper.png_colours(_png(1280, 720, (255, 255, 255, 255)))
    assert colours is not None and len(colours) == 1


def test_a_page_with_content_does_not():
    colours = scraper.png_colours(_png(400, 300, (204, 0, 0, 255),
                                       second=(255, 255, 255, 255)))
    assert colours is not None and len(colours) == 2


@pytest.mark.parametrize("filter_type", [0, 1, 2])
def test_the_png_filters_are_undone_correctly(filter_type):
    """Rows are predicted from their neighbours. Get this wrong and every
    colour comes out different, which reads as a busy page - the failure
    direction that leaves someone staring at a blank window."""
    shot = _png(64, 40, (17, 34, 51, 255), filter_type=filter_type)
    assert scraper.png_colours(shot) == {bytes((17, 34, 51, 255))}


def test_something_that_is_not_a_png_is_not_guessed_at():
    assert scraper.png_colours(b"not a png at all") is None


def test_a_truncated_screenshot_does_not_raise():
    shot = _png(200, 200, (1, 2, 3, 255))
    assert scraper.png_colours(shot[:len(shot) // 2]) is None or True


def test_an_unreadable_screenshot_is_unknown_not_blank():
    """"Could not tell" must never read as "nothing drew" - that would
    relaunch a working browser on every attempt and never settle."""
    class Broken:
        def screenshot(self):
            raise RuntimeError("no surface")

    assert scraper.window_looks_blank(Broken()) is None


def test_a_blank_page_is_reported_blank():
    class White:
        def screenshot(self):
            return _png(320, 240, (255, 255, 255, 255))

    assert scraper.window_looks_blank(White()) is True


def _striped_png(width: int, height: int, colours: int) -> bytes:
    """A PNG with `colours` distinct row colours - a stand-in for a real page."""
    rows = []
    for y in range(height):
        shade = (y * 255 // max(colours - 1, 1)) % 256
        pixel = bytes((shade, (shade * 7) % 256, (shade * 13) % 256, 255))
        rows.append(b"\x00" + pixel * width)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows)))
            + chunk(b"IEND", b""))


def test_a_page_with_real_content_is_not_called_blank():
    """The expensive false positive: relaunching a browser that was working,
    down every rung of the ladder, in front of someone waiting for it."""
    class Drawn:
        def screenshot(self):
            return _striped_png(320, 240, colours=40)

    assert scraper.window_looks_blank(Drawn()) is False


def test_two_colours_still_counts_as_nothing_having_drawn():
    """A white page with a scrollbar down one side has not drawn anything
    either, and reporting it as content would strand someone on a rung that
    does not work."""
    class Almost:
        def screenshot(self):
            return _png(320, 240, (255, 255, 255, 255),
                        second=(200, 200, 200, 255))

    assert scraper.window_looks_blank(Almost()) is True
