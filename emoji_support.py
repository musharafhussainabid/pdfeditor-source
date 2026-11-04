"""Emoji in edited text: found whole (skin tones, families, flags, keycaps), drawn as small colour pictures on the
text line, measured as one wide character, and read back from a PDF the editor saved.

PDF text can't carry colour emoji, so each emoji is a picture (Twemoji, see tools/get_emoji.py), marked with its
code sequence so the editor knows it again when the PDF is opened for more changes.
"""
import functools
import os
import struct
import zlib

import pymupdf as fitz

ADVANCE = 1.2      # how far an emoji moves the pen, in em (phones and browsers: about 1.2)
BOX = 1.0          # the picture's size, in em
DROP = 0.13        # how far it reaches below the baseline, in em
MARK = "PdfEditEmoji"

EMOJI_DIR = os.environ.get("EMOJI_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "emoji")

VS16, ZWJ, KEYCAP = 0xFE0F, 0x200D, 0x20E3
SKIN = range(0x1F3FB, 0x1F400)
TAGS = range(0xE0020, 0xE0080)
FLAGS = range(0x1F1E6, 0x1F200)

# Symbols below U+1F000 that can be emoji (Extended_Pictographic). They are ordinary text unless the
# emoji selector (U+FE0F) follows, except the ones that are emoji by default (EMOJI_DEFAULT).
_BMP_PICT = [(0xA9, 0xA9), (0xAE, 0xAE), (0x203C, 0x203C), (0x2049, 0x2049), (0x2122, 0x2122), (0x2139, 0x2139),
             (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B), (0x2328, 0x2328), (0x23CF, 0x23CF),
             (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB), (0x25B6, 0x25B6),
             (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x27BF), (0x2934, 0x2935), (0x2B05, 0x2B07),
             (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D),
             (0x3297, 0x3297), (0x3299, 0x3299)]
EMOJI_DEFAULT = {0x231A, 0x231B, 0x23E9, 0x23EA, 0x23EB, 0x23EC, 0x23F0, 0x23F3, 0x25FD, 0x25FE, 0x2614, 0x2615,
                 *range(0x2648, 0x2654), 0x267F, 0x2693, 0x26A1, 0x26AA, 0x26AB, 0x26BD, 0x26BE, 0x26C4, 0x26C5,
                 0x26CE, 0x26D4, 0x26EA, 0x26F2, 0x26F3, 0x26F5, 0x26FA, 0x26FD, 0x2705, 0x270A, 0x270B, 0x2728,
                 0x274C, 0x274E, 0x2753, 0x2754, 0x2755, 0x2757, 0x2795, 0x2796, 0x2797, 0x27B0, 0x27BF,
                 0x2B1B, 0x2B1C, 0x2B50, 0x2B55}


def _bmp_pict(cp):
    return any(a <= cp <= b for a, b in _BMP_PICT)


def _pict(cp):
    """Can this code point be (part of) an emoji?"""
    return 0x1F000 <= cp <= 0x1FAFF or _bmp_pict(cp)


def _cluster_at(text, i):
    """Length of the emoji that starts at text[i], or 0 if none does."""
    n, cp = len(text), ord(text[i])
    nxt = ord(text[i + 1]) if i + 1 < n else 0
    if cp in FLAGS:
        return 2 if nxt in FLAGS else 1
    if text[i] in "0123456789#*":                    # keycaps: 1️⃣ #️⃣
        if nxt == KEYCAP:
            return 2
        if nxt == VS16 and i + 2 < n and ord(text[i + 2]) == KEYCAP:
            return 3
        return 0
    if 0x1F000 <= cp <= 0x1FAFF:
        pass
    elif _bmp_pict(cp) and (nxt == VS16 or cp in EMOJI_DEFAULT):
        pass
    else:
        return 0
    j = i + 1
    while j < n:
        c = ord(text[j])
        if c == VS16 or c in SKIN or c in TAGS:
            j += 1
        elif c == ZWJ and j + 1 < n and _pict(ord(text[j + 1])):
            j += 2
        else:
            break
    return j - i


def has_emoji(text):
    if not any(ord(c) >= 0x203C or c in "0123456789#*©®" for c in text):
        return False
    return any(e for _, e in split(text))


@functools.lru_cache(maxsize=4096)
def _split(text):
    out, buf, i = [], [], 0
    while i < len(text):
        k = _cluster_at(text, i)
        if k and svg_for(text[i:i + k]):
            if buf:
                out.append(("".join(buf), False))
                buf = []
            out.append((text[i:i + k], True))
            i += k
        else:
            buf.append(text[i])
            i += 1
    if buf:
        out.append(("".join(buf), False))
    return tuple(out)


def split(text):
    """[(piece, is_emoji)]: ordinary text and whole emoji, in order. Joined back, they give `text`."""
    if not any(ord(c) >= 0xA9 for c in text):
        return [(text, False)] if text else []
    return list(_split(text))


def strip(text):
    """The text without its emoji (for choosing a font that has every letter)."""
    return "".join(t for t, e in split(text) if not e)


# ---------- pictures -------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def _files():
    try:
        return frozenset(os.listdir(EMOJI_DIR))
    except OSError:
        return frozenset()


def _name(cps):
    return "-".join(format(c, "x") for c in cps) + ".svg"


@functools.lru_cache(maxsize=4096)
def svg_for(cluster):
    """The SVG file for an emoji (or the closest one: without selectors, or its first emoji), or None."""
    files, cps = _files(), [ord(c) for c in cluster]
    tries = [cps, [c for c in cps if c != VS16]]
    if ZWJ in cps:
        tries.append(cps[:cps.index(ZWJ)])
    tries += [[c for c in tries[-1] if c != VS16], cps[:2] if len(cps) > 1 and cps[1] in SKIN else [], cps[:1]]
    for t in tries:
        if t and _name(t) in files:
            return os.path.join(EMOJI_DIR, _name(t))
    return None


@functools.lru_cache(maxsize=1024)
def png_for(cluster, px=96):
    path = svg_for(cluster)
    if not path:
        return None
    with fitz.open(path) as d:            # MuPDF reads the SVG: vector art drawn sharp at this size
        page = d[0]
        z = px / max(page.rect.width, page.rect.height)
        return page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=True).tobytes("png")


def _unique(png, n):
    """The same PNG with a counter in a text chunk: every emoji gets its own picture in the PDF, so one can be
    removed later without touching any other."""
    data = b"pdfedit\x00" + str(n).encode()
    chunk = struct.pack(">I", len(data)) + b"tEXt" + data + struct.pack(">I", zlib.crc32(b"tEXt" + data) & 0xFFFFFFFF)
    return png[:-12] + chunk + png[-12:]


def draw(page, x, baseline, cluster, size, fc):
    """Draw one emoji with its left edge at x; returns how far the pen moves."""
    png = png_for(cluster)
    if png:
        fc["emoji_n"] = fc.get("emoji_n", 0) + 1
        y1 = baseline + DROP * size
        pad = (ADVANCE - BOX) / 2 * size
        xref = page.insert_image(fitz.Rect(x + pad, y1 - BOX * size, x + pad + BOX * size, y1),
                                 stream=_unique(png, fc["emoji_n"]))
        page.parent.xref_set_key(xref, MARK, "(" + "-".join(format(ord(c), "x") for c in cluster) + ")")
    return ADVANCE * size


def cluster_of(doc, xref):
    """The emoji an image drawn by the editor stands for, or None for any other picture."""
    if not xref:
        return None
    try:
        kind, val = doc.xref_get_key(xref, MARK)
    except Exception:
        return None
    if kind != "string" or not val:
        return None
    try:
        return "".join(chr(int(h, 16)) for h in val.split("-"))
    except ValueError:
        return None


def page_emoji(page):
    """The editor's emoji on a page: [(cluster, Rect, xref)]."""
    doc, out = page.parent, []
    for im in page.get_image_info(xrefs=True):
        c = cluster_of(doc, im.get("xref") or 0)
        if c:
            out.append((c, fitz.Rect(im["bbox"]), im["xref"]))
    return out


def delete_in(page, rects):
    """Remove the editor's emoji whose centre is inside any of rects (the line is being rewritten)."""
    for _c, r, xref in page_emoji(page):
        centre = fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
        if any(centre in fitz.Rect(q) for q in rects):
            page.delete_image(xref)


# ---------- measuring ------------------------------------------------------------------------
class EmojiFont:
    """A font that measures emoji as one ADVANCE-wide character; everything else is the font itself."""

    def __init__(self, font):
        self._font = font

    def __getattr__(self, name):
        return getattr(self._font, name)

    def text_length(self, text, fontsize=11, **kw):
        if not has_emoji(text):
            return self._font.text_length(text, fontsize=fontsize, **kw)
        return sum(ADVANCE * fontsize if e else self._font.text_length(t, fontsize=fontsize, **kw)
                   for t, e in split(text))
