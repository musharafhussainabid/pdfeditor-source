"""Text in the world's other scripts: right-to-left (Urdu, Arabic, Persian, Pashto, Hebrew), scripts whose
letters join or change order (Hindi and the other Indic scripts, Thai, Khmer...), and characters the chosen
font doesn't have (Chinese, Japanese, Korean...).

Writing: such text can't be written letter by letter. MuPDF's own text layout draws it: it joins and orders
the letters (HarfBuzz and the Unicode bidi rules) and has fonts for these scripts built in (Noto, Droid Sans
Fallback). English letters, digits and punctuation in the same line keep the document's font. Each drawn
piece carries its real text (ActualText) for copy and search in other PDF readers, and the editor marks the
whole line with its text and style so the line reads back exactly when the PDF is opened again.

Reading: right-to-left lines in other PDFs come out of MuPDF jumbled (English words and numbers in them
reversed or split off). Their characters still have their true positions, so the reading order is rebuilt
from those positions.
"""
import functools
import html
import json
import os
import re
import threading
import unicodedata

import pymupdf as fitz

import emoji_support as em

# Scripts that need shaping or reordering: Hebrew, Arabic, Syriac, Thaana, N'Ko and Arabic extensions; the
# Indic scripts; Thai, Lao, Tibetan; Myanmar; Khmer; Mongolian; Arabic and Hebrew presentation forms.
SHAPED_RANGES = ((0x0590, 0x08FF), (0x0900, 0x0DFF), (0x0E00, 0x0FFF), (0x1000, 0x109F), (0x1780, 0x17FF),
                 (0x1800, 0x18AF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF))
PRESENTATION = ((0xFB50, 0xFDFF), (0xFE70, 0xFEFF))          # Arabic letters stored in their joined shapes
MARK = b"PdfEditLine"                                       # the marker of a line the editor wrote this way
BOX = b"PdfEditBox"                                         # ... and of each piece drawn for it


def _in(ranges, o):
    return any(a <= o <= b for a, b in ranges)


_SHAPED_RE = re.compile("[" + "".join(re.escape(chr(a)) + "-" + re.escape(chr(b)) for a, b in SHAPED_RANGES) + "]")


def is_shaped_char(c):
    return _SHAPED_RE.match(c) is not None


def has_shaped(text):
    """Does the text have any letter of SHAPED_RANGES? (one regex search: fast on long documents)"""
    return _SHAPED_RE.search(text) is not None


_NASTALIQ = re.compile(r"nasta[l']?[ie]{1,2}q", re.I)


@functools.lru_cache(maxsize=4096)
def is_nastaliq(font_name):
    """Urdu fonts in the Nastaliq style (Jameel Noori Nastaleeq, Noto Nastaliq Urdu...). PDFs made with them by
    Word or WeasyPrint store wrong or unknown letters for most shapes, so their text can't be trusted."""
    return bool(_NASTALIQ.search(font_name or ""))


def is_indic(c):
    """Hindi, Bengali, Tamil and the other Indic scripts."""
    return 0x0900 <= ord(c) <= 0x0DFF


def needs_layout(text, font):
    """Does this text need MuPDF's layout? Yes for joined/reordered scripts and for characters `font` lacks."""
    for c in em.strip(text):
        if c.isspace() or c in "‌‍‎‏":
            continue
        if is_shaped_char(c) or not font.has_glyph(ord(c)):
            return True
    return False


def _bidi(c):
    return unicodedata.bidirectional(c)


def is_rtl(text):
    """Is this text right-to-left? (its first letter that has a direction is Arabic, Hebrew...)"""
    for c in text:
        b = _bidi(c)
        if b in ("R", "AL"):
            return True
        if b == "L":
            return False
    return False


def has_rtl(text):
    return any(_bidi(c) in ("R", "AL") for c in text)


def _own_font_char(c):
    """Drawn in the document's font: Latin, Greek, Cyrillic, digits, common punctuation and currency."""
    o = ord(c)
    return (o < 0x0590 or 0x2010 <= o <= 0x205E or 0x20A0 <= o <= 0x20CF) and not c.isspace()


# ---------- measuring and drawing -----------------------------------------------------------------
GENERIC = {"he": "sans-serif", "ti": "serif", "co": "monospace"}
_files = {}


def _font_bytes(choice):
    if choice["kind"] == "buf":
        return choice["ref"]
    if choice["kind"] == "file":
        if choice["ref"] not in _files:
            if sum(len(b) for b in _files.values()) > 250_000_000:
                _files.clear()                     # Chinese/Japanese/Korean fonts are ~17 MB each
            with open(choice["ref"], "rb") as f:
                _files[choice["ref"]] = f.read()
        return _files[choice["ref"]]
    return None


_subsets = {}
_fonts = {}                        # installed script fonts by path, for measuring
SUBSET_SCOPE = threading.local()   # .text: every letter a save writes (set by edit_doc.apply_changes)


def _script_font_bytes(path, text):
    """The bytes of an installed script font for `text`. Large CFF fonts (Noto CJK, 16-25 MB) can't be shrunk
    by PyMuPDF when saving, so the whole font would be embedded (a one-line Chinese edit gave a 14 MB file),
    and cutting them with fontTools is either unreadable for MuPDF (renumbered CID fonts draw nothing) or
    slow (keeping 65,000 glyph slots: 16 s). So a small TrueType font is built with just the letters of the
    save (SUBSET_SCOPE: every piece then embeds the same bytes, and saving keeps one copy)."""
    try:
        big = path.lower().endswith(".otf") and os.path.getsize(path) > 2_000_000
    except OSError:
        big = False
    if not big:
        return _font_bytes({"kind": "file", "ref": path})
    scope = getattr(SUBSET_SCOPE, "text", None)
    letters = "".join(sorted(set(text) | set(scope or "")))
    key = (path, letters)
    if key not in _subsets:
        if len(_subsets) > 500:
            _subsets.clear()
        _subsets[key] = _small_ttf(path, letters)
    return _subsets[key]


def _small_ttf(path, letters):
    """A TrueType font with only `letters` from the font at `path`: outlines copied (cubic curves turned
    into TrueType's quadratic ones), widths, ascent/descent and names kept."""
    import io
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont
    src = TTFont(path, lazy=True)
    cmap, gs, hmtx = src.getBestCmap(), src.getGlyphSet(), src["hmtx"]
    upm = src["head"].unitsPerEm
    order, glyphs, adv, chars = [".notdef"], {".notdef": TTGlyphPen(None).glyph()}, {".notdef": upm // 2}, {}
    for c in letters:
        name = cmap.get(ord(c))
        if name is None or ord(c) in chars:
            continue
        new = f"g{len(order)}"
        pen = TTGlyphPen(None)
        gs[name].draw(Cu2QuPen(pen, max_err=upm / 1000, reverse_direction=True))
        order.append(new)
        glyphs[new], adv[new], chars[ord(c)] = pen.glyph(), hmtx[name][0], new
    names = src["name"]
    family = names.getDebugName(1) or "Noto CJK"
    style = names.getDebugName(2) or "Regular"
    hhea, os2 = src["hhea"], src["OS/2"]
    fb = FontBuilder(upm, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(chars)
    fb.setupGlyf(glyphs)
    glyf = fb.font["glyf"]
    fb.setupHorizontalMetrics({n: (adv[n], getattr(glyf[n], "xMin", 0) or 0) for n in order})
    fb.setupHorizontalHeader(ascent=hhea.ascent, descent=hhea.descent)
    fb.setupNameTable({"familyName": family, "styleName": style,
                       "psName": f"{family}-{style}".replace(" ", "")})
    fb.setupOS2(sTypoAscender=os2.sTypoAscender, sTypoDescender=os2.sTypoDescender,
                sTypoLineGap=os2.sTypoLineGap, usWinAscent=os2.usWinAscent, usWinDescent=os2.usWinDescent,
                usWeightClass=os2.usWeightClass, fsSelection=os2.fsSelection)
    fb.setupPost()
    fb.font["head"].macStyle = src["head"].macStyle      # bold / italic flags as in the source font
    out = io.BytesIO()
    fb.save(out)
    return out.getvalue()


# Writing systems, for choosing an installed font per script (Noto Sans / Serif Devanagari, Bengali...)
SCRIPT_RANGES = (("deva", 0x0900, 0x097F), ("beng", 0x0980, 0x09FF), ("guru", 0x0A00, 0x0A7F), ("gujr", 0x0A80, 0x0AFF),
                 ("orya", 0x0B00, 0x0B7F), ("taml", 0x0B80, 0x0BFF), ("telu", 0x0C00, 0x0C7F), ("knda", 0x0C80, 0x0CFF),
                 ("mlym", 0x0D00, 0x0D7F), ("sinh", 0x0D80, 0x0DFF), ("thai", 0x0E00, 0x0E7F), ("laoo", 0x0E80, 0x0EFF),
                 ("tibt", 0x0F00, 0x0FFF), ("mymr", 0x1000, 0x109F), ("geor", 0x10A0, 0x10FF), ("ethi", 0x1200, 0x139F),
                 ("khmr", 0x1780, 0x17FF), ("armn", 0x0530, 0x058F), ("hebr", 0x0590, 0x05FF), ("arab", 0x0600, 0x06FF),
                 ("arab", 0x0750, 0x077F), ("arab", 0x08A0, 0x08FF), ("hebr", 0xFB1D, 0xFB4F), ("arab", 0xFB50, 0xFDFF),
                 ("arab", 0xFE70, 0xFEFF), ("cjk", 0x1100, 0x11FF), ("cjk", 0x2E80, 0x9FFF), ("cjk", 0xAC00, 0xD7AF),
                 ("cjk", 0xF900, 0xFAFF), ("cjk", 0xFF00, 0xFFEF), ("cjk", 0x20000, 0x3FFFF))


def script_of(c):
    """The writing system of a letter (None for Latin, digits, punctuation and spaces)."""
    o = ord(c[:1] or " ")
    for name, a, b in SCRIPT_RANGES:
        if a <= o <= b:
            return name
    return None


class ShapedFont:
    """Measures text the way MuPDF's layout will draw it. `base` is the font choice for the document's own
    letters; `generic` (sans-serif / serif / monospace) picks the look of the built-in fonts for the rest."""

    def __init__(self, base, generic, bold, italic, script_font=None, script_scale=1.0, script_files=None, cjk=None):
        """script_font: a font file for the other letters instead of MuPDF's own (Noto Nastaliq Urdu for
        lines that were in a Nastaliq font); script_scale: its size against the line's size.
        script_files(script, serif, bold): an installed font file for a writing system (Noto Sans Devanagari
        Bold...), or None. MuPDF's own fonts for other scripts are regular weight and partly serif only, and its
        layout never makes a font bold or italic itself: bold needs a bold file, else it is drawn thicker here
        (as word processors do), and italic is drawn slanted (these scripts have no italic fonts)."""
        self.base, self.generic, self.bold, self.italic = base, generic, bold, italic
        self.script_font, self.script_scale = script_font, script_scale
        self.script_files = script_files
        self.cjk = cjk                             # "sc", "tc", "jp" or "kr" when the line's font says which
        self.serif = generic == "serif"
        self._font = base["font"]
        self._widths = {}

    def _region(self, text):
        """Which version of the Chinese/Japanese/Korean font: the line's font tells, else kana = Japanese,
        hangul = Korean, otherwise Simplified Chinese."""
        if self.cjk:
            return self.cjk
        if any(0x3040 <= ord(c) <= 0x30FF or 0x31F0 <= ord(c) <= 0x31FF for c in text):
            return "jp"
        if any(0xAC00 <= ord(c) <= 0xD7AF or 0x1100 <= ord(c) <= 0x11FF or 0x3130 <= ord(c) <= 0x318F for c in text):
            return "kr"
        return "sc"

    def _key(self, script, text):
        return "cjk" + self._region(text) if script == "cjk" else script

    def _scripts(self, text):
        """Writing systems of the letters MuPDF's layout draws (not the document font's own letters)."""
        return {self._key(s, text) for s in (script_of(c) for c in text
                                             if not (_own_font_char(c) and self._font.has_glyph(ord(c)))) if s}

    def _file(self, script, bold):
        return self.script_files(script, self.serif, bold) if self.script_files else None

    def fakes(self, text):
        """(drawn thicker, drawn slanted): bold or italic that no installed font can give these letters."""
        scripts = self._scripts(text)
        if self.script_font:                       # a Nastaliq line: its own font, bold if installed
            fake_bold = self.bold and "bold" not in self.script_font.lower() and bool(scripts)
        else:
            fake_bold = self.bold and any(self._file(s, True) is None for s in scripts)
        return fake_bold, self.italic and bool(scripts)

    def __getattr__(self, name):           # ascender, descender, name... come from the document's font
        return getattr(self._font, name)

    def has_glyph(self, c):
        return 1

    # The HTML for a piece of text, its CSS and the archive holding the document's font
    def html(self, text, size, color=0):
        buf = _font_bytes(self.base)
        fake_bold, fake_italic = self.fakes(text)
        # drawn thicker / slanted as a whole (see draw): the fonts themselves are asked for regular upright
        weight = "bold" if self.bold and not fake_bold else "normal"
        style = "italic" if self.italic and not fake_italic else "normal"
        if buf is not None:
            own = "docf"
            face = f"@font-face{{font-family:docf;src:url(docf.ttf);font-weight:{weight};font-style:{style}}}"
        else:
            own, face = self.generic, ""
        other = self.generic
        if self.script_font:
            face += f"@font-face{{font-family:scrf;src:url(scrf.ttf);font-weight:{weight};font-style:{style}}}"
            other = f"scrf,{self.generic};font-size:{self.script_scale:g}em"
        css = (f"{face} *{{margin:0;padding:0}} div{{font-size:{size}px;line-height:1.5;white-space:pre;"
               f"text-align:left;font-weight:{weight};font-style:{style};color:#{color & 0xFFFFFF:06x}}} "
               f"span.d{{font-family:{own},{self.generic}}} span.o{{font-family:{other}}}")
        runs, cur, cur_own = [], "", None
        for c in text:
            mine = _own_font_char(c) and self._font.has_glyph(ord(c)) if not c.isspace() else cur_own
            if cur and mine != cur_own:
                runs.append((cur, cur_own))
                cur = ""
            cur, cur_own = cur + c, mine
        if cur:
            runs.append((cur, cur_own))
        # other letters: each writing system in its installed Noto font when there is one (sans or serif
        # like the line, bold when asked), else MuPDF's own
        files = {}
        if not self.script_font:
            pieces = []
            for t, mine in runs:
                if mine:
                    pieces.append((t, "d", None))
                    continue
                cur, cur_s = "", None
                for c in t:
                    s_ = (self._key(script_of(c), text) if script_of(c) else None) or cur_s
                    if cur and s_ != cur_s:
                        pieces.append((cur, "o", cur_s))
                        cur = ""
                    cur, cur_s = cur + c, s_
                if cur:
                    pieces.append((cur, "o", cur_s))
            spans = []
            for t, cls, s_ in pieces:
                path = self._file(s_, weight == "bold") if s_ else None
                if path:
                    files[s_] = path
                    spans.append(f'<span class="o" style="font-family:s{s_},{self.generic}">{html.escape(t)}</span>')
                else:
                    spans.append(f'<span class="{cls}">{html.escape(t)}</span>')
            body = "".join(spans)
            face += "".join(f"@font-face{{font-family:s{s_};src:url(s{s_}.ttf);font-weight:{weight};font-style:{style}}}"
                            for s_ in files)
            css = face + css[css.index(" *{"):] if face else css
        else:
            body = "".join(f'<span class="{"d" if mine else "o"}">{html.escape(t)}</span>' for t, mine in runs)
        archive = None
        if buf is not None or self.script_font or files:
            archive = fitz.Archive()
        if buf is not None:
            archive.add((buf, "docf.ttf"))
        if self.script_font:
            archive.add((_font_bytes({"kind": "file", "ref": self.script_font}), "scrf.ttf"))
        for s_, path in files.items():
            archive.add((_script_font_bytes(path, text), f"s{s_}.ttf"))
        return f"<div>{body}</div>", css, archive      # MuPDF reads the direction from the text itself

    def layout(self, text, size):
        """(left offset, width, baseline offset, ascent, descent) of `text` laid out at `size`."""
        key = (text, size)
        if key not in self._widths:
            body, css, archive = self.html(text, size)
            doc = fitz.open()
            page = doc.new_page(width=_room(text, size).width, height=_room(text, size).height)
            page.insert_htmlbox(page.rect, body, css=css, archive=archive, scale_low=1)
            chars = [c for b in page.get_text("rawdict")["blocks"] for l in b.get("lines", [])
                     for s in l["spans"] for c in s["chars"]]
            if not chars:
                self._widths[key] = (0.0, 0.0, size, size, 0.0)
            else:
                x0 = min(c["bbox"][0] for c in chars)
                x1 = max(c["bbox"][2] for c in chars)
                base = max(c["origin"][1] for c in chars)
                top = min(c["bbox"][1] for c in chars)
                bottom = max(c["bbox"][3] for c in chars)
                self._widths[key] = (x0, x1 - x0, base, base - top, bottom - base)
            doc.close()
        return self._widths[key]

    def text_length(self, text, fontsize=11, **_kw):
        width = 0.0
        for t, is_emoji in em.split(text):
            if is_emoji:
                width += em.ADVANCE * fontsize
            elif t:
                width += self._plain_length(t, fontsize)
        return width

    def _plain_length(self, text, size):
        if any(is_shaped_char(c) for c in text):              # joined/reordered: measure the whole text
            return self.layout(text, 20.0)[1] * size / 20.0
        width = 0.0                                          # otherwise widths add up character by character
        for c in text:
            if (c.isspace() or _own_font_char(c)) and self._font.has_glyph(ord(c)):
                width += self._font.text_length(c, fontsize=size)
            else:
                adv = self._file_advance(c)
                width += adv * size if adv is not None else self.layout(c, 20.0)[1] * size / 20.0
        return width

    def _file_advance(self, c):
        """A letter's width (em) read from its installed script font, when MuPDF's layout will draw it in that
        font: no layout run per letter (each loads the font: Chinese text took 25 s to measure)."""
        s_ = script_of(c)
        if not s_ or self.script_font:
            return None
        key = self._key(s_, c)
        fake_bold, _i = self.fakes(c)
        path = self._file(key, self.bold and not fake_bold)
        if not path:
            return None
        if path not in _fonts:
            _fonts[path] = fitz.Font(fontfile=path)
        f = _fonts[path]
        return f.glyph_advance(ord(c)) if f.has_glyph(ord(c)) else None


def _room(text, size):
    """The box text is laid out in, the same when measuring and drawing (so it lands where it was measured,
    whichever way MuPDF aligns it)."""
    return fitz.Rect(0, 0, max(400, 3 * size * (len(text) + 2)), 6 * size + 20)


def _hex(text):
    return "FEFF" + text.encode("utf-16-be").hex().upper()


def _num(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def draw(page, x, baseline, text, size, color, font):
    """Draw text (no emoji) with its left edge at x on a baseline; returns its width."""
    x0, width, base, _a, _d = font.layout(text, size)
    if not width:
        return 0.0
    rect = _room(text, size) + (x - x0, baseline - base, x - x0, baseline - base)
    doc = page.parent
    before = set(page.get_contents())
    body, css, archive = font.html(text, size, color)
    page.insert_htmlbox(rect, body, css=css, archive=archive, scale_low=1)
    box = f"[{_num(x)} {_num(baseline - size)} {_num(x + width)} {_num(baseline + 0.3 * size)}]".encode()
    fake_bold, fake_italic = font.fakes(text) if hasattr(font, "fakes") else (False, False)
    pre, post = b"", b""
    if fake_bold or fake_italic:          # bold drawn thicker (outline stroked too), italic slanted at the baseline
        pre, post = b"q\n", b"\nQ"
        if fake_bold:
            rgb = " ".join(_num(((color >> k) & 255) / 255) for k in (16, 8, 0))
            pre += f"2 Tr {_num(0.04 * size)} w {rgb} RG\n".encode()
        if fake_italic:
            yb = (fitz.Point(x, baseline) * ~page.transformation_matrix).y
            pre += f"1 0 {SLANT} 1 {_num(-SLANT * yb)} 0 cm\n".encode()
    for xref in page.get_contents():
        if xref not in before:
            stream = doc.xref_stream(xref) or b""
            # thicker / slanted only around the piece itself: PyMuPDF also adds streams that wrap the page's
            # older content in q ... Q, and a slant there would tilt everything else on the page
            draws = b" Do" in stream
            doc.update_stream(xref, b"/Span <</ActualText <" + _hex(text).encode() + b"> /" + BOX + b" " + box +
                              b">> BDC\n" + (pre if draws else b"") + stream + (post if draws else b"") + b"\nEMC\n")
    return width


SLANT = 0.21                               # about 12 degrees, like a word processor's synthetic italic


def mark(page, text, box, baseline, style, right=False):
    """Mark a line the editor wrote: its text, box, style and whether it is anchored at its right edge,
    read back by page_marks()."""
    data = json.dumps({"t": text, "b": [round(v, 2) for v in box], "y": round(baseline, 2), "r": int(bool(right)),
                       "s": {k: style[k] for k in ("font", "flags", "size", "color")}}, ensure_ascii=False)
    op = b"\n/" + MARK + b" <</D <" + data.encode("utf-8").hex().encode() + b">>> DP\n"
    doc, xrefs = page.parent, page.get_contents()
    if xrefs:
        doc.update_stream(xrefs[-1], (doc.xref_stream(xrefs[-1]) or b"") + op)


_MARK_RE = re.compile(rb"/" + MARK + rb"\s*<<\s*/D\s*<([0-9A-Fa-f\s]*)>\s*>>\s*DP")
_BOX_RE = re.compile(rb"/Span\s*<<\s*/ActualText\s*<[0-9A-Fa-f\s]*>\s*/" + BOX + rb"\s*\[([^\]]*)\]\s*>>\s*BDC(.*?)EMC",
                     re.S)


def _decode(hexdata):
    try:
        d = json.loads(bytes.fromhex(re.sub(rb"\s", b"", hexdata).decode()).decode("utf-8"))
        return {"text": str(d["t"]), "box": fitz.Rect(d["b"]), "baseline": float(d["y"]), "style": d["s"],
                "right": bool(d.get("r"))}
    except Exception:
        return None


def page_marks(page):
    """The lines the editor wrote on this page with MuPDF's layout: [{text, box, baseline, style}]."""
    try:
        data = page.read_contents()
    except Exception:
        return []
    if MARK not in data:
        return []
    return [m for m in (_decode(h) for h in _MARK_RE.findall(data)) if m]


def _hit(box, rects):
    c = fitz.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
    return any(c in fitz.Rect(r) for r in rects)


def delete_in(page, rects):
    """Remove the marked lines (and the pieces drawn for them) whose centre is inside any of rects."""
    doc = page.parent
    for xref in page.get_contents():
        data = doc.xref_stream(xref) or b""
        if MARK not in data and BOX not in data:
            continue

        def cut_mark(m):
            d = _decode(m.group(1))
            return b"" if d and _hit(d["box"], rects) else m.group(0)

        def cut_piece(m):
            try:
                box = fitz.Rect([float(v) for v in m.group(1).split()])
            except ValueError:
                return m.group(0)
            return b"" if _hit(box, rects) else m.group(0)

        new = _BOX_RE.sub(cut_piece, _MARK_RE.sub(cut_mark, data))
        if new != data:
            doc.update_stream(xref, new)


def unmark(page, rect):
    """Forget the marks of lines that overlap `rect` (part of them is being erased)."""
    doc, rect = page.parent, fitz.Rect(rect)
    for xref in page.get_contents():
        data = doc.xref_stream(xref) or b""
        if MARK not in data:
            continue

        def cut(m):
            d = _decode(m.group(1))
            return b"" if d and d["box"].intersects(rect) else m.group(0)

        new = _MARK_RE.sub(cut, data)
        if new != data:
            doc.update_stream(xref, new)


def copy_marks(src_page, target, keep, dx, dy):
    """A region of a page was copied onto `target`, moved by (dx, dy): its marked lines go with it."""
    for m in page_marks(src_page):
        if _hit(m["box"], [keep]):
            mark(target, m["text"], m["box"] + (dx, dy, dx, dy), m["baseline"] + dy, m["style"], m["right"])


# ---------- reading other PDFs ---------------------------------------------------------------------
def _combining(c):
    return len(c) == 1 and unicodedata.category(c) in ("Mn", "Mc", "Me")   # (an emoji is several characters)


def clusters(chars):
    """Group (char, bbox, ...) tuples into a letter plus its marks, dropping marks repeated by mistake
    (some PDFs map one vowel sign twice: हूँँ)."""
    out = []
    for ch in chars:
        c = ch[0]
        if out and _combining(c) and not out[-1][0][0].isspace():
            if out[-1][-1][0] == c:
                # keep the copy that has a width: the zero-width one would leave the letter's end
                # outside the line's box, and erasing the line would leave it behind (Bengali ক্ষা)
                if ch[1][2] - ch[1][0] > out[-1][-1][1][2] - out[-1][-1][1][0]:
                    out[-1][-1] = ch
                continue
            out[-1].append(ch)
        else:
            out.append([ch])
    return out


UNKNOWN = chr(0xFFFD)          # what MuPDF reads for a letter the PDF doesn't identify
I_SIGN = chr(0x093F)           # Hindi vowel sign i, drawn before its letter


def _deva_consonant(c):
    return len(c) == 1 and (0x0915 <= ord(c) <= 0x0939 or 0x0958 <= ord(c) <= 0x095F)


def repair_devanagari(chars):
    """Some PDF makers (WeasyPrint) don't say which letter the Hindi vowel sign i is: it reads as an unknown
    character in front of its letter ("?हंदी"). It's the only sign drawn there, so it can be put back:
    after the letter and any letters joined to it (हिंदी, कृत्रिम). If the sign is already there, the
    unknown character is just dropped."""
    if UNKNOWN not in "".join(c[0] for c in chars):
        return chars
    out, i = list(chars), 0
    while i < len(out):
        if out[i][0] == UNKNOWN and i + 1 < len(out) and _deva_consonant(out[i + 1][0]):
            j = i + 2                                   # end of the letter cluster: nukta, virama + letter
            while j < len(out):
                if out[j][0] == chr(0x093C):
                    j += 1
                elif out[j][0] == chr(0x094D) and j + 1 < len(out) and _deva_consonant(out[j + 1][0]):
                    j += 2
                else:
                    break
            junk = out.pop(i)
            if not (j - 1 < len(out) and out[j - 1][0] == I_SIGN):
                out.insert(j - 1, (I_SIGN,) + tuple(junk[1:]))
            i = j
        else:
            i += 1
    return out


def dedupe_marks(chars):
    return [ch for cl in clusters(chars) for ch in cl]


def _centre(cl):
    xs = [v for ch in cl for v in (ch[1][0], ch[1][2])]
    return (min(xs) + max(xs)) / 2


def visual_sort(chars):
    """Characters left to right as they appear on the page (marks stay with their letter)."""
    return [ch for cl in sorted(clusters(chars), key=_centre) for ch in cl]


def _kind(cl):
    b = _bidi(cl[0][0][:1] or " ")
    if b in ("R", "AL"):
        return "R"
    if b == "L":
        return "L"
    if b in ("EN", "AN"):
        return "D"
    return "N"


def _reverse_runs(cls, inside, joiners=lambda cl: _kind(cl) == "N"):
    """Reverse each run of clusters for which inside() holds; joiners between two such clusters belong to the run."""
    flags = [inside(cl) for cl in cls]
    for i, cl in enumerate(cls):
        if not flags[i] and joiners(cl):
            j = i
            while j < len(cls) and not flags[j] and joiners(cls[j]):
                j += 1
            if i > 0 and flags[i - 1] and j < len(cls) and flags[j]:
                for k in range(i, j):
                    flags[k] = True
    out, i = [], 0
    while i < len(cls):
        if flags[i]:
            j = i
            while j < len(cls) and flags[j]:
                j += 1
            out.append((cls[i:j], True))
            i = j
        else:
            out.append(([cls[i]], False))
            i += 1
    return out


def logical(chars):
    """Characters of one line (in visual order, left to right) in reading order."""
    cls = clusters(chars)
    n_r = sum(_kind(cl) == "R" for cl in cls)
    n_l = sum(_kind(cl) == "L" for cl in cls)
    if not n_r:
        return [ch for cl in cls for ch in cl]
    out = []
    if n_r >= n_l:                          # a right-to-left line: read from the right; English and numbers inside read left to right
        cls = cls[::-1]
        for run, inside in _reverse_runs(cls, lambda cl: _kind(cl) in ("L", "D")):
            out += run[::-1] if inside else run
    else:                                   # a left-to-right line with Arabic/Hebrew words in it
        for run, inside in _reverse_runs(cls, lambda cl: _kind(cl) == "R",
                                         joiners=lambda cl: _kind(cl) in ("N", "D")):
            if inside:
                run = run[::-1]
                fixed, k = [], 0            # numbers inside an Arabic phrase still read left to right
                while k < len(run):
                    if _kind(run[k]) == "D":
                        j = k
                        while j < len(run) and _kind(run[j]) == "D":
                            j += 1
                        fixed += run[k:j][::-1]
                        k = j
                    else:
                        fixed.append(run[k])
                        k += 1
                run = fixed
            out += run
    flat = [ch for cl in out for ch in cl]
    return _unshape(flat)


def _unshape(chars):
    """Arabic letters stored in their joined shapes (presentation forms) back to ordinary letters."""
    out = []
    for ch in chars:
        if len(ch[0]) == 1 and _in(PRESENTATION, ord(ch[0])):
            plain = unicodedata.normalize("NFKC", ch[0])
            if plain != ch[0]:
                for p in plain:
                    out.append((p,) + tuple(ch[1:]))
                continue
        out.append(ch)
    return out
