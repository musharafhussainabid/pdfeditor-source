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
import html
import json
import re
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


def is_shaped_char(c):
    return _in(SHAPED_RANGES, ord(c))


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
            with open(choice["ref"], "rb") as f:
                _files[choice["ref"]] = f.read()
        return _files[choice["ref"]]
    return None


class ShapedFont:
    """Measures text the way MuPDF's layout will draw it. `base` is the font choice for the document's own
    letters; `generic` (sans-serif / serif / monospace) picks the look of the built-in fonts for the rest."""

    def __init__(self, base, generic, bold, italic):
        self.base, self.generic, self.bold, self.italic = base, generic, bold, italic
        self._font = base["font"]
        self._widths = {}

    def __getattr__(self, name):           # ascender, descender, name... come from the document's font
        return getattr(self._font, name)

    def has_glyph(self, c):
        return 1

    # The HTML for a piece of text, its CSS and the archive holding the document's font
    def html(self, text, size, color=0):
        buf = _font_bytes(self.base)
        weight = "bold" if self.bold else "normal"
        style = "italic" if self.italic else "normal"
        if buf is not None:
            own = "docf"
            face = f"@font-face{{font-family:docf;src:url(docf.ttf);font-weight:{weight};font-style:{style}}}"
        else:
            own, face = self.generic, ""
        css = (f"{face} *{{margin:0;padding:0}} div{{font-size:{size}px;line-height:1.5;white-space:pre;"
               f"text-align:left;font-weight:{weight};font-style:{style};color:#{color & 0xFFFFFF:06x}}} "
               f"span.d{{font-family:{own},{self.generic}}} span.o{{font-family:{self.generic}}}")
        runs, cur, cur_own = [], "", None
        for c in text:
            mine = _own_font_char(c) and self._font.has_glyph(ord(c)) if not c.isspace() else cur_own
            if cur and mine != cur_own:
                runs.append((cur, cur_own))
                cur = ""
            cur, cur_own = cur + c, mine
        if cur:
            runs.append((cur, cur_own))
        body = "".join(f'<span class="{"d" if mine else "o"}">{html.escape(t)}</span>' for t, mine in runs)
        archive = None
        if buf is not None:
            archive = fitz.Archive()
            archive.add((buf, "docf.ttf"))
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
                width += self.layout(c, 20.0)[1] * size / 20.0
        return width


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
    for xref in page.get_contents():
        if xref not in before:
            stream = doc.xref_stream(xref) or b""
            doc.update_stream(xref, b"/Span <</ActualText <" + _hex(text).encode() + b"> /" + BOX + b" " + box +
                              b">> BDC\n" + stream + b"\nEMC\n")
    return width


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
                continue
            out[-1].append(ch)
        else:
            out.append([ch])
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
