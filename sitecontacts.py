"""Step 4 fallback: free, keyless contact finding from public sources.

Used when contacts.csv and Hunter give nothing (no key, quota used up, or no one with the right title).
It follows the same idea as open-source finders such as ColdReach and theHarvester, without their
Docker stack, so it runs inside a Vercel function:

  1. People   - the company's own team / about / leadership pages, plus JSON-LD "founder" data.
                The model extracts name + title, and each one must quote a sentence that really
                appears on the page (the code checks), so nobody is invented.
  2. Emails   - addresses on the company's own domain published anywhere on those pages.
  3. Format   - learned from a published personal address that matches a named person;
                otherwise guessed (first@domain) and clearly flagged as a guess.
  4. Mail     - an MX lookup over DNS-over-HTTPS confirms the domain receives email at all.

Confidence scale (same 0-100 field Hunter uses, so poc.py's ranking and threshold still apply):
  90 published on their site, 70 format learned from a real address, 60 format inferred from the
  shape of other addresses, 50 default first@domain guess. 0 if the domain has no mail server.
"""
import json
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import llm
from llm import LLMError, LLMRefused
from scrape import fetch_page

# whole path segments only: "/team" counts, "/solutions/sales-team" does not
PEOPLE_SEGMENTS = {"about", "about-us", "aboutus", "team", "our-team", "the-team", "leadership", "founders", "people",
                   "company", "who-we-are", "our-story", "story", "management", "meet-the-team"}
FALLBACK_PATHS = ["/about", "/team", "/about-us", "/company", "/leadership"]
MAX_PAGES = 3
ROLE_LOCALPARTS = {
    "info", "hello", "hi", "contact", "support", "help", "sales", "press", "media", "jobs", "careers", "hiring", "hr",
    "security", "privacy", "legal", "admin", "team", "billing", "accounts", "partners", "partnerships", "marketing",
    "noreply", "no-reply", "office", "enquiries", "inquiries", "feedback", "success", "compliance", "dpo", "abuse", "ops",
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

PATTERNS = {
    "first": lambda f, l: f,
    "first.last": lambda f, l: f"{f}.{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "flast": lambda f, l: f"{f[:1]}{l}",
    "f.last": lambda f, l: f"{f[:1]}.{l}",
    "first_last": lambda f, l: f"{f}_{l}",
    "firstl": lambda f, l: f"{f}{l[:1]}",
    "last": lambda f, l: l,
}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _norm(s: str) -> str:
    """Lowercase letters and digits only, single-spaced: quotes match whatever separators the page used."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def _people_pages(home: Dict, domain: str) -> List[str]:
    """Pick up to MAX_PAGES likely people pages from real homepage links, then fall back to guessed paths."""
    host = domain.replace("www.", "")
    found: List[str] = []
    for link in home.get("links", []):
        full = urljoin(home["url"], link["href"])
        p = urlparse(full)
        if not p.netloc.replace("www.", "").endswith(host):
            continue
        segs = [x for x in p.path.lower().split("/") if x]
        if segs and segs[-1] in PEOPLE_SEGMENTS and len(segs) <= 2:
            clean_url = f"{p.scheme}://{p.netloc}{p.path}"
            if clean_url not in found:
                found.append(clean_url)
    for path in FALLBACK_PATHS:
        if len(found) >= MAX_PAGES:
            break
        url = urljoin(home["url"], path)
        if url not in found:
            found.append(url)
    return found[:MAX_PAGES]


def _page_text(html: str) -> str:
    """All visible lines, short ones included. The shared cleaner drops blocks under 8 words,
    which is exactly what a name or a job title is."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
        tag.decompose()
    lines, last = [], None
    for raw in soup.get_text("\n").splitlines():
        line = " ".join(raw.split())
        if line and line != last and len(line) < 400:
            lines.append(line)
            last = line
    return "\n".join(lines)


def _founders_from_jsonld(html: str) -> List[Dict]:
    out = []
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            founders = item.get("founder") or item.get("founders") or []
            for f in founders if isinstance(founders, list) else [founders]:
                name = f.get("name") if isinstance(f, dict) else f if isinstance(f, str) else None
                if name and 1 < len(name.split()) <= 4:
                    out.append({"name": name.strip(), "title": "Founder", "quote": f"JSON-LD founder: {name.strip()}"})
    return out


def _emails_on(html: str, domain: str) -> List[str]:
    host = domain.replace("www.", "").lower()
    found = []
    for m in EMAIL_RE.findall(html or ""):
        e = m.lower().strip(".")
        d = e.split("@", 1)[1]
        if (d == host or d.endswith("." + host)) and not re.search(r"\.(png|jpg|jpeg|gif|svg|webp)$", e):
            if e not in found:
                found.append(e)
    return found


def _extract_people(pages: List[Dict], log: List[str]) -> List[Dict]:
    # people pages first (the homepage is last), each capped, so a long homepage cannot crowd out the team page
    ordered = pages[1:] + pages[:1]
    text = "\n\n".join(f"PAGE {p['url']}\n{_page_text(p.get('html', ''))[:7000]}" for p in ordered if p.get("html"))[:18000]
    if not text:
        return []
    schema = llm.obj({"people": {"type": "array", "items": llm.obj({"name": llm.S, "title": llm.S, "quote": llm.S})}})
    system = ("You extract people who work at the company from its own web pages. Only include a person when the page "
              "states their full name and their job title or role at this company. 'quote' must be copied exactly from "
              "the page and must contain the person's name. Do not include customers, investors, testimonial authors "
              "or advisors. Return an empty list if nobody qualifies.")
    try:
        out = llm.call_json(system, text, schema, effort="low", max_tokens=3000)
    except (LLMError, LLMRefused) as e:
        log.append(f"site contacts: people extraction failed ({e})")
        return []
    haystack = _norm(text)
    kept = []
    for p in out.get("people", []):
        name, quote = (p.get("name") or "").strip(), (p.get("quote") or "").strip()
        if len(name.split()) < 2 or not quote:
            continue
        if _norm(quote) not in haystack or _norm(name) not in haystack:
            log.append(f"site contacts: dropped '{name}' (quote not found on the page)")
            continue
        kept.append({"name": name, "title": (p.get("title") or "").strip(), "quote": quote})
    return kept


def _has_mx(domain: str, log: List[str]) -> Optional[bool]:
    try:
        r = requests.get("https://cloudflare-dns.com/dns-query", params={"name": domain, "type": "MX"},
                         headers={"accept": "application/dns-json"}, timeout=8)
        answers = r.json().get("Answer") or []
        ok = any(a.get("type") == 15 for a in answers)
        log.append(f"site contacts: MX records {'found' if ok else 'NOT found'} for {domain}")
        return ok
    except (requests.RequestException, ValueError):
        log.append("site contacts: MX lookup failed, carrying on")
        return None


def _learn_pattern(personal: List[str], people: List[Dict]) -> Tuple[Optional[str], int, str]:
    """Return (pattern, confidence, basis)."""
    for e in personal:
        local = e.split("@")[0]
        for p in people:
            parts = p["name"].split()
            f, l = _slug(parts[0]), _slug(parts[-1])
            for name, fn in PATTERNS.items():
                if f and l and fn(f, l) == local:
                    return name, 70, f"format learned from {e} ({p['name']})"
    if personal:
        local = personal[0].split("@")[0]
        guess = "first.last" if "." in local else "first_last" if "_" in local else "first"
        return guess, 60, f"format inferred from the shape of {personal[0]}"
    return None, 50, "no addresses published; guessed first@domain"


def find(domain: str, log: List[str]) -> List[Dict]:
    home = fetch_page(domain)
    if home.get("status") != 200:
        log.append(f"site contacts: homepage unavailable ({home.get('status')})")
        return []
    pages, emails = [home], _emails_on(home.get("html", ""), domain)
    for url in _people_pages(home, domain):
        u = urlparse(url)
        pg = fetch_page(f"{u.scheme}://{u.netloc}", u.path)   # fetch_page keeps only the base of a full URL
        if pg.get("status") == 200 and pg.get("html"):
            pages.append(pg)
            emails += [e for e in _emails_on(pg.get("html", ""), domain) if e not in emails]
    log.append(f"site contacts: read {len(pages)} page(s): " + ", ".join(urlparse(p['url']).path or "/" for p in pages))

    people = _extract_people(pages, log) + _founders_from_jsonld(home.get("html", ""))
    seen, uniq = set(), []
    for p in people:
        k = _norm(p["name"])
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    personal = [e for e in emails if e.split("@")[0] not in ROLE_LOCALPARTS]
    log.append(f"site contacts: {len(uniq)} named people, {len(emails)} published address(es)")
    if not uniq:
        return []

    mx = _has_mx(domain.replace("www.", ""), log)
    pattern, conf, basis = _learn_pattern(personal, uniq)
    host = domain.replace("www.", "")
    out = []
    for p in uniq:
        parts = p["name"].split()
        first, last = parts[0], parts[-1]
        f, l = _slug(first), _slug(last)
        published = next((e for e in personal if f and (f in e.split("@")[0]) and (l[:1] in e.split("@")[0])), None)
        if published:
            email, c, src, note = published, 90, "site_published", "address published on their site"
        else:
            local = PATTERNS[pattern or "first"](f, l)
            email, c = f"{local}@{host}", conf
            src = "site_pattern" if pattern else "site_pattern_guess"
            note = f"{basis}; not verified"
        if mx is False:
            c = 0
        out.append({
            "first_name": first, "last_name": last if len(parts) > 1 else "", "position": p["title"],
            "email": email, "confidence": c, "source": src, "linkedin": "",
            "email_basis": note, "evidence": p["quote"][:200],
        })
    return out
