"""
edit_doc.py: PDF editing engine: text edits, list insertion and paragraph reflow on real PDF
page content (PyMuPDF). Pure functions: bytes in, bytes out. No web server, no disk.

    analyze(pdf_bytes)                         -> (info, layout)
    apply_changes(pdf_bytes, info, edits, inserts, additions) -> (pdf_bytes, page_count, warnings)

`info` is the engine's private analysis (store it with the session); `layout` is
the public JSON the apps use to draw edit boxes and list controls.
"""
import bisect
import json
import os
import re
import zlib
from statistics import median

import pymupdf as fitz

EPS = 0.05
FALLBACK_FONT = os.environ.get("FALLBACK_FONT")  # optional Unicode TTF/OTF for any script


def r2(seq):
    return [round(float(v), 2) for v in seq]


def int_to_rgb(c: int):
    return ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255


# --------------------------------------------------------------------------- #
# Vector drawings: serialize, redraw, classify
# --------------------------------------------------------------------------- #
def ser_drawing(d):
    items = []
    for it in d["items"]:
        k = it[0]
        if k == "l":
            items.append(["l", r2(it[1]), r2(it[2])])
        elif k == "re":
            items.append(["re", r2(it[1])])
        elif k == "qu":
            q = it[1]
            items.append(["qu", [r2(q.ul), r2(q.ur), r2(q.ll), r2(q.lr)]])
        elif k == "c":
            items.append(["c", r2(it[1]), r2(it[2]), r2(it[3]), r2(it[4])])
    cap = d.get("lineCap") or 0
    if isinstance(cap, (list, tuple)):
        cap = max(cap) if cap else 0
    fo, so = d.get("fill_opacity"), d.get("stroke_opacity")
    return {
        "items": items,
        "fill": list(d["fill"]) if d.get("fill") is not None else None,
        "color": list(d["color"]) if d.get("color") is not None else None,
        "width": float(d.get("width") or 1.0),
        "closePath": bool(d.get("closePath")),
        "even_odd": bool(d.get("even_odd")),
        "fill_opacity": 1.0 if fo is None else float(fo),
        "stroke_opacity": 1.0 if so is None else float(so),
        "lineCap": int(cap),
        "lineJoin": int(d.get("lineJoin") or 0),
        "rect": r2(d["rect"]),
    }


def redraw(page, spec, dy=0.0, dx=0.0):
    """Draw a serialized vector path on `page`, shifted by (dx, dy)."""
    sh = page.new_shape()
    off = fitz.Point(dx, dy)
    for it in spec["items"]:
        k = it[0]
        if k == "l":
            sh.draw_line(fitz.Point(it[1]) + off, fitz.Point(it[2]) + off)
        elif k == "re":
            sh.draw_rect(fitz.Rect(it[1]) + (dx, dy, dx, dy))
        elif k == "qu":
            sh.draw_quad(fitz.Quad([fitz.Point(p) + off for p in it[1]]))
        elif k == "c":
            sh.draw_bezier(*[fitz.Point(p) + off for p in it[1:5]])
    sh.finish(fill=spec["fill"], color=spec["color"], width=spec["width"],
              closePath=spec["closePath"], even_odd=spec["even_odd"],
              fill_opacity=spec["fill_opacity"], stroke_opacity=spec["stroke_opacity"],
              lineCap=spec["lineCap"], lineJoin=spec["lineJoin"])
    sh.commit()


def ink_rect(d):
    """A drawing's area including its stroke (thin rules have zero-height rects,
    which PyMuPDF treats as empty and never intersecting)."""
    w = (d.get("width") or 0) / 2 if d.get("color") is not None else 0
    return fitz.Rect(d["rect"]) + (-w - 0.05, -w - 0.05, w + 0.05, w + 0.05)


def is_background(d, page_area):
    """Page-sized fills and page frames: redrawn on every rebuilt or new page."""
    return d["rect"].get_area() >= 0.7 * page_area


def draw_entry(d):
    """
    Obstacle entry for a vector drawing. `patch` means that if a cut passes
    through it, the gap can be closed by extending it (fills and vertical edges).
    """
    r = fitz.Rect(d["rect"])
    items = d["items"]

    def axis(it):
        if it[0] == "re":
            return True
        if it[0] == "l":
            return abs(it[1].x - it[2].x) < 0.01 or abs(it[1].y - it[2].y) < 0.01
        if it[0] == "qu":
            return it[1].is_rectangular
        return False

    aligned = all(axis(it) for it in items)
    big = r.width > 20 and r.height > 20
    fill, color = d.get("fill"), d.get("color")
    fills, vlines = [], []
    if fill is not None:
        if aligned and all(it[0] in ("re", "qu") for it in items):
            for it in items:
                rr = it[1] if it[0] == "re" else it[1].rect
                fills.append(r2(rr))
        else:
            fills.append(r2(r))
    if color is not None:
        w = d.get("width") or 1.0
        if aligned:
            for it in items:
                if it[0] == "l" and abs(it[1].x - it[2].x) < 0.01:
                    vlines.append([it[1].x, min(it[1].y, it[2].y), max(it[1].y, it[2].y)])
                elif it[0] in ("re", "qu"):
                    rr = it[1] if it[0] == "re" else it[1].rect
                    vlines += [[rr.x0, rr.y0, rr.y1], [rr.x1, rr.y0, rr.y1]]
        elif big:
            vlines += [[r.x0 + w / 2, r.y0, r.y1], [r.x1 - w / 2, r.y0, r.y1]]
    fo, so = d.get("fill_opacity"), d.get("stroke_opacity")
    return {
        "rect": r, "patch": aligned or big,
        "fill": list(fill) if fill is not None else None,
        "fop": 1.0 if fo is None else fo,
        "color": list(color) if color is not None else None,
        "width": d.get("width") or 1.0,
        "sop": 1.0 if so is None else so,
        "fills": fills, "vlines": vlines,
    }


def moved_entry(e, r):
    """Copy of a drawing entry at a new rectangle (edges recomputed from it)."""
    w = e["width"]
    return {
        **e, "rect": r,
        "fills": [r2(r)] if e["fill"] is not None else [],
        "vlines": ([[r.x0 + w / 2, r.y0, r.y1], [r.x1 - w / 2, r.y0, r.y1]]
                   if e["color"] is not None and e["vlines"] else []),
    }


def draw_patch(page, entries, cut, y0, y1, x0, x1):
    """Close the gap [y0, y1) opened at `cut` for drawings that the cut crossed."""
    if y1 <= y0 or not entries:
        return
    sh = page.new_shape()
    drawn = False
    for e in entries:
        for fx0, fy0, fx1, fy1 in e["fills"]:
            if fy0 < cut < fy1 and fx1 > x0 and fx0 < x1:
                ov = 0.3 if e["fop"] >= 0.999 else 0.0  # overlap hides seams
                sh.draw_rect(fitz.Rect(max(fx0, x0), y0 - ov, min(fx1, x1), y1 + ov))
                sh.finish(fill=e["fill"], color=None, fill_opacity=e["fop"])
                drawn = True
        for vx, vy0, vy1 in e["vlines"]:
            if vy0 < cut < vy1 and x0 <= vx <= x1:
                sh.draw_line((vx, y0), (vx, y1))
                sh.finish(color=e["color"], width=e["width"], stroke_opacity=e["sop"])
                drawn = True
    if drawn:
        sh.commit()


# --------------------------------------------------------------------------- #
# Layout model: what's where on a page (kept up to date as pages are rebuilt,
# because content inside clipped slices can't be re-measured reliably)
# --------------------------------------------------------------------------- #
def build_model(page):
    area = page.rect.get_area()
    text = []
    for b in page.get_text("dict", flags=fitz.TEXT_ACCURATE_BBOXES)["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            if "".join(s["text"] for s in ln["spans"]).strip():
                text.append(fitz.Rect(ln["bbox"]))
    img = []
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"]) & page.rect
        if not r.is_empty:
            img.append(r)
    draw, bg = [], []
    for d in page.get_drawings():
        if d["rect"].is_empty and d["rect"].width == 0 and d["rect"].height == 0:
            continue
        if is_background(d, area):
            bg.append(ser_drawing(d))
        else:
            draw.append(draw_entry(d))
    return {"text": text, "img": img, "draw": draw, "bg": bg}


def get_model(models, page):
    if page.xref not in models:
        models[page.xref] = build_model(page)
    return models[page.xref]


def _hov(r, x0, x1):
    return r.x1 > x0 + EPS and r.x0 < x1 - EPS


def crossing(model, y, x0, x1, allow_draw=True):
    """
    Can the page be cut horizontally at y (between x0 and x1)?
    Returns (ok, drawings_to_patch). Text and images must not be cut.
    """
    for r in model["text"] + model["img"]:
        if r.y0 < y - EPS and r.y1 > y + EPS and _hov(r, x0, x1):
            return False, []
    patches = []
    for e in model["draw"]:
        r = e["rect"]
        if r.y0 < y - EPS and r.y1 > y + EPS and _hov(r, x0, x1):
            if not (allow_draw and e["patch"]):
                return False, []
            patches.append(e)
    return True, patches


def region_free(model, y0, y1, x0, x1):
    """True if nothing but enclosing boxes/rules occupies [x0,x1] x [y0,y1]."""
    for r in model["text"] + model["img"]:
        if r.y1 > y0 + EPS and r.y0 < y1 - EPS and _hov(r, x0, x1):
            return False
    for e in model["draw"]:
        r = e["rect"]
        if r.y1 > y0 + EPS and r.y0 < y1 - EPS and _hov(r, x0, x1):
            if e["patch"] and r.y0 < y0 - EPS and r.y1 > y1 + EPS:
                continue  # a box or rule that encloses the region
            return False
    return True


def obstacle_rects(model):
    return model["text"] + model["img"] + [e["rect"] for e in model["draw"]]


def column_range(model, x0, x1, y0, y1, width):
    """Grow [x0, x1] until no content in [y0, y1] straddles its edges."""
    changed = True
    while changed:
        changed = False
        for r in model["text"] + model["img"]:
            if r.y1 > y0 and r.y0 < y1 and _hov(r, x0, x1) and (r.x0 < x0 or r.x1 > x1):
                x0, x1, changed = min(x0, r.x0), max(x1, r.x1), True
        for e in model["draw"]:
            r = e["rect"]
            if e["patch"] and r.y0 < y0 and r.y1 > y1:
                continue  # container box around the column: extended, not moved
            if r.y1 > y0 and r.y0 < y1 and _hov(r, x0, x1) and (r.x0 < x0 or r.x1 > x1):
                x0, x1, changed = min(x0, r.x0), max(x1, r.x1), True
    return max(0.0, x0 - 0.5), min(width, x1 + 0.5)


# --------------------------------------------------------------------------- #
# Page slicing: copy a rectangle of real page content to another position
# --------------------------------------------------------------------------- #
class Slicer:
    """Places regions of a source page onto target pages. Every character whose
    center lies outside the region is removed from that slice's copy, so no
    hidden duplicate text ends up in the output (search and copy stay correct).

    Removal targets each line's or character's own core rather than the region
    boundary: MuPDF removes a character once a redaction covers ~10% of its full
    line-height box, and those boxes overlap between adjacent lines."""

    def __init__(self, src_bytes, pno):
        # Work on a one-page copy: re-opening the whole document for every slice
        # was ~95% of the time on long documents.
        src = src_bytes if isinstance(src_bytes, fitz.Document) else fitz.open("pdf", src_bytes)
        one = fitz.open()
        one.insert_pdf(src, from_page=pno, to_page=pno)
        self.src_bytes, self.pno, self.keep, self._lines = one.tobytes(), 0, [], None

    def _text(self):
        if self._lines is None:
            doc = fitz.open("pdf", self.src_bytes)
            raw = doc[self.pno].get_text("rawdict")
            self._lines = [
                (fitz.Rect(l["bbox"]), abs(l["dir"][0] - 1) < 1e-3,
                 [fitz.Rect(c["bbox"]) for s in l["spans"] for c in s["chars"]])
                for b in raw["blocks"] if b.get("type") == 0 for l in b["lines"]]
        return self._lines

    def place(self, target, clip, dy, dx=0.0, keep=None):
        """Show `clip` of the source page at an offset. Characters are kept if their
        center lies in `keep` (default: the clip itself)."""
        clip = fitz.Rect(clip)
        if clip.is_empty or clip.height < 0.01 or clip.width < 0.01:
            return
        keep = fitz.Rect(keep) if keep is not None else clip
        rects = []
        for lb, horizontal, chars in self._text():
            if keep.contains(lb):
                continue
            if horizontal and not lb.intersects(keep):  # whole line outside
                cy, h = (lb.y0 + lb.y1) / 2, lb.height
                rects.append(fitz.Rect(lb.x0, cy - 0.2 * h, lb.x1, cy + 0.2 * h))
                continue
            for cb in chars:  # line straddles the region: decide per character
                c = (cb.tl + cb.br) / 2
                if cb.width > 0 and not keep.contains(c):
                    w, h = cb.width, cb.height
                    rects.append(fitz.Rect(cb.x0 + 0.25 * w, c.y - 0.2 * h,
                                           cb.x1 - 0.25 * w, c.y + 0.2 * h))
        doc = fitz.open("pdf", self.src_bytes)
        pg = doc[self.pno]
        if rects:
            for r in rects:
                pg.add_redact_annot(r, fill=False)
            pg.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        target.show_pdf_page(clip + (dx, dy, dx, dy), doc, self.pno, clip=clip)
        self.keep.append(doc)


def wipe_page(doc, page):
    xrefs = [x for x in page.get_contents() if 0 < x < doc.xref_length()]
    if xrefs:
        for xref in xrefs:
            doc.update_stream(xref, b" ")
    else:  # unusual /Contents: give the page one fresh, empty content stream
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, b" ")
        doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")

# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
BASE14 = {
    "he": {(0, 0): "helv", (0, 1): "heit", (1, 0): "hebo", (1, 1): "hebi"},
    "ti": {(0, 0): "tiro", (0, 1): "tiit", (1, 0): "tibo", (1, 1): "tibi"},
    "co": {(0, 0): "cour", (0, 1): "coit", (1, 0): "cobo", (1, 1): "cobi"},
}


def base14_for(fontname: str, flags: int) -> str:
    """Closest standard PDF font for an original font name + span flags."""
    n = fontname.lower()
    bold = bool(flags & 16) or any(w in n for w in ("bold", "black", "heavy", "semibold"))
    italic = bool(flags & 2) or "italic" in n or "oblique" in n
    if flags & 8 or "courier" in n or "mono" in n:
        family = "co"
    elif (flags & 4 or "times" in n or "serif" in n) and "sans" not in n:
        family = "ti"
    else:
        family = "he"
    return BASE14[family][(int(bold), int(italic))]


def load_embedded_font(doc, page, basefont):
    """Extract the page's embedded font matching basefont, if it's reusable."""
    for xref, ext, _ftype, bf, _name, _enc in page.get_fonts():
        if bf.split("+")[-1] != basefont or ext not in ("ttf", "otf", "cff"):
            continue
        try:
            _n, _e, _t, buf = doc.extract_font(xref)
            if buf:
                return {"buf": buf, "font": fitz.Font(fontbuffer=buf), "alias": f"E{xref}"}
        except Exception:
            pass
    return None


FONT_DIRS = [
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.expanduser(r"~\AppData\Local\Microsoft\Windows\Fonts"),
    "/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"),
    "/System/Library/Fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts"),
]
_system_fonts = None  # normalized font name -> file path


def _norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9]", "", name.split("+")[-1].lower())
    for suffix in ("mt", "ps", "regular", "book", "roman", "normal"):
        if n.endswith(suffix) and len(n) > len(suffix) + 2:
            n = n[: -len(suffix)]
    return n


def system_fonts():
    """Index installed TTF/OTF fonts by normalized name (built once, lazily)."""
    global _system_fonts
    if _system_fonts is None:
        _system_fonts = {}
        for d in FONT_DIRS:
            for root, _dirs, files in os.walk(d) if os.path.isdir(d) else ():
                for fn in files:
                    if not fn.lower().endswith((".ttf", ".otf")):
                        continue
                    path = os.path.join(root, fn)
                    try:
                        name = fitz.Font(fontfile=path).name
                    except Exception:
                        continue
                    for key in {_norm(name), _norm(os.path.splitext(fn)[0])}:
                        _system_fonts.setdefault(key, path)
    return _system_fonts


# Free fonts with the same letter widths as common Microsoft/Adobe fonts, so text
# still lines up on servers that don't have the originals installed (Linux VPS).
METRIC_TWINS = [
    ("timesnewroman", "liberationserif"), ("timesnew", "liberationserif"), ("couriernew", "liberationmono"),
    ("arialnarrow", "liberationsansnarrow"), ("helvetica", "liberationsans"),
    ("calibri", "carlito"), ("cambria", "caladea"), ("arial", "liberationsans"),
    ("times", "liberationserif"), ("courier", "liberationmono"),
]


def find_system_font(fontname):
    """The installed file for a PDF font name, or its metric-compatible twin."""
    fonts = system_fonts()
    key = _norm(fontname)
    if key in fonts:
        return fonts[key]
    for src, twin in METRIC_TWINS:
        if key.startswith(src):
            style = key[len(src):]
            style = style[2:] if style.startswith("ps") else style  # TimesNewRomanPS-Bold
            path = fonts.get(twin + style)
            if path:
                return path
    return None


def default_fallback():
    """A Unicode-capable font for characters the standard PDF fonts can't encode."""
    if FALLBACK_FONT:
        return FALLBACK_FONT
    fonts = system_fonts()
    for key in ("arial", "dejavusans", "notosans", "liberationsans", "helvetica", "segoeui"):
        if key in fonts:
            return fonts[key]
    return None




_font_objs = {}


def font_obj(kind, ref):
    key = (kind, ref if kind != "buf" else zlib.crc32(ref))
    if key not in _font_objs:
        if kind == "buf":
            _font_objs[key] = fitz.Font(fontbuffer=ref)
        elif kind == "file":
            _font_objs[key] = fitz.Font(fontfile=ref)
        else:
            _font_objs[key] = fitz.Font(ref)
    return _font_objs[key]


def file_alias(path):
    return "S%08x" % zlib.crc32(path.encode())


def choose_font(doc, font_page, style, text, cache):
    """
    Choose a font for `text` in `style`, most faithful first:
      1. the PDF's own embedded font, if it's complete and has every glyph
      2. the same font installed on this machine (e.g. Arial, Calibri, Times New Roman)
      3. the closest standard PDF font, for plain Latin-1 text
      4. a Unicode fallback font (FALLBACK_FONT, or Arial / DejaVu / Noto if installed)
    """
    needed = [ord(c) for c in text if not c.isspace()]

    def covers(f):
        return all(f.has_glyph(c) for c in needed)

    key = ("emb", font_page.xref, style["font"])
    if key not in cache:
        cache[key] = load_embedded_font(doc, font_page, style["font"])
    emb = cache[key]
    if emb and covers(emb["font"]) and metrics_match(emb["font"], font_page, style["font"], cache):
        return {"font": emb["font"], "kind": "buf", "ref": emb["buf"], "alias": emb["alias"]}

    path = find_system_font(style["font"])
    if path:
        f = font_obj("file", path)
        if covers(f) and metrics_match(f, font_page, style["font"], cache):
            return {"font": f, "kind": "file", "ref": path, "alias": file_alias(path)}

    name = base14_for(style["font"], style["flags"])
    fallback = default_fallback()
    if all(c < 256 for c in needed) or not fallback:
        return {"font": font_obj("base14", name), "kind": "base14", "ref": name, "alias": name}
    return {"font": font_obj("file", fallback), "kind": "file", "ref": fallback,
            "alias": file_alias(fallback)}


def pdf_advances(page, fontname, cache):
    """How far each character of `fontname` actually advances on this page (in em),
    measured from the extracted text."""
    key = ("adv", page.xref, fontname)
    if key not in cache:
        seen = {}
        raw = page.get_text("rawdict")
        for b in raw["blocks"]:
            for l in b.get("lines", []):
                for sp in l["spans"]:
                    if sp["font"] != fontname or sp["size"] <= 0:
                        continue
                    ch = sp["chars"]
                    for a_, b_ in zip(ch, ch[1:]):
                        if a_["c"].isspace() or b_["c"].isspace():
                            continue  # word spacing stretches spaces
                        adv = (b_["origin"][0] - a_["origin"][0]) / sp["size"]
                        if 0 < adv < 3:
                            seen.setdefault(a_["c"], []).append(adv)
        cache[key] = {c: median(v) for c, v in seen.items()}
    return cache[key]


def metrics_match(font, page, fontname, cache):
    """Do this font's character widths agree with how the PDF spaces its text?
    A constant difference (letter spacing) is fine; inconsistent widths mean the
    font would render with uneven gaps and wrong line widths."""
    adv = pdf_advances(page, fontname, cache)
    diffs = [a - font.glyph_advance(ord(c)) for c, a in adv.items() if font.has_glyph(ord(c))]
    if len(diffs) < 4:
        return True  # too little evidence to reject
    mid = median(diffs)
    return median(abs(d - mid) for d in diffs) < 0.02 and abs(mid) < 0.15


def use_font(page, choice, style, registered):
    """Make sure the chosen font is available on `page`; return its name there."""
    if choice["kind"] == "base14":
        return choice["alias"]
    reg = registered.setdefault(page.xref, set())
    if choice["alias"] not in reg:
        try:
            if choice["kind"] == "buf":
                page.insert_font(fontname=choice["alias"], fontbuffer=choice["ref"])
            else:
                page.insert_font(fontname=choice["alias"], fontfile=choice["ref"])
        except Exception:
            return base14_for(style["font"], style["flags"])
        reg.add(choice["alias"])
    return choice["alias"]


def write_text(page, x, y, text, style, choice, fc, size=None):
    fontname = use_font(page, choice, style, fc["registered"])
    page.insert_text((x, y), text, fontsize=size or style["size"], fontname=fontname,
                     color=int_to_rgb(style["color"]))


def new_font_context():
    return {"cache": {}, "registered": {}}


def wrap_text(text, font, size, width):
    """Greedy word wrap using real glyph widths."""
    out, cur = [], ""
    for w in text.split():
        while len(w) > 1 and font.text_length(w, fontsize=size) > width:
            cut = len(w)
            while cut > 1 and font.text_length(w[:cut], fontsize=size) > width:
                cut -= 1
            if cur:
                out.append(cur)
                cur = ""
            out.append(w[:cut])
            w = w[cut:]
        trial = f"{cur} {w}" if cur else w
        if font.text_length(trial, fontsize=size) <= width or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out or [""]


# --------------------------------------------------------------------------- #
# Extraction: one editable item per text line, with list markers detected
# --------------------------------------------------------------------------- #
BULLETS = set("•●○◦▪▫■□◆◇►▸▹▶➢➤➔✓✔✗✘❖‣⁃∙–—-*·")
LOOSE_BULLETS = set("–—-*·")  # need a space after them to count as bullets
NUM_RE = re.compile(r"^(\(?)([0-9]{1,3}|[A-Za-z]|[ivxlcdm]{2,6}|[IVXLCDM]{2,6})([.)])$")


def _style(span):
    return {"font": span["font"], "flags": span["flags"],
            "size": round(span["size"], 2), "color": span["color"]}


def _marker(chars, size):
    """Detect a bullet or number at the start of a line (from rawdict chars)."""
    idx = [i for i, c in enumerate(chars) if not c[0].isspace()]
    if not idx:
        return None, set()
    i0 = idx[0]
    c0, b0, o0, s0 = chars[i0]
    fn = s0["font"].lower()
    if (c0 in BULLETS or "\ue000" <= c0 <= "\uf8ff"
            or "wingding" in fn or "dingbat" in fn):
        kind, end = "glyph", i0
    else:
        end = i0
        while (end + 1 < len(chars) and not chars[end + 1][0].isspace()
               and chars[end + 1][1][0] - chars[end][1][2] < 0.25 * size):
            end += 1
        if not NUM_RE.match("".join(c[0] for c in chars[i0:end + 1])):
            return None, set()
        kind = "num"
    token = "".join(c[0] for c in chars[i0:end + 1])
    box = fitz.Rect(b0)
    for c in chars[i0 + 1:end + 1]:
        box |= fitz.Rect(c[1])
    m = {"kind": kind, "text": token, "bbox": r2(box), "origin": r2(o0), "style": _style(s0)}
    rest = [i for i in idx if i > end]
    if not rest:
        m["lone"] = True
        return m, set(range(i0, end + 1))
    k = rest[0]
    spaced = any(chars[i][0].isspace() for i in range(end + 1, k))
    gap = chars[k][1][0] - box.x1
    if kind == "glyph" and c0 in LOOSE_BULLETS and not spaced:
        return None, set()
    if not spaced and gap < 0.15 * size:
        return None, set()
    m["text_x"] = round(chars[k][1][0], 2)
    return m, set(range(i0, end + 1))


CELL_GAP = 0.9  # gaps wider than this (in em) separate table cells / columns


def _segments(chars, size):
    """Split one extracted line where a gap is far wider than a word space: MuPDF
    joins neighbouring table cells into one line when they're close together."""
    segs, cur, last = [], [], None
    for c in chars:
        if not c[0].isspace():
            if last is not None and c[1][0] - last > CELL_GAP * size:
                while cur and cur[-1][0].isspace():
                    cur.pop()
                segs.append(cur)
                cur = []
            last = c[1][2]
        if cur or not c[0].isspace():
            cur.append(c)
    if cur:
        segs.append(cur)
    return segs


def extract_lines(page):
    """Text lines (one per table cell / text run) with position, font, size, color,
    and any list marker."""
    lines = []
    if page.rotation:  # rotated pages are shown but not editable
        return lines
    raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_ACCURATE_BBOXES)
    for bno, block in enumerate(raw["blocks"]):
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            allchars = [(ch["c"], ch["bbox"], ch["origin"], s)
                        for s in line["spans"] for ch in s["chars"]]
            if not any(not c[0].isspace() for c in allchars):
                continue
            horizontal = abs(line["dir"][0] - 1) < 1e-3 and abs(line["dir"][1]) < 1e-3
            size0 = max(s["size"] for s in line["spans"])
            for chars in (_segments(allchars, size0) if horizontal else [allchars]):
                entry = _line_entry(chars, horizontal, bno)
                if entry:
                    lines.append(entry)
    return lines


def _line_entry(chars, horizontal, bno):
    solid = [c for c in chars if not c[0].isspace()]
    if not solid:
        return None
    counts = {}
    for c in solid:
        counts[id(c[3])] = counts.get(id(c[3]), 0) + 1
    main = max((c[3] for c in solid), key=lambda s: counts[id(s)])
    marker, mchars = _marker(chars, main["size"]) if horizontal else (None, set())
    if marker and not marker.get("lone"):
        # style of the item text, ignoring the bullet/number itself
        tc = {}
        for i, c in enumerate(chars):
            if i not in mchars and not c[0].isspace():
                tc[id(c[3])] = tc.get(id(c[3]), 0) + 1
        spans = {id(c[3]): c[3] for c in chars}
        main = spans[max(tc, key=tc.get)]
    first = solid[0]
    # "tbox": tight glyph extents (for cutting/moving); "bbox": full line box
    # from the fonts' ascender/descender (for the editing overlay).
    tb = fitz.Rect(solid[0][1])
    for c in solid[1:]:
        tb |= fitz.Rect(c[1])
    used = {id(c[3]): c[3] for c in solid}.values()
    y0 = min(s["origin"][1] - s["ascender"] * s["size"] for s in used)
    y1 = max(s["origin"][1] - s["descender"] * s["size"] for s in used)
    entry = {
        "text": "".join(c[0] for c in chars).strip(),
        "bbox": r2((tb.x0, min(y0, tb.y0), tb.x1, max(y1, tb.y1))),
        "tbox": r2(tb),
        "origin": r2(first[2]),
        "x": round(first[1][0], 2),
        "size": round(main["size"], 2),
        "color": main["color"],
        "font": main["font"],
        "flags": main["flags"],
        "editable": horizontal,
        "block": bno,
    }
    # styled runs, so edits can keep bold labels, italics and symbol fonts
    runs = []
    for c in chars:
        st = _style(c[3])
        if runs and runs[-1]["style"] == st:
            runs[-1]["text"] += c[0]
        else:
            runs.append({"text": c[0], "style": st})
    if len({json.dumps(r["style"], sort_keys=True) for r in runs if r["text"].strip()}) > 1:
        runs[0]["text"] = runs[0]["text"].lstrip()
        runs[-1]["text"] = runs[-1]["text"].rstrip()
        entry["runs"] = [r for r in runs if r["text"]]
    if marker:
        entry["_marker"] = marker
    return entry


def _num_class(token):
    m = NUM_RE.match(token)
    pre, body, suf = m.groups()
    t = "d" if body.isdigit() else ("l" if body.islower() else "u")
    return pre, t, suf, body


ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
         (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]


def roman_value(s):
    s = s.lower()
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    if not s or any(ch not in vals for ch in s):
        return None
    total = 0
    for i, ch in enumerate(s):
        v = vals[ch]
        total += -v if i + 1 < len(s) and vals[s[i + 1]] > v else v
    return total if to_roman(total) == s else None


def to_roman(n):
    out = ""
    for v, s in ROMAN:
        while n >= v:
            out, n = out + s, n - v
    return out


def to_alpha(n):
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(97 + r) + out
    return out


def fmt_number(num, value):
    s = num["style"]
    if s == "dec":
        body = str(value)
    elif s in ("roman", "ROMAN"):
        body = to_roman(value)
    else:
        body = to_alpha(value)
    if s in ("upper", "ROMAN"):
        body = body.upper()
    return num["prefix"] + body + num["suffix"]


def _numbering(tokens):
    """Numbering style and each item's value, e.g. ['iv.', 'v.'] -> roman 4, 5."""
    pre, t, suf, _ = _num_class(tokens[0])
    bodies = [_num_class(tok)[3] for tok in tokens]
    if t == "d":
        return {"style": "dec", "prefix": pre, "suffix": suf}, [int(b) for b in bodies]
    romans = [roman_value(b) for b in bodies]
    alphas = [ord(b.lower()) - 96 if len(b) == 1 else None for b in bodies]

    def consecutive(v):
        return None not in v and all(b - a == 1 for a, b in zip(v, v[1:]))

    use_roman = (None not in romans and (any(len(b) > 1 for b in bodies)
                 or (consecutive(romans) and not consecutive(alphas))
                 or (len(bodies) == 1 and bodies[0].lower() == "i")))
    style = ("roman" if use_roman else "lower") if t == "l" else ("ROMAN" if use_roman else "upper")
    return {"style": style, "prefix": pre, "suffix": suf}, (romans if use_roman else alphas)


def detect_lists(page, lines):
    """Group lines into bulleted/numbered lists and measure their geometry."""
    W = page.rect.width
    L = lines

    def base(i):
        return L[i]["origin"][1]

    boxes = [fitz.Rect(l["tbox"]) for l in L]   # built once: box() is called very often

    def box(i):
        return boxes[i]

    markers = {}
    lone = [i for i, l in enumerate(L) if l.get("_marker", {}).get("lone")]
    used_lone = set()

    for i, l in enumerate(L):
        m = l.get("_marker")
        if l["editable"] and m and not m.get("lone"):
            markers[i] = dict(m, marker_line=None)

    # bullet written as its own text line next to the item text
    for i, l in enumerate(L):
        if i in markers or not l["editable"] or l.get("_marker"):
            continue
        best = None
        for j in lone:
            if j in used_lone:
                continue
            mb = L[j]["_marker"]["bbox"]
            if (abs(base(j) - base(i)) < 0.35 * l["size"] and mb[2] <= l["x"] + 1
                    and l["x"] - mb[2] <= 4 * l["size"]):
                d = l["x"] - mb[2]
                if best is None or d < best[0]:
                    best = (d, j)
        if best:
            j = best[1]
            used_lone.add(j)
            markers[i] = dict(L[j]["_marker"], text_x=l["x"], marker_line=j)

    # bullet drawn as a vector shape
    drawings = page.get_drawings()
    area = page.rect.get_area()
    images = [fitz.Rect(x["bbox"]) for x in page.get_image_info()]
    # Only bullet-sized drawings can be (part of) a bullet. Pages with tables can have thousands
    # of drawings, so keep the small ones once, as plain numbers: (x0, y0, x1, y1, w, h, drawing).
    max_sz = max((l["size"] for l in L if l["editable"]), default=0)
    small = []
    for d in drawings:
        r = d["rect"]
        w, h = max(r.x1 - r.x0, 0), max(r.y1 - r.y0, 0)   # as Rect.width / height
        if w <= 0.9 * max_sz and h <= 0.9 * max_sz:
            small.append((r.x0, r.y0, r.x1, r.y1, w, h, d))
    for i, l in enumerate(L):
        if i in markers or not l["editable"] or l.get("_marker"):
            continue
        sz, bb, lx = l["size"], box(i), l["x"]
        best = None
        for x0, y0, x1, y1, w, h, d in small:
            if not (0.8 <= w <= 0.9 * sz and 0.8 <= h <= 0.9 * sz):
                continue
            cy = (y0 + y1) / 2
            if bb.y0 <= cy <= bb.y1 and x1 <= lx + 0.5 and lx - x1 <= 3 * sz:
                if best is None or lx - x1 < best[0]:
                    best = (lx - x1, d["rect"])
        if best:
            r0 = best[1]
            g = (r0.x0 - 0.5, r0.y0 - 0.5, r0.x1 + 0.5, r0.y1 + 0.5)
            parts = [d for x0, y0, x1, y1, w, h, d in small
                     if w > 0 and h > 0 and x0 < g[2] and g[0] < x1 and y0 < g[3] and g[1] < y1   # = Rect.intersects
                     and w <= 0.9 * sz and h <= 0.9 * sz and not is_background(d, area)]
            u = fitz.Rect(r0)
            for d in parts:
                u |= d["rect"]
            markers[i] = {"kind": "vector", "text": "", "bbox": r2(u), "origin": [u.x0, base(i)],
                          "style": None, "drawings": [ser_drawing(d) for d in parts],
                          "text_x": l["x"], "marker_line": None}

    member = set(markers) | used_lone
    assigned = set()

    def lines_between(top, bot, x0, x1, skip):
        out = []
        for j, l in enumerate(L):
            if j in skip:
                continue
            b = box(j)
            cy = (b.y0 + b.y1) / 2
            if top < cy < bot and b.x1 > x0 and b.x0 < x1:
                out.append(j)
        return out

    # items: marker line + wrapped continuation lines at the text indent
    items = []
    for i in sorted(markers, key=base):
        m, sz = markers[i], L[i]["size"]
        it = {"lines": [i], "m": m, "size": sz}
        cur = i
        while True:
            cand = None
            for j, l in enumerate(L):
                if j in member or j in assigned or j in it["lines"]:
                    continue
                db = base(j) - base(cur)
                if (abs(l["x"] - m["text_x"]) <= 2 and 0.3 * sz < db <= 2.2 * sz
                        and abs(l["size"] - sz) <= 1.5):
                    if cand is None or base(j) < base(cand):
                        cand = j
            if cand is None:
                break
            if lines_between(box(cur).y1 - 0.5, box(cand).y0 + 0.5, m["bbox"][0], box(cand).x1,
                             {cur, cand}):
                break
            it["lines"].append(cand)
            assigned.add(cand)
            cur = cand
        items.append(it)

    def compatible(a, b):
        ma, mb = a["m"], b["m"]
        if ma["kind"] != mb["kind"] or abs(ma["text_x"] - mb["text_x"]) > 2.5:
            return False
        if abs(a["size"] - b["size"]) > 0.8:
            return False
        if ma["kind"] == "glyph":
            return ma["text"] == mb["text"] and abs(ma["bbox"][0] - mb["bbox"][0]) <= 2.5
        if ma["kind"] == "vector":
            ra, rb = fitz.Rect(ma["bbox"]), fitz.Rect(mb["bbox"])
            return (abs(ra.x0 - rb.x0) <= 2.5 and abs(ra.width - rb.width) <= 1
                    and abs(ra.height - rb.height) <= 1)
        ca, cb = _num_class(ma["text"]), _num_class(mb["text"])
        return (ca[:3] == cb[:3] and (abs(ma["bbox"][0] - mb["bbox"][0]) <= 2.5
                                      or abs(ma["bbox"][2] - mb["bbox"][2]) <= 2.5))

    def inside(it, x):
        """Does a line starting at x belong under item `it`? Yes if it starts right of
        the midpoint between the item's bullet and its text (sub-bullets are often
        placed right at, or even slightly left of, the parent's text indent)."""
        return x > it["m"]["bbox"][0] + 0.5 * (it["m"]["text_x"] - it["m"]["bbox"][0])

    # chain items into lists; lines indented under an item between items are its nested content
    items.sort(key=lambda it: base(it["lines"][0]))
    used, lists = set(), []
    for s, start in enumerate(items):
        if s in used:
            continue
        chain = [start]
        start["nested"] = []
        used.add(s)
        while True:
            prev = chain[-1]
            nxt = None
            for t in range(s + 1, len(items)):
                if t in used or not compatible(prev, items[t]):
                    continue
                if base(items[t]["lines"][0]) <= base(prev["lines"][-1]):
                    continue
                if nxt is None or base(items[t]["lines"][0]) < base(items[nxt]["lines"][0]):
                    nxt = t
            if nxt is None:
                break
            cand = items[nxt]
            sz = prev["size"]
            x0 = min(prev["m"]["bbox"][0], cand["m"]["bbox"][0])
            x1 = max(box(j).x1 for j in prev["lines"] + cand["lines"])
            top = max(box(j).y1 for j in prev["lines"])
            bot = min(box(j).y0 for j in cand["lines"])
            skip = set(prev["lines"] + cand["lines"])
            skip |= {it["m"]["marker_line"] for it in (prev, cand) if it["m"]["marker_line"] is not None}
            between = sorted(lines_between(top - 0.5, bot + 0.5, x0, x1, skip),
                             key=lambda j: box(j).y0)
            if any(not inside(prev, L[j]["x"]) for j in between):
                break
            edges = [top] + [v for j in between for v in (box(j).y0, box(j).y1)] + [bot]
            if any(edges[q + 1] - edges[q] > 3 * sz for q in range(0, len(edges) - 1, 2)):
                break
            prev["nested"] = between
            cand["nested"] = []
            chain.append(cand)
            used.add(nxt)
        # nested content under the last item (deeper-indented lines right below it)
        last = chain[-1]
        sz = last["size"]
        cur = max(box(j).y1 for j in last["lines"])
        x0 = last["m"]["bbox"][0]
        x1 = max(box(j).x1 for j in last["lines"])
        taken = set(last["lines"]) | ({last["m"]["marker_line"]} - {None})
        below = sorted((j for j, l in enumerate(L)
                        if j not in taken and box(j).y0 >= cur - 0.5 and box(j).x1 > x0 and box(j).x0 < x1),
                       key=lambda j: box(j).y0)
        for j in below:
            if box(j).y0 - cur > 3 * sz or not inside(last, L[j]["x"]):
                break
            last["nested"].append(j)
            cur = max(cur, box(j).y1)
        lists.append(chain)

    result = []
    for chain in lists:
        kind = chain[0]["m"]["kind"]
        if kind == "vector" and len(chain) < 2:
            continue  # a single small shape next to text is too weak a signal
        sz = median(it["size"] for it in chain)

        pitches = [base(it["lines"][q + 1]) - base(it["lines"][q])
                   for it in chain for q in range(len(it["lines"]) - 1)]
        steps = [base(b["lines"][0]) - base(a["lines"][-1])
                 for a, b in zip(chain, chain[1:]) if not a["nested"]]
        page_leading = []
        for q in range(len(L) - 1):
            a, b = L[q], L[q + 1]
            d = b["origin"][1] - a["origin"][1]
            if (a["block"] == b["block"] and abs(a["size"] - sz) < 0.6
                    and abs(b["size"] - sz) < 0.6 and 0.9 * sz <= d <= 2 * sz):
                page_leading.append(d)
        if pitches:
            line_pitch = median(pitches)
        elif steps and median(steps) <= 1.5 * sz:
            line_pitch = median(steps)
        else:
            line_pitch = median(page_leading) if page_leading else 1.2 * sz
        item_step = median(steps) if steps else line_pitch  # Word's default: no extra gap
        nsteps = [base(b["lines"][0]) - max(base(j) for j in a["nested"])
                  for a, b in zip(chain, chain[1:]) if a["nested"]]
        step_after_nested = median(nsteps) if nsteps else item_step
        if not steps and nsteps:
            item_step = step_after_nested  # every item has details: best evidence we have

        text_x = median(it["m"]["text_x"] for it in chain)
        marker_x0 = min(it["m"]["bbox"][0] for it in chain)
        # Wrap width: the right edge of the text column the list sits in, i.e. the
        # widest text that overlaps the list's left edge (wrapped items, paragraphs).
        right = max(box(j).x1 for it in chain for j in it["lines"] + it["nested"])
        column = [l["bbox"][2] for l in L
                  if l["bbox"][0] <= text_x + 3 and l["bbox"][2] >= marker_x0 and l["editable"]]
        if column:
            right = max(right, max(column))
        right = min(right, W - 18)

        first = chain[0]
        # Item text style: what most of the list's text is set in (a bold lead-in
        # such as "Machine Learning:" must not make new items bold).
        votes = {}
        for it in chain:
            for j in it["lines"]:
                key = (L[j]["font"], L[j]["flags"], L[j]["size"], L[j]["color"])
                votes[key] = votes.get(key, 0) + len(L[j]["text"])
        f_, fl_, sz_, col_ = max(votes, key=votes.get)
        style = {"font": f_, "flags": fl_, "size": sz_, "color": col_}
        tm = first["m"]
        template = {"bbox": tm["bbox"], "origin": tm["origin"], "baseline": base(first["lines"][0]),
                    "text": tm["text"], "style": tm["style"], "drawings": tm.get("drawings")}
        if kind == "glyph":
            # clone region: the bullet's width at full line height (keep/remove decisions
            # use full-height character boxes, so a tight box could drop thin glyphs)
            ml = tm["marker_line"] if tm["marker_line"] is not None else first["lines"][0]
            lb = L[ml]["bbox"]
            # "keep": the bullet's column at full line height decides which characters
            # survive (thin glyphs like "–" have their center outside their ink box);
            # "clip": only the bullet's ink plus a hair, so nothing near it (a heading
            # rule, the item above) is copied along with it.
            tb = fitz.Rect(tm["bbox"])
            pad = max(0.6, 0.08 * sz)
            vis = fitz.Rect(tb.x0 - pad, tb.y0 - pad, tb.x1 + pad, tb.y1 + pad)
            template["clip"] = r2(vis)
            template["keep"] = r2(fitz.Rect(tb.x0 - 0.5, lb[1], tb.x1 + 0.5, lb[3]))
            template["clone"] = (not any(ink_rect(d).intersects(vis) and not is_background(d, area)
                                         for d in drawings)
                                 and not any(r.intersects(vis) for r in images))

        num, values = None, [None] * len(chain)
        if kind == "num":
            num, values = _numbering([it["m"]["text"] for it in chain])
            x0s = [it["m"]["bbox"][0] for it in chain]
            x1s = [it["m"]["bbox"][2] for it in chain]
            num["align"] = "right" if (max(x1s) - min(x1s)) < (max(x0s) - min(x0s)) else "left"
            template["x1"] = tm["bbox"][2]

        out_items = []
        for it, v in zip(chain, values):
            own = list(it["lines"]) + ([it["m"]["marker_line"]] if it["m"]["marker_line"] is not None else [])
            ext = own + it["nested"]
            u = fitz.Rect(it["m"]["bbox"])
            for j in ext:
                u |= box(j)
            out_items.append({
                "lines": it["lines"], "marker_line": it["m"]["marker_line"],
                "bbox": r2(u), "top": round(u.y0, 2), "bottom": round(u.y1, 2),
                "anchor_baseline": max(base(j) for j in ext),
                "own_baseline": max(base(j) for j in own),
                "nested": sorted(it["nested"], key=base),
                "value": v,
                "marker": {"bbox": it["m"]["bbox"], "origin": it["m"]["origin"],
                           "style": it["m"]["style"], "text": it["m"]["text"]},
            })
        result.append({
            "kind": kind, "marker_x0": round(marker_x0, 2), "text_x": round(text_x, 2),
            "right": round(right, 2), "line_pitch": round(line_pitch, 3),
            "item_step": round(item_step, 3), "step_after_nested": round(step_after_nested, 3),
            "style": style, "template": template,
            "num": num, "items": out_items,
        })

    # Which list sits nested under which item (e.g. ➢ details under a • job title).
    # New items can then get sub-points built from that sub-list's template.
    first_line = {}
    for mi, m in enumerate(result):
        first_line[m["items"][0]["lines"][0]] = mi
    for lst in result:
        lst.setdefault("parent", None)
    for li_, lst in enumerate(result):
        offsets = []
        for ki, it in enumerate(lst["items"]):
            it["child_list"] = None
            for j in it["nested"]:
                if j in first_line and result[first_line[j]] is not lst:
                    it["child_list"] = first_line[j]
                    result[first_line[j]]["parent"] = [li_, ki]
                    offsets.append(base(j) - it["own_baseline"])
                    break
        lst["child_offset"] = round(median(offsets), 3) if offsets else None
    return result


# --------------------------------------------------------------------------- #
# Headers, footers and body margins (used when content moves or flows)
# --------------------------------------------------------------------------- #
PAGENUM_RE = re.compile(r"^\s*(page\s+)?[-–]?\s*\d{1,4}\s*[-–]?(\s*(of|/)\s*\d{1,4})?\s*$", re.I)


def page_bands(doc, all_lines):
    """Detect repeating headers/footers and page numbers; compute body limits."""
    def norm(t):
        return re.sub(r"\d+", "#", t.strip().lower())

    tops, bots = {}, {}
    for p, lines in enumerate(all_lines):
        H = doc[p].rect.height
        for l in lines:
            if l["bbox"][3] <= 0.12 * H:
                tops.setdefault(norm(l["text"]), set()).add((p, round(l["bbox"][1])))
            if l["bbox"][1] >= 0.88 * H:
                bots.setdefault(norm(l["text"]), set()).add((p, round(l["bbox"][1])))

    def repeats(table, text, y):
        hits = {p for p, yy in table.get(norm(text), ()) if abs(yy - y) <= 3}
        return len(hits) >= 2

    info, top_margin, bottom_gaps = [], None, []
    for p, lines in enumerate(all_lines):
        page = doc[p]
        H = page.rect.height
        head, foot, body = [], [], []
        for l in lines:
            b = l["bbox"]
            if b[3] <= 0.12 * H and (PAGENUM_RE.match(l["text"]) or repeats(tops, l["text"], round(b[1]))):
                head.append(l)
            elif b[1] >= 0.88 * H and (PAGENUM_RE.match(l["text"]) or repeats(bots, l["text"], round(b[1]))):
                foot.append(l)
            else:
                body.append(fitz.Rect(b))
        body += [fitz.Rect(x["bbox"]) & page.rect for x in page.get_image_info()]
        body = [r for r in body if not r.is_empty]
        pi = {
            "header_bottom": round(max(l["bbox"][3] for l in head), 2) if head else None,
            "footer_top": round(min(l["bbox"][1] for l in foot), 2) if foot else None,
            "header_pagenum": any(PAGENUM_RE.match(l["text"]) for l in head),
            "footer_pagenum": any(PAGENUM_RE.match(l["text"]) for l in foot),
        }
        if body:
            t = min(r.y0 for r in body)
            top_margin = t if top_margin is None else min(top_margin, t)
            bottom_gaps.append(H - max(r.y1 for r in body))
        info.append(pi)

    top_margin = top_margin if top_margin is not None else 72.0
    bottom_margin = max(18.0, min([top_margin] + bottom_gaps))
    for p, pi in enumerate(info):
        H = doc[p].rect.height
        limit = H - bottom_margin
        if pi["footer_top"]:
            limit = min(limit, pi["footer_top"] - 4)
        body_top = top_margin
        if pi["header_bottom"]:
            body_top = max(body_top, pi["header_bottom"] + 4)
        pi["limit"], pi["body_top"] = round(limit, 2), round(body_top, 2)
    return info


# --------------------------------------------------------------------------- #
# Column boundaries: how far each line may extend before it must wrap
# --------------------------------------------------------------------------- #
def column_rights(page, lines):
    """
    Column bounds for every editable line: line["left"] and line["right"] are how far
    its text may extend (table cell or box edges, neighbouring columns, or the page's
    text area), judged from the whole paragraph's width. Also sets line["align"].
    """
    area = page.rect.get_area()
    vedges, spans = [], []  # vertical edges (x, y0, y1); horizontal extents (x0, x1, y0, y1)
    # Text boxes sorted by top edge, as plain tuples: a page can have thousands of drawings and
    # hundreds of lines, so each drawing is only compared with the lines at its height.
    tboxes = sorted((tuple(l["tbox"]) for l in lines), key=lambda t: t[1])
    tops = [t[1] for t in tboxes]
    tallest = max((t[3] - t[1] for t in tboxes), default=0)

    def sits_on_text(r):
        need = 0.5 * max(r.width * r.height, 1e-6)
        for k in range(bisect.bisect_left(tops, r.y0 - tallest), bisect.bisect_right(tops, r.y1)):
            x0, y0, x1, y1 = tboxes[k]
            w, h = min(r.x1, x1) - max(r.x0, x0), min(r.y1, y1) - max(r.y0, y0)
            if w > 0 and h > 0 and w * h > need:
                return True
        return False

    for d in page.get_drawings():
        if is_background(d, area):
            continue
        r = ink_rect(d)
        # boxes that mostly sit on text are highlights/inline backgrounds, not cells
        if r.height < 30 and sits_on_text(r):
            continue
        if r.width < 3 and r.height > 3:
            vedges.append((r.x0 + r.width / 2, r.y0, r.y1))
        elif r.width > 20:
            spans.append((r.x0, r.x1, r.y0, r.y1))
            if r.height > 3:
                vedges += [(r.x0, r.y0, r.y1), (r.x1, r.y0, r.y1)]
    body_left = min((l["x"] for l in lines), default=page.rect.x0 + 36)
    body_right = max((l["bbox"][2] for l in lines), default=page.rect.x1 - 36)

    # pass 1: each line on its own; pass 2: whole paragraphs (found using pass 1)
    extent = {i: ((l["x"], l["bbox"][2]), {i}) for i, l in enumerate(lines) if l["editable"]}
    _bounds(lines, extent, vedges, spans, body_left, body_right)
    extent = {}
    for i, l in enumerate(lines):
        if l["editable"] and i not in extent:
            para = _paragraph(lines, i)
            ext = (min(lines[j]["x"] for j in para), max(lines[j]["bbox"][2] for j in para))
            for j in para:
                extent[j] = (ext, set(para))
    _bounds(lines, extent, vedges, spans, body_left, body_right)
    line_alignment(lines)


class _YIndex:
    """Finds the items whose vertical range [y0, y1] may touch a band, without scanning them all.
    Only a pre-filter: callers still apply their exact test, in the original item order."""

    def __init__(self, ranges, lo, hi, step=16.0):
        self.step, self.lo, self.hi, self.bins = step, int(lo // step), int(hi // step), {}
        for k, (y0, y1) in enumerate(ranges):
            if not (y0 <= y1):                       # NaN or inverted: keep it findable everywhere
                y0, y1 = lo, hi
            y0, y1 = min(max(y0, lo), hi), max(min(y1, hi), lo)   # clamp (also handles infinities)
            for b in range(int(y0 // step), int(y1 // step) + 1):
                self.bins.setdefault(b, []).append(k)

    def near(self, a, b):
        found = set()
        for k in range(max(int(a // self.step), self.lo), min(int(b // self.step), self.hi) + 1):
            found.update(self.bins.get(k, ()))
        return sorted(found)


def _bounds(lines, extent, vedges, spans, body_left, body_right):

    def vov(o, tb, sz):
        return min(o["tbox"][3], tb[3]) - max(o["tbox"][1], tb[1]) > 0.3 * sz

    # Tables can mean thousands of edges and hundreds of lines: index them by height on the page.
    if not lines:
        return
    big = max(l["size"] for l in lines)
    lo = min(l["tbox"][1] for l in lines) - 4 * big - 1
    hi = max(l["tbox"][3] for l in lines) + 4 * big + 1
    v_idx = _YIndex([(y0, y1) for _, y0, y1 in vedges], lo, hi)
    s_idx = _YIndex([(s[2], s[3]) for s in spans], lo, hi)
    l_idx = _YIndex([(o["tbox"][1], o["tbox"][3]) for o in lines], lo, hi)

    for i, l in enumerate(lines):
        if i not in extent:
            l["left"], l["right"] = l["x"], l["bbox"][2]
            continue
        (px0, px1), para = extent[i]
        tb = l["tbox"]
        lx0, lx1 = l["x"], l["bbox"][2]
        cy, sz = (tb[1] + tb[3]) / 2, l["size"]
        rc, lc = [], []
        here_v = [vedges[k] for k in v_idx.near(cy, cy)]
        right_v = [x for x, y0, y1 in here_v if y0 <= cy <= y1 and x >= lx1 - 0.5]
        left_v = [x for x, y0, y1 in here_v if y0 <= cy <= y1 and x <= lx0 + 0.5]
        gap = lx0 - max(left_v) if left_v else None
        vpad = gap if gap is not None and gap <= 3 * sz else 4.0
        if right_v:
            rc.append(min(right_v) - vpad)
        if left_v:
            lc.append(max(left_v) + vpad)
        others = [lines[j] for j in l_idx.near(tb[1], tb[3]) if j not in para and vov(lines[j], tb, sz)]
        beside_r = [o["tbox"][0] for o in others if o["tbox"][0] >= lx1 - 0.5]
        beside_l = [o["tbox"][2] for o in others if o["tbox"][2] <= lx0 + 0.5]
        gutter = max(8.0, 0.8 * sz)
        if beside_r:
            rc.append(min(beside_r) - gutter)
        if beside_l:
            lc.append(max(beside_l) + gutter)
        # rules / cell borders / boxes spanning the whole paragraph bound its column
        near = [s for s in (spans[k] for k in s_idx.near(tb[1] - 4 * sz, tb[3] + 4 * sz))
                if s[0] <= px0 + 0.5 and s[1] >= px1 - 0.5
                and s[2] <= tb[3] + 4 * sz and s[3] >= tb[1] - 4 * sz]
        if near:
            x0s, x1s = min(near, key=lambda s: s[1] - s[0])[:2]
            pads = [o["x"] - x0s for o in lines if x0s - 0.5 <= o["x"] <= x1s
                    and abs((o["tbox"][1] + o["tbox"][3]) / 2 - cy) < 30 * sz]
            pad = min(pads) if pads else 4.0
            rc.append(x1s - pad)
            lc.append(x0s + pad)
        l["right"] = round(max(min(rc) if rc else body_right, px1), 2)
        l["left"] = round(min(max(lc) if lc else body_left, px0), 2)


def line_alignment(lines):
    """Left, centered or right-aligned, by comparing each line with the other lines
    of its column (same bounds, overlapping)."""
    for l in lines:
        l["align"] = "left"
    undecided = []
    for l in lines:
        if not l["editable"]:
            continue
        sz = l["size"]
        x0, x1 = l["tbox"][0], l["tbox"][2]
        c = (x0 + x1) / 2
        votes = {"left": 0, "center": 0, "right": 0}
        for o in lines:
            if o is l or not o["editable"] or abs(o["tbox"][1] - l["tbox"][1]) > 40 * sz:
                continue
            if abs(o["size"] - sz) > 0.5 or (o["flags"] & 16) != (l["flags"] & 16):
                continue  # headers are often aligned differently from the data below
            ox0, ox1 = o["tbox"][0], o["tbox"][2]
            if min(ox1, x1) <= max(ox0, x0):
                continue  # not in the same column
            if abs(o["right"] - l["right"]) > 2 * sz or abs(o["left"] - l["left"]) > 2 * sz:
                continue
            dl, dr, dc = abs(ox0 - x0), abs(ox1 - x1), abs((ox0 + ox1) / 2 - c)
            if dl < 2 and dc < 2:
                continue  # (nearly) the same extent says nothing
            if dl < 1:
                votes["left"] += 1
            elif dc < 1.5:
                votes["center"] += 1
            elif dr < 1:
                votes["right"] += 1
        best = max(votes, key=votes.get)
        if votes[best] > 0 and best != "left" and votes[best] >= votes["left"]:
            l["align"] = best
        elif not any(votes.values()):
            # alone in its column: judge by the room on each side within the column
            gl, gr = x0 - l["left"], l["right"] - x1
            tol = max(3.0, 0.015 * (l["right"] - l["left"]))
            if gl > 2 * sz and gr > 2 * sz and abs(gl - gr) < tol:
                l["align"] = "center"
            elif gr < tol and gl > 2 * sz:
                l["align"] = "right"
            elif gl < tol and gr > 2 * sz:
                l["align"] = "left"
            else:
                undecided.append(l)  # fills its column: no evidence either way
    # no evidence of its own: follow the other cells on the same table row
    for l in undecided:
        sz = l["size"]
        mates = [o["align"] for o in lines if o is not l and o not in undecided and o["editable"]
                 and abs(o["origin"][1] - l["origin"][1]) < 0.3 * sz]
        # only for centered tables; otherwise columns differ (text left, numbers right)
        if len(mates) >= 2 and mates.count("center") > len(mates) / 2:
            l["align"] = "center"


# --------------------------------------------------------------------------- #
# Applying text edits
# --------------------------------------------------------------------------- #
def _redact_text(page, rects):
    for r in rects:
        pad = r.height * 0.2  # stay clear of neighbouring lines
        page.add_redact_annot(fitz.Rect(r.x0, r.y0 + pad, r.x1, r.y1 - pad), fill=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE)


def _style_of(line):
    return {k: line[k] for k in ("font", "flags", "size", "color")}


def _right(page, line):
    return line.get("right", (page.rect.x1 if page is not None else 612) - 36)


def _avail(page, line):
    """Width a line's text may use, given its column and its alignment."""
    right, left = _right(page, line), line.get("left", line["x"])
    x0, x1 = line["tbox"][0], line["tbox"][2]
    align = line.get("align", "left")
    if align == "center":
        c = (x0 + x1) / 2
        return max(2 * min(c - left, right - c), x1 - x0)
    if align == "right":
        return max(x1 - left, x1 - x0)
    return right - line["x"]


def _place_x(line, width):
    """Where text of this width starts, keeping the line's alignment."""
    align = line.get("align", "left")
    if align == "center":
        return (line["tbox"][0] + line["tbox"][2]) / 2 - width / 2
    if align == "right":
        return line["tbox"][2] - width
    return line["origin"][0]


def _edit_runs(line, text):
    """Split new text into styled runs: unchanged beginning and end keep their
    original styles; typed text takes the style where it was typed."""
    base = _style_of(line)
    runs = line.get("runs")
    old = line["text"]
    if not runs or "".join(r["text"] for r in runs) != old:
        return [(text, base)]
    p = len(os.path.commonprefix([old, text]))
    s = len(os.path.commonprefix([old[p:][::-1], text[p:][::-1]]))
    style_at = []
    for r in runs:
        style_at += [r["style"]] * len(r["text"])
    here = style_at[min(p, len(old) - 1)] if p < len(old) else style_at[-1]
    if 0 < p < len(old) and old[p - 1] != " " and old[p:p + 1] in (" ", ""):
        here = style_at[p - 1]  # typing at the end of a word continues that word's style
    chars = [(ch, style_at[i]) for i, ch in enumerate(old[:p])]
    chars += [(ch, here) for ch in text[p:len(text) - s]]
    chars += [(ch, style_at[len(old) - s + i]) for i, ch in enumerate(old[len(old) - s:])]
    out = []
    for ch, st in chars:
        if out and out[-1][1] == st:
            out[-1] = (out[-1][0] + ch, st)
        else:
            out.append((ch, st))
    return out or [(text, base)]


def write_line(doc, page, line, text, fc, keep_styles=True):
    """Write text on a line's baseline with its alignment and (where the text is
    unchanged) its original mixed styles; shrinks only if even a single
    unbreakable piece can't fit its column."""
    runs = _edit_runs(line, text) if keep_styles else [(text, _style_of(line))]
    parts = []
    for t, st in runs:
        choice = choose_font(doc, page, st, t, fc["cache"])
        parts.append((t, st, choice, choice["font"].text_length(t, fontsize=st["size"])))
    width = sum(p[3] for p in parts)
    room = _avail(page, line)
    scale = room / width if width > room > 0 else 1.0
    if scale < 1:
        scale = max(scale, 4 / max(p[1]["size"] for p in parts))
    x = _place_x(line, width * scale)
    for t, st, choice, w in parts:
        write_text(page, x, line["origin"][1], t, st, choice, fc, size=st["size"] * scale)
        x += w * scale


def _fits(doc, page, line, text, fc):
    if "\n" in text:
        return False
    choice = choose_font(doc, page, _style_of(line), text, fc["cache"])
    return choice["font"].text_length(text, fontsize=line["size"]) <= _avail(page, line) + 0.5


def wrap_widths(text, font, size, widths):
    """
    Word wrap where line i may be widths[i] wide (the last width repeats).
    "\n" forces a new line (an empty segment gives a blank line), and a word too
    long for a whole line is broken across lines rather than shrunk.
    """
    out = []

    def width():
        return widths[min(len(out), len(widths) - 1)]

    for seg in text.split("\n"):
        cur = ""
        for word in seg.split():
            while len(word) > 1 and font.text_length(word, fontsize=size) > width():
                if cur:
                    out.append(cur)
                    cur = ""
                cut = len(word)
                while cut > 1 and font.text_length(word[:cut], fontsize=size) > width():
                    cut -= 1
                out.append(word[:cut])
                word = word[cut:]
            trial = f"{cur} {word}" if cur else word
            if not cur or font.text_length(trial, fontsize=size) <= width():
                cur = trial
            else:
                out.append(cur)
                cur = word
        out.append(cur)
    return out


_MARKER_START = re.compile(r"^\W|^\(?[0-9a-zA-Z]{1,3}[.)]\s")


def _basic_join(lines, A, B):
    """Could line B (directly below A) continue A's paragraph, judged on these two
    lines alone: spacing, column, alignment, and A being full."""
    sz = A["size"]
    if not (A["editable"] and B["editable"]) or abs(B["size"] - sz) > 0.6:
        return False
    if not any(ch.isalnum() for ch in A["text"] + B["text"]):
        return False  # stacked bullets or symbols, not a paragraph
    if not 0.5 * sz < B["origin"][1] - A["origin"][1] <= 1.5 * sz:
        return False
    ax0, ax1, bx0, bx1 = A["tbox"][0], A["tbox"][2], B["tbox"][0], B["tbox"][2]
    if min(ax1, bx1) <= max(ax0, bx0):
        return False
    # labels written as separate text on A's row ("Machine Learning:" + "Data …")
    row_left = min([o["x"] for o in lines if abs(o["origin"][1] - A["origin"][1]) < 0.3 * sz
                    and o["x"] <= A["x"] and A["x"] - o["bbox"][2] < 2 * sz] + [A["x"]])
    same_edge = (abs(A["x"] - B["x"]) <= 1.5 or abs(row_left - B["x"]) <= 1.5
                 or abs((ax0 + ax1) / 2 - (bx0 + bx1) / 2) <= 1.5 or abs(ax1 - bx1) <= 1.5)
    hanging = A["x"] < B["x"] <= A["x"] + 3 * sz and bool(_MARKER_START.match(A["text"]))
    if not (same_edge or hanging):
        return False
    # If B's first word would have fitted at the end of A, A ended its paragraph.
    words = B["text"].split()
    if not words:
        return False
    first = (bx1 - bx0) * len(words[0]) / max(len(B["text"]), 1)
    room = A.get("right", ax1) - A.get("left", A["x"])
    used = (ax1 - row_left) if abs(row_left - B["x"]) <= 1.5 else (ax1 - ax0)
    return used + 0.3 * sz + first > room


def _above(lines, o):
    """The line directly above line o in its column."""
    best = None
    for p in lines:
        if p is not o and p["origin"][1] < o["origin"][1] - 0.2 * o["size"] \
                and min(p["tbox"][2], o["tbox"][2]) > max(p["tbox"][0], o["tbox"][0]):
            if best is None or p["origin"][1] > best["origin"][1]:
                best = p
    return best


def _joins(lines, a, b):
    """Does line b (directly below line a) continue a's paragraph? Besides the two
    lines themselves, table rows are respected: a row boundary is shared by all
    columns, so if any column moving in step clearly ends there, so does this one;
    and a line where several other columns start fresh begins a new row."""
    A, B = lines[a], lines[b]
    if not _basic_join(lines, A, B):
        return False
    sz = A["size"]
    fresh = 0
    for o in lines:
        if o is B or abs(o["origin"][1] - B["origin"][1]) > 0.3 * sz:
            continue
        if min(o["tbox"][2], B["tbox"][2]) > max(o["tbox"][0], B["tbox"][0]):
            continue  # same column
        prev = _above(lines, o)
        if prev is None or abs(prev["origin"][1] - A["origin"][1]) > 0.3 * sz:
            fresh += 1
        elif not _basic_join(lines, prev, o):
            return False  # that column ends a cell here: row boundary
    return fresh < 2


def _paragraph(lines, idx):
    """Lines of the paragraph holding line idx, found from the layout: consecutive
    lines in one column, aligned alike, each line full before the next begins."""
    def neighbour(i, below):
        l = lines[i]
        best = None
        for j, o in enumerate(lines):
            if j == i or not o["editable"]:
                continue
            d = o["origin"][1] - l["origin"][1]
            if (d > 0.2 * l["size"]) if below else (d < -0.2 * l["size"]):
                if min(o["tbox"][2], l["tbox"][2]) > max(o["tbox"][0], l["tbox"][0]):
                    if best is None or abs(d) < abs(lines[best]["origin"][1] - l["origin"][1]):
                        best = j
        return best

    para = [idx]
    cur = idx
    while True:
        up = neighbour(cur, below=False)
        if up is None or up in para or not _joins(lines, up, cur):
            break
        para.insert(0, up)
        cur = up
    cur = idx
    while True:
        down = neighbour(cur, below=True)
        if down is None or down in para or not _joins(lines, cur, down):
            break
        para.append(down)
        cur = down
    return para


def _row_bottom(lines, para):
    """Bottom of the table row a paragraph sits in: cells of a row start on the same
    baseline, so it's the lowest bottom of paragraphs starting on that baseline."""
    first = lines[para[0]]
    sz = first["size"]
    bottom = max(lines[j]["tbox"][3] for j in para)
    for i, o in enumerate(lines):
        if i in para or not o["editable"] or abs(o["origin"][1] - first["origin"][1]) > 0.3 * sz:
            continue
        other = _paragraph(lines, i)
        if other[0] == i:
            bottom = max(bottom, max(lines[j]["tbox"][3] for j in other))
    return bottom


def _leading(lines, para):
    """Line spacing of a paragraph (or typical spacing for its size on the page)."""
    base = [lines[j]["origin"][1] for j in para]
    diffs = [b - a for a, b in zip(base, base[1:])]
    sz = lines[para[0]]["size"]
    if not diffs:
        diffs = [b["origin"][1] - a["origin"][1] for a, b in zip(lines, lines[1:])
                 if a["block"] == b["block"] and abs(a["size"] - sz) < 0.6
                 and abs(b["size"] - sz) < 0.6 and abs(a["x"] - b["x"]) <= 1.5
                 and 0.9 * sz <= b["origin"][1] - a["origin"][1] <= 2 * sz]
    return median(diffs) if diffs else 1.2 * sz


def _growth_insert(pno, lines, para, extra, style):
    """An insertion (run by the list engine) that adds `extra` wrapped lines below a
    paragraph, making room the same way new list items do."""
    last = lines[para[-1]]
    pitch = _leading(lines, para)
    item = {
        "lines": para, "marker_line": None, "nested": [], "value": None, "child_list": None,
        "top": min(lines[j]["tbox"][1] for j in para),
        "bottom": max(lines[j]["tbox"][3] for j in para),
        "anchor_baseline": last["origin"][1], "own_baseline": last["origin"][1],
        "row_bottom": _row_bottom(lines, para),
    }
    align = last.get("align", "left")
    width = _avail(None, last) if "right" in last else last["bbox"][2] - last["x"]
    center = (last["tbox"][0] + last["tbox"][2]) / 2
    x0 = {"center": center - width / 2, "right": last["tbox"][2] - width}.get(align, last["x"])
    item["bbox"] = [x0, item["top"], x0 + width, item["bottom"]]
    pseudo = {
        "kind": "none", "marker_x0": x0, "text_x": x0, "right": x0 + width,
        "align": align, "center": center, "anchor": last["tbox"][2],
        "line_pitch": pitch, "item_step": pitch,
        "step_after_nested": pitch, "child_offset": None, "style": style, "template": None,
        "num": None, "items": [item], "parent": None,
    }
    return {"page": pno, "list": -1, "after": 0, "_list": pseudo, "_item": item,
            "items": [{"text": t, "children": [], "blank": not t} for t in extra]}


def apply_text_edits(doc, info, edits, fc, warnings, growth):
    """
    Apply line edits. A line whose new text still fits its column is rewritten in
    place. Otherwise its paragraph is re-wrapped from that line at the same font
    size, and if it needs more lines than it had, room is made below it (a table
    row grows, content below moves down) via `growth` insertions.
    """
    per_page = {}
    for e in edits:
        text = "\n".join(part.strip() for part in str(e["text"]).replace("\r", "").split("\n"))
        per_page.setdefault(int(e["page"]), {})[int(e["line"])] = text.strip(" \t")
    for pno, changed in per_page.items():
        page = doc[pno]
        lines = info["pages"][pno]["lines"]
        changed = {i: t for i, t in changed.items() if lines[i]["editable"] and t != lines[i]["text"]}
        if not changed:
            continue
        redact, writes, done = set(), [], set()
        for idx in sorted(changed, key=lambda j: lines[j]["origin"][1]):
            if idx in done:
                continue
            para = _paragraph(lines, idx)
            done |= set(para)
            texts = [changed.get(j, lines[j]["text"]) for j in para]
            edited = [q for q, j in enumerate(para) if j in changed]
            if all(_fits(doc, page, lines[para[q]], texts[q], fc) for q in edited):
                for q in edited:
                    redact.add(para[q])
                    writes.append((lines[para[q]], texts[q]))
                continue
            first = edited[0]
            slots = para[first:]
            style = _style_of(lines[slots[0]])
            full = " ".join(t for t in texts[first:] if t.strip())
            choice = choose_font(doc, page, style, full, fc["cache"])
            widths = [_avail(page, lines[j]) for j in slots]
            wrapped = wrap_widths(full, choice["font"], style["size"], widths)
            redact |= set(slots)
            fit, extra = wrapped[:len(slots)], wrapped[len(slots):]
            if extra:
                ins = _growth_insert(pno, lines, para, extra, style)
                try:
                    ok = insert_items(doc, None, info, ins, {}, fc, [], dry_run=True)
                except InsertError:
                    ok = None
                if ok:
                    growth.append(ins)
                else:
                    fit = fit[:-1] + [" ".join(fit[-1:] + extra)]  # squeeze into the last line
                    warnings.append(f"Page {pno + 1}: no room to add lines below “{fit[0][:30]}…”, "
                                    "so the last line was set smaller to fit.")
            for j, t in zip(slots, fit + [""] * (len(slots) - len(fit))):
                writes.append((lines[j], t))
        if redact:
            _redact_text(page, [fitz.Rect(lines[j]["bbox"]) for j in redact])
        for line, t in writes:
            if t:  # empty text = delete the line
                write_line(doc, page, line, t, fc)


def renumber(doc, info, inserts, fc):
    """Shift the numbers of items that come after inserted items."""
    by_list = {}
    for ins in inserts:
        n = len(headings(normalize_items(ins.get("items"))))
        if n:
            by_list.setdefault((int(ins["page"]), int(ins["list"])), []).append((int(ins["after"]), n))
    for (pno, li), added in by_list.items():
        lst = info["pages"][pno]["lists"][li]
        if lst["kind"] != "num":
            continue
        changes = []
        for j, it in enumerate(lst["items"]):
            off = sum(n for a, n in added if a < j)
            if off and it["value"] is not None:
                changes.append((it, it["value"] + off))
        if not changes:
            continue
        page = doc[pno]
        for it, _ in changes:
            page.add_redact_annot(fitz.Rect(it["marker"]["bbox"]), fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        for it, v in changes:
            token = fmt_number(lst["num"], v)
            mstyle = it["marker"]["style"]
            choice = choose_font(doc, page, mstyle, token, fc["cache"])
            x = it["marker"]["origin"][0]
            if lst["num"]["align"] == "right":
                x = it["marker"]["bbox"][2] - choice["font"].text_length(token, fontsize=mstyle["size"])
            write_text(page, x, it["marker"]["origin"][1], token, mstyle, choice, fc)


# --------------------------------------------------------------------------- #
# Adding list items
# --------------------------------------------------------------------------- #
class InsertError(Exception):
    pass


SUB_PREFIX = re.compile(r"^\s*[-*>•◦▪➢–]\s*(?=\S)")


def normalize_items(raw):
    """
    New items as [{"text", "children": [...]}]. Accepts objects, or plain lines
    where a line starting with a dash or bullet ("-text", "- text", "•", "➢", "*")
    or indented is a detail (sub-point) of the item above it. Details typed before
    any heading become {"text": None, "children": [...]}: more details for the
    item the user clicked.
    """
    out = []
    for x in raw or []:
        if isinstance(x, dict):
            if x.get("blank"):
                out.append({"text": "", "children": [], "blank": True})
                continue
            text = str(x.get("text") or "").strip() or None
            kids = [str(c).strip() for c in x.get("children", []) if str(c).strip()]
            if text or kids:
                out.append({"text": text, "children": kids})
            continue
        line = str(x)
        if not line.strip():
            continue
        is_sub = bool(SUB_PREFIX.match(line)) or line[:1] in (" ", "\t")
        clean = SUB_PREFIX.sub("", line).strip()
        if is_sub:
            if not out:
                out.append({"text": None, "children": []})
            out[-1]["children"].append(clean)
        else:
            out.append({"text": clean, "children": []})
    return out


def headings(entries):
    return [e for e in entries if e["text"]]


def route_inserts(info, inserts):
    """
    Put typed structure where it belongs, whichever + the user clicked:
      * a heading with details added from inside a sub-list (e.g. a ➢ line under a
        job) is a new parent item, placed after the whole parent item;
      * inside a sub-list, lines are plain items of that sub-list.
    Insertions that end up at the same place are merged, in order.
    """
    merged, order = {}, []
    for ins in inserts:
        p, l, k = int(ins["page"]), int(ins["list"]), int(ins["after"])
        entries = normalize_items(ins.get("items"))
        lst = info["pages"][p]["lists"][l]
        if lst.get("parent"):
            if any(e["text"] and e["children"] for e in entries):
                l, k = lst["parent"]
                if entries and not entries[0]["text"]:
                    # details typed before the heading belong to the clicked sub-list
                    order_key = (p, int(ins["list"]), int(ins["after"]))
                    if order_key not in merged:
                        merged[order_key] = []
                        order.append(order_key)
                    merged[order_key] += [{"text": c, "children": []} for c in entries[0]["children"]]
                    entries = entries[1:]
            else:  # flat items in the sub-list
                entries = [{"text": t, "children": []}
                           for e in entries for t in ([e["text"]] if e["text"] else []) + e["children"]]
        key = (p, l, k)
        if key not in merged:
            merged[key] = []
            order.append(key)
        merged[key] += entries
    return [{"page": p, "list": l, "after": k, "items": merged[(p, l, k)]}
            for p, l, k in order if merged[(p, l, k)]]


def _child_list(pinfo, lst, item):
    """Template for sub-points: the sub-list under this item, else under any item
    of the list, else the list's own bullet one indent deeper."""
    idx = item.get("child_list")
    if idx is None:
        idx = next((it["child_list"] for it in lst["items"] if it.get("child_list") is not None), None)
    if idx is not None:
        return pinfo["lists"][idx], lst["child_offset"] or lst["line_pitch"]
    indent = lst["text_x"] - lst["marker_x0"]
    synth = dict(lst, marker_x0=lst["marker_x0"] + indent, text_x=lst["text_x"] + indent,
                 dx=indent, items=[dict(lst["items"][0], value=1 if lst["kind"] == "num" else None)])
    return synth, lst["item_step"]  # sub-points spaced like the list's own items


def _y2(model, pi, W, H):
    """Top of the fixed bottom band (footer): the first clean cut at or below the body limit."""
    cands = [pi["limit"] + 0.3] + ([pi["footer_top"] - 0.3] if pi["footer_top"] else [])
    for y in sorted(c for c in cands if c >= pi["limit"]):
        if crossing(model, y, 0, W)[0]:
            return y
    return H


def _para_gap(model, size):
    """Typical vertical gap between paragraphs on a page (used where pages join)."""
    rs = sorted(model["text"], key=lambda r: r.y0)
    gaps = [b.y0 - a.y1 for a, b in zip(rs, rs[1:])
            if _hov(a, b.x0, b.x1) and 0.45 * size < b.y0 - a.y1 < 4 * size]
    return median(gaps) if gaps else 0.6 * size


def _strip_links(doc, page):
    """Remove every link annotation from a page, whether stored as its own object
    or written inline in the /Annots array (those have no xref, so update_link and
    delete_link can't touch them)."""
    from pymupdf import mupdf
    pobj = fitz._as_pdf_page(page).obj()
    annots = mupdf.pdf_dict_get(pobj, mupdf.PDF_ENUM_NAME_Annots)
    if not mupdf.pdf_is_array(annots):
        return
    keep = mupdf.pdf_new_array(fitz._as_pdf_document(doc), mupdf.pdf_array_len(annots))
    for i in range(mupdf.pdf_array_len(annots)):
        a = mupdf.pdf_array_get(annots, i)
        sub = mupdf.pdf_dict_get(a, mupdf.PDF_ENUM_NAME_Subtype)
        if not mupdf.pdf_name_eq(sub, mupdf.PDF_ENUM_NAME_Link):
            mupdf.pdf_array_push(keep, a)
    mupdf.pdf_dict_put(pobj, mupdf.PDF_ENUM_NAME_Annots, keep)


def _move_annotations(doc, live, rebuilt, placements, warnings):
    """Links follow their content (to another page if it flowed there); other
    annotations on moved content are reported."""
    moves, stale = [], {}
    for x in rebuilt:
        page = live(x)
        mine = [(r, t, dy) for s_, r, t, dy in placements if s_ == x]

        def target(r):
            c = (r.tl + r.br) / 2
            for src, t, dy in mine:
                if src.contains(c):
                    return t, r + (0, dy, 0, dy)
            return x, r

        links = page.get_links()
        for link in links:
            t, nr = target(link["from"])
            nl = {k: v for k, v in link.items() if k not in ("xref", "id")}
            nl["from"] = nr
            moves.append((t, nl))
        if links:
            _strip_links(doc, page)
        for annot in list(page.annots()) + list(page.widgets()):
            t, nr = target(annot.rect)
            if t != x or nr != annot.rect:
                stale[x] = stale.get(x, 0) + 1
    for t, nl in moves:
        try:
            live(t).insert_link(nl)
        except Exception:
            warnings.append("A link on moved content couldn't be re-created.")
    for x, n in stale.items():
        warnings.append(f"Page {live(x).number + 1}: {n} comment(s) or form field(s) sit on "
                        "content that moved and weren't repositioned.")


def insert_items(doc, orig_bytes, info, ins, models, fc, warnings, dry_run=False):
    """
    Add new items after item `ins["after"]` of a list. Returns the method used
    ("write", "shift" or "flow"); raises InsertError if it can't be done cleanly.
    With dry_run=True it only plans (nothing on the page changes).
    """
    pno, li, k = int(ins["page"]), int(ins["list"]), int(ins["after"])
    entries = normalize_items(ins.get("items"))
    if not entries:
        return None
    pinfo = info["pages"][pno]
    lst = ins.get("_list") or pinfo["lists"][li]
    item = ins.get("_item") or lst["items"][k]
    page = doc[pno]
    pxref = page.xref
    W, H = page.rect.width, page.rect.height
    size = lst["style"]["size"]
    limit, body_top = pinfo["limit"], pinfo["body_top"]
    model = get_model(models, page)

    # --- lay out the new items (and their sub-points) with the list's rhythm --
    child, child_offset = _child_list(pinfo, lst, item)
    values = [None] * len(entries)
    if lst["kind"] == "num" and item["value"] is not None:
        earlier = sum(len(headings(normalize_items(o.get("items"))))
                      for o in info["_inserts"]
                      if (int(o["page"]), int(o["list"])) == (pno, li) and int(o["after"]) < k)
        q = 0
        for i_, e in enumerate(entries):
            if e["text"]:
                values[i_] = item["value"] + earlier + q + 1
                q += 1
    child_start = child["items"][0]["value"] if child["kind"] == "num" else None
    own_child = pinfo["lists"][item["child_list"]] if item.get("child_list") is not None else None

    layout, rel = [], 0.0
    step = lst["step_after_nested"] if item["nested"] else lst["item_step"]
    for n, e in enumerate(entries):
        if e["text"] or e.get("blank"):
            blocks = [(lst, e["text"], values[n], step)]
            first_gap, start = child_offset, child_start
        else:
            # more details for the clicked item: continue its own sub-list
            blocks = []
            first_gap = child["item_step"] if own_child is not None else child_offset
            start = (own_child["items"][-1]["value"] + 1
                     if own_child is not None and own_child["kind"] == "num"
                     and own_child["items"][-1]["value"] is not None else child_start)
        for c, t in enumerate(e["children"]):
            gap = first_gap if c == 0 else child["item_step"]
            val = start + c if start is not None else None
            blocks.append((child, t, val, gap))
        for L_, text, val, first_step in blocks:
            st = L_["style"]
            choice = choose_font(doc, page, st, text, fc["cache"])
            width = max(L_["right"] - L_["text_x"], st["size"] * 4)
            for i, piece in enumerate(wrap_text(text, choice["font"], st["size"], width)):
                rel += first_step if i == 0 else L_["line_pitch"]
                layout.append({"L": L_, "i": i, "text": piece, "rel": rel, "choice": choice,
                               "value": val})
        step = lst["step_after_nested"] if e["children"] else lst["item_step"]
    D = rel
    f0 = layout[0]["choice"]["font"]
    asc = max(f0.ascender, 0.7) * size
    desc = max(-f0.descender, 0.15) * size
    anchor, a = item["anchor_baseline"], item["bottom"]
    # A table cell growing inside a taller row only pushes content down by what
    # sticks out below the row; the cut goes under the whole row.
    if item.get("row_bottom") is not None:
        a_cut = max(a, item["row_bottom"])
        Dm = max(0.0, anchor + D + desc - a_cut)
    else:
        a_cut, Dm = a, D
    lx0, lx1 = lst["marker_x0"] - 1, max(lst["right"], child["right"]) + 1

    def render(positions, extra, live):
        """Write lines and markers. positions[i] = (page xref, baseline)."""
        for E, (tx, bl) in zip(layout, positions):
            tp, L_ = live(tx), E["L"]
            st = L_["style"]
            if E["text"]:
                w = E["choice"]["font"].text_length(E["text"], fontsize=st["size"])
                x = {"center": L_.get("center", 0) - w / 2,
                     "right": L_.get("anchor", 0) - w}.get(L_.get("align"), L_["text_x"])
                write_text(tp, x, bl, E["text"], st, E["choice"], fc)
                extra.append((tx, fitz.Rect(x, bl - 0.75 * st["size"], x + w, bl + 0.22 * st["size"])))
            if E["i"] == 0 and L_["kind"] != "none":
                _draw_marker(doc, orig_bytes, pno, live(pxref), L_, tp, bl, E["value"], fc)
                mb = fitz.Rect(L_["template"]["bbox"])
                dy, dx = bl - L_["template"]["baseline"], L_.get("dx", 0.0)
                extra.append((tx, mb + (dx, dy, dx, dy)))

    pgap = _para_gap(model, size)

    def gap_ok(top, used_to, x0, x1):
        """May whitespace below `top` be used down to `used_to`? Whitespace at the
        bottom of the page, yes. A gap before more content only if it stays generous
        (spacing between blocks is part of the design, not free space)."""
        below = [r.y0 for r in obstacle_rects(model)
                 if r.y0 >= top - EPS and _hov(r, x0, x1)
                 and not any(r is e["rect"] and e["patch"] and r.y0 < top for e in model["draw"])]
        if not below:
            return True
        yt = min(below)
        g = yt - top
        return yt - used_to >= max(3 * pgap, 0.5 * g)

    # --- (a) free space right below the item, in the list's own column -----
    fits_in_row = item.get("row_bottom") is not None and Dm <= 0.01 \
        and region_free(model, a + EPS, anchor + D + desc, lx0, lx1)
    if fits_in_row or (anchor + D + desc <= limit and region_free(model, a + EPS, anchor + D + desc, lx0, lx1)
                       and gap_ok(a, anchor + D + desc, lx0, lx1)):
        if dry_run:
            return "write"
        extra = []
        render([(pxref, anchor + L["rel"]) for L in layout], extra, _page_resolver(doc))
        for tx, r in extra:
            models[tx]["text"].append(r)
        ins["_moved"] = 0.0
        return "write"

    # --- where can the page be cut, just below the item? --------------------
    Y2 = _y2(model, pinfo, W, H)
    a = a_cut
    if k + 1 < len(lst["items"]):
        b = lst["items"][k + 1]["top"]
    else:
        tops = [r.y0 for r in obstacle_rects(model) if r.y0 >= a + EPS and _hov(r, lx0, lx1)]
        b = min(tops) if tops else Y2
    b = min(b, Y2)
    cands = {a + 0.3, (a + b) / 2} | {r.y1 + 0.3 for r in obstacle_rects(model) if a <= r.y1 <= b}
    c_cands = sorted(c for c in cands if a <= c <= max(b, a + 0.3))

    def find_E(C, x0, x1):
        ys = sorted({C} | {r.y1 + 0.3 for r in obstacle_rects(model) if r.y1 + 0.3 >= C})
        for E in ys:
            if E + Dm > limit + 0.01:
                return None
            if (region_free(model, E, E + Dm, x0, x1) and crossing(model, E, x0, x1)[0]
                    and gap_ok(E, E + Dm, x0, x1)):
                return E
        return None

    # Is the list in a column with other content beside it? Then only that
    # column may move; shifting the full width would tear the other columns.
    cx0, cx1 = column_range(model, lx0, lx1, a, Y2, W)
    in_column = any(r.y1 > a + EPS and r.y0 < Y2 - EPS and not _hov(r, cx0, cx1)
                    for r in model["text"] + model["img"])
    if not in_column:
        cx0, cx1 = 0.0, W

    plan = None
    for C in c_cands:  # (b) move only what's between the item and free space below
        ok, patch = crossing(model, C, cx0, cx1)
        if ok:
            E = find_E(C, cx0, cx1)
            if E is not None:
                plan = ("shift", C, E, cx0, cx1, patch)
                break
    if plan is None and in_column:
        raise InsertError(
            f"Page {pno + 1}: couldn't add items after “{_item_label(info, pno, item)}”. "
            "The list is in a column that's already full, and content can't flow between "
            "columns. Shorten the new items or remove something below the list.")
    if plan is None:   # (c) everything below moves down and reflows into the next pages
        for C in c_cands:
            ok, patch = crossing(model, C, 0, W)
            if ok:
                plan = ("flow", C, patch)
                break
    if plan is None:
        raise InsertError(
            f"Page {pno + 1}: couldn't add items after “{_item_label(info, pno, item)}”. "
            "There's no free space below it, and the content beside it can't be moved "
            "without cutting through it (for example a full multi-column page, or an image beside the list).")
    if dry_run:
        return plan[0]

    # --- plan the rebuild -----------------------------------------------------
    src_doc = fitz.open("pdf", doc.tobytes())  # snapshot, opened once for all pages
    snap = {doc.page_xref(i): i for i in range(doc.page_count)}
    slicers = {}

    def slicer(xref):
        i = snap[xref]
        if i not in slicers:
            slicers[i] = Slicer(src_doc, i)
        return slicers[i]

    placements = []   # (source page xref, source rect, target page xref, dy)
    rebuilt = [pxref]  # pages whose content is replaced
    patch_ops, positions = [], []

    if plan[0] == "shift":
        _, C, E, x0, x1, patch = plan
        if x0 <= 0.01 and x1 >= W - 0.01:
            regions = [(fitz.Rect(0, 0, W, C), 0), (fitz.Rect(0, C, W, E), Dm),
                       (fitz.Rect(0, E + Dm, W, H), 0)]
        else:
            regions = [(fitz.Rect(0, 0, W, C), 0), (fitz.Rect(0, C, x0, Y2), 0),
                       (fitz.Rect(x1, C, W, Y2), 0), (fitz.Rect(x0, C, x1, E), Dm),
                       (fitz.Rect(x0, E + Dm, x1, Y2), 0), (fitz.Rect(0, Y2, W, H), 0)]
        placements = [(pxref, r, pxref, dy) for r, dy in regions]
        patch_ops = [(pxref, C, C, C + Dm, x0, x1, patch)]
        positions = [(pxref, anchor + L["rel"]) for L in layout]
    else:
        _, C, patch = plan
        placements.append((pxref, fitz.Rect(0, 0, W, C), pxref, 0))
        if Y2 < H:
            placements.append((pxref, fitz.Rect(0, Y2, W, H), pxref, 0))
        gap = pgap
        tgt = {"xref": pxref, "limit": limit, "top": body_top, "fresh": False}
        budget = [60]      # pages that may be reflowed before a page is added instead
        queue = []         # content still to place: bands of source pages, in order

        def segment(xref, y0, y1):
            m = models[xref]
            obst = [(r, True) for r in m["text"] + m["img"] if r.y1 > y0 and r.y0 < y1]
            obst += [(e["rect"], False) for e in m["draw"] if e["rect"].y1 > y0 and e["rect"].y0 < y1]
            return {"src": xref, "y0": y0, "y1": y1, "obst": obst}

        def new_page_after(xref):
            idx = next(i for i in range(doc.page_count) if doc.page_xref(i) == xref)
            np = doc.new_page(idx + 1, width=W, height=H)
            for spec in model["bg"]:
                redraw(np, spec)
            m = {"text": [], "img": [], "draw": [], "bg": list(model["bg"])}
            band = {**pinfo, "header_bottom": None, "footer_top": None,
                    "header_pagenum": False, "footer_pagenum": False}
            parts = []
            if pinfo["header_bottom"] and not pinfo["header_pagenum"]:
                parts.append(fitz.Rect(0, 0, W, pinfo["header_bottom"] + 0.3))
                band["header_bottom"] = pinfo["header_bottom"]
            if pinfo["footer_top"] and not pinfo["footer_pagenum"]:
                parts.append(fitz.Rect(0, pinfo["footer_top"] - 0.3, W, H))
                band["footer_top"] = pinfo["footer_top"]
            for r in parts:
                slicer(pxref).place(np, r, 0)
                m["text"] += [x for x in model["text"] if r.contains(x)]
                m["img"] += [x for x in model["img"] if r.contains(x)]
                m["draw"] += [e for e in model["draw"] if r.contains(e["rect"])]
            m["band"] = band
            models[np.xref] = m
            return np.xref

        def next_target():
            """Continue on the next page: reflow into it when possible, else add a page."""
            idx = next(i for i in range(doc.page_count) if doc.page_xref(i) == tgt["xref"])
            if budget[0] > 0 and idx + 1 < doc.page_count:
                nx = doc.page_xref(idx + 1)
                npi = info["_by_xref"].get(nx) or models.get(nx, {}).get("band")
                npg = doc[idx + 1]
                if (npi and nx in snap and not npg.rotation and abs(npg.rect.width - W) < 0.5
                        and abs(npg.rect.height - H) < 0.5):
                    nm = get_model(models, npg)
                    hb = npi["header_bottom"] + 0.3 if npi["header_bottom"] else 0.0
                    y2 = _y2(nm, npi, W, H)
                    if hb < y2 and (hb == 0 or crossing(nm, hb, 0, W)[0]):
                        budget[0] -= 1
                        rebuilt.append(nx)
                        if hb > 0:
                            placements.append((nx, fitz.Rect(0, 0, W, hb), nx, 0))
                        if y2 < H:
                            placements.append((nx, fitz.Rect(0, y2, W, H), nx, 0))
                        queue.append(segment(nx, hb, y2))
                        tgt.update(xref=nx, limit=npi["limit"], top=npi["body_top"], fresh=True)
                        return
            nx = new_page_after(tgt["xref"])
            tgt.update(xref=nx, limit=pinfo["limit"], top=pinfo["body_top"], fresh=True)

        # 1. the new lines
        prev, last_rel = anchor, 0.0
        for L in layout:
            bl = prev + (L["rel"] - last_rel)
            if bl + desc > tgt["limit"]:
                next_target()
                bl = tgt["top"] + asc
            positions.append((tgt["xref"], bl))
            tgt["fresh"] = False
            prev, last_rel = bl, L["rel"]
        off = prev - anchor - (D - Dm)  # content below keeps its distance to the new text
        if tgt["xref"] == pxref:
            patch_ops.append((pxref, C, C, min(C + off, limit), 0, W, patch))
        else:
            patch_ops.append((pxref, C, C, limit, 0, W, patch))
            patch_ops.append((tgt["xref"], C, tgt["top"], C + off, 0, W, patch))

        # 2. the content below, then each reflowed page's own content
        queue.insert(0, segment(pxref, C, Y2))
        cursor, continuing = None, True
        guard = 0
        while queue:
            seg = queue.pop(0)
            s = seg["y0"]
            live_obst = [(r, t) for r, t in seg["obst"] if r.y1 > s + EPS]
            if not live_obst:
                continuing = False
                continue
            if not continuing:
                first = min(max(r.y0, s) for r, _ in live_obst)
                base_y = tgt["top"] if tgt["fresh"] or cursor is None else cursor + gap
                off = base_y - first
            continuing = False
            while True:
                guard += 1
                if guard > 2000:
                    raise InsertError(f"Page {pno + 1}: the content below couldn't be reflowed.")
                rem = [(r, t) for r, t in seg["obst"] if r.y1 > s + EPS]
                if not rem:
                    break
                end = max(r.y1 for r, _ in rem)
                if end + off <= tgt["limit"] + 0.01:
                    placements.append((seg["src"], fitz.Rect(0, s, W, min(seg["y1"], end + 0.3)),
                                       tgt["xref"], off))
                    cursor, tgt["fresh"] = end + off, False
                    break
                max_s = tgt["limit"] - off
                ys = sorted({r.y1 + 0.3 for r, _ in rem} | {r.y0 - 0.3 for r, _ in rem}, reverse=True)
                ys = [y for y in ys if s + 0.5 < y <= max_s]

                def clean(y, strict):
                    return not any(r.y0 < y - EPS and r.y1 > y + EPS and (strict or t)
                                   for r, t in rem)

                S = next((y for y in ys if clean(y, True)), None)
                if S is None:
                    S = next((y for y in ys if clean(y, False)), None)
                if S is None:
                    if tgt["fresh"]:
                        S = max_s
                        warnings.append(f"Page {pno + 1}: an element taller than a page had to be split.")
                    else:
                        S = s
                if S > s + 0.5:
                    placements.append((seg["src"], fitz.Rect(0, s, W, S), tgt["xref"], off))
                s = S
                next_target()
                first = min(max(r.y0, s) for r, _ in seg["obst"] if r.y1 > s + EPS)
                off = tgt["top"] - first

    # --- rebuild: backgrounds, slices, gap patches, new text -----------------
    live = _page_resolver(doc)
    olds = {x: models[x] for x in rebuilt}
    for x in rebuilt:
        pg = live(x)
        wipe_page(doc, pg)
        for spec in olds[x]["bg"]:
            redraw(pg, spec)
    for src, rect, tx, dy in placements:
        slicer(src).place(live(tx), rect, dy)
    for tx, cut, y0, y1, x0, x1, entries in patch_ops:
        draw_patch(live(tx), entries, cut, y0, y1, x0, x1)
    extra = []
    render(positions, extra, live)
    _move_annotations(doc, live, rebuilt, placements, warnings)

    # --- keep the layout models in sync --------------------------------------
    out = {}

    def bucket(xref):
        return out.setdefault(xref, {"text": [], "img": [], "draw": []})

    for x in rebuilt:
        mine = [(r, t, dy) for s_, r, t, dy in placements if s_ == x]
        for kind in ("text", "img"):
            for r in olds[x][kind]:
                c = (r.tl + r.br) / 2
                for src, t, dy in mine:
                    if src.contains(c):
                        bucket(t)[kind].append(r + (0, dy, 0, dy))
                        break
        for e in olds[x]["draw"]:
            pieces = {}
            for src, t, dy in mine:
                part = e["rect"] & src
                if part.x0 > part.x1 or part.y0 > part.y1 or (part.width <= 0 and part.height <= 0):
                    continue
                moved = part + (0, dy, 0, dy)
                pieces[t] = pieces[t] | moved if t in pieces else moved
            if x == pxref:
                for tx, cut, y0, y1, x0, x1, entries in patch_ops:
                    if any(e is p for p in entries) and y1 > y0:
                        strip = fitz.Rect(max(e["rect"].x0, x0), y0, min(e["rect"].x1, x1), y1)
                        pieces[tx] = pieces[tx] | strip if tx in pieces else strip
            for t, r in pieces.items():
                bucket(t)["draw"].append(moved_entry(e, r))
    for tx, r in extra:
        bucket(tx)["text"].append(r)
    for x in rebuilt:
        keep = {kk: v for kk, v in olds[x].items() if kk not in ("text", "img", "draw")}
        models[x] = {**{"text": [], "img": [], "draw": []}, **out.pop(x, {}), **keep}
    for x, m in out.items():
        for kind in ("text", "img", "draw"):
            models[x][kind] += m[kind]
    ins["_moved"] = Dm
    return plan[0]


def _page_resolver(doc):
    """Page objects go stale when pages are inserted; look them up by xref."""
    def live(xref):
        for i in range(doc.page_count):
            if doc.page_xref(i) == xref:
                return doc[i]
        raise KeyError(xref)
    return live


def _item_label(info, pno, item):
    t = info["pages"][pno]["lines"][item["lines"][0]]["text"]
    return t if len(t) <= 40 else t[:38] + "…"


def _draw_marker(doc, orig_bytes, pno, font_page, lst, tp, baseline, value, fc):
    t = lst["template"]
    dy, dx = baseline - t["baseline"], lst.get("dx", 0.0)
    if lst["kind"] == "vector":
        for spec in t["drawings"]:
            redraw(tp, spec, dy, dx)
        return
    mstyle = t["style"]
    if lst["kind"] == "glyph":
        if t.get("clone"):  # exact copy of the original bullet
            Slicer(orig_bytes, pno).place(tp, fitz.Rect(t["clip"]), dy, dx, keep=t.get("keep"))
            return
        token = t["text"] if not ("\ue000" <= t["text"] <= "\uf8ff") else "•"
    else:
        token = fmt_number(lst["num"], value) if value is not None else t["text"]
    x = t["origin"][0] + dx
    choice = choose_font(doc, font_page, mstyle, token, fc["cache"])
    if lst["kind"] == "num" and lst["num"]["align"] == "right":
        x = t["x1"] + dx - choice["font"].text_length(token, fontsize=mstyle["size"])
    write_text(tp, x, t["origin"][1] + dy, token, mstyle, choice, fc)


ADD_FONTS = {"sans": "sans-serif", "serif": "serif", "mono": "monospace"}


def font_family(name):
    """'ABCDEF+Carlito-Bold' -> 'Carlito', 'TimesNewRomanPS-BoldMT' -> 'TimesNewRoman'."""
    fam = re.split(r"[-,]", name.split("+")[-1])[0]
    return re.sub(r"(PSMT|MT|PS)$", "", fam) or fam


def _face(line):
    n = line["font"].lower()
    return (bool(line["flags"] & 16) or any(w in n for w in ("bold", "black", "heavy")),
            bool(line["flags"] & 2) or "italic" in n or "oblique" in n)


def doc_font_choice(doc, info, src, bold, italic, text, fc):
    """The document's own font for new text: the same family as line `src`
    ((page, line)), in the wanted bold/italic face if the document uses it anywhere."""
    sp, sl = src
    line = info["pages"][sp]["lines"][sl]
    fam = font_family(line["font"])
    for pno, p in enumerate(info["pages"]):
        for l in p["lines"]:
            if font_family(l["font"]) == fam and _face(l) == (bold, italic):
                style = {"font": l["font"], "flags": l["flags"]}
                return choose_font(doc, doc[pno], style, text, fc["cache"]), style
    # The document never uses this face: try the installed font, else the closest standard one.
    flags = (line["flags"] & ~18) | (16 if bold else 0) | (2 if italic else 0)
    suffix = ("Bold" if bold else "") + ("Italic" if italic else "")
    style = {"font": f"{fam}-{suffix}" if suffix else fam, "flags": flags}
    path = find_system_font(style["font"])
    if path:
        return {"font": font_obj("file", path), "kind": "file", "ref": path, "alias": file_alias(path)}, style
    name = base14_for(fam, flags)
    return {"font": font_obj("base14", name), "kind": "base14", "ref": name, "alias": name}, style


def _doc_text(doc, info, page, rect, a, fc, warnings):
    """Write a text box in the document's own font, wrapped like the editor preview
    (CSS line-height 1.2), shrinking the text only if it would run off the page."""
    choice, style = doc_font_choice(doc, info, a["src"], a["bold"], a["italic"], a["text"], fc)
    style = {**style, "color": int(a["color"][1:], 16)}
    font, size = choice["font"], a["size"]
    asc, desc = font.ascender, font.descender

    def layout(sz):
        out = []
        for para in a["text"].split("\n"):
            out += wrap_text(para, font, sz, rect.width) if para.strip() else [""]
        return out

    lines = layout(size)
    room = page.rect.y1 - rect.y0
    while len(lines) * size * 1.2 > room and size > 4:
        size = max(4, size * 0.92)
        lines = layout(size)
    if size < a["size"]:
        warnings.append(f"Page {a['page'] + 1}: a text box ran off the page, so its text was made smaller to fit.")
    lh = size * 1.2
    for n, text in enumerate(lines):
        if not text.strip():
            continue
        x = rect.x0
        if a["align"] != "left":
            spare = rect.width - font.text_length(text, fontsize=size)
            x += spare / 2 if a["align"] == "center" else spare
        baseline = rect.y0 + n * lh + (lh - (asc - desc) * size) / 2 + asc * size
        write_text(page, x, baseline, text, style, choice, fc, size)


def apply_additions(doc, info, additions, fc, warnings):
    """Place new content on top of the pages. Nothing on the page moves.
      additions: [{"type": "text"|"image"|"signature", "page", "rect": [x0,y0,x1,y1], ...}]
        text:              "text", "font" (doc|sans|serif|mono), "src" (page, line) when font is doc,
                           "size", "color" "#rrggbb", "bold", "italic", "align"
        image / signature: "image" (the bytes, already looked up by the caller)
    Coordinates are PDF points from the page's top-left, the same as /analyze."""
    import html
    for a in additions:
        page = doc[a["page"]]
        if page.rotation:
            warnings.append(f"Page {a['page'] + 1} is turned sideways, so content added to it was skipped.")
            continue
        rect = fitz.Rect(a["rect"]) & page.rect
        if rect.is_empty or rect.width < 2 or rect.height < 2:
            warnings.append(f"Page {a['page'] + 1}: something added outside the page was skipped.")
            continue
        if a["type"] == "text" and a.get("font") == "doc":
            _doc_text(doc, info, page, rect, a, fc, warnings)
        elif a["type"] == "text":
            body = "<br>".join(html.escape(s) for s in a["text"].split("\n"))
            css = (f"font-family:{ADD_FONTS.get(a.get('font'), 'sans-serif')};font-size:{a['size']}pt;"
                   f"line-height:1.2;color:{a['color']};text-align:{a['align']};"
                   f"font-weight:{'bold' if a['bold'] else 'normal'};font-style:{'italic' if a['italic'] else 'normal'}")
            # Grow the box downwards a little if needed, then shrink the text as a last resort.
            room = fitz.Rect(rect.x0, rect.y0, rect.x1, min(page.rect.y1, rect.y1 + a["size"] * 1.2))
            spare, scale = page.insert_htmlbox(room, f'<div style="{css}">{body}</div>', scale_low=0)
            if scale < 0.98:
                warnings.append(f"Page {a['page'] + 1}: a text box was too small for its text, so the text was made smaller to fit.")
        else:
            try:
                page.insert_image(rect, stream=a["image"], keep_proportion=True)
            except Exception:
                warnings.append(f"Page {a['page'] + 1}: an image couldn't be read, so it was skipped.")
    return warnings


def is_digitally_signed(doc):
    """True when the PDF carries signature fields: editing invalidates them."""
    try:
        return doc.get_sigflags() > 0
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Existing images (remove / move / replace) and erased areas
# --------------------------------------------------------------------------- #
def page_images(page):
    """Images drawn on a page, in drawing order: [{"bbox", "xref", "full"}]; an image's id is its index.
    "full" = it covers most of the page (a scan or a background), so it isn't offered for clicking.
    Pages turned sideways get none, like the rest of their editing."""
    if page.rotation:
        return []
    area, out = page.rect.get_area(), []
    for im in page.get_image_info(xrefs=True):
        r = fitz.Rect(im["bbox"]) & page.rect
        if r.is_empty or r.width < 3 or r.height < 3:
            continue
        out.append({"bbox": r2(r), "xref": int(im.get("xref") or 0), "full": r.get_area() >= 0.85 * area})
    return out


def image_stream(doc, xref):
    """The picture of an image object as bytes insert_image accepts, keeping its transparency."""
    try:
        meta = doc.extract_image(xref) or {}
    except Exception:
        meta = {}
    if meta.get("image") and not meta.get("smask") and meta.get("ext") in ("png", "jpeg", "jpg"):
        return meta["image"]                       # the original file: no quality loss
    pix = fitz.Pixmap(doc, xref)
    if meta.get("smask"):
        try:
            pix = fitz.Pixmap(pix, fitz.Pixmap(doc, meta["smask"]))
        except Exception:
            pass                                   # mask of another size: keep the picture without it
    if pix.colorspace and pix.colorspace.n > 3:     # CMYK -> RGB for PNG
        pix = fitz.Pixmap(fitz.csRGB, pix)
    return pix.tobytes("png")


def apply_objects(doc, info, objects, erase, warnings):
    """Remove existing images and erase areas, before any other change.
      objects: [{"page", "index", "action": "remove"|"move"|"replace", "allPages",
                 "to_page", "rect" (new place, move/replace), "image" (bytes, replace)}]
      erase:   [{"page", "rect", "text": also remove text}]
    An image is removed for real, not covered. When every copy of a picture goes, the picture
    itself is blanked (exact, whatever lies around it); a single copy is cut out with a redaction,
    which is refused if another image overlaps it (the redaction would take that one too).
    Returns the pictures to draw once every other change is done: [(page, rect, ("xref"|"bytes", v))]."""
    pages = info["pages"]
    copies = {}
    for p, pg in enumerate(pages):
        for k, im in enumerate(pg.get("images", [])):
            if im["xref"]:
                copies.setdefault(im["xref"], []).append((p, k))

    # 1. which copies each change removes
    plans = []
    for o in objects:
        p, k = o["page"], o["index"]
        im = pages[p]["images"][k]
        if o["action"] == "move" and not im["xref"]:
            warnings.append(f"Page {p + 1}: this image is stored in a way that can't be moved, so it was left in place.")
            continue
        group = copies[im["xref"]] if (o.get("allPages") and im["xref"]) else [(p, k)]
        plans.append((o, group))
    targets = {t for _, group in plans for t in group}
    blank = {x for x, where in copies.items() if all(t in targets for t in where)}

    # 2. copies cut out one by one must not overlap another image
    blocked = set()
    for (p, k) in targets:
        im = pages[p]["images"][k]
        if im["xref"] in blank:
            continue
        r = fitz.Rect(im["bbox"]) + (1, 1, -1, -1)
        for j, other in enumerate(pages[p]["images"]):
            if j != k and (p, j) not in targets and other["xref"] not in blank and (fitz.Rect(other["bbox"]) & r).get_area() > 0:
                blocked.add((p, k))
                break
    done = []
    for o, group in plans:
        if any(t in blocked for t in group):
            warnings.append(f"Page {o['page'] + 1}: an image overlaps another picture, so it couldn't be changed on its own "
                            "and was left in place." + (" Try “all pages”, or Erase area." if len(group) == 1 else ""))
            targets -= set(group)
        else:
            done.append((o, group))
    blank = {x for x, where in copies.items() if all(t in targets for t in where)}

    # 3. what to draw afterwards (pictures that are about to be blanked are copied out first)
    streams = {}
    def source(xref):
        if xref not in blank:
            return ("xref", xref)
        if xref not in streams:
            streams[xref] = image_stream(doc, xref)
        return ("bytes", streams[xref])
    placements = []
    for o, group in done:
        p, k = o["page"], o["index"]
        im = pages[p]["images"][k]
        if o["action"] == "move":
            placements.append((o["to_page"], o["rect"], source(im["xref"])))
        elif o["action"] == "replace":
            if len(group) > 1:
                placements += [(q, pages[q]["images"][j]["bbox"], ("bytes", o["image"])) for q, j in group]
            else:
                placements.append((o.get("to_page", p), o.get("rect") or im["bbox"], ("bytes", o["image"])))

    # 4. remove
    for xref in blank:
        p = copies[xref][0][0]
        doc[p].delete_image(xref)
    by_page = {}
    for (p, k) in targets:
        if pages[p]["images"][k]["xref"] not in blank:
            by_page.setdefault(p, []).append(fitz.Rect(pages[p]["images"][k]["bbox"]) + (1, 1, -1, -1))
    for p, rects in by_page.items():
        page = doc[p]
        for r in rects:
            page.add_redact_annot(r, fill=False, cross_out=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_NONE)

    # 5. erase areas: drawings fully inside go, images are blanked inside, text only if asked
    for e in erase:
        page = doc[e["page"]]
        if page.rotation:
            warnings.append(f"Page {e['page'] + 1} is turned sideways, so the area to erase on it was skipped.")
            continue
        r = fitz.Rect(e["rect"]) & page.rect
        if r.is_empty:
            continue
        page.add_redact_annot(r, fill=False, cross_out=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS, graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_REMOVE if e.get("text") else fitz.PDF_REDACT_TEXT_NONE)
    return placements


def place_pictures(doc, placements, warnings):
    """Draw moved and replacement images (after every other change, under the additions)."""
    for p, rect, (kind, value) in placements:
        page = doc[p]
        if page.rotation:
            warnings.append(f"Page {p + 1} is turned sideways, so an image moved or replaced on it was skipped.")
            continue
        r = fitz.Rect(rect) & page.rect
        if r.is_empty or r.width < 2 or r.height < 2:
            continue
        try:
            if kind == "xref":
                page.insert_image(r, xref=value, keep_proportion=True)
            else:
                page.insert_image(r, stream=value, keep_proportion=True)
        except Exception:
            warnings.append(f"Page {p + 1}: an image couldn't be placed, so it was skipped.")


def apply_changes(pdf_bytes, info, edits, inserts, additions=(), objects=(), erase=()):
    """
    Remove/move/replace existing images and erase areas, apply text edits and list insertions,
    then place additions on top.
      edits:     [{"page", "line", "text"}]            (text may contain "\n" line breaks)
      inserts:   [{"page", "list", "after", "items"}]   (items: strings; "-" lines are sub-points)
      additions: see apply_additions;  objects, erase: see apply_objects
    Returns (edited_pdf_bytes, page_count, warnings).
    """
    info = json.loads(json.dumps(info))  # never mutate the stored analysis
    doc = fitz.open("pdf", pdf_bytes)
    fc, warnings = new_font_context(), []
    # first, while everything is where /analyze saw it
    pictures = apply_objects(doc, info, objects, erase, warnings) if (objects or erase) else []
    info["_by_xref"] = {doc.page_xref(i): info["pages"][i] for i in range(doc.page_count)}
    info["_inserts"] = []

    growth = []  # paragraphs that grew and need room below them
    apply_text_edits(doc, info, edits, fc, warnings, growth)
    inserts = route_inserts(info, inserts)

    # Plan every insertion first; only those that can be done renumber their list.
    models, feasible = {}, []
    for ins in inserts:
        try:
            if insert_items(doc, pdf_bytes, info, ins, models, fc, warnings, dry_run=True):
                feasible.append(ins)
        except InsertError as exc:
            warnings.append(str(exc))
    info["_inserts"] = feasible
    renumber(doc, info, feasible, fc)

    def key(ins):
        p, l, k = int(ins["page"]), int(ins["list"]), int(ins["after"])
        item = ins.get("_item") or info["pages"][p]["lists"][l]["items"][k]
        return (-p, -(item.get("row_bottom") or item["bottom"]))

    for ins in growth:
        ins["_item"]["row_bottom0"] = ins["_item"]["row_bottom"]
    pending = sorted(feasible + growth, key=key)
    for n, ins in enumerate(pending):
        try:
            insert_items(doc, pdf_bytes, info, ins, models, fc, warnings)
        except InsertError as exc:
            warnings.append(str(exc))
        moved = ins.get("_moved") or 0.0
        if moved and "_item" in ins:
            for other in pending[n + 1:]:
                it = other.get("_item")
                if it and int(other["page"]) == int(ins["page"]) \
                        and abs(it["row_bottom0"] - ins["_item"]["row_bottom0"]) < 0.5:
                    it["row_bottom"] += moved

    place_pictures(doc, pictures, warnings)
    apply_additions(doc, info, additions, fc, warnings)
    out = doc.tobytes(garbage=4, deflate=True)
    pages = doc.page_count
    doc.close()
    return out, pages, warnings


class PdfRejected(Exception):
    """The PDF can't be edited (encrypted, unreadable, too large)."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def analyze(pdf_bytes, max_pages=None):
    """Extract editable lines and lists. Returns (info, layout)."""
    try:
        doc = fitz.open("pdf", pdf_bytes)
    except Exception:
        raise PdfRejected("PDF_UNREADABLE", "The file isn't a readable PDF.")
    if doc.needs_pass:
        raise PdfRejected("PDF_ENCRYPTED", "The PDF is password-protected.")
    if max_pages and doc.page_count > max_pages:
        raise PdfRejected("PDF_TOO_MANY_PAGES", f"The PDF has more than {max_pages} pages.")
    all_lines = [extract_lines(page) for page in doc]
    bands = page_bands(doc, all_lines)
    all_images = [page_images(page) for page in doc]
    copies = {}
    for imgs in all_images:
        for im in imgs:
            if im["xref"]:
                copies[im["xref"]] = copies.get(im["xref"], 0) + 1
    pages, layout_pages = [], []
    for pno, (page, lines, band) in enumerate(zip(doc, all_lines, bands)):
        column_rights(page, lines)
        lists = detect_lists(page, lines) if lines else []
        for l in lines:
            l.pop("_marker", None)
        pages.append({"lines": lines, "lists": lists, "images": all_images[pno], **band})
        layout_pages.append(_public_page(pno, page, lines, lists, all_images[pno], copies))
    has_text = any(all_lines)
    signed = is_digitally_signed(doc)
    doc.close()
    return {"pages": pages}, {"schemaVersion": 1, "hasText": has_text, "signed": signed, "pages": layout_pages}


def _public_page(pno, page, lines, lists, images=(), copies=None):
    """What the apps need to draw editing controls. Coordinates are PDF points,
    origin top-left of the page's visible area (multiply by zoom to get pixels)."""
    out_lines = []
    for i, l in enumerate(lines):
        out_lines.append({
            "id": f"p{pno}-l{i}", "text": l["text"], "bbox": l["bbox"], "editable": l["editable"],
            "font": l["font"], "size": l["size"], "bold": bool(l["flags"] & 16),
            "italic": bool(l["flags"] & 2), "color": "#%06x" % l["color"],
            "align": l.get("align", "left"), "maxRight": l.get("right"), "minLeft": l.get("left"),
        })
    out_lists = []
    for li, lst in enumerate(lists):
        t = lst["template"]["text"] or ""
        out_lists.append({
            "id": f"p{pno}-L{li}", "kind": {"glyph": "bullet", "vector": "bullet", "num": "numbered"}[lst["kind"]],
            "marker": "•" if (lst["kind"] == "vector" or "\ue000" <= (t or " ")[0] <= "\uf8ff") else t,
            "numbering": lst["num"], "markerX": lst["marker_x0"], "textX": lst["text_x"],
            "right": lst["right"], "linePitch": lst["line_pitch"], "itemStep": lst["item_step"],
            "stepAfterNested": lst["step_after_nested"], "childOffset": lst["child_offset"],
            "style": {"font": lst["style"]["font"], "size": lst["style"]["size"],
                      "bold": bool(lst["style"]["flags"] & 16), "color": "#%06x" % lst["style"]["color"]},
            "parent": ({"listId": f"p{pno}-L{lst['parent'][0]}", "itemIndex": lst["parent"][1]}
                       if lst.get("parent") else None),
            "items": [{"index": k, "bbox": it["bbox"], "value": it["value"],
                       "lineIds": [f"p{pno}-l{j}" for j in it["lines"]],
                       "childListId": f"p{pno}-L{it['child_list']}" if it.get("child_list") is not None else None}
                      for k, it in enumerate(lst["items"])],
        })
    copies = copies or {}
    out_images = [{"id": f"p{pno}-i{k}", "bbox": im["bbox"], "full": im["full"],
                   # the same picture drawn elsewhere too (a logo in every page header): "copies" > 1
                   "copies": copies.get(im["xref"], 1) if im["xref"] else 1,
                   "group": f"g{im['xref']}" if im["xref"] and copies.get(im["xref"], 1) > 1 else None}
                  for k, im in enumerate(images)]
    return {"index": pno, "width": page.rect.width, "height": page.rect.height,
            "rotated": bool(page.rotation), "lines": out_lines, "lists": out_lists, "images": out_images}
