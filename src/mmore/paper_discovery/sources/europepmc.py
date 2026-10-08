import logging
import time

import requests

from ..schema import Paper, SourceName
from ._utils import first_year, normalize_doi, unique_urls
from .base import SourceAdapter

logger = logging.getLogger(__name__)

API_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
RATE_LIMIT_SECONDS = 1.0

# Europe PMC availability codes for a full-text link: open access and free.
# Other codes (subscription, registration) go after these.
FREE_AVAILABILITY = {"OA", "F"}


class EuropePmcAdapter(SourceAdapter):
    name = "europepmc"

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
                "query": query,
                "format": "json",
                "resultType": "core",
                "pageSize": min(25, self.max_results - len(papers)),
                "cursorMark": cursor,
            }
            try:
                r = requests.get(
                    API_URL, params=params, headers=self.headers, timeout=30
                )
                r.raise_for_status()
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                logger.warning("Europe PMC request failed: %s", e)
                break

            for entry in data.get("resultList", {}).get("result", []):
                papers.append(self._to_paper(entry, category_title))
                if len(papers) >= self.max_results:
                    return papers

            next_cursor = data.get("nextCursorMark")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            time.sleep(RATE_LIMIT_SECONDS)

        return papers

    def _to_paper(self, entry: dict, category_title: str) -> Paper:
        links = entry.get("fullTextUrlList", {}).get("fullTextUrl", [])
        # Free PDFs first, then other PDFs, then free pages, then the rest.
        # `sorted` is stable, so the API's order is kept within each group.
        ranked = sorted(
            links,
            key=lambda u: (
                u.get("documentStyle", "").lower() != "pdf",
                u.get("availabilityCode") not in FREE_AVAILABILITY,
            ),
        )
        urls = unique_urls(*(u.get("url") for u in ranked))
        year = first_year(entry, "pubYear", "firstPublicationDate")

        return Paper(
            title=entry.get("title"),
            authors=_parse_authors(entry),
            url=urls[0] if urls else None,
            doi=normalize_doi(entry.get("doi")),
            candidate_urls=urls or None,
            abstract=entry.get("abstractText"),
            year=year,
            source=SourceName.EUROPEPMC,
            search_category=category_title,
        )


def _parse_authors(entry: dict) -> list[str] | None:
    """Prefer the structured `authorList.author[].fullName` (available with
    `resultType=core`). Fall back to naively splitting the pre-joined
    `authorString` when the structured shape is missing - imperfect
    (some names contain commas) but better than dropping the field.
    """
    author_list = (entry.get("authorList") or {}).get("author") or []
    names = [a["fullName"] for a in author_list if a.get("fullName")]
    if names:
        return names
    raw = entry.get("authorString")
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or None
