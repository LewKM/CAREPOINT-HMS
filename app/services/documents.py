"""Printable documents: one A4 template each, served as a print preview or, with ?format=pdf, as a PDF."""
import re
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import Response
from weasyprint import HTML

from ..config import BASE_DIR
from ..templating import render, render_html

STATIC = (BASE_DIR / "app" / "static").resolve()
MIME = {".css": "text/css", ".svg": "image/svg+xml", ".woff": "font/woff", ".woff2": "font/woff2", ".png": "image/png"}


def _fetch(url: str, *args, **kwargs) -> dict:
    """Resolve /static/ from disk and refuse everything else, so a PDF never waits on the network."""
    path = urlparse(url).path
    if path.startswith("/static/"):
        file = (STATIC / path.removeprefix("/static/")).resolve()
        if file.is_relative_to(STATIC) and file.is_file():
            return dict(string=file.read_bytes(), mime_type=MIME.get(file.suffix, "application/octet-stream"), redirected_url=url)
    raise ValueError(f"External resource not allowed in documents: {url}")


def document(request: Request, template: str, filename: str, **ctx):
    if request.query_params.get("format") != "pdf":
        return render(request, template, pdf=False, **ctx)
    html = render_html(request, template, pdf=True, **ctx)
    pdf = HTML(string=html, base_url="http://carepoint.internal/", url_fetcher=_fetch).write_pdf()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-") or "document"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{safe}.pdf"', "Cache-Control": "no-store"})
