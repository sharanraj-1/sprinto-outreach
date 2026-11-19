"""Run the whole 7-step pipeline over a list of domains.

    python run.py domains.txt              # research, qualify, draft, SEND to your inbox
    python run.py domains.txt --no-send    # everything except Gmail (prints the emails)
    python run.py acme.com other.io        # domains straight on the command line

Each company is independent: one failure is logged with its reason and the run moves on
(L4: "degrade gracefully"). Every decision lands in runs/<timestamp>/ as JSON + a
human-readable report you can defend from.
"""
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Dict, List

from llm import LLMError, LLMRefused
from poc import find_poc
from qualify import qualify
from send import SendAuthError, send
from write import pain_points, write_email


def run_one(domain: str, do_send: bool, state: Dict) -> Dict:
    t0 = time.time()
    rec: Dict = {"domain": domain}
    try:
        # Steps 1-3: scrape, extract, qualify (agent loop inside)
        q = qualify(domain)
        rec.update(q)
        if q["status"] not in ("qualified", "qualified_flagged"):
            return rec

        # Step 4: point of contact
        poc_log: List[str] = []
        poc = find_poc(domain, poc_log)
        rec["poc"], rec["poc_log"] = poc, poc_log
        if not poc["found"]:
            rec["status"] = "needs_contact"
            rec["reason"] += f" | {poc['why']}"
            return rec

        # Step 5: two pain points
        rec["pain_points"] = pain_points(q["facts"], poc)

        # Step 6: email (draft -> code checks -> one redraft)
        write_log: List[str] = []
        email = write_email(q["facts"], poc, rec["pain_points"], q["judgment"]["why_now"], write_log)
        rec["email"], rec["write_log"] = email, write_log

        # Step 7: send to your own inbox
        if do_send and not state.get("send_disabled"):
            try:
                rec["send_status"] = send(email, poc, q["facts"].get("company_name") or domain, q["reason"])
            except SendAuthError as e:
                state["send_disabled"] = str(e)
                rec["send_status"] = f"not sent: {e}"
        else:
            rec["send_status"] = "not sent (--no-send)" if not do_send else f"not sent: {state['send_disabled']}"
        rec["status"] = "emailed" if rec["send_status"] == "sent" else "drafted"
    except LLMRefused as e:
        rec.update(status="manual_review", reason=f"Model could not complete a step: {e}")
    except LLMError as e:
        rec.update(status="error", reason=f"Model API error: {e}")
    except Exception as e:  # never let one company kill the batch
        rec.update(status="error", reason=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-800:])
    finally:
        rec["seconds"] = round(time.time() - t0, 1)
    return rec


def report(results: List[Dict]) -> str:
    order = {"emailed": 0, "drafted": 1, "needs_contact": 2, "manual_review": 3, "disqualified": 4, "error": 5}
    results = sorted(results, key=lambda r: (order.get(r.get("status"), 9), -(r.get("rank_score") or 0)))
    out = ["# Sprinto outreach run\n",
           "| Company | Status | Fit | Contact | Time |", "|---|---|---|---|---|"]
    for r in results:
        fit = r.get("judgment", {}).get("fit_score", "-")
        who = f"{r['poc']['first_name']} {r['poc']['last_name']} ({r['poc']['position']})" if r.get("poc", {}).get("found") else "-"
        out.append(f"| {r['domain']} | {r.get('status')} | {fit} | {who} | {r.get('seconds')}s |")

    for r in results:
        out += [f"\n## {r['domain']} - {r.get('status')}", f"**Why:** {r.get('reason', '')}"]
        if r.get("flags"):
            out.append(f"**Flags:** {'; '.join(r['flags'])}")
        if r.get("checklist"):
            icon = {"pass": "✅", "fail": "❌", "conflict": "⚠️", "unknown": "❔"}
            out.append("**Verification checklist:**\n" + "\n".join(
                f"- {icon.get(c['status'], '?')} {c['label']}: {c['detail']}"
                + (f" _({'; '.join(c['support'])})_" if c.get("support") else "") for c in r["checklist"]))
        if r.get("research_log"):
            out.append("**Research loop:**\n" + "\n".join(f"- {x}" for x in r["research_log"]))
        j = r.get("judgment")
        if j:
            out.append("**Evidence the score rests on:**\n" + "\n".join(f"- {x}" for x in j["evidence_used"]))
            if j.get("risks"):
                out.append("**Risks:** " + "; ".join(j["risks"]))
        if r.get("poc", {}).get("found"):
            out.append(f"**Why this person:** {r['poc']['why']}")
            if r["poc"].get("alternatives"):
                out.append(f"**Next best:** {', '.join(r['poc']['alternatives'])}")
        if r.get("pain_points"):
            out.append("**Pain points:**\n" + "\n".join(
                f"- {p['pain']} _(evidence: {p['evidence']}; cost: {p['cost_to_them']})_" for p in r["pain_points"]))
        e = r.get("email")
        if e:
            out += [f"**Why this angle:** {e['why_this_angle']}", f"**Hook:** {e['hook_fact_used']}",
                    f"\n**Subject:** {e['subject']}\n", "```", e["body"], "```",
                    f"Send: {r.get('send_status')}"]
    return "\n".join(out)


def main(argv: List[str]) -> None:
    do_send = "--no-send" not in argv
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    domains: List[str] = []
    for a in args:
        if Path(a).is_file():
            domains += [l.strip() for l in Path(a).read_text().splitlines() if l.strip() and not l.startswith("#")]
        else:
            domains.append(a)
    domains = [d.replace("https://", "").replace("http://", "").strip("/") for d in domains]

    run_dir = Path("runs") / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    state: Dict = {}
    results = []
    for i, d in enumerate(domains, 1):
        print(f"[{i}/{len(domains)}] {d} ...", flush=True)
        r = run_one(d, do_send, state)
        results.append(r)
        (run_dir / f"{d}.json").write_text(json.dumps(r, indent=2, default=str))
        print(f"    -> {r.get('status')}: {r.get('reason', '')[:140]}")
        if r.get("email") and not do_send:
            print(f"\n    Subject: {r['email']['subject']}\n" + "\n".join("    " + l for l in r["email"]["body"].splitlines()) + "\n")

    (run_dir / "report.md").write_text(report(results))
    print(f"\nReport: {run_dir / 'report.md'}")
    if state.get("send_disabled"):
        print(f"Sending was switched off mid-run: {state['send_disabled']}")


if __name__ == "__main__":
    main(sys.argv[1:])
