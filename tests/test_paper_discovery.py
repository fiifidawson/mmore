import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from mmore.paper_discovery.boolean import (
    _sanitize_term,
    build_boolean_queries,
    load_synonyms,
)
from mmore.paper_discovery.config import PaperDiscoveryConfig
from mmore.paper_discovery.pdf import (
    MAX_RETRIES,
    DownloadResult,
    _find_pdf_link,
    _looks_like_login_page,
    _looks_like_pdf,
    _proxify,
    download_pdf,
    expected_pdf_path,
)
from mmore.paper_discovery.pipeline import PaperDiscoveryPipeline, _dedupe
from mmore.paper_discovery.schema import Paper, PdfStatus, SourceName, SynonymEntry
from mmore.paper_discovery.sources._utils import (
    coerce_year,
    first_year,
    normalize_doi,
    unique_urls,
)
from mmore.paper_discovery.sources.arxiv import (
    _build_simplified_queries,
    _extract_terms,
    _parse_atom,
)
from mmore.paper_discovery.sources.europepmc import EuropePmcAdapter, _parse_authors
from mmore.paper_discovery.sources.openalex import OpenAlexAdapter, _rebuild_abstract

# ---------------------------------------------------------------------------
# Stage 1: boolean builder
# ---------------------------------------------------------------------------


class TestBooleanBuilder:
    def test_or_group_is_alphabetical(self):
        syns = [SynonymEntry(word="LLM", synonyms=["GPT", "foundation model"])]
        queries = build_boolean_queries(syns, {"Cat": ["LLM"]})
        assert len(queries) == 1
        # OR group is sorted, AND-joined
        assert '"GPT"' in queries[0].boolean_combination
        assert '"LLM"' in queries[0].boolean_combination

    def test_unknown_word_logged_not_raised(self, caplog):
        syns = [SynonymEntry(word="LLM", synonyms=["GPT"])]
        queries = build_boolean_queries(syns, {"Cat": ["LLM", "UnknownWord"]})
        assert len(queries) == 1
        assert "UnknownWord" in caplog.text

    def test_empty_category_dropped(self):
        syns = [SynonymEntry(word="LLM", synonyms=["GPT"])]
        queries = build_boolean_queries(syns, {"Cat": ["NotPresent"]})
        assert queries == []


# ---------------------------------------------------------------------------
# OpenAlex helpers + adapter
# ---------------------------------------------------------------------------


class TestRebuildAbstract:
    def test_empty(self):
        assert _rebuild_abstract(None) is None
        assert _rebuild_abstract({}) is None

    def test_orders_tokens_by_position(self):
        inverted = {"world": [1], "hello": [0]}
        assert _rebuild_abstract(inverted) == "hello world"


class TestOpenAlexAdapter:
    def test_returns_empty_on_request_failure(self):
        adapter = OpenAlexAdapter(max_pages=1, max_results=10)
        with patch(
            "mmore.paper_discovery.sources.openalex.requests.get",
            side_effect=Exception("boom"),
        ):
            assert adapter.search("anything", "Cat") == []

    def test_parses_one_result(self):
        adapter = OpenAlexAdapter(max_pages=1, max_results=5)
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {
                    "title": "A Paper",
                    "publication_year": 2024,
                    "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
                    "primary_location": {"pdf_url": "http://x/p.pdf"},
                    "abstract_inverted_index": {"hi": [0]},
                }
            ],
            "meta": {"next_cursor": None},
        }
        mock_response.raise_for_status = MagicMock()
        with patch(
            "mmore.paper_discovery.sources.openalex.requests.get",
            return_value=mock_response,
        ):
            papers = adapter.search("q", "MyCat")
        assert len(papers) == 1
        p = papers[0]
        assert isinstance(p, Paper)
        assert p.title == "A Paper"
        assert p.year == 2024
        assert p.authors == ["Ada Lovelace"]
        assert p.url == "http://x/p.pdf"
        assert p.abstract == "hi"
        assert p.source == "openalex"
        assert p.search_category == "MyCat"


# ---------------------------------------------------------------------------
# arXiv simplification
# ---------------------------------------------------------------------------


class TestArxivSimplification:
    def test_extract_terms_drops_stopwords(self):
        q = '"LLM" OR "GPT" AND "data"'
        assert _extract_terms(q) == ["LLM", "GPT"]

    def test_extract_terms_empty(self):
        assert _extract_terms("") == []

    def test_build_simplified_includes_pair(self):
        queries = _build_simplified_queries(["LLM", "GPT", "BERT"], top_n=4)
        assert 'all:"LLM"' in queries
        assert 'all:"GPT"' in queries
        assert any("AND" in q for q in queries)

    def test_build_simplified_pair_disabled(self):
        queries = _build_simplified_queries(
            ["LLM", "GPT", "BERT"], top_n=4, enable_pair=False
        )
        assert 'all:"LLM"' in queries
        assert not any("AND" in q for q in queries)


# ---------------------------------------------------------------------------
# Login-page detection
# ---------------------------------------------------------------------------


class TestLooksLikeLoginPage:
    def _resp(self, body, ctype="text/html; charset=UTF-8"):
        r = MagicMock()
        r.headers = {"Content-Type": ctype}
        r.text = body
        return r

    def test_detects_a_shibboleth_page(self):
        assert _looks_like_login_page(
            self._resp("<html><body>Shibboleth Identity Provider</body></html>")
        )

    def test_detects_a_password_form(self):
        assert _looks_like_login_page(
            self._resp('<form><input type="password" name="pw"></form>')
        )

    def test_ignores_non_html(self):
        # A real PDF must never be mistaken for a login page.
        assert not _looks_like_login_page(
            self._resp("%PDF-1.7 ...", ctype="application/pdf")
        )

    def test_ignores_ordinary_article_html(self):
        assert not _looks_like_login_page(
            self._resp("<html><body>Abstract: we introduce...</body></html>")
        )


class TestProxify:
    def test_wraps_the_url_when_a_prefix_is_set(self):
        out = _proxify("https://x.com/a.pdf", "https://ezproxy.example.edu")
        assert out.startswith("https://ezproxy.example.edu/login?url=")
        assert "https%3A%2F%2Fx.com%2Fa.pdf" in out

    def test_is_a_noop_without_a_prefix(self):
        assert _proxify("https://x.com/a.pdf", None) == "https://x.com/a.pdf"

    def test_does_not_double_wrap(self):
        already = "https://ezproxy.example.edu/login?url=https%3A%2F%2Fx.com"
        assert _proxify(already, "https://ezproxy.example.edu") == already

    def test_tolerates_a_trailing_slash_on_the_prefix(self):
        out = _proxify("https://x.com/a.pdf", "https://ezproxy.example.edu/")
        assert "//login?url=" not in out


class TestLooksLikePdf:
    def _resp(
        self, content, url="https://example.org/paper.pdf", ctype="application/pdf"
    ):
        r = MagicMock()
        r.content = content
        r.url = url
        r.headers = {"Content-Type": ctype}
        return r

    def test_accepts_the_pdf_signature(self):
        assert _looks_like_pdf(self._resp(b"%PDF-1.7\n..."))

    def test_rejects_html_from_a_pdf_url(self):
        # The URL and Content-Type both say PDF, but the body is a login page.
        assert not _looks_like_pdf(self._resp(b"<html><body>Sign in</body></html>"))


class TestDownloadPdf:
    def _resp(self, content, ctype):
        r = MagicMock()
        r.status_code = 200
        r.content = content
        r.text = content.decode("utf-8", errors="ignore")
        r.url = "https://example.org/paper.pdf"
        r.headers = {"Content-Type": ctype}
        return r

    def test_login_page_on_a_pdf_url_is_not_saved(self, tmp_path):
        login = self._resp(b'<form><input type="password"></form>', "text/html")
        with patch("mmore.paper_discovery.pdf.requests.get", return_value=login):
            result = download_pdf("https://example.org/paper.pdf", str(tmp_path))
        assert result.path is None
        assert result.outcome == PdfStatus.LOGIN_PAGE
        assert not any(tmp_path.iterdir())

    def _landing_then_pdf(self, final_url):
        landing = self._resp(
            b'<meta name="citation_pdf_url" content="/pdf/1.pdf">', "text/html"
        )
        landing.url = final_url  # where the request ended up after redirects
        return [landing, self._resp(b"%PDF-1.7\n", "application/pdf")]

    def test_pdf_from_a_landing_page_is_cached_under_the_paper_url(self, tmp_path):
        url = "https://example.org/article/1"
        with patch(
            "mmore.paper_discovery.pdf.requests.get",
            side_effect=self._landing_then_pdf(url),
        ):
            result = download_pdf(url, str(tmp_path))
        assert result.path == str(expected_pdf_path(url, str(tmp_path)))

    def test_relative_pdf_link_follows_the_redirect(self, tmp_path):
        get = MagicMock(
            side_effect=self._landing_then_pdf("https://publisher.example.org/a/1")
        )
        with patch("mmore.paper_discovery.pdf.requests.get", get):
            download_pdf("https://doi.org/10.1/abc", str(tmp_path))
        assert (
            get.call_args_list[1].args[0] == "https://publisher.example.org/pdf/1.pdf"
        )


class TestExpectedPdfPath:
    def test_urls_ending_alike_get_different_files(self, tmp_path):
        a = expected_pdf_path("https://a.org/article/1/pdf", str(tmp_path))
        b = expected_pdf_path("https://b.org/article/2/pdf", str(tmp_path))
        assert a != b

    def test_same_url_gets_the_same_file(self, tmp_path):
        url = "https://a.org/article/1/pdf"
        assert expected_pdf_path(url, str(tmp_path)) == expected_pdf_path(
            url, str(tmp_path)
        )


class TestFindPdfLink:
    def test_prefers_citation_pdf_url(self):
        html = """
        <html><head>
          <meta name="citation_pdf_url" content="/article/1/main.pdf">
        </head><body>
          <a href="/related/2/pdf">Related article</a>
        </body></html>
        """
        assert (
            _find_pdf_link(html, base="https://pub.org/article/1")
            == "https://pub.org/article/1/main.pdf"
        )

    def test_falls_back_to_links(self):
        html = '<a href="/article/1/pdf">PDF</a>'
        assert (
            _find_pdf_link(html, base="https://pub.org/article/1")
            == "https://pub.org/article/1/pdf"
        )


class TestEnrichWithPdfText:
    def _pipeline(self, tmp_path):
        cfg = PaperDiscoveryConfig(
            synonyms_path="unused",
            categories_path="unused",
            output_file=str(tmp_path / "out.jsonl"),
            pdf_dir=str(tmp_path / "pdfs"),
        )
        return PaperDiscoveryPipeline(cfg)

    def _download_ok(self, tmp_path):
        def fake(url, save_dir, cache_key=None, **kwargs):
            path = expected_pdf_path(cache_key or url, save_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.7\n")
            return DownloadResult(PdfStatus.DOWNLOADED, path=str(path))

        return fake

    def test_empty_text_is_not_a_success(self, tmp_path, caplog):
        paper = Paper(title="T", url="https://example.org/a.pdf")
        with (
            patch(
                "mmore.paper_discovery.pipeline.download_pdf",
                side_effect=self._download_ok(tmp_path),
            ),
            patch("mmore.paper_discovery.pipeline.extract_text", return_value=""),
            caplog.at_level("INFO"),
        ):
            self._pipeline(tmp_path)._enrich_with_pdf_text([paper])
        assert paper.extracted_text is None
        assert "0/1 succeeded" in caplog.text
        assert "1 no text" in caplog.text

    def test_non_pdf_cache_file_is_replaced(self, tmp_path, caplog):
        pipeline = self._pipeline(tmp_path)
        url = "https://example.org/a.pdf"
        paper = Paper(title="T", url=url)
        cached = expected_pdf_path(url, pipeline.config.pdf_dir)
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"<html>login</html>")

        download = MagicMock(side_effect=self._download_ok(tmp_path))
        with (
            patch("mmore.paper_discovery.pipeline.download_pdf", download),
            patch("mmore.paper_discovery.pipeline.extract_text", return_value="text"),
            caplog.at_level("INFO"),
        ):
            pipeline._enrich_with_pdf_text([paper])
        download.assert_called_once()
        assert cached.read_bytes().startswith(b"%PDF-")
        assert paper.extracted_text == "text"
        assert "1/1 succeeded (0 cached, 1 fresh)" in caplog.text

    def test_valid_cache_file_skips_download(self, tmp_path, caplog):
        pipeline = self._pipeline(tmp_path)
        url = "https://example.org/a.pdf"
        paper = Paper(title="T", url=url)
        cached = expected_pdf_path(url, pipeline.config.pdf_dir)
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"%PDF-1.7\n")

        download = MagicMock()
        with (
            patch("mmore.paper_discovery.pipeline.download_pdf", download),
            patch("mmore.paper_discovery.pipeline.extract_text", return_value="text"),
            caplog.at_level("INFO"),
        ):
            pipeline._enrich_with_pdf_text([paper])
        download.assert_not_called()
        assert "1/1 succeeded (1 cached, 0 fresh)" in caplog.text


class TestTryEveryUrl:
    def _pipeline(self, tmp_path):
        cfg = PaperDiscoveryConfig(
            synonyms_path="unused",
            categories_path="unused",
            output_file=str(tmp_path / "out.jsonl"),
            pdf_dir=str(tmp_path / "pdfs"),
        )
        return PaperDiscoveryPipeline(cfg)

    def test_falls_back_to_the_next_url(self, tmp_path, caplog):
        pipeline = self._pipeline(tmp_path)
        paper = Paper(
            title="T",
            doi="10.1000/xyz",
            candidate_urls=[
                "https://example.org/oa.pdf",
                "https://publisher.example.org/paper.pdf",
            ],
        )
        cached = expected_pdf_path("doi:10.1000/xyz", pipeline.config.pdf_dir)

        def fake(url, save_dir, cache_key=None, **kwargs):
            if url == "https://example.org/oa.pdf":
                return DownloadResult(PdfStatus.NETWORK_ERROR)
            path = expected_pdf_path(cache_key or url, save_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.7\n")
            return DownloadResult(PdfStatus.DOWNLOADED, path=str(path))

        download = MagicMock(side_effect=fake)
        with (
            patch("mmore.paper_discovery.pipeline.download_pdf", download),
            patch("mmore.paper_discovery.pipeline.extract_text", return_value="text"),
            caplog.at_level("INFO"),
        ):
            pipeline._enrich_with_pdf_text([paper])
        assert download.call_count == 2
        assert cached.exists()  # named after the DOI, not the URL
        assert "1/1 succeeded" in caplog.text


class TestDownloadPdfCacheKey:
    def test_pdf_found_on_landing_page_is_saved_under_the_key(self, tmp_path):
        landing = MagicMock(status_code=200, url="https://example.org/article")
        landing.content = b"<html></html>"
        landing.text = '<meta name="citation_pdf_url" content="/main.pdf">'
        landing.headers = {"Content-Type": "text/html"}
        pdf = MagicMock(status_code=200, url="https://example.org/main.pdf")
        pdf.content = b"%PDF-1.7\n"
        pdf.headers = {"Content-Type": "application/pdf"}

        with patch(
            "mmore.paper_discovery.pdf.requests.get", side_effect=[landing, pdf]
        ):
            result = download_pdf(
                "https://example.org/article", str(tmp_path), cache_key="doi:10.1/a"
            )
        assert result.path == str(expected_pdf_path("doi:10.1/a", str(tmp_path)))


class TestDedupe:
    def test_merges_urls_of_the_same_doi(self):
        a = Paper(title="A", doi="10.1/a", candidate_urls=["https://pub.org/a"])
        b = Paper(title="A (preprint)", doi="10.1/a", url="https://arxiv.org/a.pdf")
        out = _dedupe([a, b])
        assert out == [a]
        assert a.candidate_urls == ["https://pub.org/a", "https://arxiv.org/a.pdf"]

    def test_merges_by_title_and_fills_the_doi(self):
        a = Paper(title="Same Title", url="https://arxiv.org/a.pdf")
        b = Paper(title="same title ", doi="10.1/a", url="https://pub.org/a")
        out = _dedupe([a, b])
        assert out == [a]
        assert a.doi == "10.1/a"
        assert a.download_urls() == ["https://arxiv.org/a.pdf", "https://pub.org/a"]

    def test_keeps_different_papers(self):
        out = _dedupe([Paper(title="A"), Paper(title="B"), Paper(title=None)])
        assert [p.title for p in out] == ["A", "B"]


# ---------------------------------------------------------------------------
# Shared source helpers
# ---------------------------------------------------------------------------


class TestCoerceYear:
    def test_handles_the_shapes_each_source_returns(self):
        assert coerce_year(2024) == 2024  # OpenAlex: int
        assert coerce_year("2024") == 2024  # Google Scholar: year string
        assert coerce_year("2024-01-15T00:00:00Z") == 2024  # arXiv: ISO date
        assert coerce_year(" 2024 ") == 2024  # padded

    def test_returns_none_when_unusable(self):
        assert coerce_year(None) is None
        assert coerce_year("") is None
        assert coerce_year("n/a") is None

    def test_first_year_takes_the_first_parseable_key(self):
        entry = {"pubYear": "", "firstPublicationDate": "2019-04-02"}
        assert first_year(entry, "pubYear", "firstPublicationDate") == 2019

    def test_first_year_returns_none_when_no_key_parses(self):
        assert first_year({"pubYear": "n/a"}, "pubYear", "missing") is None


# ---------------------------------------------------------------------------
# SourceName enum
# ---------------------------------------------------------------------------


class TestSourceName:
    def test_serializes_as_plain_string(self):
        # The (str, Enum) mixin is what keeps papers.jsonl unchanged.
        # Without it json.dumps would emit "SourceName.ARXIV".
        payload = json.dumps(Paper(title="t", source=SourceName.ARXIV).to_dict())
        assert '"source": "arxiv"' in payload

    def test_compares_equal_to_its_string_value(self):
        assert SourceName.OPENALEX == "openalex"

    def test_covers_every_registered_source(self):
        from mmore.paper_discovery.sources import REGISTRY

        assert {s.value for s in SourceName} == set(REGISTRY)


# ---------------------------------------------------------------------------
# Europe PMC parsing
# ---------------------------------------------------------------------------


class TestEuropePmcToPaper:
    def _entry(self, urls):
        return {
            "title": "A Paper",
            "fullTextUrlList": {"fullTextUrl": urls},
            "pubYear": "2024",
        }

    def test_prefers_pdf_url_over_landing_page(self):
        # Regression: the key was misspelled `docementStyle`, so this branch
        # never matched and every result fell through to the landing page.
        adapter = EuropePmcAdapter()
        entry = self._entry(
            [
                {"url": "http://x/landing", "documentStyle": "html"},
                {"url": "http://x/paper.pdf", "documentStyle": "pdf"},
            ]
        )
        assert adapter._to_paper(entry, "Cat").url == "http://x/paper.pdf"

    def test_falls_back_to_first_url_when_no_pdf(self):
        adapter = EuropePmcAdapter()
        entry = self._entry([{"url": "http://x/landing", "documentStyle": "html"}])
        assert adapter._to_paper(entry, "Cat").url == "http://x/landing"

    def test_url_is_none_when_no_urls(self):
        adapter = EuropePmcAdapter()
        assert adapter._to_paper(self._entry([]), "Cat").url is None


class TestEuropePmcParseAuthors:
    def test_prefers_structured_author_list(self):
        entry = {
            "authorList": {
                "author": [
                    {"fullName": "Ada Lovelace"},
                    {"fullName": "Alan Turing"},
                ]
            },
            "authorString": "Lovelace A, Turing A",
        }
        assert _parse_authors(entry) == ["Ada Lovelace", "Alan Turing"]

    def test_falls_back_to_author_string_when_structured_missing(self):
        entry = {"authorString": "Lovelace A, Turing A"}
        assert _parse_authors(entry) == ["Lovelace A", "Turing A"]

    def test_returns_none_when_both_missing(self):
        assert _parse_authors({}) is None


class TestLoadSynonyms:
    def test_loads_jsonl_line_per_object(self, tmp_path):
        f = tmp_path / "syns.jsonl"
        f.write_text(
            '{"word": "LLM", "synonyms": ["GPT"]}\n'
            '{"word": "Crisis", "synonyms": ["disaster response"]}\n'
        )
        entries = load_synonyms(f)
        assert [e.word for e in entries] == ["LLM", "Crisis"]
        assert entries[1].synonyms == ["disaster response"]

    def test_jsonl_skips_blank_lines(self, tmp_path):
        f = tmp_path / "syns.jsonl"
        f.write_text(
            '{"word": "LLM", "synonyms": ["GPT"]}\n'
            "\n"
            "   \n"
            '{"word": "Crisis", "synonyms": ["disaster"]}\n'
        )
        entries = load_synonyms(f)
        assert [e.word for e in entries] == ["LLM", "Crisis"]

    def test_sanitize_strips_embedded_double_quote(self):
        # The reviewer's concern: `"` inside a term would close the quoted
        # phrase early and produce a malformed boolean query.
        assert _sanitize_term('bad"term') == "badterm"
        assert _sanitize_term('  many   spaces  "x" ') == "many spaces x"


# ---------------------------------------------------------------------------
# Smoke test on Paper schema
# ---------------------------------------------------------------------------


class TestPaperSchema:
    def test_to_dict_includes_all_fields(self):
        p = Paper(title="t", source=SourceName.ARXIV)
        d = p.to_dict()
        for k in (
            "title",
            "authors",
            "url",
            "abstract",
            "year",
            "extracted_text",
            "source",
            "search_category",
        ):
            assert k in d

    def test_nullable_fields_default_none(self):
        p = Paper()
        assert p.title is None
        assert p.year is None


class TestPaperToMultimodalSample:
    def test_text_prefers_extracted_over_abstract(self):
        p = Paper(title="T", abstract="abs", extracted_text="full")
        s = p.to_multimodal_sample()
        assert s.text == "full"

    def test_text_falls_back_to_abstract_then_title(self):
        assert Paper(abstract="abs", title="T").to_multimodal_sample().text == "abs"
        assert Paper(title="T").to_multimodal_sample().text == "T"
        assert Paper().to_multimodal_sample().text == ""

    def test_metadata_carries_paper_fields(self):
        p = Paper(
            title="A Paper",
            authors=["Ada Lovelace"],
            url="http://x/p.pdf",
            abstract="abs",
            year=2024,
            source=SourceName.ARXIV,
            search_category="Cat",
        )
        s = p.to_multimodal_sample(pdf_path="/tmp/p.pdf")
        assert s.metadata.file_path == "/tmp/p.pdf"
        assert s.metadata.processor_type == "paper_discovery"
        assert s.metadata.extra["title"] == "A Paper"
        assert s.metadata.extra["source"] == "arxiv"
        assert s.metadata.extra["search_category"] == "Cat"
        assert s.metadata.extra["year"] == 2024

    def test_none_fields_dropped_from_extra(self):
        p = Paper(title="T", source=SourceName.ARXIV)  # authors, url, year, ... = None
        s = p.to_multimodal_sample()
        assert "authors" not in s.metadata.extra
        assert "year" not in s.metadata.extra
        assert s.metadata.extra["title"] == "T"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ---------------------------------------------------------------------------
# Open-access URLs and DOIs
# ---------------------------------------------------------------------------


class TestNormalizeDoi:
    def test_handles_the_shapes_sources_return(self):
        assert normalize_doi("https://doi.org/10.1000/XYZ") == "10.1000/xyz"
        assert normalize_doi("doi:10.1000/xyz") == "10.1000/xyz"
        assert normalize_doi(" 10.1000/xyz ") == "10.1000/xyz"

    def test_rejects_non_dois(self):
        assert normalize_doi(None) is None
        assert normalize_doi("") is None
        assert normalize_doi("not a doi") is None


def test_unique_urls_keeps_order_and_drops_repeats():
    assert unique_urls("a", None, "b", "a", "") == ["a", "b"]


class TestPaperUrls:
    def test_cache_key_prefers_the_doi(self):
        p = Paper(doi="10.1/a", url="https://example.org/a.pdf")
        assert p.cache_key() == "doi:10.1/a"

    def test_cache_key_falls_back_to_the_url(self):
        url = "https://example.org/a.pdf"
        assert Paper(url=url).cache_key() == url
        assert Paper().cache_key() is None

    def test_download_urls_falls_back_to_url(self):
        assert Paper(url="u").download_urls() == ["u"]
        assert Paper(url="u", candidate_urls=["c", "u"]).download_urls() == ["c", "u"]

    def test_to_dict_includes_doi_and_candidates(self):
        d = Paper(doi="10.1/a", candidate_urls=["u"]).to_dict()
        assert d["doi"] == "10.1/a"
        assert d["candidate_urls"] == ["u"]


class TestOpenAlexOpenAccess:
    def test_open_access_copy_comes_first(self):
        work = {
            "title": "A Paper",
            "doi": "https://doi.org/10.1/A",
            "best_oa_location": {"pdf_url": "https://arxiv.org/pdf/1"},
            "open_access": {"oa_url": "https://europepmc.org/1"},
            "primary_location": {
                "pdf_url": "https://publisher.org/1.pdf",
                "landing_page_url": "https://publisher.org/1",
            },
        }
        p = OpenAlexAdapter()._to_paper(work, "Cat")
        assert p.url == "https://arxiv.org/pdf/1"
        assert p.doi == "10.1/a"
        assert p.candidate_urls == [
            "https://arxiv.org/pdf/1",
            "https://europepmc.org/1",
            "https://publisher.org/1.pdf",
            "https://publisher.org/1",
        ]

    def test_falls_back_to_primary_location(self):
        work = {"primary_location": {"pdf_url": "https://publisher.org/1.pdf"}}
        p = OpenAlexAdapter()._to_paper(work, "Cat")
        assert p.url == "https://publisher.org/1.pdf"
        assert p.doi is None


class TestEuropePmcOpenAccess:
    def test_free_pdf_comes_before_subscription_pdf(self):
        links = [
            {
                "url": "https://pub.org/1.pdf",
                "documentStyle": "pdf",
                "availabilityCode": "S",
            },
            {
                "url": "https://pub.org/1",
                "documentStyle": "html",
                "availabilityCode": "OA",
            },
            {
                "url": "https://pmc.org/1.pdf",
                "documentStyle": "pdf",
                "availabilityCode": "OA",
            },
        ]
        entry = {"doi": "10.1/A", "fullTextUrlList": {"fullTextUrl": links}}
        p = EuropePmcAdapter()._to_paper(entry, "Cat")
        assert p.doi == "10.1/a"
        assert p.candidate_urls == [
            "https://pmc.org/1.pdf",
            "https://pub.org/1.pdf",
            "https://pub.org/1",
        ]


class TestArxivDoi:
    def test_reads_the_doi_when_present(self):
        xml = """<feed xmlns="http://www.w3.org/2005/Atom"
                       xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry>
            <id>http://arxiv.org/abs/1</id>
            <title>A Paper</title>
            <link type="application/pdf" href="http://arxiv.org/pdf/1"/>
            <arxiv:doi>10.1/A</arxiv:doi>
          </entry>
        </feed>"""
        (p,) = _parse_atom(xml, "Cat")
        assert p.doi == "10.1/a"
        assert p.candidate_urls == ["http://arxiv.org/pdf/1", "http://arxiv.org/abs/1"]


# ---------------------------------------------------------------------------
# Failure outcomes and retries
# ---------------------------------------------------------------------------


def _response(status=200, content=b"%PDF-1.7\n", ctype="application/pdf", headers=None):
    r = MagicMock(status_code=status, url="https://example.org/a.pdf")
    r.content = content
    r.text = content.decode("utf-8", errors="ignore")
    r.headers = {"Content-Type": ctype, **(headers or {})}
    return r


class TestDownloadOutcomes:
    @pytest.mark.parametrize(
        "status, outcome",
        [
            (401, PdfStatus.REFUSED),
            (403, PdfStatus.REFUSED),
            (404, PdfStatus.NOT_FOUND),
            (410, PdfStatus.NOT_FOUND),
            (418, PdfStatus.HTTP_ERROR),
        ],
    )
    def test_status_codes_map_to_outcomes(self, tmp_path, status, outcome):
        with patch(
            "mmore.paper_discovery.pdf.requests.get", return_value=_response(status)
        ):
            result = download_pdf("https://example.org/a.pdf", str(tmp_path))
        assert result.outcome == outcome
        assert result.status == status
        assert result.path is None

    def test_success_is_downloaded(self, tmp_path):
        with patch("mmore.paper_discovery.pdf.requests.get", return_value=_response()):
            result = download_pdf("https://example.org/a.pdf", str(tmp_path))
        assert result.outcome == PdfStatus.DOWNLOADED
        assert result.path

    def test_landing_page_without_a_pdf_link(self, tmp_path):
        page = _response(content=b"<html>no links</html>", ctype="text/html")
        with patch("mmore.paper_discovery.pdf.requests.get", return_value=page):
            result = download_pdf("https://example.org/article", str(tmp_path))
        assert result.outcome == PdfStatus.NO_PDF_LINK

    def test_pdf_link_that_is_not_a_pdf(self, tmp_path):
        page = _response(content=b'<a href="/x/pdf">PDF</a>', ctype="text/html")
        html = _response(content=b"<html>viewer</html>", ctype="text/html")
        with patch("mmore.paper_discovery.pdf.requests.get", side_effect=[page, html]):
            result = download_pdf("https://example.org/article", str(tmp_path))
        assert result.outcome == PdfStatus.NOT_PDF

    def test_network_error_is_not_retried(self, tmp_path):
        get = MagicMock(side_effect=requests.ConnectionError("dns"))
        with patch("mmore.paper_discovery.pdf.requests.get", get):
            result = download_pdf("https://example.org/a.pdf", str(tmp_path))
        assert result.outcome == PdfStatus.NETWORK_ERROR
        assert get.call_count == 1


class TestDownloadRetries:
    def test_server_error_is_retried(self, tmp_path):
        get = MagicMock(side_effect=[_response(503), _response()])
        with (
            patch("mmore.paper_discovery.pdf.requests.get", get),
            patch("mmore.paper_discovery.pdf.time.sleep") as sleep,
        ):
            result = download_pdf("https://example.org/a.pdf", str(tmp_path))
        assert result.outcome == PdfStatus.DOWNLOADED
        assert get.call_count == 2
        sleep.assert_called_once()

    def test_rate_limit_honours_retry_after(self, tmp_path):
        limited = _response(429, headers={"Retry-After": "5"})
        get = MagicMock(side_effect=[limited, _response()])
        with (
            patch("mmore.paper_discovery.pdf.requests.get", get),
            patch("mmore.paper_discovery.pdf.time.sleep") as sleep,
        ):
            result = download_pdf("https://example.org/a.pdf", str(tmp_path))
        assert result.outcome == PdfStatus.DOWNLOADED
        sleep.assert_called_once_with(5.0)

    def test_persistent_rate_limit_is_reported(self, tmp_path):
        get = MagicMock(return_value=_response(429))
        with (
            patch("mmore.paper_discovery.pdf.requests.get", get),
            patch("mmore.paper_discovery.pdf.time.sleep"),
        ):
            result = download_pdf("https://example.org/a.pdf", str(tmp_path))
        assert result.outcome == PdfStatus.RATE_LIMITED
        assert get.call_count == MAX_RETRIES + 1

    def test_persistent_timeout_is_reported(self, tmp_path):
        get = MagicMock(side_effect=requests.Timeout())
        with (
            patch("mmore.paper_discovery.pdf.requests.get", get),
            patch("mmore.paper_discovery.pdf.time.sleep"),
        ):
            result = download_pdf("https://example.org/a.pdf", str(tmp_path))
        assert result.outcome == PdfStatus.TIMEOUT
        assert get.call_count == MAX_RETRIES + 1

    def test_refused_is_not_retried(self, tmp_path):
        get = MagicMock(return_value=_response(403))
        with patch("mmore.paper_discovery.pdf.requests.get", get):
            download_pdf("https://example.org/a.pdf", str(tmp_path))
        assert get.call_count == 1


class TestPipelineOutcomes:
    def _pipeline(self, tmp_path, **kwargs):
        cfg = PaperDiscoveryConfig(
            synonyms_path="unused",
            categories_path="unused",
            output_file=str(tmp_path / "out.jsonl"),
            pdf_dir=str(tmp_path / "pdfs"),
            **kwargs,
        )
        return PaperDiscoveryPipeline(cfg)

    def test_refusal_is_reported_over_a_page_with_no_link(self, tmp_path, caplog):
        paper = Paper(title="T", candidate_urls=["https://a.org/1", "https://b.org/1"])
        results = [
            DownloadResult(PdfStatus.REFUSED, status=403),
            DownloadResult(PdfStatus.NO_PDF_LINK, status=200),
        ]
        with (
            patch("mmore.paper_discovery.pipeline.download_pdf", side_effect=results),
            caplog.at_level("INFO"),
        ):
            self._pipeline(tmp_path)._enrich_with_pdf_text([paper])
        assert "0/1 succeeded" in caplog.text
        assert "Not downloaded: 1 refused" in caplog.text

    def test_counts_each_outcome(self, tmp_path, caplog):
        papers = [
            Paper(title="A", url="https://a.org/1"),
            Paper(title="B", url="https://b.org/1"),
            Paper(title="C"),
        ]
        results = [
            DownloadResult(PdfStatus.NOT_FOUND, status=404),
            DownloadResult(PdfStatus.TIMEOUT),
        ]
        with (
            patch("mmore.paper_discovery.pipeline.download_pdf", side_effect=results),
            caplog.at_level("INFO"),
        ):
            self._pipeline(tmp_path)._enrich_with_pdf_text(papers)
        assert "0/3 succeeded" in caplog.text
        assert "1 no url, 1 not found, 1 timeout" in caplog.text
        assert "Raise `pdf_timeout`" in caplog.text

    def test_doi_without_links_is_no_url(self, tmp_path, caplog):
        with caplog.at_level("INFO"):
            self._pipeline(tmp_path)._enrich_with_pdf_text([Paper(doi="10.1/a")])
        assert "1 no url" in caplog.text

    def test_pdf_timeout_is_passed_on(self, tmp_path):
        download = MagicMock(return_value=DownloadResult(PdfStatus.TIMEOUT))
        with patch("mmore.paper_discovery.pipeline.download_pdf", download):
            self._pipeline(tmp_path, pdf_timeout=90)._enrich_with_pdf_text(
                [Paper(title="T", url="https://a.org/1")]
            )
        assert download.call_args.kwargs["timeout"] == 90


# ---------------------------------------------------------------------------
# Per-paper status, manual-download list and run summary
# ---------------------------------------------------------------------------


def _config(tmp_path, **kwargs):
    return PaperDiscoveryConfig(
        synonyms_path=str(tmp_path / "synonyms.jsonl"),
        categories_path=str(tmp_path / "categories.yaml"),
        output_file=str(tmp_path / "out" / "papers.jsonl"),
        pdf_dir=str(tmp_path / "pdfs"),
        **kwargs,
    )


def _fake_download(outcomes):
    """A `download_pdf` stand-in: `outcomes` maps a URL to a status code,
    or to None to "download" a real-looking PDF."""

    def fake(url, save_dir, cache_key=None, **kwargs):
        status = outcomes[url]
        if status is not None:
            return DownloadResult(PdfStatus.REFUSED, status=status)
        path = expected_pdf_path(cache_key or url, save_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.7\n")
        return DownloadResult(PdfStatus.DOWNLOADED, path=str(path), status=200)

    return fake


class TestPerPaperStatus:
    def test_each_paper_records_what_happened(self, tmp_path):
        ok = Paper(title="A", url="https://a.org/1.pdf")
        refused = Paper(title="B", url="https://b.org/1.pdf")
        no_url = Paper(title="C")
        with (
            patch(
                "mmore.paper_discovery.pipeline.download_pdf",
                side_effect=_fake_download(
                    {"https://a.org/1.pdf": None, "https://b.org/1.pdf": 403}
                ),
            ),
            patch("mmore.paper_discovery.pipeline.extract_text", return_value="text"),
        ):
            PaperDiscoveryPipeline(_config(tmp_path))._enrich_with_pdf_text(
                [ok, refused, no_url]
            )
        assert (ok.pdf_status, ok.pdf_http_status) == (PdfStatus.DOWNLOADED, 200)
        assert (refused.pdf_status, refused.pdf_http_status) == (
            PdfStatus.REFUSED,
            403,
        )
        assert (no_url.pdf_status, no_url.pdf_http_status) == (PdfStatus.NO_URL, None)

    def test_status_is_written_as_plain_text(self):
        paper = Paper(pdf_status=PdfStatus.REFUSED, pdf_http_status=403)
        line = json.loads(json.dumps(paper.to_dict()))
        assert line["pdf_status"] == "refused"
        assert line["pdf_http_status"] == 403


class TestFailedPdfList:
    def _run(self, tmp_path, papers, outcomes, text="text"):
        pipeline = PaperDiscoveryPipeline(_config(tmp_path))
        with (
            patch(
                "mmore.paper_discovery.pipeline.download_pdf",
                side_effect=_fake_download(outcomes),
            ),
            patch("mmore.paper_discovery.pipeline.extract_text", return_value=text),
        ):
            pipeline._enrich_with_pdf_text(papers)
        return pipeline

    def _rows(self, path):
        import csv

        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    def test_lists_failed_papers_with_a_link_and_save_path(self, tmp_path):
        paper = Paper(title="Paywalled", doi="10.1/a", url="https://pub.org/1.pdf")
        pipeline = self._run(tmp_path, [paper], {"https://pub.org/1.pdf": 403})
        assert pipeline.failed_list is not None
        (row,) = self._rows(pipeline.failed_list)
        assert row["title"] == "Paywalled"
        assert row["status"] == "refused"
        assert row["http_status"] == "403"
        assert row["link"] == "https://doi.org/10.1/a"
        expected = expected_pdf_path("doi:10.1/a", pipeline.config.pdf_dir)
        assert row["save_as"] == str(expected.resolve())

    def test_saved_pdf_is_picked_up_on_the_next_run(self, tmp_path):
        paper = Paper(title="Paywalled", url="https://pub.org/1.pdf")
        pipeline = self._run(tmp_path, [paper], {"https://pub.org/1.pdf": 403})
        assert pipeline.failed_list is not None
        (row,) = self._rows(pipeline.failed_list)

        # The user downloads the PDF in a browser and saves it there.
        Path(row["save_as"]).write_bytes(b"%PDF-1.7\n")

        again = Paper(title="Paywalled", url="https://pub.org/1.pdf")
        pipeline = self._run(tmp_path, [again], {"https://pub.org/1.pdf": 403})
        assert again.pdf_status == PdfStatus.CACHED
        assert pipeline.failed_list is None

    def test_no_list_when_nothing_failed_and_old_list_removed(self, tmp_path):
        stale = tmp_path / "out" / "papers_failed_pdfs.csv"
        stale.parent.mkdir(parents=True)
        stale.write_text("old")
        paper = Paper(title="Free", url="https://a.org/1.pdf")
        pipeline = self._run(tmp_path, [paper], {"https://a.org/1.pdf": None})
        assert pipeline.failed_list is None
        assert not stale.exists()

    def test_pdfs_without_text_are_not_listed(self, tmp_path):
        paper = Paper(title="Scanned", url="https://a.org/1.pdf")
        pipeline = self._run(tmp_path, [paper], {"https://a.org/1.pdf": None}, text="")
        assert paper.pdf_status == PdfStatus.NO_TEXT
        assert pipeline.failed_list is None


class TestRunSummary:
    def _write_inputs(self, tmp_path):
        (tmp_path / "synonyms.jsonl").write_text(
            '{"word": "LLM", "synonyms": ["large language model"]}\n',
            encoding="utf-8",
        )
        (tmp_path / "categories.yaml").write_text(
            "categories:\n  Cat:\n    - LLM\n", encoding="utf-8"
        )

    def test_run_counts_sources_and_duplicates(self, tmp_path):
        self._write_inputs(tmp_path)
        cfg = _config(tmp_path, sources=["openalex", "arxiv"], download_pdfs=False)
        results = {
            "openalex": [Paper(title="A"), Paper(title="B")],
            "arxiv": [Paper(title="a "), Paper(title="C")],
        }

        def fake_adapter(name, **kwargs):
            adapter = MagicMock()
            adapter.search.return_value = results[name]
            return adapter

        with patch("mmore.paper_discovery.pipeline.get_adapter", fake_adapter):
            pipeline = PaperDiscoveryPipeline(cfg)
            papers = pipeline.run()

        assert [p.title for p in papers] == ["A", "B", "C"]
        summary = pipeline.summary()
        assert summary["openalex"] == "2 papers"
        assert summary["arxiv"] == "2 papers"
        assert summary["unique"] == "3 (1 duplicates removed)"
        assert "full text" not in summary  # PDFs were off
        assert summary["output"] == cfg.output_file
        assert Path(cfg.output_file).exists()

    def test_summary_reports_full_text_and_the_list(self, tmp_path):
        pipeline = PaperDiscoveryPipeline(_config(tmp_path))
        pipeline.n_unique = 3
        pipeline.found["openalex"] = 3
        pipeline.pdf_counts.update(
            {PdfStatus.DOWNLOADED: 1, PdfStatus.CACHED: 1, PdfStatus.REFUSED: 1}
        )
        pipeline.failed_list = tmp_path / "papers_failed_pdfs.csv"
        summary = pipeline.summary()
        assert summary["full text"] == "2 papers · 1 without"
        assert summary["to download"] == str(pipeline.failed_list)
