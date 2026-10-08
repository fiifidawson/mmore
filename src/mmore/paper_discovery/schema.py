from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..type import MultimodalSample


class SourceName(str, Enum):
    """Which repository a `Paper` came from.

    Subclasses `str` so members serialize as their plain value, `"arxiv"`
    rather than `"SourceName.ARXIV"`, with no custom JSON encoder.
    """

    ARXIV = "arxiv"
    OPENALEX = "openalex"
    EUROPEPMC = "europepmc"
    GOOGLE_SCHOLAR = "google_scholar"


class PdfStatus(str, Enum):
    """What happened when we tried to get a paper's PDF."""

    DOWNLOADED = "downloaded"
    CACHED = "cached"  # reused from `pdf_dir`
    NO_TEXT = "no_text"  # a real PDF, but no text came out of it
    NO_URL = "no_url"  # the source gave no link
    REFUSED = "refused"  # 401/402/403: no subscription, or bots blocked
    RATE_LIMITED = "rate_limited"  # 429, still refused after retries
    LOGIN_PAGE = "login_page"  # a sign-in page instead of the PDF
    NOT_FOUND = "not_found"  # 404/410
    NO_PDF_LINK = "no_pdf_link"  # a web page with no PDF link on it
    NOT_PDF = "not_pdf"  # the PDF link returned something else
    TIMEOUT = "timeout"  # no answer in `pdf_timeout`, after retries
    SERVER_ERROR = "server_error"  # 5xx, after retries
    HTTP_ERROR = "http_error"  # any other unexpected status
    NETWORK_ERROR = "network_error"  # DNS, SSL or connection failure


@dataclass
class CategoryQuery:
    """A boolean query for one category. Stage 1 builds these, stage 2 runs them."""

    combination_title: str
    boolean_combination: str

    def to_dict(self) -> dict[str, str]:
        return {
            "combination_title": self.combination_title,
            "boolean_combination": self.boolean_combination,
        }


@dataclass
class Paper:
    """One paper, in the same shape whichever source it came from.

    Every field is optional because sources differ in what they return.
    `None` means we do not know, not that the value is empty.
    """

    title: str | None = None
    authors: list[str] | None = None
    url: str | None = None
    doi: str | None = None
    # Every known link to the paper, best first. `url` is the first one.
    candidate_urls: list[str] | None = None
    abstract: str | None = None
    year: int | None = None
    extracted_text: str | None = None
    source: SourceName | None = None
    search_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "authors": self.authors,
            "url": self.url,
            "doi": self.doi,
            "candidate_urls": self.candidate_urls,
            "abstract": self.abstract,
            "year": self.year,
            "extracted_text": self.extracted_text,
            "source": self.source,
            "search_category": self.search_category,
        }

    def download_urls(self) -> list[str]:
        """URLs to try for the PDF, best first."""
        if self.candidate_urls:
            return self.candidate_urls
        return [self.url] if self.url else []

    def cache_key(self) -> str | None:
        """What the cached PDF is named after: the DOI, else the first URL."""
        if self.doi:
            return f"doi:{self.doi}"
        urls = self.download_urls()
        return urls[0] if urls else None

    def to_multimodal_sample(self, pdf_path: str = "") -> "MultimodalSample":
        """Convert to mmore's document shape so index and rag can read it.

        Args:
          pdf_path: Path to the cached PDF, if one was downloaded.

        Returns:
          A `MultimodalSample` whose text is the extracted PDF body, falling
          back to the abstract and then the title. The paper's own fields go
          in `metadata.extra` so nothing is lost through JSONL.
        """
        # Imported here rather than at module scope to keep this module
        # cheap to import when only the dataclasses are needed.
        from ..type import DocumentMetadata, MultimodalSample

        body = self.extracted_text or self.abstract or self.title or ""
        extra = {
            k: v
            for k, v in {
                "title": self.title,
                "authors": self.authors,
                "year": self.year,
                "source": self.source,
                "url": self.url,
                "doi": self.doi,
                "candidate_urls": self.candidate_urls,
                "search_category": self.search_category,
                "abstract": self.abstract,
            }.items()
            if v is not None
        }
        return MultimodalSample(
            text=body,
            modalities=[],
            metadata=DocumentMetadata(
                file_path=pdf_path,
                processor_type="paper_discovery",
                extra=extra,
            ),
        )


@dataclass
class SynonymEntry:
    """A canonical word and the terms that mean the same thing."""

    word: str
    synonyms: list[str] = field(default_factory=list)
