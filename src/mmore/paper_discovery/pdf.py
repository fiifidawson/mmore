"""Download PDFs and pull text out of them. Never raises on remote errors."""

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from .schema import PdfStatus

logger = logging.getLogger(__name__)

# 401/402/403: no subscription, or the publisher blocks automated tools.
# We can't tell these apart, so both are reported as "refused".
REFUSED_STATUSES = {401, 402, 403}
NOT_FOUND_STATUSES = {404, 410}

# Timeouts, 429 and 5xx are often temporary, so they are retried.
MAX_RETRIES = 2
BACKOFF_SECONDS = 2.0
MAX_RETRY_AFTER_SECONDS = 60.0

# Substrings that mark an HTML body as a sign-in page rather than content.
# A proxy that needs authentication answers 200 OK with one of these, which
# would otherwise be counted as a silent skip.
LOGIN_PAGE_MARKERS = (
    "shibboleth",
    "tequila",
    'name="saml',
    'type="password"',
    "discovery service",
    "institutional login",
)


@dataclass
class DownloadResult:
    """Outcome of a single PDF fetch. The pipeline tallies these at the end."""

    outcome: PdfStatus
    path: str | None = None  # local file path on success
    status: int | None = None  # last HTTP status seen, if any


def download_pdf(
    url: str,
    save_dir: str,
    user_agent: str = "mmore-paper-discovery/1.0",
    timeout: int = 30,
    proxy_prefix: str | None = None,
    cache_key: str | None = None,
) -> DownloadResult:
    """Fetch one PDF, following a landing page if that is what we get.

    Args:
      url: Where the PDF is, or a page that links to it.
      save_dir: Directory to cache the file in.
      user_agent: Sent on the request. The default identifies this tool
        honestly rather than posing as a browser.
      timeout: Per-request timeout in seconds.
      proxy_prefix: Optional EZproxy host to route through.
      cache_key: What to name the cached file after. Defaults to `url`.

    Returns:
      A `DownloadResult` with the outcome and, on success, the file path.
    """
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    save_path = expected_pdf_path(cache_key or url, save_dir)
    headers = {"User-Agent": user_agent}

    r = _get(_proxify(url, proxy_prefix), headers, timeout)
    if isinstance(r, DownloadResult):
        return r
    if r.status_code != 200:
        return _failed(r.status_code, url)

    if _looks_like_pdf(r):
        return _saved(r.content, save_path)
    if _looks_like_login_page(r):
        logger.debug("download_pdf got a sign-in page for %s", url)
        return DownloadResult(PdfStatus.LOGIN_PAGE, status=r.status_code)

    # Relative links are relative to the page we ended up on after redirects,
    # e.g. the publisher's page a DOI link redirects to.
    pdf_url = _find_pdf_link(r.text, base=r.url)
    if not pdf_url:
        return DownloadResult(PdfStatus.NO_PDF_LINK, status=r.status_code)

    r2 = _get(_proxify(pdf_url, proxy_prefix), headers, timeout)
    if isinstance(r2, DownloadResult):
        return r2
    if r2.status_code != 200:
        return _failed(r2.status_code, pdf_url)
    if _looks_like_pdf(r2):
        # Saved under the paper's key, not the link we followed, so the
        # cache check finds it next time.
        return _saved(r2.content, save_path)
    if _looks_like_login_page(r2):
        return DownloadResult(PdfStatus.LOGIN_PAGE, status=r2.status_code)
    return DownloadResult(PdfStatus.NOT_PDF, status=r2.status_code)


def _get(
    url: str, headers: dict[str, str], timeout: int
) -> "requests.Response | DownloadResult":
    """GET with retries for timeouts, 429 and 5xx.

    Returns the response, or a `DownloadResult` when the request never got
    an answer. A 429 or 5xx that persists is returned as a response.
    """
    for attempt in range(MAX_RETRIES + 1):
        last_try = attempt == MAX_RETRIES
        try:
            r = requests.get(
                url, headers=headers, timeout=timeout, allow_redirects=True
            )
        except requests.Timeout:
            logger.debug("download_pdf timed out for %s", url)
            if last_try:
                return DownloadResult(PdfStatus.TIMEOUT)
            time.sleep(_backoff(attempt))
            continue
        except requests.RequestException as e:
            logger.debug("download_pdf network error for %s: %s", url, e)
            return DownloadResult(PdfStatus.NETWORK_ERROR)

        if last_try or not (r.status_code == 429 or r.status_code >= 500):
            return r
        logger.debug("download_pdf got %s for %s, retrying", r.status_code, url)
        time.sleep(_retry_after(r) or _backoff(attempt))
    raise AssertionError("unreachable")


def _backoff(attempt: int) -> float:
    return BACKOFF_SECONDS * 2**attempt


def _retry_after(response: requests.Response) -> float | None:
    """Seconds asked for by a `Retry-After` header, capped. None if absent."""
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None  # missing, or an HTTP date we don't parse
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _failed(status: int, url: str) -> DownloadResult:
    """Outcome for a non-200 response."""
    logger.debug("download_pdf got %s for %s", status, url)
    if status in REFUSED_STATUSES:
        outcome = PdfStatus.REFUSED
    elif status == 429:
        outcome = PdfStatus.RATE_LIMITED
    elif status in NOT_FOUND_STATUSES:
        outcome = PdfStatus.NOT_FOUND
    elif status >= 500:
        outcome = PdfStatus.SERVER_ERROR
    else:
        outcome = PdfStatus.HTTP_ERROR
    return DownloadResult(outcome, status=status)


def _saved(content: bytes, path: Path) -> DownloadResult:
    path.write_bytes(content)
    return DownloadResult(PdfStatus.DOWNLOADED, path=str(path), status=200)


def _proxify(url: str, prefix: str | None) -> str:
    """Route a URL through an EZproxy host.

        "https://ezproxy.example.edu" + "https://wiley.com/x.pdf"
        -> "https://ezproxy.example.edu/login?url=https%3A%2F%2F..."

    Does nothing without a prefix, or if the URL is already routed. Only
    relevant where the institution runs EZproxy, since VPN and IP
    recognition need no rewriting.
    """
    if not prefix or prefix in url:
        return url
    return f"{prefix.rstrip('/')}/login?url={quote(url, safe='')}"


# A PDF starts with this signature, sometimes after a few junk bytes.
# The URL and Content-Type can't be trusted: a login page served from a
# `.pdf` URL would otherwise be saved as a PDF.
PDF_SIGNATURE = b"%PDF-"
SIGNATURE_WINDOW = 1024


def _looks_like_pdf(response: requests.Response) -> bool:
    return PDF_SIGNATURE in response.content[:SIGNATURE_WINDOW]


def is_pdf_file(path: str | Path) -> bool:
    """True when the file on disk starts like a PDF."""
    try:
        with open(path, "rb") as f:
            return PDF_SIGNATURE in f.read(SIGNATURE_WINDOW)
    except OSError:
        return False


def _looks_like_login_page(response: requests.Response) -> bool:
    """True when an HTML body is a sign-in form rather than content.

    A proxy that cannot authenticate us answers 200 OK with its login page.
    Detecting it is what stops the pipeline recording a silent skip.
    """
    if "html" not in response.headers.get("Content-Type", "").lower():
        return False
    body = response.text[:20000].lower()
    return any(marker in body for marker in LOGIN_PAGE_MARKERS)


def expected_pdf_path(key: str, save_dir: str) -> Path:
    """Where a PDF for `key` (a URL or `doi:...`) would be cached. No I/O.

    Shared by the writer and the pipeline's cache check so the two agree.
    Named after a hash of the key, so URLs ending in the same word
    (`/pdf`, `/fulltext`) don't share a file.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return Path(save_dir) / f"{digest}.pdf"


def _find_pdf_link(html: str, base: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    # Most publishers name the article's own PDF in this meta tag. Prefer it
    # over scanning links, which can hit a related article or supplement.
    meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta:
        content = meta.get("content")
        if isinstance(content, str) and content.strip():
            return urljoin(base, content.strip())

    for a in soup.find_all("a", href=True):
        # bs4 types an attribute as str | list[str]; join covers the rare
        # multi-valued case so the rest of the function sees one string.
        raw = a["href"]
        href = " ".join(raw) if isinstance(raw, list) else str(raw)
        lowered = href.lower()
        if lowered.endswith(".pdf") or "/pdf" in lowered or "/epdf" in lowered:
            return urljoin(base, href)
    return None


def extract_text(pdf_path: str, mode: str = "fast") -> str:
    """Pull text out of a PDF using mmore's own `PDFProcessor`.

    Args:
      pdf_path: Local path to the PDF.
      mode: `"fast"` uses the PyMuPDF path and loads no models. `"full"`
        uses marker and surya, which handles complex layouts better but
        downloads weights on first use. Unknown values fall back to fast.

    Returns:
      The extracted text, or an empty string if extraction failed.
    """
    try:
        from ..process.processors.base import ProcessorConfig
        from ..process.processors.pdf_processor import PDFProcessor
    except ImportError as e:
        logger.error(
            "mmore.process not installed - install `mmore[paper_discovery]` "
            "(which depends on `mmore[process]`) to enable PDF extraction. (%s)",
            e,
        )
        return ""

    if mode not in {"fast", "full"}:
        logger.warning("Unknown pdf_extractor mode %r; falling back to 'fast'", mode)
        mode = "fast"

    try:
        processor = PDFProcessor(
            ProcessorConfig(custom_config={"extract_images": False})
        )
        sample = (
            processor.process(pdf_path)
            if mode == "full"
            else processor.process_fast(pdf_path)
        )
        return sample.text or ""
    except Exception as e:
        logger.warning("extract_text failed for %s: %s", pdf_path, e)
        return ""
