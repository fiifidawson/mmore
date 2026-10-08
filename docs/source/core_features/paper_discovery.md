# 📄 Paper Discovery

## Overview

The **Paper Discovery** module helps you build a targeted collection of academic papers on a topic you care about. You describe the topic once — as a list of keywords with synonyms — and the module searches several open academic repositories (OpenAlex, Europe PMC, arXiv, optionally Google Scholar) on your behalf, downloads whatever PDFs it can, and writes a JSONL file with the metadata and extracted text.

The output has one paper per line, each with a status saying what happened to its PDF. What you do with it next is up to you — feed it into an indexer, hand it to a screening tool, or just read the abstracts. This page describes the standalone `paper_discovery` module. It is independent of the `rag` and `index` pipelines.

## Installation

```bash
uv pip install "mmore[paper_discovery]"
```

> The `paper_discovery` extra isn't in the `2.0.0` release on PyPI. Until the next release, install from GitHub:
>
> ```bash
> uv pip install "mmore[paper_discovery] @ git+https://github.com/EPFLiGHT/mmore.git@main"
> ```

For optional Google Scholar support (captcha-prone, best-effort):

```bash
uv pip install "mmore[google_scholar]"
```

This is a separate extra because Google Scholar is captcha-prone and `scholarly` pulls in extra packages such as selenium. Install it only if you need it.

## Supported sources

| Source | What it covers |
|--------|----------------|
| **OpenAlex** | Broadest general index of academic papers. Abstracts included by default. |
| **Europe PMC** | Biomedical and life-sciences literature with links to full text where available. |
| **arXiv** | Preprints in ML, physics, math, and CS. Slower than the others because arXiv enforces a 3-second gap between requests. |
| **Google Scholar** | Widest overall coverage but captcha-prone. Opt-in — requires the `google_scholar` extra. |

All four sources are anonymous — no API keys needed. Precise rate limits, retry back-off, and API-specific details live in each adapter's docstring under `src/mmore/paper_discovery/sources/`.

## 🔁 Workflow

```
synonyms.jsonl + categories.yaml
        │
        ▼
Stage 1: build boolean queries (pure, offline)
        │
        ▼
Stage 2: fetch from each source, dedupe, optionally download PDFs
        │
        ▼
   papers.jsonl  (+ papers_failed_pdfs.csv when some PDFs are missing)
```

Stage 1 doesn't touch the network — it just turns your synonyms + categories into search queries. Stage 2 is where everything network-related happens: hitting each source, respecting their rate limits, downloading PDFs, retrying when things go wrong.

## 💻 Minimal Example

### 1. Prepare your synonym table

A **JSONL** file with one `{"word": ..., "synonyms": [...]}` object per line. Easy to diff, append, and edit line-by-line:

```jsonl
{"word": "Foundation model", "synonyms": ["LLM", "large language model", "GPT"]}
{"word": "Humanitarian & Crisis Response", "synonyms": ["humanitarian aid", "disaster response"]}
```

You don't have to worry about capitalization — `"Foundation model"`, `"foundation model"` and `"FOUNDATION MODEL"` are treated as the same word. Whitespace does need to match. If any of your terms happen to contain a `"` character, don't stress — it's silently stripped when the file is loaded.

### 2. Define your categories

Categories live in their own YAML file, loaded via a small `CategoriesFile` dataclass:

```yaml
# categories.yaml
categories:
  Broad Foundational Search:
    - Foundation model
    - Machine Learning
  Humanitarian AI Search:
    - Foundation model
    - Humanitarian & Crisis Response
```

Each name under a category must match a `word` in your synonyms file. For every category, the module builds one search that finds papers mentioning **at least one term from each group of synonyms**. So the "Broad Foundational Search" example above will match a paper if it talks about *any* foundation-model synonym AND *any* machine-learning synonym.

### 3. Create a config file

See [`examples/paper_discovery/config.yaml`](https://github.com/EPFLiGHT/mmore/blob/main/examples/paper_discovery/config.yaml). It points at your `synonyms_path` and `categories_path`.

### 4. Run the pipeline

```bash
python3 -m mmore paper-discovery --config-file examples/paper_discovery/config.yaml
```

You see one line when the run starts, a progress bar for each stage, and a summary at the end. If some PDFs are missing, a short list of next steps follows:

```
▸ Paper Discovery 📄  Find papers for your keywords · sources: openalex, europepmc, arxiv
  Searching ━━━━━━━━━━━━━━━━━━━━━━━━   6/6 search 100% 0:00:15  arxiv · Humanitarian AI Search
  PDFs      ━━━━━━━━━━━━━━━━━━━━━━━━ 122/122 paper 100% 0:08:41  ok=68 cache=22 refused=41 failed=54

╭─ mmore ▸ Paper Discovery 📄 · done in 175.2s ───────────────────────────────────────╮
│ sources: openalex, europepmc, arxiv   openalex          50 papers                   │
│ PDFs: on                              europepmc         48 papers                   │
│                                       arxiv             50 papers                   │
│                                       unique            122 (26 duplicates removed) │
│                                       full text         68 (22 cached · 46 new)     │
│                                       no full text      54                          │
│                                         · refused       41                          │
│                                         · no pdf link   8                           │
│                                         · not pdf       2                           │
│                                         · http error    2                           │
│                                         · login page    1                           │
│                                       output            results/papers.jsonl        │
╰─────────────────────────────────────────────────────────────────────────────────────╯
╭─ mmore ▸ Next steps ─────────────────────────────────────────────────────────────────────────────╮
│ 54 PDFs to get by hand   Open the links in results/papers_failed_pdfs.csv in your browser, save  │
│                          each PDF to its save_as path, then run again.                           │
│ 1 login page             Check pdf_proxy_prefix is your library's EZproxy, or unset it and use   │
│                          the VPN.                                                                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Set `MMORE_VERBOSE=1` to also see each search and its result count.

Press **Ctrl+C** at any time — the pipeline catches the interrupt and writes whatever it has so far to `output_file` before exiting.

## 📦 Output

A JSONL file — one `Paper` record per line. Example line:

```json
{"title": "A foundation model for humanitarian response", "authors": ["Ada Lovelace", "Alan Turing"], "url": "https://arxiv.org/pdf/2401.00001.pdf", "doi": "10.1000/xyz123", "candidate_urls": ["https://arxiv.org/pdf/2401.00001.pdf", "https://publisher.example.org/article/xyz123"], "abstract": "We introduce …", "year": 2024, "extracted_text": "<full PDF text>", "source": "arxiv", "search_category": "Humanitarian AI Search", "pdf_status": "downloaded", "pdf_http_status": 200}
```

Line-per-record makes the file streamable (read one paper at a time), diff-friendly, and easy to append to. Any JSONL-aware tool (`jq`, `pandas.read_json(lines=True)`, mmore's `MultimodalSample.from_jsonl`) can consume it directly.

Fields are **nullable on purpose** — sources differ in what they return. `null` means "we don't know."

`candidate_urls` lists every known link to the paper, best first. `url` is the first one. When the same paper comes from several sources, their links are merged into one record.

`pdf_status` says what happened to the PDF (see *PDF downloads* below), and `pdf_http_status` is the last HTTP status seen. Both are `null` when `download_pdfs` is off.

## ⚙️ Configuration knobs

| Knob | Default | Notes |
|------|---------|-------|
| `synonyms_path` | *(required)* | Path to a `.jsonl` synonyms file (one object per line) |
| `categories_path` | *(required)* | Path to a `categories.yaml` file (see step 2) |
| `sources` | `[openalex, europepmc, arxiv]` | Add `google_scholar` to opt in |
| `download_pdfs` | `true` | Set `false` to skip the PDF stage entirely |
| `max_pages` | `3` | Pages per source per query |
| `max_results` | `50` | Hard cap per source per query |
| `pdf_dir` | `./pdf_cache` | Reused across runs (see *PDF caching* below) |
| `force_redownload` | `false` | Set `true` to ignore the on-disk cache and re-fetch every PDF |
| `pdf_extractor` | `"fast"` | Which mmore PDF processor to use. `"fast"` = PyMuPDF-backed, no models loaded. `"full"` = marker + surya for better parsing (slow, downloads models) |
| `multimodal_output_file` | `null` | If set, also write a JSONL of `MultimodalSample` records that mmore's post-process / index / RAG pipelines can consume directly (see [Feeding results into mmore's index / RAG](#-feeding-results-into-mmores-index--rag)) |
| `pdf_proxy_prefix` | `null` | EZproxy host, only if your institution runs one. Leave unset for VPN-based access (see *Paywalled PDFs* below) |
| `pdf_timeout` | `30` | Seconds to wait for each PDF request. Raise it for slow publishers or proxies |
| `user_agent` | `mmore-paper-discovery/1.0 …` | HTTP `User-Agent` header sent on every outbound request — see below |
| `arxiv_category_map` | `null` | Maps a substring of your category title to an arXiv code (e.g. `Foundational` → `cs.LG`) — adds `cat:<code>` to the arXiv query |
| `arxiv_enable_pair_query` | `true` | Runs one extra arXiv search per category that requires the top two terms together (better precision). Turn off if you'd rather save a few seconds per category |

### `user_agent`

This is the "who's asking?" string sent with every network request the module makes. Sources use it to identify who's hitting their API, and OpenAlex specifically gives faster, more reliable responses to requests that include a contact address.

You should set it to something that identifies your project so the source's team can reach you if you're making too many requests. A concrete example:

```yaml
user_agent: "my-lab-pipeline/1.0 (mailto:alice@example.com)"
```

The default just identifies mmore + the repo URL, which works but doesn't tell anyone who *you* are.

## 📥 PDF downloads

Each paper's links are tried in order until one gives a PDF. A paper only counts as a success if text was extracted from it. The summary card lists why the other PDFs are missing, largest group first. Only the outcomes that happened are listed. Each paper's `pdf_status` uses the same names, with `_` instead of spaces:

| Outcome | Meaning |
|---|---|
| `refused` | The publisher said no (401/402/403). See *Paywalled PDFs* below |
| `rate limited` | Still told to slow down (429) after retries. Rerun later |
| `login page` | A sign-in page came back instead of the PDF |
| `not found` | The link is dead (404/410) |
| `no pdf link` | A web page with no PDF link on it |
| `not pdf` | The PDF link returned something else |
| `timeout` | No answer in time, after retries. Raise `pdf_timeout` |
| `server error` | The publisher's server failed (5xx), after retries |
| `http error` | Any other unexpected status |
| `network error` | DNS, SSL or connection failure |
| `no url` | The source gave no link |
| `no text` | A real PDF, but no text came out. Try `pdf_extractor: full` |

Timeouts, rate limits and server errors are retried twice, waiting longer each time. A `Retry-After` header is honoured.

## 💾 PDF caching

`pdf_dir` is reused across runs. Each PDF is saved under a hash of the paper's DOI, or of its URL when there is no DOI. If that file already exists and is a real PDF, the download is skipped. If it isn't a real PDF, it is deleted and downloaded again.

> Caches made before DOI-based names aren't reused. Those PDFs are downloaded once more.

This makes interrupted runs cheap to resume — every PDF that landed on disk before Ctrl+C is reused, only the missing ones are fetched.

To force a full re-download (e.g. after a publisher updates a paper), set `force_redownload: true` in your config.

## 🔒 Paywalled PDFs

Expect a chunk of your run to come back without full text. Some of that you can fix, some of it you cannot. Read this section before spending time on it.

### Free copies are tried first

Many paywalled papers also have a free, legal copy on arXiv, PubMed Central or a university repository. The pipeline tries those first and falls back to the publisher's copy. If one link fails, it tries the next one in `candidate_urls`.

### There are two different reasons a PDF fails

Both show up as `refused` in the summary line, but they have completely different fixes.

**1. You don't have access.** Your institution has no subscription to that journal. Nothing in this pipeline can fix that. Request the paper through your library instead.

**2. You have access, but the publisher blocks automated tools.** This is the common one, and it surprises people. Publishers like Wiley, ACM, and Science return `403` to anything that doesn't look like a browser, *regardless of whether your institution subscribes*. You can click the same link in your browser and get the PDF, then watch the pipeline get refused for the identical URL.

mmore does **not** work around this by pretending to be a browser. Spoofing the User-Agent violates most publishers' terms of service, and a spoofed default would get the project's identifier blocklisted for every user of the library. That's a deliberate choice, not an oversight. If you have an agreement with a publisher, such as a registered crawler, set `user_agent` to what they require. That's your responsibility, not a library default.

### How your institution grants access matters

Two common models. Check which one yours uses before touching any config.

**VPN and IP recognition.** You connect to your institution's VPN, the publisher sees an institutional IP, and access is granted automatically. **Leave `pdf_proxy_prefix` unset.** The direct URL already works. EPFL works this way.

**EZproxy.** Your library gives you a hostname that rewrites URLs. If, and only if, your institution runs one, set:

```yaml
pdf_proxy_prefix: "https://ezproxy.example.edu"
```

Use the exact host your library publishes. Do not guess it. A wrong host either fails DNS or serves you a login page, and neither yields a PDF.

Even with the right host, an EZproxy that needs an interactive sign-in will return its login page instead of the PDF. The pipeline detects this and warns you:

```
12 downloads returned a sign-in page instead of a PDF. This pipeline
cannot log in for you.
```

There is no headless workaround for that today. The pipeline cannot complete a SAML or Shibboleth login.

### Download the rest by hand

When some PDFs are missing, the pipeline writes a list next to your output, e.g. `results/papers_failed_pdfs.csv`:

| Column | What it is |
|---|---|
| `title` | The paper |
| `status` | Why it failed, e.g. `refused` |
| `http_status` | The last HTTP status, if any |
| `link` | Open this in your browser. For papers with a DOI it's the `doi.org` link, which takes you to the publisher, where your institution's sign-in works |
| `save_as` | Save the PDF to exactly this path |

Then run the pipeline again. It finds the saved PDFs in the cache and extracts their text. The list is rewritten on every run, and removed once nothing is missing.

Run from the same folder each time, so relative paths such as `pdf_dir` point to the same place.

### Skip PDFs entirely

If full text isn't essential, this is the cheapest path and it always works:

```yaml
download_pdfs: false
```

You still get every paper's metadata and abstract. Only `extracted_text` is left empty.

## 📄 PDF text extraction

Text extraction goes through the same PDF processor the rest of mmore uses, so you get consistent output whether a paper comes from Paper Discovery or from another `mmore process` run. There are two settings you can pick between with `pdf_extractor`:

- **`fast` (default)** — Uses PyMuPDF under the hood. Nothing to download, works right out of the box, and it's good enough for most academic PDFs.
- **`full`** — Uses mmore's fuller pipeline (with layout-aware parsing). Better on messy PDFs — multi-column layouts, scanned pages, complex figures — but it downloads model weights the first time it runs, and it's really only worth it if you have a GPU.

Start with `fast`. Only switch to `full` if you notice extraction is losing structure on the papers you care about.

## 🔌 Feeding results into mmore's index / RAG

If you plan to index the discovered papers or run RAG over them, you don't need to send them back through `mmore process`. Ask the pipeline to write an extra output file in mmore's canonical `MultimodalSample` shape:

```yaml
multimodal_output_file: examples/paper_discovery/papers.samples.jsonl
```

Every paper is converted to a `MultimodalSample`:

- **`text`** — the extracted PDF body if we downloaded it, otherwise the abstract, otherwise the title.
- **`metadata.file_path`** — points at the cached PDF when we have one.
- **`metadata.processor_type`** — always `"paper_discovery"`, so downstream filters can recognise the source.
- **`metadata.extra`** — carries the paper-specific fields (title, authors, year, source, url, doi, candidate_urls, search_category, pdf_status, abstract).

The resulting JSONL is a drop-in input for the post-process, index, and RAG pipelines. The default `papers.jsonl` output is still written the same way alongside it.

## 🐍 Programmatic use

For embedding the pipeline in another script:

```python
from mmore.paper_discovery import PaperDiscoveryConfig, PaperDiscoveryPipeline
from mmore.utils import load_config

config = load_config("examples/paper_discovery/config.yaml", PaperDiscoveryConfig)
pipeline = PaperDiscoveryPipeline(config)
papers = pipeline.run()
print(f"Got {len(papers)} papers")
print(pipeline.summary())  # the rows of the end-of-run card

missing = [p for p in papers if p.pdf_status not in (None, "downloaded", "cached")]
```

Or compose Stage 1 alone (no network) for testing:

```python
from mmore.paper_discovery import build_boolean_queries
from mmore.paper_discovery.boolean import load_synonyms

synonyms = load_synonyms("examples/paper_discovery/synonyms.jsonl")
queries = build_boolean_queries(synonyms, {"My Category": ["Foundation model"]})
for q in queries:
    print(q.combination_title, "->", q.boolean_combination)
```

## See also

- [Indexing](../getting_started/indexing.md) — feed `extracted_text` into the indexer
- [RAG](../getting_started/rag.md) — query the indexed papers
- [Processing pipeline](../getting_started/process.md) — convert other document formats
