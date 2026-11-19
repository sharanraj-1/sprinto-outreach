"""Step 1 - Scrape. Request -> Parse -> Extract -> Clean -> Store (L4 slide 11).

Decisions:
- Request-based, not a headless browser: milliseconds and free. If a page comes back
  thin (< THIN_PAGE_WORDS) we record it as `thin_or_js_rendered` instead of pretending
  it worked - the agent loop then fetches a different page.
- Heuristic cleaning (L4 slide 13 option 2): works on unseen sites with no per-site rules.
- robots.txt is checked before every fetch. Disallowed = we stop, we don't work around it.
- Every response is cached on disk, so re-running costs nothing and hits no site twice.
"""
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config

CACHE_DIR = Path(config.CACHE_ROOT) / "pages"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_robots: Dict[str, robotparser.RobotFileParser] = {}
_last_hit: Dict[str, float] = {}

NOISE_TAGS = ["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "button"]
NOISE_HINTS = re.compile(r"cookie|consent|banner|newsletter|modal|popup|nav|menu|footer", re.I)
CERT_HINT = re.compile(r"soc\s?2|iso\s?27001|gdpr|hipaa|pci|trust center|security", re.I)


def _base(domain: str) -> str:
    domain = domain.strip().lower()
    if not domain.startswith("http"):
        domain = "https://" + domain
    p = urlparse(domain)
    return f"{p.scheme}://{p.netloc}"


def _allowed(url: str) -> bool:
    root = _base(url)
    if root not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            r = requests.get(root + "/robots.txt", timeout=config.REQUEST_TIMEOUT,
                             headers={"User-Agent": config.USER_AGENT})
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        _robots[root] = rp
    return _robots[root].can_fetch(config.USER_AGENT, url)


def _get(url: str) -> Dict:
    """Fetch raw HTML, with cache, politeness delay and robots.txt."""
    key = hashlib.sha1(url.encode()).hexdigest()
    cached = CACHE_DIR / f"{key}.json"
    if cached.exists():
        return json.loads(cached.read_text())

    if not _allowed(url):
        return {"url": url, "status": "blocked_by_robots", "html": ""}

    host = urlparse(url).netloc
    wait = config.POLITE_DELAY_SECONDS - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()

    try:
        r = requests.get(url, timeout=config.REQUEST_TIMEOUT, allow_redirects=True,
                         headers={"User-Agent": config.USER_AGENT, "Accept-Language": "en"})
        result = {"url": r.url, "status": r.status_code, "html": r.text if r.ok else ""}
    except requests.RequestException as e:
        result = {"url": url, "status": f"error: {type(e).__name__}", "html": ""}

    if result["status"] == 200:
        cached.write_text(json.dumps(result))
    return result


def clean(html: str) -> Dict:
    """Heuristic clean: strip noise tags, keep blocks of 8+ words, dedupe, cap length.

    Certification badges in the footer are the one footer signal we keep, because the
    Evidence Map says "already certified?" usually lives there.
    """
    soup = BeautifulSoup(html, "html.parser")

    footer_cert_lines = []
    for f in soup.find_all("footer"):
        for t in f.stripped_strings:
            if CERT_HINT.search(t):
                footer_cert_lines.append(t)
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "") + " " + (img.get("src") or "")
        if CERT_HINT.search(alt):
            footer_cert_lines.append(f"[badge image: {alt.strip()[:120]}]")

    title = soup.title.get_text(strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"})
    meta_desc = meta.get("content", "") if meta else ""

    links = []
    for a in soup.find_all("a", href=True):
        links.append({"text": a.get_text(" ", strip=True)[:60], "href": a["href"]})

    for tag in soup(NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(attrs={"class": NOISE_HINTS}):
        tag.decompose()
    for tag in soup.find_all(attrs={"id": NOISE_HINTS}):
        tag.decompose()

    seen, blocks = set(), []
    for el in soup.find_all(["h1", "h2", "h3", "p", "li", "td", "blockquote"]):
        text = " ".join(el.get_text(" ", strip=True).split())
        is_heading = el.name in ("h1", "h2", "h3")
        if not text or (len(text.split()) < config.MIN_BLOCK_WORDS and not is_heading):
            continue
        norm = text.lower()
        if norm in seen:
            continue
        seen.add(norm)
        blocks.append(("## " + text) if is_heading else text)

    body = "\n".join(blocks)
    header = "\n".join(x for x in [f"TITLE: {title}", f"META: {meta_desc}"] if x.split(": ", 1)[1])
    certs = ("\nCERT/SECURITY MENTIONS: " + " | ".join(dict.fromkeys(footer_cert_lines))) if footer_cert_lines else ""
    text = (header + "\n" + body + certs).strip()[: config.MAX_CLEAN_CHARS]
    words = len(body.split())
    return {"text": text, "words": words, "links": links}


def fetch_page(domain_or_url: str, path: str = "") -> Dict:
    """Fetch + clean one page. Returns a record the rest of the pipeline can log."""
    url = urljoin(_base(domain_or_url) + "/", path.lstrip("/")) if path else _base(domain_or_url)
    raw = _get(url)
    rec = {"url": raw["url"], "requested": url, "status": raw["status"], "text": "", "words": 0, "links": [],
           "html": raw["html"]}   # raw HTML kept for the deep scan (JSON-LD, scripts, CSS, app data)
    if raw["html"]:
        rec.update(clean(raw["html"]))
    if rec["status"] == 200 and rec["words"] < config.THIN_PAGE_WORDS:
        rec["note"] = "thin_or_js_rendered"
    return rec


def discover_path(links: List[Dict], base_url: str, candidates: List[str]) -> Optional[str]:
    """Prefer a real link on the homepage over guessing a path (e.g. careers lives on
    jobs.lever.co, or /company/careers). Falls back to the first guessed path."""
    host = urlparse(base_url).netloc.replace("www.", "")
    for cand in candidates:
        word = cand.strip("/").lower()
        for l in links:
            href, txt = l["href"].lower(), l["text"].lower()
            if word in href.rstrip("/").split("/")[-1] or word == txt:
                full = urljoin(base_url, l["href"])
                link_host = urlparse(full).netloc.replace("www.", "")
                # allow the company's own site or a known ATS for careers
                if link_host.endswith(host) or any(a in link_host for a in
                                                     ("lever.co", "greenhouse.io", "ashbyhq.com", "workable.com")):
                    return full
    return candidates[0] if candidates else None
