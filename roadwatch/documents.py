"""Turn uploaded or fetched files into page text. Shared by upload and URL import."""
import html
import io
import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from pypdf import PdfReader

MAX_BYTES = 15_000_000
MAX_PAGES = 150
MAX_CHARS = 400_000

# Official publishers of Canadian procurement, audit, and infrastructure records.
# Imports are limited to these hosts so the server cannot be used to fetch arbitrary URLs.
ALLOWED_HOSTS = {
    "toronto.ca", "open.toronto.ca", "torontoauditor.ca", "ontario.ca", "auditor.on.ca",
    "montreal.ca", "donnees.montreal.ca", "bigmtl.ca", "canada.ca", "open.canada.ca",
    "canadabuys.canada.ca", "statcan.gc.ca", "competition-bureau.canada.ca", "seao.ca",
    "upac.gouv.qc.ca", "oag.ab.ca", "bcauditor.com", "auditor.sk.ca", "oag.mb.ca",
}


def host_allowed(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def resolves_public(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


def pdf_pages(raw):
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF")
        if len(reader.pages) > MAX_PAGES:
            raise ValueError("Too many pages")
        return [{"number": n, "text": page.extract_text() or ""} for n, page in enumerate(reader.pages, 1)]
    except Exception:
        raise HTTPException(422, f"Cannot read PDF. Use an unencrypted PDF with at most {MAX_PAGES} pages.")


def html_text(raw):
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|table)>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def pages_from_bytes(raw, filename="", content_type=""):
    name, ctype = filename.lower(), content_type.lower()
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, f"Files must be at most {MAX_BYTES // 1_000_000} MB.")
    if name.endswith(".pdf") or "pdf" in ctype or raw[:5] == b"%PDF-":
        pages = pdf_pages(raw)
    elif name.endswith((".htm", ".html")) or "html" in ctype:
        pages = [{"number": 1, "text": html_text(raw)}]
    elif name.endswith((".txt", ".csv", ".md")) or ctype.startswith("text/"):
        try:
            pages = [{"number": 1, "text": raw.decode("utf-8-sig")}]
        except UnicodeDecodeError:
            raise HTTPException(422, "Text files must use UTF-8 encoding.")
    else:
        raise HTTPException(422, "Supported formats: PDF, HTML, TXT, CSV, and Markdown.")
    text = "\n".join(p["text"] for p in pages)
    if not text.strip():
        raise HTTPException(422, "No readable text. Scanned PDFs need OCR before import.")
    if len(text) > MAX_CHARS:
        raise HTTPException(422, f"Document exceeds {MAX_CHARS:,} characters. Import a smaller excerpt.")
    return pages


async def fetch_official(url):
    """Download a document from an allowlisted public institution."""
    if not host_allowed(url):
        raise HTTPException(422, "Imports are limited to official government and audit publishers. "
                                 "Download the file and upload it instead.")
    if urlparse(url).scheme != "https":
        raise HTTPException(422, "Only https links can be imported.")
    if not resolves_public(urlparse(url).hostname):
        raise HTTPException(422, "The source host could not be resolved to a public address.")
    headers = {"User-Agent": "Roadwatch/0.2 (public records research)"}
    async with httpx.AsyncClient(timeout=45, follow_redirects=False, headers=headers) as client:
        for _ in range(5):
            response = await client.get(url)
            if response.is_redirect:
                url = str(response.next_request.url)
                if not host_allowed(url) or urlparse(url).scheme != "https":
                    raise HTTPException(422, "The source redirected outside the allowed publishers.")
                continue
            break
        else:
            raise HTTPException(422, "Too many redirects.")
    if response.status_code != 200:
        raise HTTPException(422, f"The source returned HTTP {response.status_code}.")
    raw = response.content
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, f"The document is larger than {MAX_BYTES // 1_000_000} MB.")
    filename = urlparse(url).path.rsplit("/", 1)[-1]
    return raw, filename, response.headers.get("content-type", ""), url
