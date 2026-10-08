"""End-to-end orchestrator: synonyms + categories -> deduplicated papers.jsonl."""

import csv
import json
import logging
from collections import Counter
from pathlib import Path

import yaml
from dacite import from_dict

from ..ux import plural, progress
from .boolean import build_boolean_queries, load_synonyms
from .config import CategoriesFile, PaperDiscoveryConfig
from .pdf import (
    DownloadResult,
    download_pdf,
    expected_pdf_path,
    extract_text,
    is_pdf_file,
)
from .schema import CategoryQuery, Paper, PdfStatus
from .sources import get_adapter

logger = logging.getLogger(__name__)


class PaperDiscoveryPipeline:
    """Runs a Paper Discovery job from one config.

    Stage 1 builds boolean queries offline. Stage 2 queries each source,
    dedupes the results, and optionally downloads PDFs and extracts text.
    """

    def __init__(self, config: PaperDiscoveryConfig):
        self.config = config
        # Filled in by `run`, for the end-of-run summary.
        self.found: Counter[str] = Counter()  # papers per source, before dedupe
        self.n_unique = 0
        self.pdf_counts: Counter[PdfStatus] = Counter()
        self.failed_list: Path | None = None

    def run(self) -> list[Paper]:
        """Run both stages and return the deduplicated papers.

        Also writes them to `config.output_file`. Ctrl+C is safe, whatever
        has been collected so far still gets written.
        """
        cfg = self.config
        synonyms = load_synonyms(cfg.synonyms_path)
        categories = _load_categories(cfg.categories_path)
        queries = build_boolean_queries(synonyms, categories)
        logger.debug("Built %d category queries", len(queries))

        all_papers: list[Paper] = []
        deduped: list[Paper] = []
        try:
            self._search(queries, all_papers)
            deduped = _dedupe(all_papers)
            logger.debug(
                "After dedupe: %d papers (from %d)", len(deduped), len(all_papers)
            )

            if cfg.download_pdfs:
                self._enrich_with_pdf_text(deduped)
        except KeyboardInterrupt:
            deduped = _dedupe(all_papers) if not deduped else deduped
            logger.warning(
                "Interrupted - writing partial results (%d papers) to %s",
                len(deduped),
                cfg.output_file,
            )

        self.n_unique = len(deduped)
        self._write_output(deduped)
        return deduped

    def summary(self) -> dict[str, object]:
        """Rows for the end-of-run card: what was found and where it went."""
        rows: dict[str, object] = {
            src: plural(n, "paper") for src, n in self.found.items()
        }
        duplicates = sum(self.found.values()) - self.n_unique
        rows["unique"] = f"{self.n_unique} ({duplicates} duplicates removed)"
        counts = self.pdf_counts
        if counts:
            cached, new = counts[PdfStatus.CACHED], counts[PdfStatus.DOWNLOADED]
            rows["full text"] = f"{cached + new} ({cached} cached · {new} new)"
            missing = sorted(
                ((s, n) for s, n in counts.items() if s not in SUCCESS and n),
                key=lambda item: -item[1],
            )
            if missing:
                rows["no full text"] = sum(n for _, n in missing)
                for status, n in missing:
                    rows[f"  · {_label(status)}"] = n
        rows["output"] = self.config.output_file
        return rows

    def next_steps(self) -> dict[str, object]:
        """What the user can do about missing PDFs. Empty if nothing."""
        steps: dict[str, object] = {}
        if self.failed_list:
            n = sum(self.pdf_counts[s] for s in MANUAL_DOWNLOAD)
            steps[f"{plural(n, 'PDF')} to get by hand"] = (
                f"Open the links in {self.failed_list} in your browser, save "
                "each PDF to its save_as path, then run again."
            )
        for status, hint in HINTS.items():
            if self.pdf_counts[status]:
                steps[f"{self.pdf_counts[status]} {_label(status)}"] = hint
        return steps

    def _search(self, queries: list[CategoryQuery], out: list[Paper]) -> None:
        """Run every query on every source, adding results to `out`."""
        cfg = self.config
        total = len(queries) * len(cfg.sources)
        with progress(total=total, desc="Searching", unit="search") as bar:
            for query in queries:
                for src_name in cfg.sources:
                    bar.set_postfix_str(f"{src_name} · {query.combination_title}")
                    papers = self._search_source(src_name, query)
                    self.found[src_name] += len(papers)
                    out.extend(papers)
                    bar.update()

    def _search_source(self, src_name: str, query: CategoryQuery) -> list[Paper]:
        cfg = self.config
        extra: dict = {}
        if src_name == "arxiv":
            if cfg.arxiv_category_map:
                extra["category_map"] = cfg.arxiv_category_map
            extra["enable_pair_query"] = cfg.arxiv_enable_pair_query
        adapter = get_adapter(
            src_name,
            user_agent=cfg.user_agent,
            max_pages=cfg.max_pages,
            max_results=cfg.max_results,
            **extra,
        )
        papers = adapter.search(query.boolean_combination, query.combination_title)
        logger.debug(
            "%s returned %d papers for %r",
            src_name,
            len(papers),
            query.combination_title,
        )
        return papers

    def _enrich_with_pdf_text(self, papers: list[Paper]) -> None:
        counts = self.pdf_counts
        with progress(total=len(papers), desc="PDFs", unit="paper") as bar:
            for paper in papers:
                counts[self._fetch_text(paper)] += 1
                failed = sum(n for s, n in counts.items() if s not in SUCCESS)
                bar.set_postfix_str(
                    f"ok={counts[PdfStatus.DOWNLOADED] + counts[PdfStatus.CACHED]} "
                    f"cache={counts[PdfStatus.CACHED]} "
                    f"refused={counts[PdfStatus.REFUSED]} failed={failed}"
                )
                bar.update()
        self.failed_list = self._write_failed_list(papers)
        _log_pdf_summary(counts)

    def _fetch_text(self, paper: Paper) -> PdfStatus:
        """Get the paper's PDF from the cache or the web, and extract its text.

        Records the outcome on the paper and returns it.
        """
        cfg = self.config
        key = paper.cache_key()
        if key is None:
            return _set_status(paper, PdfStatus.NO_URL)

        pdf_path: str | None = None
        status, http_status = PdfStatus.DOWNLOADED, None
        cached_path = expected_pdf_path(key, cfg.pdf_dir)
        if not cfg.force_redownload and cached_path.exists():
            if is_pdf_file(cached_path):
                # Cache hit - skip the HTTP fetch entirely.
                pdf_path, status = str(cached_path), PdfStatus.CACHED
            else:
                logger.debug("Removing non-PDF cache file %s", cached_path)
                cached_path.unlink(missing_ok=True)

        if pdf_path is None:
            result = self._download_first(paper, key)
            if not result.path:
                return _set_status(paper, result.outcome, result.status)
            pdf_path, http_status = result.path, result.status

        paper.extracted_text = extract_text(pdf_path, mode=cfg.pdf_extractor) or None
        # Only a PDF that gave us text counts as a success.
        if not paper.extracted_text:
            status = PdfStatus.NO_TEXT
        return _set_status(paper, status, http_status)

    def _write_failed_list(self, papers: list[Paper]) -> Path | None:
        """Write a CSV of papers to download by hand. None if there are none.

        Each row has a link to open in a browser and the exact path to save
        the PDF to. The next run finds the saved file in the cache.
        """
        cfg = self.config
        out = Path(cfg.output_file)
        path = out.with_name(f"{out.stem}_failed_pdfs.csv")
        rows = []
        for p in papers:
            key, status = p.cache_key(), p.pdf_status
            if status is None or status not in MANUAL_DOWNLOAD or key is None:
                continue
            # The DOI link resolves to the publisher's page, where a browser
            # can use the institution's sign-in.
            urls = p.download_urls()
            link = f"https://doi.org/{p.doi}" if p.doi else urls[0]
            rows.append(
                {
                    "title": p.title or "",
                    "status": status.value,
                    "http_status": p.pdf_http_status or "",
                    "link": link,
                    "save_as": str(expected_pdf_path(key, cfg.pdf_dir).resolve()),
                }
            )

        if not rows:
            path.unlink(missing_ok=True)  # don't leave last run's list behind
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        # So the user can save straight into it.
        Path(cfg.pdf_dir).mkdir(parents=True, exist_ok=True)
        # utf-8-sig so Excel shows accented titles correctly.
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _download_first(self, paper: Paper, key: str) -> DownloadResult:
        """Try each of the paper's URLs until one gives a PDF.

        If none does, reports the failure the user can act on, if any.
        """
        cfg = self.config
        results: list[DownloadResult] = []
        for url in paper.download_urls():
            result = download_pdf(
                url,
                cfg.pdf_dir,
                user_agent=cfg.user_agent,
                timeout=cfg.pdf_timeout,
                proxy_prefix=cfg.pdf_proxy_prefix,
                cache_key=key,
            )
            if result.path:
                return result
            results.append(result)
        if not results:  # a DOI but no link
            return DownloadResult(PdfStatus.NO_URL)
        for outcome in TELLING_FAILURES:
            for result in results:
                if result.outcome == outcome:
                    return result
        return results[-1]

    def _write_output(self, papers: list[Paper]) -> None:
        cfg = self.config
        out_path = Path(cfg.output_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            for paper in papers:
                f.write(json.dumps(paper.to_dict(), ensure_ascii=False) + "\n")
        logger.debug("Wrote %d papers to %s", len(papers), out_path)

        if cfg.multimodal_output_file:
            self._write_multimodal_jsonl(papers, cfg.multimodal_output_file)

    def _write_multimodal_jsonl(self, papers: list[Paper], path: str) -> None:
        """Write the results again as `MultimodalSample` JSONL, which mmore
        post-process, index and rag can read without re-processing."""
        from ..type import MultimodalSample

        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Overwrite - `to_jsonl` appends by design, so start clean.
        if out_path.exists():
            out_path.unlink()

        samples = []
        for p in papers:
            key = p.cache_key()
            pdf_path = (
                str(expected_pdf_path(key, self.config.pdf_dir))
                if key and p.extracted_text
                else ""
            )
            samples.append(p.to_multimodal_sample(pdf_path=pdf_path))
        MultimodalSample.to_jsonl(str(out_path), samples)
        logger.debug(
            "Wrote %d MultimodalSample records to %s (mmore-native shape)",
            len(samples),
            out_path,
        )


SUCCESS = {PdfStatus.DOWNLOADED, PdfStatus.CACHED}

# When every link fails, report these first: they tell the user what to do.
# A landing page with no PDF link says less than the publisher refusing us.
TELLING_FAILURES = (PdfStatus.LOGIN_PAGE, PdfStatus.REFUSED, PdfStatus.RATE_LIMITED)

# Failures a person can often fix by downloading the PDF in a browser.
MANUAL_DOWNLOAD = set(PdfStatus) - SUCCESS - {PdfStatus.NO_TEXT}

# What to do about outcomes that have a fix besides downloading by hand.
HINTS = {
    PdfStatus.LOGIN_PAGE: "Check pdf_proxy_prefix is your library's EZproxy, "
    "or unset it and use the VPN.",
    PdfStatus.RATE_LIMITED: "Run again later.",
    PdfStatus.TIMEOUT: "Raise pdf_timeout for slow publishers.",
    PdfStatus.NO_TEXT: "Probably scanned. Try pdf_extractor: full.",
}


def _set_status(
    paper: Paper, status: PdfStatus, http_status: int | None = None
) -> PdfStatus:
    paper.pdf_status, paper.pdf_http_status = status, http_status
    return status


def _log_pdf_summary(counts: "Counter[PdfStatus]") -> None:
    """One line with every outcome. Shown with MMORE_VERBOSE=1."""
    succeeded = counts[PdfStatus.DOWNLOADED] + counts[PdfStatus.CACHED]
    failures = ", ".join(
        f"{counts[s]} {_label(s)}" for s in PdfStatus if s not in SUCCESS and counts[s]
    )
    logger.debug(
        "PDF download: %d/%d succeeded (%d cached, %d fresh)%s",
        succeeded,
        sum(counts.values()),
        counts[PdfStatus.CACHED],
        counts[PdfStatus.DOWNLOADED],
        f". Not downloaded: {failures}" if failures else "",
    )


def _label(status: PdfStatus) -> str:
    """`login_page` -> `login page`, for people to read."""
    return status.value.replace("_", " ")


def _load_categories(path: str) -> dict[str, list[str]]:
    """Read `categories.yaml`, validated through the `CategoriesFile` dataclass."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return from_dict(CategoriesFile, data).categories


def _dedupe(papers: list[Paper]) -> list[Paper]:
    """Keep one paper per DOI or title, merging what the copies know.

    The first copy is kept. Later copies add their URLs as fallbacks and
    fill in a missing DOI, so a paywalled publisher link can fall back to
    the arXiv PDF of the same paper.
    """
    by_doi: dict[str, Paper] = {}
    by_title: dict[str, Paper] = {}
    out: list[Paper] = []
    for p in papers:
        title = (p.title or "").strip().lower()
        if not title and not p.doi:
            continue
        kept = by_doi.get(p.doi) if p.doi else None
        if kept is None and title:
            kept = by_title.get(title)
        if kept is None:
            kept = p
            out.append(p)
        else:
            urls = [*kept.download_urls(), *p.download_urls()]
            kept.candidate_urls = list(dict.fromkeys(urls)) or None
            kept.url = kept.url or p.url
            kept.doi = kept.doi or p.doi
        if kept.doi:
            by_doi.setdefault(kept.doi, kept)
        if title:
            by_title.setdefault(title, kept)
    return out
