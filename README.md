# PDF Editor

Edit the text of an existing PDF while keeping its fonts and layout, add points to its lists,
and place text boxes, images and signatures on its pages. A small HTTP API (FastAPI) built on
[PyMuPDF](https://github.com/pymupdf/PyMuPDF), with a browser test page.

This is the complete source code of the PDF editing service, published under the
**GNU Affero General Public License v3.0** (see [LICENSE](LICENSE)), as required by PyMuPDF's licence.

```
app.py            the API: POST /analyze, POST /edit, GET /healthz
edit_doc.py       the PDF editing engine
index.html        browser test page (served at /)
Dockerfile        container image, fonts included
requirements.txt  exact dependency versions
```

## Run it
```bash
pip install -r requirements.txt
uvicorn app:app --port 8080          # then open http://localhost:8080
```
Or with Docker:
```bash
docker build -t pdf-editor .
docker run -p 8080:8080 pdf-editor
```

## The API
- **`POST /analyze`** (multipart: `file` = PDF) returns the editable lines and lists of each page,
  with ids, positions (PDF points from the top-left) and styles. `?previews=true` adds page images.
- **`POST /edit`** (multipart: `file` = the same PDF, `changes` = JSON, optional `images` = PNG/JPEG files)
  returns the edited PDF. `changes`:
  ```json
  { "edits":     [{ "lineId": "p0-l12", "text": "New text" }],
    "inserts":   [{ "listId": "p0-L0", "afterItem": 1, "items": ["New point", "- a sub-point"] }],
    "additions": [{ "type": "text", "page": 0, "rect": [72, 700, 300, 740], "text": "Hello",
                    "font": "sans", "size": 12, "color": "#000000" },
                  { "type": "signature", "page": 0, "rect": [350, 700, 520, 760], "imageId": "sig-1" }] }
  ```
  An image or signature's `imageId` is the file name of one `images` part.
  Notices come in the `X-Edit-Warnings` header (JSON list).
- Errors: `{"error": {"code", "message", "requestId"}}`.

## Settings (environment variables)
| Variable | Default | |
|---|---|---|
| `API_KEY` | empty | If set, callers must send it in the `X-Api-Key` header |
| `ALLOWED_ORIGINS` | `*` | Websites allowed to call the API (CORS), comma-separated |
| `MAX_FILE_MB` / `MAX_PAGES` | `30` / `200` | Limits |
| `WORKERS` | `1` | Parallel worker processes |
| `RATE_LIMIT_PER_MIN` | `0` (off) | Requests per minute per client IP, per worker |
| `SERVE_UI` | `1` | `0` hides the test page |

## Licence
Copyright (C) 2026 Musharaf Hussain.

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU Affero General Public License as published by the Free Software Foundation, version 3.
It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE).

Uses [PyMuPDF](https://pymupdf.readthedocs.io) © Artifex Software, Inc. (AGPL-3.0).
