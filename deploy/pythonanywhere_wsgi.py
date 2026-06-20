"""Small WSGI app that serves the generated briefing files."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SUFFIXES = {".html", ".css", ".js", ".json", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".svgz", ".webp", ".ico"}


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    if method not in {"GET", "HEAD"}:
        start_response("405 Method Not Allowed", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Method not allowed"]

    requested = unquote(environ.get("PATH_INFO", "/")).lstrip("/") or "index.html"
    candidate = (ROOT / requested).resolve()
    relative_parts = candidate.relative_to(ROOT).parts if ROOT in candidate.parents else ()
    is_public = (
        relative_parts
        and not any(part.startswith(".") for part in relative_parts)
        and candidate.suffix.lower() in PUBLIC_SUFFIXES
    )
    if not is_public or not candidate.is_file():
        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Not found"]

    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    headers = [("Content-Type", content_type)]
    if candidate.suffix.lower() in {".html", ".js", ".json"}:
        headers.append(("Cache-Control", "no-cache"))
    data = candidate.read_bytes()
    headers.append(("Content-Length", str(len(data))))
    start_response("200 OK", headers)
    return [] if method == "HEAD" else [data]
