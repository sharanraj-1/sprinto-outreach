"""All the decisions you have to defend live here, in one place.

Change the ICP, the evidence map, the caps or the PoC ranking here, never inside
the step code.
"""
import os

from dotenv import load_dotenv

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
# Vercel functions can only write to /tmp; locally caches sit next to the code.
CACHE_ROOT = "/tmp/sprinto-cache" if os.getenv("VERCEL") else os.path.join(HERE, ".cache")
# Password for the web app (Vercel). Empty = no password (only OK when running locally).
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

# --- Model -------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# OpenRouter: its own variable, or an OpenRouter key (sk-or-...) pasted into GEMINI_API_KEY.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "") or (GEMINI_API_KEY if GEMINI_API_KEY.startswith("sk-or-") else "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3.8-flash")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openrouter" if OPENROUTER_API_KEY else "gemini").strip().lower()   # openrouter | gemini | claude
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")

# --- Keys and identity ---------------------------------------------------------
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")          # you send FROM and TO this
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
# Resend (tried first when set). The sandbox sender only delivers to the Resend account's own email.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM = os.getenv("RESEND_FROM", "Sprinto Outreach Drafts <onboarding@resend.dev>")
# The one inbox every reviewed draft goes to. Defaults to the Gmail address.
SEND_TO = os.getenv("SEND_TO", "") or GMAIL_ADDRESS
SENDER_NAME = os.getenv("SENDER_NAME", "Aryan")         # who signs the email
SENDER_TITLE = os.getenv("SENDER_TITLE", "Growth, Sprinto")

# --- Scraping (L4: single-fetch, request-based, heuristic clean) ---------------
USER_AGENT = "SprintoOutreachResearchBot/1.0 (+student project; respects robots.txt)"
REQUEST_TIMEOUT = 15
POLITE_DELAY_SECONDS = 1.5      # between requests to the same site
MAX_CLEAN_CHARS = 8000          # ~2,000 tokens, the cap the lecture sets
MIN_BLOCK_WORDS = 8             # "drop anything under eight words"
THIN_PAGE_WORDS = 200           # below this the page is probably JS-rendered

# --- The agent loop (L4: "a loop without a limit is a cost without a ceiling") --
MAX_EXTRA_FETCHES = 4           # worst case from the Evidence Map: 4 beyond the homepage

# --- The Evidence Map (L4 slide 40), as data the code checks ------------------
# Each question: which field answers it, where to look, how many fetches it is worth,
# and what happens if it stays unanswered.
EVIDENCE_MAP = [
    # Checkpoint 1 - asked FIRST. Anything that is not B2B SaaS stops here, immediately.
    {
        "question": "b2b_saas",
        "fields": ["business_model", "is_b2b_saas"],
        "where": ["/product", "/platform", "/pricing", "/about"],
        "limit": 1,
        "if_unanswered": "manual_review",
        "short_circuit": "disqualify immediately if NOT B2B SaaS",
    },
    {
        "question": "already_certified",
        "fields": ["certifications_held"],
        "where": ["/trust", "/security"],
        "limit": 1,
        "if_unanswered": "treat_as_not_certified_and_flag",
        "short_circuit": "disqualify if a SOC 2 or ISO 27001 certification is held",
    },
    {
        "question": "right_stage",
        "fields": ["funding_stage", "open_roles_count", "enterprise_tier_on_pricing"],
        "where": ["/careers", "/pricing", "/customers", "/jobs"],
        "limit": 2,
        "if_unanswered": "qualify_lower_rank_flagged",
        "short_circuit": None,
    },
]

# Verification phase: extra pages it may fetch to double-check answers (own budget, after the loop)
VERIFY_MAX_FETCHES = 2
# Below this much readable text (visible + embedded app data) the scrape is called incomplete.
MIN_GOOD_WORDS = 400
NOT_B2B_MODELS = ["b2c_consumer_app", "ecommerce_store", "agency_or_services", "marketplace",
                  "hardware_or_physical", "media_or_content", "nonprofit_or_government"]

# Certifications that mean Sprinto's core wedge is already sold.
DISQUALIFYING_CERTS = ["soc 2", "soc2", "iso 27001", "iso27001"]

# Stage evidence that counts as "Series A-C sized" (sufficient-when rule).
MIN_OPEN_ROLES_FOR_STAGE = 5
FITTING_STAGES = ["seed", "series a", "series b", "series c"]
TOO_LATE_STAGES = ["series d", "series e", "series f", "public", "ipo", "acquired"]

# --- PoC ranking (Step 4) -----------------------------------------------------
# Who signs off on a compliance tool at a Series A-C SaaS company, in order.
# Small teams (no security hire yet) -> the CTO / founder owns it.
POC_TITLE_PRIORITY = [
    ("security_compliance_owner", ["ciso", "head of security", "security lead", "director of security",
                                   "head of compliance", "compliance", "grc", "information security", "trust"]),
    ("cto", ["cto", "chief technology", "vp engineering", "vp of engineering", "vp, engineering",
             "vice president of engineering", "vice president, engineering", "vice president engineering",
             "head of engineering", "director of engineering", "engineering director"]),
    ("founder_ceo", ["ceo", "founder", "co-founder", "chief executive"]),
    ("coo_ops", ["coo", "chief operating", "head of operations"]),
]
MIN_HUNTER_CONFIDENCE = 50
# Free, keyless contact finding from the company's own pages when the CSV and Hunter give nobody (sitecontacts.py).
FREE_CONTACT_FINDER = os.getenv("FREE_CONTACT_FINDER", "1") != "0"
CONTACTS_CSV = os.getenv("CONTACTS_CSV", os.path.join(HERE, "contacts.csv"))   # Apollo fallback (Option B)

# --- What Sprinto actually does (only claims we can stand behind) ---------------
SPRINTO_FACTS = """Sprinto is a compliance automation platform for SaaS companies.
It gets companies audit-ready for SOC 2, ISO 27001, GDPR, HIPAA and other frameworks by
connecting to their cloud, HR and code tools, continuously monitoring controls, collecting
evidence automatically and guiding them through the audit. It is built for lean teams that
do not have a full-time compliance function."""
