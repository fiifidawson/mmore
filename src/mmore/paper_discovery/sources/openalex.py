import logging
import time

import requests

from ..schema import Paper, SourceName
from ._utils import coerce_year, normalize_doi, unique_urls
from .base import SourceAdapter

logger = logging.getLogger(__name__)

API_URL = "https://api.openalex.org/works"
RATE_LIMIT_SECONDS = 1.0


class OpenAlexAdapter(SourceAdapter):
    name = "openalex"

    def __init__(
        self,
        user_agent: str = "mmore-paper-discovery/1.0",
        max_pages: int = 3,
        max_results: int = 50,
    ):
        self.headers = {"User-Agent": user_agent}
        self.max_pages = max_pages
        self.max_results = max_results

    def search(self, query: str, category_title: str) -> list[Paper]:
        papers: list[Paper] = []
        cursor = "*"

        for _ in range(self.max_pages):
            params = {
                "search": query,
                "per-page": min(25, self.max_results - len(papers)),
                "cursor": cursor,
            }
            try:
                r = requests.get(
                    API_URL, params=params, headers=self.headers, timeout=30
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                logger.warning("OpenAlex request failed: %s", e)
                break

            for work in data.get("results", []):
                papers.append(self._to_paper(work, category_title))
                if len(papers) >= self.max_results:
                    return papers

            cursor = data.get("meta", {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(RATE_LIMIT_SECONDS)

        return papers

    def _to_paper(self, work: dict, category_title: str) -> Paper:
        authors = [
            a["author"]["display_name"]
            for a in work.get("authorships", [])
            if a.get("author", {}).get("display_name")
        ]
        # Free legal copies (arXiv, PubMed Central, repositories) come first.
        # The primary location is usually the publisher's paywalled page.
        best_oa = work.get("best_oa_location") or {}
        primary = work.get("primary_location") or {}
        urls = unique_urls(
            best_oa.get("pdf_url"),
            (work.get("open_access") or {}).get("oa_url"),
            primary.get("pdf_url"),
            primary.get("landing_page_url"),
        )
        # The OpenAlex record page never has the PDF. Keep it only as the
        # paper's link when there is nothing else.
        urls = urls or unique_urls(work.get("id"))

        return Paper(
            title=work.get("title"),
            authors=authors or None,
            url=urls[0] if urls else None,
            doi=normalize_doi(work.get("doi")),
            candidate_urls=urls or None,
            abstract=_rebuild_abstract(work.get("abstract_inverted_index")),
            year=coerce_year(work.get("publication_year")),
            source=SourceName.OPENALEX,
            search_category=category_title,
        )


def _rebuild_abstract(inverted: dict[str, list[int]] | None) -> str | None:
    """OpenAlex returns abstracts as {token: [positions]} not a string."""
    if not inverted:
        return None
    pairs = [(pos, tok) for tok, positions in inverted.items() for pos in positions]
    pairs.sort()
    return " ".join(tok for _, tok in pairs)
