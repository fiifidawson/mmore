"""End-to-end orchestrator: synonyms + categories -> deduplicated papers.jsonl."""

import json
import logging
from collections import Counter
from pathlib import Path

import yaml
from dacite import from_dict

from ..ux import progress
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

    def run(self) -> list[Paper]:
        """Run both stages and return the deduplicated papers.

        Also writes them to `config.output_file`. Ctrl+C is safe, whatever
        has been collected so far still gets written.
        """
        cfg = self.config
        synonyms = load_synonyms(cfg.synonyms_path)
        categories = _load_categories(cfg.categories_path)
        queries = build_boolean_queries(synonyms, categories)
        logger.info("Built %d category queries", len(queries))

        all_papers: list[Paper] = []
        deduped: list[Paper] = []
        try:
            for q in queries:
                all_papers.extend(self._fetch_one(q))

            deduped = _dedupe(all_papers)
            logger.info(
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

        self._write_output(deduped)
        return deduped

    def _fetch_one(self, query: CategoryQuery) -> list[Paper]:
        cfg = self.config
        out: list[Paper] = []
        for src_name in cfg.sources:
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
            logger.info("Searching %s for %r", src_name, query.combination_title)
            papers = adapter.search(query.boolean_combination, query.combination_title)
            logger.info("%s returned %d papers", src_name, len(papers))
            out.extend(papers)
        return out

    def _enrich_with_pdf_text(self, papers: list[Paper]) -> None:
        counts: Counter[PdfStatus] = Counter()
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
        _log_pdf_summary(counts)

    def _fetch_text(self, paper: Paper) -> PdfStatus:
        """Get the paper's PDF from the cache or the web, and extract its text."""
        cfg = self.config
        key = paper.cache_key()
        if key is None:
            return PdfStatus.NO_URL

        pdf_path: str | None = None
        status = PdfStatus.DOWNLOADED
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
                return result.outcome
            pdf_path = result.path

        paper.extracted_text = extract_text(pdf_path, mode=cfg.pdf_extractor) or None
        # Only a PDF that gave us text counts as a success.
        return status if paper.extracted_text else PdfStatus.NO_TEXT

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
        logger.info("Wrote %d papers to %s", len(papers), out_path)

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
        logger.info(
            "Wrote %d MultimodalSample records to %s (mmore-native shape)",
            len(samples),
            out_path,
        )


SUCCESS = {PdfStatus.DOWNLOADED, PdfStatus.CACHED}

# When every link fails, report these first: they tell the user what to do.
# A landing page with no PDF link says less than the publisher refusing us.
TELLING_FAILURES = (PdfStatus.LOGIN_PAGE, PdfStatus.REFUSED, PdfStatus.RATE_LIMITED)


def _log_pdf_summary(counts: "Counter[PdfStatus]") -> None:
    """One summary line, then a hint for each failure the user can act on."""
    succeeded = counts[PdfStatus.DOWNLOADED] + counts[PdfStatus.CACHED]
    failures = ", ".join(
        f"{counts[s]} {s.value.replace('_', ' ')}"
        for s in PdfStatus
        if s not in SUCCESS and counts[s]
    )
    logger.info(
        "PDF download: %d/%d succeeded (%d cached, %d fresh)%s",
        succeeded,
        sum(counts.values()),
        counts[PdfStatus.CACHED],
        counts[PdfStatus.DOWNLOADED],
        f". Not downloaded: {failures}" if failures else "",
    )

    if counts[PdfStatus.NO_TEXT]:
        logger.warning(
            "%d PDFs gave no text. They may be scanned images. "
            "Try `pdf_extractor: full`.",
            counts[PdfStatus.NO_TEXT],
        )
    if counts[PdfStatus.LOGIN_PAGE]:
        logger.warning(
            "%d downloads returned a sign-in page instead of a PDF. "
            "This pipeline cannot log in for you. If you set "
            "`pdf_proxy_prefix`, check the host is your institution's "
            "real EZproxy and that you can reach it. If your institution "
            "grants access by VPN instead, unset `pdf_proxy_prefix` and "
            "connect to the VPN.",
            counts[PdfStatus.LOGIN_PAGE],
        )
    if counts[PdfStatus.REFUSED]:
        logger.info(
            "%d PDFs were refused by the publisher (401/402/403). Either "
            "there is no subscription, or the publisher blocks automated "
            "tools. Being on the VPN does not always help. Set "
            "`download_pdfs: false` to keep metadata and abstracts only.",
            counts[PdfStatus.REFUSED],
        )
    if counts[PdfStatus.RATE_LIMITED]:
        logger.info(
            "%d PDFs were still rate-limited (429) after retries. Rerun "
            "later. PDFs already downloaded are reused.",
            counts[PdfStatus.RATE_LIMITED],
        )
    if counts[PdfStatus.TIMEOUT]:
        logger.info(
            "%d PDFs timed out. Raise `pdf_timeout` for slow publishers or proxies.",
            counts[PdfStatus.TIMEOUT],
        )


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
