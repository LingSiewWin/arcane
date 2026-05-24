"""docs_to_skills.py — turn a documentation site into one organized skills.md.

Feed it a docs base URL; it produces a single, section-grouped Markdown
"reference collection" (a top index + each source's prose with code blocks
preserved and its `Source:` URL cited). Built dependency-light: only `httpx`
(already in the venv) + the standard library — no trafilatura/bs4 needed.

Source resolution (cheapest, cleanest first):
  1. ``<origin>/llms-full.txt`` then ``/llms.txt`` — the LLM-docs convention.
     Many doc sites (incl. Circle: developers.circle.com/llms-full.txt) publish
     this as already-clean Markdown, so we skip crawling entirely.
  2. ``<origin>/sitemap.xml`` — crawl the in-domain doc URLs it lists.
  3. Shallow BFS crawl of in-domain links from the start URL (``--depth``).

For (2)/(3) we extract with a small stdlib ``html.parser`` subclass that keeps
headings, paragraphs, list items and — importantly — ``<pre>``/``<code>`` blocks
as fenced Markdown (generic readability extractors often drop code).

No fabrication: a page that fails to fetch is skipped and noted in the run log;
an empty result writes an honest "nothing extracted" stub rather than inventing
content.

CLI:
    python -m scripts.docs_to_skills --url https://developers.circle.com \
        --out docs/skills/circle.skills.md
    python -m scripts.docs_to_skills --url https://example.com/docs \
        --out out.md --max-pages 40 --depth 2 --no-llms
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse
from xml.etree import ElementTree

import httpx

USER_AGENT = "AgoraHack-docs_to_skills/1.0"
DEFAULT_TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


@dataclass
class Fetched:
    url: str
    status: int
    text: str
    content_type: str


def fetch(client: httpx.Client, url: str) -> Fetched:
    """GET a URL; never raises — failures come back as status 0 / empty text."""
    try:
        r = client.get(url, follow_redirects=True, timeout=DEFAULT_TIMEOUT)
        ctype = r.headers.get("content-type", "").split(";")[0].strip()
        return Fetched(url=str(r.url), status=r.status_code, text=r.text, content_type=ctype)
    except Exception as exc:  # noqa: BLE001
        return Fetched(url=url, status=0, text=f"<fetch error: {exc}>", content_type="")


def origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


# ---------------------------------------------------------------------------
# Source 1 — llms.txt / llms-full.txt fast-path
# ---------------------------------------------------------------------------


def try_llms_txt(client: httpx.Client, base: str) -> tuple[str, str] | None:
    """Return (variant_url, markdown) for the first llms*.txt that resolves."""
    origin = origin_of(base)
    for name in ("llms-full.txt", "llms.txt"):
        url = f"{origin}/{name}"
        f = fetch(client, url)
        # Guard against SPAs that 200 with an HTML shell for any path.
        if f.status == 200 and f.text.strip() and "text/html" not in f.content_type:
            if "<html" not in f.text[:512].lower():
                return url, f.text
    return None


# ---------------------------------------------------------------------------
# Source 2 — sitemap.xml
# ---------------------------------------------------------------------------


def parse_sitemap(client: httpx.Client, base: str) -> list[str]:
    """Return in-domain URLs from <origin>/sitemap.xml (empty list if absent)."""
    origin = origin_of(base)
    f = fetch(client, f"{origin}/sitemap.xml")
    if f.status != 200 or not f.text.strip():
        return []
    urls: list[str] = []
    try:
        root = ElementTree.fromstring(f.text)
    except ElementTree.ParseError:
        return []
    # Strip namespaces so <loc> matches regardless of the sitemap ns.
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "loc" and el.text:
            u = el.text.strip()
            if urlparse(u).netloc == urlparse(origin).netloc:
                urls.append(u)
    return urls


# ---------------------------------------------------------------------------
# HTML extraction (stdlib) — prose + fenced code blocks
# ---------------------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "nav", "footer", "header", "aside", "noscript", "svg"}
_BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "article"}


class _Extractor(HTMLParser):
    """Pull readable text + code from HTML into Markdown-ish lines."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: str = ""
        self._skip_depth = 0
        self._in_title = False
        self._in_pre = False
        self._pre_buf: list[str] = []
        self._heading: str | None = None
        self._line: list[str] = []

    def handle_starttag(self, tag: str, attrs):  # noqa: ANN001
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "pre":
            self._flush_line()
            self._in_pre = True
            self._pre_buf = []
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush_line()
            self._heading = "#" * int(tag[1])
        elif tag in _BLOCK_TAGS:
            self._flush_line()
        elif tag == "br":
            self._flush_line()

    def handle_endtag(self, tag: str):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "pre":
            code = "".join(self._pre_buf).strip("\n")
            if code.strip():
                self.parts.append(f"```\n{code}\n```")
            self._in_pre = False
            self._pre_buf = []
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = "".join(self._line).strip()
            if text:
                self.parts.append(f"{self._heading or '##'} {text}")
            self._line = []
            self._heading = None
        elif tag in _BLOCK_TAGS:
            self._flush_line()

    def handle_data(self, data: str):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        elif self._in_pre:
            self._pre_buf.append(data)
        else:
            self._line.append(data)

    def _flush_line(self) -> None:
        text = re.sub(r"[ \t]+", " ", "".join(self._line)).strip()
        if text:
            self.parts.append(text)
        self._line = []

    def result(self) -> tuple[str, str]:
        self._flush_line()
        # Collapse 3+ blank lines; join blocks with a blank line.
        body = "\n\n".join(p for p in self.parts if p.strip())
        return self.title.strip(), body


def extract_html(html: str) -> tuple[str, str]:
    ex = _Extractor()
    try:
        ex.feed(html)
    except Exception:  # noqa: BLE001
        return "", ""
    return ex.result()


def in_domain_links(html: str, base_url: str) -> list[str]:
    """Absolute in-domain hrefs found in the page (deduped, fragment-stripped)."""
    origin = urlparse(base_url).netloc
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
        href = m.group(1)
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absu = urldefrag(urljoin(base_url, href)).url
        if urlparse(absu).netloc == origin and absu not in seen:
            seen.add(absu)
            out.append(absu)
    return out


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------


@dataclass
class Page:
    url: str
    title: str
    markdown: str


@dataclass
class CrawlResult:
    pages: list[Page] = field(default_factory=list)
    method: str = ""
    skipped: list[str] = field(default_factory=list)


def crawl(
    client: httpx.Client, start_url: str, *, max_pages: int, depth: int
) -> CrawlResult:
    """BFS in-domain crawl from start_url, sitemap-seeded when available."""
    result = CrawlResult(method="sitemap+crawl")
    seeds = parse_sitemap(client, start_url)
    queue: deque[tuple[str, int]] = deque()
    seen: set[str] = set()
    start = urldefrag(start_url).url
    queue.append((start, 0))
    seen.add(start)
    for s in seeds[:max_pages]:
        if s not in seen:
            seen.add(s)
            queue.append((s, 1))
    if not seeds:
        result.method = "crawl"

    while queue and len(result.pages) < max_pages:
        url, d = queue.popleft()
        f = fetch(client, url)
        if f.status != 200 or "html" not in f.content_type:
            result.skipped.append(f"{url} (status {f.status}, {f.content_type or 'no type'})")
            continue
        title, md = extract_html(f.text)
        if md.strip():
            result.pages.append(Page(url=f.url, title=title, markdown=md))
        if d < depth:
            for link in in_domain_links(f.text, f.url):
                if link not in seen and len(seen) < max_pages * 4:
                    seen.add(link)
                    queue.append((link, d + 1))
        time.sleep(0.05)  # be polite
    return result


# ---------------------------------------------------------------------------
# Organize → skills.md
# ---------------------------------------------------------------------------


def _section_key(url: str) -> str:
    """Group key = the first meaningful path segment (e.g. /wallets/... -> wallets)."""
    segs = [s for s in urlparse(url).path.split("/") if s]
    return segs[0] if segs else "root"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def organize_pages(source_url: str, result: CrawlResult) -> str:
    """Group crawled pages by path segment into one skills.md with a TOC."""
    groups: dict[str, list[Page]] = {}
    for p in result.pages:
        groups.setdefault(_section_key(p.url), []).append(p)

    out: list[str] = []
    out.append(f"# Skills reference — {urlparse(source_url).netloc}")
    out.append(
        f"> Generated by `scripts/docs_to_skills.py` on {date.today().isoformat()} "
        f"from {source_url} (method: {result.method}, {len(result.pages)} pages)."
    )
    out.append("")
    out.append("## Index")
    for section in sorted(groups):
        out.append(f"- **{section}** ({len(groups[section])})")
        for p in groups[section]:
            label = p.title or p.url
            out.append(f"  - [{label}](#{_slug(label)})")
    out.append("")

    for section in sorted(groups):
        out.append(f"\n# {section}\n")
        for p in groups[section]:
            label = p.title or p.url
            out.append(f"## {label}")
            out.append(f"Source: {p.url}\n")
            out.append(p.markdown)
            out.append("")
    if not result.pages:
        out.append("\n_(nothing extracted — no llms.txt, sitemap, or crawlable HTML found)_")
    return "\n".join(out).rstrip() + "\n"


def organize_llms(source_url: str, variant_url: str, markdown: str) -> str:
    """Wrap an already-clean llms*.txt with a provenance header + heading TOC."""
    headings = [
        (len(m.group(1)), m.group(2).strip())
        for m in re.finditer(r"^(#{1,3})\s+(.+)$", markdown, re.MULTILINE)
    ]
    out: list[str] = []
    out.append(f"# Skills reference — {urlparse(source_url).netloc}")
    out.append(
        f"> Generated by `scripts/docs_to_skills.py` on {date.today().isoformat()} "
        f"from {variant_url} (method: llms.txt fast-path)."
    )
    out.append("")
    if headings:
        out.append("## Index")
        for level, text in headings:
            if level <= 2:
                out.append(f"{'  ' * (level - 1)}- [{text}](#{_slug(text)})")
        out.append("")
    out.append("---")
    out.append("")
    out.append(markdown.strip())
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build(url: str, *, use_llms: bool, max_pages: int, depth: int) -> tuple[str, str]:
    """Return (skills_md, method). Pure-ish: does the network work + organizes."""
    with httpx.Client(headers={"user-agent": USER_AGENT}) as client:
        if use_llms:
            hit = try_llms_txt(client, url)
            if hit is not None:
                variant_url, md = hit
                return organize_llms(url, variant_url, md), "llms.txt"
        result = crawl(client, url, max_pages=max_pages, depth=depth)
        return organize_pages(url, result), result.method


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Crawl a docs site into one organized skills.md reference."
    )
    p.add_argument("--url", required=True, help="Docs base/start URL.")
    p.add_argument("--out", required=True, help="Output skills.md path.")
    p.add_argument("--max-pages", type=int, default=40, help="Crawl cap (default 40).")
    p.add_argument("--depth", type=int, default=2, help="BFS crawl depth (default 2).")
    p.add_argument(
        "--no-llms",
        action="store_true",
        help="Skip the llms.txt fast-path and force an HTML crawl.",
    )
    args = p.parse_args(argv)

    md, method = build(
        args.url, use_llms=not args.no_llms, max_pages=args.max_pages, depth=args.depth
    )
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(md)
    lines = md.count("\n") + 1
    print(f"wrote {args.out} ({len(md):,} bytes, {lines:,} lines) via {method}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
