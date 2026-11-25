"""Bounded HTTPS reads from the public literature providers used by this package."""
from http.client import HTTPException
from threading import Lock
from time import monotonic, sleep
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_HOSTS = {"www.ebi.ac.uk", "eutils.ncbi.nlm.nih.gov"}
_PUBMED_LOCK = Lock()
_last_pubmed_request = 0.0


def _check_destination(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in _HOSTS:
        raise ValueError("Expected a supported literature provider HTTPS endpoint.")


class _ProviderRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_destination(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(_ProviderRedirect())


def fetch_bytes(url: str, *, max_bytes: int = 2_000_000) -> bytes:
    _check_destination(url)
    # NCBI's unauthenticated E-utilities budget is three requests per second per IP.
    # Pace requests from this process, including concurrent MCP calls.
    if urlsplit(url).hostname == "eutils.ncbi.nlm.nih.gov":
        global _last_pubmed_request
        with _PUBMED_LOCK:
            delay = _last_pubmed_request + 0.35 - monotonic()
            if delay > 0:
                sleep(delay)
            _last_pubmed_request = monotonic()
    request = Request(url, headers={"User-Agent": "MedExtract-Evidence-Dossier/0.2",
                                    "Accept-Encoding": "identity"})
    try:
        with _OPENER.open(request, timeout=20) as response:
            size = response.headers.get("Content-Length")
            if size is not None and int(size) > max_bytes:
                raise ValueError("Provider response exceeds the size limit.")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("Provider response exceeds the size limit.")
            return data
    except HTTPException as exc:
        raise URLError("Incomplete response from literature provider.") from exc
