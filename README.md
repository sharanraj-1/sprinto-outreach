# Sprinto cold outreach pipeline (MESA T2 · AI Case 2)

Give it a list of domains. For each one it scrapes the site, decides if the company fits
Sprinto's ICP (fetching more pages when the homepage isn't enough), finds the right
person, works out their two likeliest pains, writes a cold email to them by name and
sends it **to your own inbox** for review. Every decision is written to a report.

## Setup (once)

Needs Python 3.9+.

```bash
cd sprinto_outreach
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it in
```

`.env` needs:
- `OPENROUTER_API_KEY` (used automatically when set; runs `google/gemini-3.8-flash`, change with `OPENROUTER_MODEL`),
  or `GEMINI_API_KEY`: free, from aistudio.google.com → Get API key (default model `gemini-flash-latest`).
  To use Claude instead, set `LLM_PROVIDER=claude` and `ANTHROPIC_API_KEY`, then `pip install anthropic`.
- `HUNTER_API_KEY`: hunter.io → API (free tier: 25 searches a month)
- `SEND_TO`: the one inbox every reviewed draft goes to.
- `RESEND_API_KEY` (tried first; the sandbox sender only delivers to your Resend account's email),
  or `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD`: Google Account → Security → 2-Step Verification → App passwords (16 characters)

If you use Apollo instead of Hunter (Option B), put the contacts you looked up into
`contacts.csv` (`domain,first_name,last_name,title,email,linkedin`). A CSV row always
beats Hunter for that domain.

## Run

```bash
python run.py domains.txt --no-send     # dry run: prints the emails, sends nothing
python run.py domains.txt               # real run: emails land in your inbox
```

Output goes to `runs/<timestamp>/`: `report.md` (the defence) plus one JSON per company.

## The 7 steps → files

| Step | File | What it decides |
|---|---|---|
| 1 Scrape | `scrape.py` | Plain requests, robots.txt checked, polite delay, cache. Heuristic clean: drop nav/footer/scripts, blocks under 8 words, duplicates; capped at ~2,000 tokens. Cert badges in the footer are kept on purpose. |
| 2 Extract | `extract.py` | The model fills **named fields** (null if the page doesn't say). Quotable facts keep the page they came from. |
| 3 Qualify + loop | `qualify.py` | The **code** checks which Evidence Map questions are still open and fetches the **named** page for each. Cap: 4 fetches beyond the homepage. Then the **model** scores fit 1-5 and the **code** applies the threshold (≥3). |
| 4 PoC | `poc.py` | Contacts from the CSV, then Hunter. The ranking is code: security/compliance owner → CTO/eng leader → founder/CEO → COO. Emails with confidence under 50 are dropped. |
| 5 Pains | `write.py` | Two pains, each tied to a scraped fact. Logged before any email is written. |
| 6 Email | `write.py` | Drafted, then checked in code (opens with their first name, 70-120 words, one question, no hype words). Redrafted once if a check fails, then flagged. |
| 7 Send | `send.py` | Gmail SMTP. `To:` is always your own address. A review footer shows the intended recipient and the reasons behind the email. |

## Defending the decisions

**"Right company" (Evidence Map, `config.py`)**

| Question | Answered when | Where | Fetches | If unanswered | Short-circuit |
|---|---|---|---|---|---|
| Already certified? | a SOC 2 / ISO 27001 cert is named | footer, /trust, /security | 1 | treat as not certified, flag it | yes: disqualify |
| B2B SaaS? | the site names a business buyer | homepage, /product, /about | 1 | manual review | yes if not B2B SaaS |
| Right stage? | stage named, or ≥5 open roles, or an enterprise pricing tier | /careers, /pricing, /customers | 2 | qualify at lower rank, flagged | Series D+ disqualifies |

Then comes **why now**, which is the model's call: hiring for security/compliance, "SOC 2 in progress", an enterprise
tier with big customer logos, regulated buyers. A generic SaaS with no trigger scores 2 and is dropped. That is how the
pipeline avoids "qualifying every company".

Two of three answered, one unknown → **qualify and flag**, ranked one point lower.

**Why this person:** At Series A-C, whoever owns security makes the compliance decision. If there is no security
hire, it is the CTO, and at the smallest companies it is the founder. Marketing, sales and HR titles are never
picked, even when Hunter is very confident about their email.

**Why this email:** The opener is a real fact from their own site (the report records which one). The pain is
written in their words, the CTA is a single question, and nothing about Sprinto or the prospect is made up.

**Failure handling (L6 retry table)**

| Failure | Kind | What happens |
|---|---|---|
| Site down, blocked or robots.txt says no | permanent | manual review, next company |
| Page thin (JS-rendered) | permanent for that page | the loop tries the next named page |
| Model rate limit (free tier) | transient | wait 15-60s, retry up to 4× |
| Model refuses / safety block | permanent | manual review |
| Hunter timeout | transient | retry once, then "needs contact" |
| No suitable contact | permanent, a human can supply one | `needs_contact`: add a row to `contacts.csv` |
| Gmail timeout | transient | retry 3×, then skip |
| Gmail login rejected | permanent, same for every company | sending switched off for the rest of the run, drafts still saved |

## Cost

Each company makes 2-6 extraction calls on cleaned text (≤2k tokens each), one qualification call, and two or
three writing calls. Disqualified companies stop before the writing calls. On Gemini's free tier that costs
nothing, but the per-minute limit means a 10-company run takes a few minutes (the pipeline waits and retries).

## Web app on Vercel

Live at **https://sprinto-outreach-three.vercel.app** (`app.py` + `web/index.html`). Open to anyone, no password.

- Open the link, paste domains, click **Run pipeline**. Each domain is one request
  (about 30-60 seconds; Vercel allows up to 300).
- Each card shows the decision, the research loop, the contact and why, the pain points, and the draft.
  You can **edit** the subject and body, then click **Send to my inbox**. Nothing is sent before that click.
- Keys live in Vercel → Project → Settings → Environment Variables, not in `.env`.
  To change one: `vercel env add NAME production --force`, then `vercel deploy --prod`.
- Vercel can't keep files between runs, so Hunter results aren't cached there: each run of the same domain uses a
  Hunter search. Use the command line (`run.py`) for repeated runs.
- Redeploy after code changes: `vercel deploy --prod`.

## Checkpoints and the deep scan (v2)

**Checkpoint 1: B2B SaaS, checked first.** Right after the homepage, the model classifies the business model
(B2B SaaS / consumer app / online store / agency or services / marketplace / hardware / media / non-profit) and
quotes the phrase that decided it. Anything that isn't B2B SaaS is **disqualified immediately**: no further
pages, no Hunter search, no email. An online-store tool (Shopify, WooCommerce) plus shopping-cart wording also fails it.

**Deep scan (`deepscan.py`): every layer of the homepage, not just the visible text**

| Layer | What it pulls |
|---|---|
| HTML | JSON-LD structured data (founding date, employees, socials, "BusinessApplication"), meta/OpenGraph tags, embedded app data (`__NEXT_DATA__`, Nuxt, Apollo: the full content of JS-built sites), every link |
| JavaScript | third-party tools (Salesforce/Marketo/6sense = enterprise sales; WorkOS/Okta = SSO for enterprises; Vanta/Drata/SafeBase = compliance already running) and keyword hits in the site's own bundles |
| CSS | site builder and certification-badge images (`soc2.svg`, `iso-27001.png`) |
| sitemap.xml | which pages really exist (/security, /trust, /careers, /pricing), used to pick the next fetch |
| Job board API | Greenhouse / Lever / Ashby / Workable public JSON: exact open-role count and security/compliance hires |

**Verification phase:** after the research loop, with its own budget of 2 pages. It fetches the trust centre and
security pages the deep scan discovered, then checks every item against all sources:

1. Is B2B SaaS
2. Not already SOC 2 / ISO 27001 certified (fails if a competitor trust centre such as Vanta or Drata is found;
   **conflict** if code or badges mention a certification the text doesn't state)
3. Series A-C sized
4. Compliance trigger (why now)
5. Enough data collected. If it fails, the company goes to manual review as "needs browser scraping".
