# PDF Editor

Edit the text of an existing PDF while keeping its fonts and layout, add points to its lists,
and place text boxes, images and signatures on its pages. A small HTTP API (FastAPI) built on
[PyMuPDF](https://github.com/pymupdf/PyMuPDF), with a browser test page.

This is the complete source code of the PDF editing service, published under the
**GNU Affero General Public License v3.0** (see [LICENSE](LICENSE)), as required by PyMuPDF's licence.

```
app.py            the API: POST /analyze, POST /edit, GET /healthz
edit_doc.py       the PDF editing engine
script_text.py    other scripts: Urdu, Arabic, Hindi, Chinese, Korean... (layout and reading order)
index.html        browser test page (served at /)
Dockerfile        container image, fonts included
requirements.txt  exact dependency versions
```

## Run it
```bash
pip install -r requirements.txt
python tools/get_emoji.py            # once: the emoji pictures (8 MB) into ./emoji
uvicorn app:app --port 8080          # then open http://localhost:8080
```
Or with Docker:
```bash
docker build -t pdf-editor .
docker run -p 8080:8080 pdf-editor
```

## The API
- **`POST /analyze`** (multipart: `file` = PDF) returns the editable lines, lists and pictures of each page,
  with ids, positions (PDF points from the top-left) and styles. `?previews=true` adds page images.
- **`POST /edit`** (multipart: `file` = the same PDF, `changes` = JSON, optional `images` = PNG/JPEG files)
  returns the edited PDF. `changes`:
  ```json
  { "edits":     [{ "lineId": "p0-l12", "text": "New text" }],
    "inserts":   [{ "listId": "p0-L0", "afterItem": 1, "items": ["New point", "- a sub-point"] }],
    "additions": [{ "type": "text", "page": 0, "rect": [72, 700, 300, 740], "text": "Hello",
                    "font": "sans", "size": 12, "color": "#000000" },
                  { "type": "signature", "page": 0, "rect": [350, 700, 520, 760], "imageId": "sig-1" }],
    "objects":   [{ "id": "p0-i3", "action": "remove", "allPages": true },
                  { "id": "p0-i5", "action": "move", "page": 1, "rect": [80, 400, 200, 480] },
                  { "id": "p1-i0", "action": "replace", "imageId": "new-logo" }],
    "erase":     [{ "page": 0, "rect": [60, 495, 200, 565], "text": false }] }
  ```
  An `imageId` is the file name of one `images` part. `objects` changes pictures already in the PDF
  (by their id from `/analyze`); `erase` removes everything drawn inside an area (text only if `"text": true`).
  Notices come in the `X-Edit-Warnings` header (JSON list).
- Errors: `{"error": {"code", "message", "requestId"}}`.

## Settings (environment variables)
| Variable | Default | |
|---|---|---|
| `API_KEY` | empty | If set, callers must send it in the `X-Api-Key` header |
| `ALLOWED_ORIGINS` | `*` | Websites allowed to call the API (CORS), comma-separated |
| `MAX_FILE_MB` / `MAX_PAGES` | `30` / `200` | Limits |
| `WORKERS` | `1` | PDFs processed at the same time (engine processes) |
| `SPLIT_PAGES` | `16` | With `WORKERS` > 1, a PDF of at least twice this many pages is read by several engines at once (same result, faster); `0` = never split |
| `RATE_LIMIT_PER_MIN` | `0` (off) | Requests per minute per client IP |
| `SERVE_UI` | `1` | `0` hides the test page |

## Licence
Copyright (C) 2026 Musharaf Hussain.

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU Affero General Public License as published by the Free Software Foundation, version 3.
It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE).

Uses [PyMuPDF](https://pymupdf.readthedocs.io) © Artifex Software, Inc. (AGPL-3.0).
Text in other scripts is drawn with MuPDF's built-in fonts (Noto, Droid Sans Fallback; SIL Open Font
License / Apache 2.0).
Emoji graphics: [Twemoji](https://github.com/jdecked/twemoji) © Twitter/X and contributors, licensed under
[CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/) (fetched at build time by `tools/get_emoji.py`).
