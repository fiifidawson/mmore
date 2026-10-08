"""Explain why a paper's PDF failed to download.

The pipeline reports one outcome per paper, such as `refused` or
`login_page`. This script takes one URL at a time and prints every signal
behind that outcome: status code, final URL after redirects, content
type, whether the body is a sign-in page, and the outcome the pipeline
would record.

Fetches each URL twice when a proxy prefix is given, once directly and
once through the proxy, so you can see whether the proxy changes anything.

Usage:
    python scripts/diagnose_pdf_access.py <url> [more urls...]
    python scripts/diagnose_pdf_access.py --from-papers 5
    python scripts/diagnose_pdf_access.py --prefix https://ezproxy.example.edu <url>

Options:
    --prefix URL      EZproxy prefix to test. Omit to test direct access only.
    --from-papers N   Take the first N URLs that produced no extracted text
                      from examples/paper_discovery/papers.jsonl.
    --papers PATH     Read from a different papers.jsonl.
"""

import argparse
import json
import sys
from pathlib import Path

import requests

from mmore.paper_discovery.pdf import (
    _failed,
    _find_pdf_link,
    _looks_like_login_page,
    _looks_like_pdf,
    _proxify,
)

UA = "mmore-paper-discovery/1.0 (https://github.com/EPFLiGHT/mmore)"
DEFAULT_PAPERS = Path("examples/paper_discovery/papers.jsonl")


def classify(resp: requests.Response) -> str:
    """The outcome the pipeline would record for this response."""
    if resp.status_code != 200:
        # The pipeline's own mapping, so the two can't drift apart.
        return _failed(resp.status_code, resp.url).outcome.value
    if _looks_like_pdf(resp):
        return "downloaded"
    if _looks_like_login_page(resp):
        return "login_page (we cannot log in)"
    if _find_pdf_link(resp.text, base=resp.url):
        return "follows a PDF link on the page (downloaded if it is a PDF)"
    return "no_pdf_link"


def probe(url: str, prefix: str | None, session: requests.Session) -> None:
    label = "PROXIED" if prefix else "DIRECT"
    target = _proxify(url, prefix) if prefix else url

    print(f"\n  [{label}]")
    if prefix:
        print(f"  rewritten -> {target[:110]}{'...' if len(target) > 110 else ''}")

    try:
        r = session.get(
            target, headers={"User-Agent": UA}, timeout=30, allow_redirects=True
        )
    except requests.RequestException as e:
        print(f"  network error: {e}")
        if prefix:
            print(
                "  -> the proxy host may be wrong. Check what your library publishes."
            )
        return

    print(f"  status       {r.status_code}")
    print(f"  content-type {r.headers.get('Content-Type', '?')}")
    print(f"  final url    {r.url[:110]}{'...' if len(r.url) > 110 else ''}")
    print(f"  redirected   {'yes' if r.url != target else 'no'}")
    print(f"  bytes        {len(r.content)}")
    print(f"  sign-in page {'YES' if _looks_like_login_page(r) else 'no'}")
    print(f"  -> pipeline would record: {classify(r)}")


def urls_from_papers(path: Path, n: int) -> list[str]:
    if not path.exists():
        sys.exit(f"{path} not found. Run the pipeline first, or pass URLs directly.")
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if len(out) >= n:
                break
            paper = json.loads(line)
            # No extracted text means this one failed.
            if paper.get("url") and not paper.get("extracted_text"):
                out.append(paper["url"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Explain why a paper's PDF failed to download."
    )
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--prefix", help="EZproxy prefix to test alongside direct access")
    ap.add_argument("--from-papers", type=int, metavar="N")
    ap.add_argument("--papers", type=Path, default=DEFAULT_PAPERS)
    args = ap.parse_args()

    urls = list(args.urls)
    if args.from_papers:
        urls += urls_from_papers(args.papers, args.from_papers)
    if not urls:
        ap.error("give at least one URL, or use --from-papers N")

    session = requests.Session()
    for url in urls:
        print("\n" + "=" * 72)
        print(url[:110] + ("..." if len(url) > 110 else ""))
        print("=" * 72)
        probe(url, None, session)
        if args.prefix:
            probe(url, args.prefix, session)

    print("\n" + "=" * 72)
    print("""How to read this

  refused on a journal you know your institution subscribes to
      Publishers block automated tools by User-Agent, independently of
      whether you have access. Being on the VPN does not always help, and
      mmore does not spoof the User-Agent to get around it.

  login_page
      The proxy needs an interactive login. This pipeline cannot complete
      a SAML or Shibboleth sign-in, so there is no headless workaround.

  Proxy request fails DNS
      The prefix host does not exist. Use the hostname your own library
      publishes rather than guessing. Many institutions run no EZproxy at
      all and grant access by VPN instead, in which case leave
      pdf_proxy_prefix unset.

  downloaded here, but the pipeline still failed
      Likely a temporary error. Re-run, the PDF cache keeps what worked.""")


if __name__ == "__main__":
    main()
