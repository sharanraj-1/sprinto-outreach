"""Deep scan - read EVERYTHING the homepage ships, not just the visible text.

The visible-text scraper (scrape.py) misses most of what a site actually tells you.
This module screens all three layers of the page, plus two public, structured sources:

  HTML  - JSON-LD structured data (Organization: name, founding date, employees, socials),
          meta / OpenGraph tags, embedded app state (__NEXT_DATA__, __NUXT__, Apollo state:
          on JS-built sites this holds the WHOLE page content the plain scrape missed),
          every link (trust centres, job boards, app stores, docs, status pages).
  JS    - which third-party tools load (the "tech stack" = how they sell: Salesforce, Marketo,
          6sense, Chili Piper = enterprise sales motion; WorkOS / Okta = selling SSO to
          enterprises; Vanta / Drata / SafeBase trust centre = already doing compliance),
          plus a keyword search of the site's OWN bundles (compliance words, job boards).
  CSS   - stylesheet hosts (site builder: Webflow, Framer, Shopify...) and image URLs that
          look like certification badges (soc2.svg, iso-27001.png).
  sitemap.xml          - the site's own list of pages: does /security, /trust, /careers exist?
  public job-board API - Greenhouse / Lever / Ashby / Workable publish open roles as JSON.
                         Exact role count + whether they're hiring for security/compliance.

All fetches go through scrape._get: robots.txt respected, polite delay, cached.
Everything is capped (number of files, bytes per file) so one site can't blow the budget.
"""
import json
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scrape import _get

MAX_JS_FILES = 4
MAX_CSS_FILES = 2
MAX_ASSET_BYTES = 2_500_000
MAX_APPSTATE_CHARS = 6000

# --- what third-party hosts tell us -------------------------------------------
VENDORS = {
    # compliance / trust centre - they already work on compliance (and may use a Sprinto competitor)
    "trust.vanta.com": ("compliance", "Vanta trust centre (Sprinto competitor)"),
    "vanta.com": ("compliance", "Vanta (Sprinto competitor)"),
    "drata.com": ("compliance", "Drata (Sprinto competitor)"),
    "secureframe.com": ("compliance", "Secureframe (Sprinto competitor)"),
    "safebase.io": ("compliance", "SafeBase trust centre"),
    "conveyor.com": ("compliance", "Conveyor trust centre"),
    "whistic.com": ("compliance", "Whistic trust centre"),
    "trustcloud.ai": ("compliance", "TrustCloud trust centre"),
    "sprinto.com": ("compliance", "Sprinto (already a customer?)"),
    # selling to enterprises
    "workos.com": ("enterprise", "WorkOS (enterprise SSO/SCIM)"),
    "okta.com": ("enterprise", "Okta"),
    "auth0.com": ("enterprise", "Auth0"),
    "pardot.com": ("enterprise", "Salesforce Pardot"),
    "salesforce.com": ("enterprise", "Salesforce"),
    "force.com": ("enterprise", "Salesforce"),
    "marketo.net": ("enterprise", "Marketo"),
    "mktoresp.com": ("enterprise", "Marketo"),
    "6sc.co": ("enterprise", "6sense (ABM)"),
    "demandbase.com": ("enterprise", "Demandbase (ABM)"),
    "chilipiper.com": ("enterprise", "Chili Piper (demo booking)"),
    "clearbit.com": ("enterprise", "Clearbit"),
    "gong.io": ("enterprise", "Gong"),
    # B2B SaaS motion
    "hubspot.com": ("b2b", "HubSpot"), "hs-scripts.com": ("b2b", "HubSpot"), "hsforms.net": ("b2b", "HubSpot forms"),
    "intercom.io": ("b2b", "Intercom"), "intercomcdn.com": ("b2b", "Intercom"),
    "drift.com": ("b2b", "Drift"), "segment.com": ("b2b", "Segment"), "segment.io": ("b2b", "Segment"),
    "calendly.com": ("b2b", "Calendly"), "stripe.com": ("b2b", "Stripe"), "js.stripe.com": ("b2b", "Stripe"),
    "posthog.com": ("b2b", "PostHog"), "mixpanel.com": ("b2b", "Mixpanel"), "amplitude.com": ("b2b", "Amplitude"),
    "statuspage.io": ("b2b", "Statuspage"),
    # site builders (context, and Shopify = online store, not SaaS)
    "webflow.com": ("builder", "Webflow"), "website-files.com": ("builder", "Webflow"),
    "framerusercontent.com": ("builder", "Framer"), "framer.com": ("builder", "Framer"),
    "wp-content": ("builder", "WordPress"), "wix.com": ("builder", "Wix"), "squarespace.com": ("builder", "Squarespace"),
    "cdn.shopify.com": ("ecommerce", "Shopify store"), "myshopify.com": ("ecommerce", "Shopify store"),
    "woocommerce": ("ecommerce", "WooCommerce store"),
    # consumer
    "apps.apple.com": ("consumer", "iOS app"), "play.google.com": ("consumer", "Android app"),
    # job boards
    "greenhouse.io": ("jobs", "Greenhouse"), "lever.co": ("jobs", "Lever"),
    "ashbyhq.com": ("jobs", "Ashby"), "workable.com": ("jobs", "Workable"),
}

ATS_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([a-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.%-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([a-z0-9_-]+)", re.I)),
]
ATS_API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
}
ATS_IGNORE = {"embed", "js", "v1", "api", "careers", "jobs", "job_board"}

KEYWORDS = {
    "soc2": re.compile(r"soc\s?-?2(?:\s*type\s*(?:ii|2|i|1))?", re.I),
    "iso27001": re.compile(r"iso[\s/-]?(?:iec\s?)?27001", re.I),
    "gdpr": re.compile(r"\bgdpr\b", re.I),
    "hipaa": re.compile(r"\bhipaa\b", re.I),
    "pci": re.compile(r"\bpci[\s-]?dss\b", re.I),
    "in_progress": re.compile(r"(?:soc\s?2|iso\s?27001)[^.]{0,40}(?:in progress|underway|coming soon|pursuing|working towards)", re.I),
    "security_questionnaire": re.compile(r"security (?:questionnaire|review)s?|vendor (?:risk|assessment)", re.I),
    "sso_saml": re.compile(r"\b(?:saml|scim|single sign-on|sso)\b", re.I),
    "enterprise": re.compile(r"\benterprise\b", re.I),
    "free_trial": re.compile(r"free trial|start for free|sign up free|get started free", re.I),
    "book_demo": re.compile(r"book a demo|request a demo|schedule a demo|talk to sales|contact sales", re.I),
    "add_to_cart": re.compile(r"add to (?:cart|bag)|shopping (?:cart|bag)|free shipping|cash on delivery", re.I),
}
BADGE_IMG = re.compile(r"[\w/.-]*(?:soc[-_ ]?2|iso[-_ ]?27001|aicpa|gdpr|hipaa)[\w.-]*\.(?:svg|png|webp|jpe?g)", re.I)
SEC_ROLE = re.compile(r"security|compliance|grc|governance|risk|privacy|trust|audit|infosec|devsecops", re.I)
INTERESTING_PATHS = re.compile(r"/(security|trust|compliance|careers|jobs|pricing|customers|case-stud|about|enterprise|soc-?2|privacy)", re.I)


def _host(u: str) -> str:
    return urlparse(u).netloc.lower()


def _vendor_for(url: str) -> Optional[tuple]:
    u = url.lower()
    for key, val in VENDORS.items():
        if key in u:
            return val
    return None


def _strings_from_json(obj, out: List[str], limit: int) -> None:
    """Walk embedded app state and keep human sentences (>= 6 words)."""
    if sum(len(s) for s in out) > limit:
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _strings_from_json(v, out, limit)
    elif isinstance(obj, list):
        for v in obj:
            _strings_from_json(v, out, limit)
    elif isinstance(obj, str):
        s = " ".join(BeautifulSoup(obj, "html.parser").get_text(" ").split()) if "<" in obj else " ".join(obj.split())
        if len(s.split()) >= 6 and not s.startswith(("http", "{", "/")) and s not in out:
            out.append(s[:400])


def _keyword_hits(text: str, source: str, hits: Dict[str, List[str]]) -> None:
    for name, rx in KEYWORDS.items():
        for m in rx.finditer(text):
            a, b = max(0, m.start() - 70), min(len(text), m.end() + 70)
            snippet = " ".join(re.sub(r"[^\w\s.,:/&()-]", " ", text[a:b]).split())
            lst = hits.setdefault(name, [])
            if len(lst) < 4 and snippet not in (x.split("] ", 1)[-1] for x in lst):
                lst.append(f"[{source}] {snippet}")


def scan(domain: str, home_html: str, home_url: str, log: List[str]) -> Dict:
    soup = BeautifulSoup(home_html or "", "html.parser")
    site_host = _host(home_url).replace("www.", "")
    sig: Dict = {"structured_data": [], "meta": {}, "app_state_text": [], "vendors": {}, "badge_images": [],
                 "keyword_hits": {}, "sitemap_pages": [], "jobs": None, "trust_center_url": None,
                 "app_store_links": [], "scanned": {"html": bool(home_html), "js_files": 0, "css_files": 0,
                                                    "sitemap": False, "job_board": False},
                 "site_builder": None}

    # ---------------- HTML layer ----------------
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for item in data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            keep = {k: item[k] for k in ("@type", "name", "description", "foundingDate", "numberOfEmployees",
                                         "applicationCategory", "offers", "sameAs", "founder", "address") if k in item}
            if keep:
                sig["structured_data"].append(json.dumps(keep, default=str)[:600])

    for m in soup.find_all("meta"):
        k = m.get("property") or m.get("name") or ""
        if k.lower() in ("og:title", "og:description", "og:type", "twitter:description", "keywords",
                         "application-name", "generator") and m.get("content"):
            sig["meta"][k.lower()] = m["content"][:300]
    gen = sig["meta"].get("generator", "")
    if gen:
        sig["site_builder"] = gen[:60]

    for sid in ("__NEXT_DATA__", "__NUXT_DATA__"):
        tag = soup.find("script", id=sid)
        if tag and tag.string:
            try:
                _strings_from_json(json.loads(tag.string), sig["app_state_text"], MAX_APPSTATE_CHARS)
            except ValueError:
                pass
    for tag in soup.find_all("script"):
        s = tag.string or ""
        m = re.search(r"window\.(?:__NUXT__|__APOLLO_STATE__|__INITIAL_STATE__|__PRELOADED_STATE__)\s*=\s*(\{.*\})\s*;?\s*$", s, re.S)
        if m:
            try:
                _strings_from_json(json.loads(m.group(1)), sig["app_state_text"], MAX_APPSTATE_CHARS)
            except ValueError:
                pass

    all_urls = set()
    for a in soup.find_all("a", href=True):
        all_urls.add(urljoin(home_url, a["href"]))
    for t in soup.find_all(["script", "img", "iframe", "source"], src=True):
        all_urls.add(urljoin(home_url, t["src"]))
    for l in soup.find_all("link", href=True):
        all_urls.add(urljoin(home_url, l["href"]))
    all_urls |= set(re.findall(r"https?://[^\s\"'<>)]+", home_html or ""))

    for u in all_urls:
        v = _vendor_for(u)
        if v:
            sig["vendors"].setdefault(v[0], set()).add(v[1])
        h = _host(u)
        if h.startswith("trust.") or "/trust" in urlparse(u).path.lower() or (v and v[0] == "compliance"):
            if not sig["trust_center_url"] and (h.endswith(site_host) or (v and v[0] == "compliance")):
                sig["trust_center_url"] = u.split("#")[0]
        if "apps.apple.com" in u or "play.google.com" in u:
            sig["app_store_links"].append(u[:120])
        if BADGE_IMG.search(u):
            sig["badge_images"].append(u.split("?")[0].split("/")[-1][:80])

    visible = soup.get_text(" ")
    _keyword_hits(visible, "html", sig["keyword_hits"])
    if sig["app_state_text"]:
        _keyword_hits(" ".join(sig["app_state_text"]), "app-data", sig["keyword_hits"])

    # ---------------- JS layer: the site's OWN bundles ----------------
    js = [u for u in (urljoin(home_url, t["src"]) for t in soup.find_all("script", src=True))
          if _host(u).replace("www.", "").endswith(site_host) or "/_next/" in u or "/assets/" in u]
    for u in js[:MAX_JS_FILES]:
        r = _get(u)
        if r["status"] != 200 or not r["html"]:
            continue
        body = r["html"][:MAX_ASSET_BYTES]
        sig["scanned"]["js_files"] += 1
        _keyword_hits(body, "js:" + u.split("/")[-1][:30], sig["keyword_hits"])
        for found in re.findall(r"https?://[a-z0-9.-]+\.[a-z]{2,}[^\s\"'`]*", body)[:400]:
            v = _vendor_for(found)
            if v and v[0] in ("compliance", "enterprise", "jobs"):
                sig["vendors"].setdefault(v[0], set()).add(v[1])
                all_urls.add(found)
        for b in BADGE_IMG.findall(body)[:5]:
            sig["badge_images"].append(b.split("?")[0].split("/")[-1][:80])

    # ---------------- CSS layer ----------------
    css = [urljoin(home_url, l["href"]) for l in soup.find_all("link", href=True)
           if "stylesheet" in (l.get("rel") or []) or l["href"].split("?")[0].endswith(".css")]
    for u in css[:MAX_CSS_FILES]:
        r = _get(u)
        if r["status"] != 200 or not r["html"]:
            continue
        body = r["html"][:MAX_ASSET_BYTES]
        sig["scanned"]["css_files"] += 1
        for b in BADGE_IMG.findall(body)[:5]:
            sig["badge_images"].append(b.split("?")[0].split("/")[-1][:80])
        v = _vendor_for(u)
        if v and v[0] in ("builder", "ecommerce") and not sig["site_builder"]:
            sig["site_builder"] = v[1]

    # ---------------- sitemap ----------------
    sm = _get(urljoin(home_url, "/sitemap.xml"))
    if sm["status"] == 200 and "<loc>" in (sm["html"] or ""):
        locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", sm["html"])
        if locs and all(l.endswith(".xml") for l in locs[:5]):          # sitemap index -> first child
            child = _get(locs[0])
            locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", child["html"] or "") if child["status"] == 200 else []
        sig["scanned"]["sitemap"] = True
        sig["sitemap_total"] = len(locs)
        sig["sitemap_pages"] = sorted({l for l in locs if INTERESTING_PATHS.search(urlparse(l).path)})[:25]

    # ---------------- public job board ----------------
    joined = " ".join(all_urls) + " " + " ".join(sig["sitemap_pages"])
    jobs = find_job_board(joined, log)
    if jobs:
        sig["jobs"] = jobs
        sig["scanned"]["job_board"] = True

    sig["vendors"] = {k: sorted(v) for k, v in sig["vendors"].items()}
    sig["badge_images"] = sorted(set(sig["badge_images"]))[:8]
    sig["app_store_links"] = sorted(set(sig["app_store_links"]))[:4]
    log.append("deep scan: " + ", ".join([
        f"{len(sig['structured_data'])} JSON-LD block(s)",
        f"{len(sig['app_state_text'])} app-data text block(s)",
        f"{sig['scanned']['js_files']} JS + {sig['scanned']['css_files']} CSS file(s) read",
        f"sitemap {'yes (' + str(sig.get('sitemap_total', 0)) + ' pages)' if sig['scanned']['sitemap'] else 'no'}",
        f"job board {sig['jobs']['board'] + ' (' + str(sig['jobs']['count']) + ' roles)' if sig['jobs'] else 'not found'}",
        f"tools: {sum(len(v) for v in sig['vendors'].values())}",
    ]))
    return sig


def find_job_board(text: str, log: List[str]) -> Optional[Dict]:
    """Find a Greenhouse/Lever/Ashby/Workable board link in any text and read its public JSON API."""
    for ats, rx in ATS_PATTERNS:
        for m in rx.finditer(text or ""):
            slug = m.group(1)
            if slug.lower() in ATS_IGNORE:
                continue
            r = _get(ATS_API[ats].format(slug=slug))
            if r["status"] != 200:
                log.append(f"deep scan: {ats} board '{slug}' found but its API returned {r['status']}")
                return None
            try:
                data = json.loads(r["html"])
            except ValueError:
                return None
            jobs = data if isinstance(data, list) else data.get("jobs", [])
            titles = [(j.get("title") or j.get("text") or "").strip() for j in jobs if isinstance(j, dict)]
            titles = [t for t in titles if t]
            log.append(f"deep scan: {ats} job board '{slug}' -> {len(titles)} open roles")
            return {"board": ats, "slug": slug, "count": len(titles),
                    "security_compliance_roles": [t for t in titles if SEC_ROLE.search(t)][:8],
                    "sample": titles[:12]}
    return None


def summary_for_llm(sig: Dict) -> str:
    """Compact text block the extraction model reads alongside the visible page text."""
    out = []
    if sig["meta"]:
        out.append("META TAGS: " + " | ".join(f"{k}={v}" for k, v in sig["meta"].items()))
    for s in sig["structured_data"][:4]:
        out.append("STRUCTURED DATA (JSON-LD): " + s)
    if sig["app_state_text"]:
        out.append("EMBEDDED APP DATA (text the visible scrape missed):\n" + "\n".join(sig["app_state_text"])[:MAX_APPSTATE_CHARS])
    if sig["vendors"]:
        out.append("THIRD-PARTY TOOLS ON THE SITE: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in sig["vendors"].items()))
    if sig["jobs"]:
        j = sig["jobs"]
        out.append(f"PUBLIC JOB BOARD ({j['board']}): {j['count']} open roles. Security/compliance roles: "
                   f"{j['security_compliance_roles'] or 'none'}. Sample titles: {j['sample']}")
    if sig["badge_images"]:
        out.append("CERTIFICATION-LIKE BADGE IMAGES: " + ", ".join(sig["badge_images"]))
    if sig["trust_center_url"]:
        out.append("TRUST CENTRE LINK: " + sig["trust_center_url"])
    if sig["app_store_links"]:
        out.append("APP STORE LINKS: " + ", ".join(sig["app_store_links"]))
    for k, v in sig["keyword_hits"].items():
        if k in ("soc2", "iso27001", "in_progress", "security_questionnaire", "add_to_cart"):
            out.append(f"KEYWORD '{k}' found: " + " || ".join(v[:3]))
    if sig["sitemap_pages"]:
        out.append("PAGES IN SITEMAP: " + ", ".join(urlparse(u).path for u in sig["sitemap_pages"][:20]))
    return "\n".join(out)
