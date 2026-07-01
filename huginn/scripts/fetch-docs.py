#!/usr/bin/env python3
"""
fetch-docs.py — crawl documentation sites and save as markdown to the
Huginn knowledge base for RAG indexing.

Usage:
  python fetch-docs.py quickshell       # fetch Quickshell docs
  python fetch-docs.py qt               # fetch Qt QML type reference
  python fetch-docs.py all              # fetch everything

The knowledge dir is huginn/knowledge/. Run the Huginn `index_knowledge`
tool (or restart the daemon) to re-index after fetching.
"""
import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"

# ── Site configs ──────────────────────────────────────────────────────────────

SITES = {
    "quickshell": {
        "roots":   ["https://quickshell.outfoxxed.me/docs/"],
        "allowed": "quickshell.outfoxxed.me",
        "out_dir": KNOWLEDGE_DIR / "quickshell",
        "strip_nav": True,
    },
    "qt": {
        "roots": [
            "https://doc.qt.io/qt-6/qtquick-qmlmodule.html",
            "https://doc.qt.io/qt-6/qtqml-qmlmodule.html",
        ],
        "allowed": "doc.qt.io",
        "out_dir": KNOWLEDGE_DIR / "qt-qml",
        "strip_nav": True,
        "max_pages": 80,
    },
}

# ── HTML → Markdown conversion ────────────────────────────────────────────────

def html_to_markdown(html: str, base_url: str) -> str:
    try:
        import html2text
        h = html2text.HTML2Text()
        h.ignore_links    = False
        h.ignore_images   = True
        h.body_width      = 0
        h.ignore_emphasis = False
        return h.handle(html)
    except ImportError:
        # Fallback: very basic tag stripping
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.S)
        text = re.sub(r'<style[^>]*>.*?</style>',  '', text,  flags=re.S)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&amp;',  '&', text)
        text = re.sub(r'&lt;',   '<', text)
        text = re.sub(r'&gt;',   '>', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'[ \t]+', ' ', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()


def extract_main(html: str) -> str:
    """Try to pull just the main content block."""
    for pattern in [
        r'<main[^>]*>(.*?)</main>',
        r'<article[^>]*>(.*?)</article>',
        r'<div[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]*class="[^"]*doc[^"]*"[^>]*>(.*?)</div>',
    ]:
        m = re.search(pattern, html, re.S | re.I)
        if m:
            return m.group(1)
    return html


def extract_links(html: str, base_url: str, allowed_host: str) -> list[str]:
    urls = []
    for href in re.findall(r'href=["\']([^"\'#?]+)["\']', html):
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.netloc == allowed_host and parsed.scheme in ("http", "https"):
            urls.append(full)
    return list(dict.fromkeys(urls))  # dedupe, preserve order


def url_to_filename(url: str) -> str:
    parsed = urlparse(url)
    path   = parsed.path.strip("/").replace("/", "_") or "index"
    return re.sub(r'[^\w\-.]', '_', path) + ".md"


# ── Crawler ───────────────────────────────────────────────────────────────────

def crawl(site_key: str) -> None:
    cfg     = SITES[site_key]
    out_dir = Path(cfg["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    allowed = cfg["allowed"]
    max_pg  = cfg.get("max_pages", 200)

    queue   = list(cfg["roots"])
    visited = set()
    saved   = 0

    print(f"\n── {site_key} → {out_dir} ──")

    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": "HuginnDocsBot/1.0"}) as client:
        while queue and saved < max_pg:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            try:
                resp = client.get(url)
                if resp.status_code != 200:
                    print(f"  SKIP {resp.status_code}: {url}")
                    continue
                html = resp.text
            except Exception as e:
                print(f"  ERROR {url}: {e}")
                continue

            # Extract and convert
            body = extract_main(html) if cfg.get("strip_nav") else html
            md   = html_to_markdown(body, url)
            md   = re.sub(r'\n{3,}', '\n\n', md).strip()

            # Save
            fname = url_to_filename(url)
            (out_dir / fname).write_text(f"<!-- source: {url} -->\n\n{md}\n")
            saved += 1
            print(f"  [{saved}] {fname}")

            # Enqueue new links
            for link in extract_links(html, url, allowed):
                if link not in visited and link not in queue:
                    queue.append(link)

            time.sleep(0.15)  # be polite

    print(f"Done: {saved} pages saved to {out_dir}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    try:
        import html2text  # noqa
    except ImportError:
        print("html2text not installed — install it for better markdown output:")
        print("  uv add html2text  or  pip install html2text")
        print("Continuing with basic fallback...\n")

    p = argparse.ArgumentParser(description="Fetch docs into Huginn knowledge base")
    p.add_argument("sites", nargs="+", choices=[*SITES.keys(), "all"])
    args = p.parse_args()

    targets = list(SITES.keys()) if "all" in args.sites else args.sites
    for site in targets:
        crawl(site)

    print("\nDone. Restart Huginn or run index_knowledge to re-index.")


if __name__ == "__main__":
    main()
