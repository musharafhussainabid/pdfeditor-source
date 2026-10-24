"""
PDF Edit API (simple version): no login, no storage, nothing kept between calls.

    POST /analyze   form: file=<pdf>                        -> JSON: editable lines and lists
    POST /edit      form: file=<pdf>, changes=<json text>,
                          images=<png/jpeg>... (optional)   -> the edited PDF

    uvicorn app:app --port 8080        then open http://localhost:8080
"""
import hmac
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

import pymupdf as fitz
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

import edit_doc
import asyncio
import multiprocessing
from contextlib import asynccontextmanager
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

MAX_MB = float(os.environ.get("MAX_FILE_MB", "30"))       # Cloud Run accepts up to 32 MiB per request
MAX_PAGES = int(os.environ.get("MAX_PAGES", "200"))
PREVIEW_PAGES = int(os.environ.get("PREVIEW_PAGES", "20"))
API_KEY = os.environ.get("API_KEY", "")                   # optional: if set, callers must send X-Api-Key
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*")
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "0"))  # per client IP (per web process); 0 = off
SERVE_UI = os.environ.get("SERVE_UI", "1") == "1"            # the test page at "/"
# PDFs processed at the same time. Above 1, jobs go to that many engine processes through one
# shared queue: a free engine always takes the next job, and a crash only replaces that engine.
ENGINES = max(1, int(os.environ.get("WORKERS", "1")))

_pool = None


JOB_TIMEOUT = 110          # seconds; answer before the proxy in front gives up (120 s)


def engine_pool():
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=ENGINES, mp_context=multiprocessing.get_context("spawn"))
    return _pool


def engines_alive():
    """False when the pool exists but has lost all its engine processes."""
    if ENGINES == 1 or _pool is None:
        return True
    procs = getattr(_pool, "_processes", None) or {}
    return any(p.is_alive() for p in procs.values())


def _reset_pool(reason):
    global _pool
    log.error(f"{reason}; restarting the PDF engines")
    old, _pool = _pool, None
    if old is not None:
        for p in (getattr(old, "_processes", None) or {}).values():
            p.kill()
        old.shutdown(wait=False, cancel_futures=True)


class RequestError(Exception):
    """A refused request, raised inside an engine and turned into an HTTP error by the web process."""
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail

    def __reduce__(self):
        return (RequestError, (self.status, self.detail))


async def engine(fn, *args):
    """Run PDF work off the web process: in a thread for a single engine, else in the engine pool."""
    try:
        if ENGINES == 1:
            return await run_in_threadpool(fn, *args)
        fut = asyncio.get_running_loop().run_in_executor(engine_pool(), fn, *args)
        return await asyncio.wait_for(fut, JOB_TIMEOUT)
    except RequestError as exc:
        raise HTTPException(exc.status, exc.detail)
    except BrokenProcessPool:
        _reset_pool("An engine process crashed")           # e.g. inside the PDF library
        raise
    except asyncio.TimeoutError:
        if not engines_alive():
            _reset_pool("No engine processes left")
        raise HTTPException(503, "The editor is very busy. Please try again in a moment.")


# --------------------------------------------------------------------------- #
# Logging: one JSON object per line, which Cloud Run / Cloudflare / CloudWatch
# all parse into searchable fields. File names and document text are never logged.
# --------------------------------------------------------------------------- #
class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {"time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                 "severity": record.levelname, "message": record.getMessage(), **getattr(record, "fields", {})}
        if record.exc_info:
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


log = logging.getLogger("pdf_edit")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
log.addHandler(_handler)
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
log.propagate = False

@asynccontextmanager
async def lifespan(_app):
    if ENGINES > 1:          # start every engine now, so the first users don't wait for it
        loop = asyncio.get_running_loop()
        await asyncio.gather(*(loop.run_in_executor(engine_pool(), edit_doc.warm_up) for _ in range(ENGINES)))
        log.info(f"{ENGINES} PDF engines ready")
    yield
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="PDF Edit API (simple)", version="1.0.0", lifespan=lifespan)


def error_body(code, message, request):
    request.state.error = code          # the request log line shows which error it was
    return {"error": {"code": code, "message": message, "requestId": getattr(request.state, "rid", None)}}


@app.middleware("http")
async def request_log(request: Request, call_next):
    """Give every request an id, log it as one line, and turn crashes into a JSON 500."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.rid, request.state.log = rid, {}
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("Unhandled error", extra={"fields": {"requestId": rid, "path": request.url.path}})
        response = JSONResponse(status_code=500, content=error_body(
            "INTERNAL_ERROR", "Something went wrong while processing this PDF. Please try again.", request))
    response.headers["X-Request-Id"] = rid
    if request.url.path != "/healthz":
        status = response.status_code
        # ERROR = our fault (5xx), WARNING = the request was refused (4xx), INFO = success
        level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
        error = getattr(request.state, "error", None)
        log.log(level, f"{request.method} {request.url.path} {status}" + (f" {error}" if error else ""), extra={"fields": {
            "requestId": rid, "method": request.method, "path": request.url.path,
            "status": status, **({"error": error} if error else {}),
            "ms": round((time.perf_counter() - start) * 1000),
            "bytesIn": int(request.headers.get("content-length") or 0), **request.state.log}})
    return response


# Added after request_log so it wraps it: error responses carry CORS headers too.
if os.environ.get("APP_CORS", "1") == "1":  # on AWS the Function URL handles CORS instead
    app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS.split(","),
                       allow_methods=["GET", "POST"], allow_headers=["*"],
                       expose_headers=["X-Edit-Warnings", "X-Request-Id"])
if ALLOWED_ORIGINS == "*":
    log.info("ALLOWED_ORIGINS is '*': any website can call this API (set API_KEY to require a key).")


def check_key(key):
    if API_KEY and not hmac.compare_digest(key.encode(), API_KEY.encode()):
        raise HTTPException(401, "Missing or wrong X-Api-Key.")


_hits = {}   # client IP -> request times in the last minute (this worker only)


def check_rate(request):
    """A simple per-IP limit so one client can't keep every worker busy."""
    if not RATE_LIMIT:
        return
    now, ip = time.monotonic(), request.client.host if request.client else "?"
    recent = [t for t in _hits.get(ip, ()) if now - t < 60]
    if len(recent) >= RATE_LIMIT:
        raise HTTPException(429, "Too many requests. Please wait a minute and try again.")
    recent.append(now)
    _hits[ip] = recent
    if len(_hits) > 10000:                                   # forget idle clients
        for k in [k for k, v in _hits.items() if now - v[-1] > 60]:
            del _hits[k]


async def read_pdf(file):
    data = await file.read()
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"PDFs up to {MAX_MB:g} MB.")
    return data


def analysis(data):
    """The engine's analysis (runs in an engine process)."""
    try:
        return edit_doc.analyze(data, max_pages=MAX_PAGES)
    except edit_doc.PdfRejected as exc:
        raise RequestError(422, {"code": exc.code, "message": str(exc)})


def analyze_job(data, previews):
    """Everything /analyze does with the PDF, in one engine call: returns the JSON body and page count."""
    _info, layout = analysis(data)
    if previews:
        pages = layout["pages"][:PREVIEW_PAGES]
        for page, url in zip(pages, edit_doc.page_previews(data, [p["index"] for p in pages])):
            page["preview"] = url
    return json.dumps(layout, separators=(",", ":")).encode(), len(layout["pages"])


@app.post("/analyze")
async def analyze(request: Request, file: UploadFile = File(...), previews: bool = False,
                  x_api_key: str = Header(default="")):
    """Editable lines and lists. With ?previews=true, also a JPEG of each page (first 20)."""
    check_key(x_api_key)
    check_rate(request)
    data = await read_pdf(file)
    body, pages = await engine(analyze_job, data, previews)
    request.state.log = {"pages": pages, "previews": previews}
    return Response(body, media_type="application/json")


MAX_ADDITIONS = 50
MAX_IMAGE_MB = 10
MAX_OBJECTS = 200          # existing images changed in one save
MAX_ERASE = 50
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


async def read_images(images):
    """The uploaded 'images' files by file name (the imageId additions and replacements use)."""
    files = {}
    for f in images or []:
        blob = await f.read()
        if len(blob) > MAX_IMAGE_MB * 1024 * 1024:
            raise HTTPException(413, f"Images up to {MAX_IMAGE_MB} MB.")
        if not (blob.startswith(b"\x89PNG\r\n\x1a\n") or blob.startswith(b"\xff\xd8\xff")):
            raise HTTPException(400, f"Image {f.filename!r} must be a PNG or JPEG.")
        files[f.filename] = blob
    return files


def _rect(rect):
    return (isinstance(rect, list) and len(rect) == 4 and all(isinstance(v, (int, float)) for v in rect)
            and rect[2] > rect[0] and rect[3] > rect[1])


def read_objects(raw, raw_erase, files, pages):
    """Check "objects" (existing images to remove / move / replace) and "erase" (areas to clear)."""
    if not isinstance(raw, list) or len(raw) > MAX_OBJECTS:
        raise HTTPException(400, f"'objects' must be a list of up to {MAX_OBJECTS} items.")
    if not isinstance(raw_erase, list) or len(raw_erase) > MAX_ERASE:
        raise HTTPException(400, f"'erase' must be a list of up to {MAX_ERASE} items.")
    objects, seen = [], set()
    for n, o in enumerate(raw):
        o = o if isinstance(o, dict) else {}
        m = re.fullmatch(r"p(\d+)-i(\d+)", str(o.get("id", "")))
        if not m or int(m[1]) >= len(pages) or int(m[2]) >= len(pages[int(m[1])]["images"]):
            raise HTTPException(400, f"objects[{n}]: unknown image id {o.get('id')!r}.")
        if o["id"] in seen:
            raise HTTPException(400, f"objects[{n}]: image {o['id']} is listed twice.")
        seen.add(o["id"])
        action = o.get("action")
        item = {"page": int(m[1]), "index": int(m[2]), "action": action, "allPages": bool(o.get("allPages"))}
        if action not in ("remove", "move", "replace"):
            raise HTTPException(400, f"objects[{n}]: action must be remove, move or replace.")
        if action in ("move", "replace") and ("page" in o or "rect" in o):
            to = o.get("page", item["page"])
            if not isinstance(to, int) or not 0 <= to < len(pages) or not _rect(o.get("rect")):
                raise HTTPException(400, f"objects[{n}]: {action} needs a valid page and rect [x0, y0, x1, y1].")
            item.update(to_page=to, rect=[float(v) for v in o["rect"]])
        elif action == "move":
            raise HTTPException(400, f"objects[{n}]: move needs page and rect.")
        if action == "replace":
            if o.get("imageId") not in files:
                raise HTTPException(400, f"objects[{n}]: no uploaded image named {o.get('imageId')!r}.")
            item["image"] = files[o["imageId"]]
        objects.append(item)
    erase = []
    for n, e in enumerate(raw_erase):
        e = e if isinstance(e, dict) else {}
        if not isinstance(e.get("page"), int) or not 0 <= e["page"] < len(pages) or not _rect(e.get("rect")):
            raise HTTPException(400, f"erase[{n}]: needs a valid page and rect [x0, y0, x1, y1].")
        erase.append({"page": e["page"], "rect": [float(v) for v in e["rect"]], "text": bool(e.get("text"))})
    return objects, erase


def read_additions(raw, files, pages):
    """Check the "additions" list and attach each image's bytes (an addition's imageId is a file name)."""
    if not isinstance(raw, list) or len(raw) > MAX_ADDITIONS:
        raise HTTPException(400, f"'additions' must be a list of up to {MAX_ADDITIONS} items.")
    out = []
    for n, a in enumerate(raw):
        a = a if isinstance(a, dict) else {}
        kind, page, rect = a.get("type"), a.get("page"), a.get("rect")
        if kind not in ("text", "image", "signature"):
            raise HTTPException(400, f"additions[{n}]: type must be text, image or signature.")
        if not isinstance(page, int) or not 0 <= page < len(pages):
            raise HTTPException(400, f"additions[{n}]: unknown page {page}.")
        if not (isinstance(rect, list) and len(rect) == 4 and all(isinstance(v, (int, float)) for v in rect)
                and rect[2] > rect[0] and rect[3] > rect[1]):
            raise HTTPException(400, f"additions[{n}]: rect must be [x0, y0, x1, y1] with x1 > x0 and y1 > y0.")
        item = {"type": kind, "page": page, "rect": [float(v) for v in rect]}
        if kind == "text":
            text, size, color = a.get("text"), a.get("size", 12), a.get("color", "#000000")
            if not isinstance(text, str) or not text.strip():
                continue                                       # an empty text box adds nothing
            if not isinstance(size, (int, float)) or not 4 <= size <= 96:
                raise HTTPException(400, f"additions[{n}]: size must be between 4 and 96.")
            if not isinstance(color, str) or not HEX_COLOR.fullmatch(color):
                raise HTTPException(400, f"additions[{n}]: color must look like #1a2b3c.")
            font = a.get("font", "sans")
            if font == "doc":                                  # the document's own font, taken from a line
                m = re.fullmatch(r"p(\d+)-l(\d+)", str(a.get("fontFrom", "")))
                if not m or int(m[1]) >= len(pages) or int(m[2]) >= len(pages[int(m[1])]["lines"]):
                    raise HTTPException(400, f"additions[{n}]: font 'doc' needs fontFrom, the lineId to take the font from.")
                item["src"] = (int(m[1]), int(m[2]))
            elif font not in ("sans", "serif", "mono"):
                raise HTTPException(400, f"additions[{n}]: font must be doc, sans, serif or mono.")
            item.update(text=text[:5000], size=float(size), color=color, font=font,
                        bold=bool(a.get("bold")), italic=bool(a.get("italic")),
                        align=a.get("align") if a.get("align") in ("left", "center", "right") else "left")
        else:
            if a.get("imageId") not in files:
                raise HTTPException(400, f"additions[{n}]: no uploaded image named {a.get('imageId')!r}.")
            item["image"] = files[a["imageId"]]
        out.append(item)
    return out


@app.post("/edit")
async def edit(request: Request, file: UploadFile = File(...), changes: str = Form(...),
               images: list[UploadFile] = File(default=[]), x_api_key: str = Header(default="")):
    """Apply changes to the PDF and return the edited file.
    changes = {"edits": [{"lineId", "text"}], "inserts": [{"listId", "afterItem", "items"}],
               "additions": [{"type": "text"|"image"|"signature", "page", "rect", ...}],
               "objects": [{"id": "p0-i2", "action": "remove"|"move"|"replace", "allPages", "page", "rect", "imageId"}],
               "erase": [{"page", "rect", "text": false}]}
    A text addition with "font": "doc" and "fontFrom": "<lineId>" is written in that line's own font.
    images  = the PNG/JPEG files that additions and replacements point to (by file name)."""
    check_key(x_api_key)
    check_rate(request)
    data = await read_pdf(file)
    files = await read_images(images)
    out, warnings, counts = await engine(edit_job, data, changes, files)
    request.state.log = {**counts, "warnings": len(warnings), "bytesOut": len(out)}
    name = (file.filename or "document.pdf").rsplit(".", 1)[0]
    return Response(out, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{re.sub(r"[^A-Za-z0-9 ._-]", "_", name)} (edited).pdf"',
        "X-Edit-Warnings": json.dumps(warnings)})


def edit_job(data, changes, files):
    """Everything /edit does with the PDF, in one engine call: check the changes, apply them."""
    try:
        return _edit(data, changes, files)
    except HTTPException as exc:          # a refused request: carry it back to the web process
        raise RequestError(exc.status_code, exc.detail)


def _edit(data, changes, files):
    try:
        body = json.loads(changes)
    except ValueError:
        raise HTTPException(400, "'changes' must be JSON.")
    if not isinstance(body, dict):
        raise HTTPException(400, "'changes' must be a JSON object.")
    info, _layout = analysis(data)   # same PDF -> same analysis -> the ids from /analyze match
    edits, inserts = [], []
    pages = info["pages"]
    for e in body.get("edits") or []:
        e = e if isinstance(e, dict) else {}
        m = re.fullmatch(r"p(\d+)-l(\d+)", str(e.get("lineId", "")))
        if not m or int(m[1]) >= len(pages) or int(m[2]) >= len(pages[int(m[1])]["lines"]):
            raise HTTPException(400, f"Unknown lineId {e.get('lineId')}")
        edits.append({"page": int(m[1]), "line": int(m[2]), "text": str(e.get("text", ""))})
    for ins in body.get("inserts") or []:
        ins = ins if isinstance(ins, dict) else {}
        m = re.fullmatch(r"p(\d+)-L(\d+)", str(ins.get("listId", "")))
        k = ins.get("afterItem")
        if not m or int(m[1]) >= len(pages) or int(m[2]) >= len(pages[int(m[1])]["lists"]) \
                or not isinstance(k, int) or not 0 <= k < len(pages[int(m[1])]["lists"][int(m[2])]["items"]):
            raise HTTPException(400, f"Unknown listId/afterItem {ins.get('listId')}/{k}")
        items = ins.get("items")
        if not isinstance(items, list) or not all(isinstance(s, str) for s in items):
            raise HTTPException(400, f"'items' for {ins.get('listId')} must be a list of strings.")
        inserts.append({"page": int(m[1]), "list": int(m[2]), "after": k, "items": items})
    additions = read_additions(body.get("additions") or [], files, pages)
    objects, erase = read_objects(body.get("objects") or [], body.get("erase") or [], files, pages)
    if not edits and not inserts and not additions and not objects and not erase:
        raise HTTPException(400, "No changes given.")
    out, page_count, warnings = edit_doc.apply_changes(data, info, edits, inserts, additions, objects, erase)
    return out, warnings, {"pages": page_count, "edits": len(edits), "inserts": len(inserts),
                           "additions": len(additions), "objects": len(objects), "erase": len(erase)}


@app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)  # uptime monitors often use HEAD
def healthz():
    if not engines_alive():
        return JSONResponse({"ok": False, "reason": "no PDF engines"}, status_code=503)
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index():
    if not SERVE_UI:
        raise HTTPException(404, "Not found.")
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))


# --------------------------------------------------------------------------- #
# Every error has the same shape: {"error": {"code", "message", "requestId"}}
# --------------------------------------------------------------------------- #
ERROR_CODES = {400: "BAD_REQUEST", 401: "UNAUTHORIZED", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED",
               413: "FILE_TOO_LARGE", 422: "PDF_REJECTED", 429: "RATE_LIMITED", 503: "BUSY"}


@app.exception_handler(StarletteHTTPException)
async def http_errors(request, exc):
    if isinstance(exc.detail, dict):
        code, message = exc.detail["code"], exc.detail["message"]
    else:
        code, message = ERROR_CODES.get(exc.status_code, "ERROR"), str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content=error_body(code, message, request))


@app.exception_handler(RequestValidationError)
async def validation_errors(request, exc):
    missing = [str(e["loc"][-1]) for e in exc.errors() if e.get("type") == "missing"]
    message = f"Missing form field: {', '.join(missing)}." if missing else "The request is not valid."
    return JSONResponse(status_code=400, content=error_body("BAD_REQUEST", message, request))
