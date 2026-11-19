"""Steps 5 and 6 - two pain points for this person, then the email.

Kept as two calls on purpose: the pain points are a decision you have to defend on
their own ("why did the email say this?"), so they are logged before any copy is written.
After drafting, the CODE checks the email: addressed by first name, under the word
limit, the hook actually comes from a scraped fact, one question as the CTA.
"""
import re
from typing import Dict, List

import config
from llm import LS, S, call_json, obj

PAIN_SCHEMA = obj({
    "pain_points": {"type": "array", "items": obj({
        "pain": S,          # one sentence, in the person's language, not Sprinto's
        "evidence": S,      # the scraped fact that makes us think this
        "cost_to_them": S,  # what it costs them if unsolved (deals, time, audit)
    })},
})

PAIN_SYSTEM = f"""You are a sharp B2B sales researcher for Sprinto.
{config.SPRINTO_FACTS}
Given facts about a company and one person there, name the TWO most likely pains this
person has around security/compliance right now. Each must be tied to a specific fact
given (quote it in evidence) and phrased the way that person would say it to a peer -
not as a product feature. No invented numbers, customers or events."""

EMAIL_SCHEMA = obj({
    "subject": S,
    "body": S,
    "hook_fact_used": S,      # the exact scraped fact the opener rests on
    "why_this_angle": S,      # one line you can say out loud to defend it
})

EMAIL_SYSTEM = f"""You write first-touch cold emails for Sprinto that a busy technical leader would reply to.
{config.SPRINTO_FACTS}
Rules:
- Start with "Hi <first name>," on its own line.
- First sentence: the hook - one specific, real fact about THEIR company (from the facts given),
  connected to why compliance is on their plate now. No flattery, no "I hope this finds you well".
- Then the pain in one or two sentences, in their words. Then one line on how Sprinto removes it.
- End with ONE low-effort question as the CTA (e.g. whether it is worth a 15-minute look), not a calendar link.
- 70-120 words in the body. Plain text. No bullet lists, no exclamation marks, no buzzwords
  ("revolutionize", "seamless", "leverage", "game-changer").
- Never invent facts, customer names, numbers or results about Sprinto or the prospect.
- Subject: 2-6 words, lowercase is fine, specific to them, not salesy.
- Sign off with: {config.SENDER_NAME}\\n{config.SENDER_TITLE}"""

BANNED = ["revolutionize", "seamless", "leverage", "game-changer", "hope this finds you", "synergy"]


def pain_points(facts: Dict, poc: Dict) -> List[Dict]:
    user = (f"PERSON: {poc['first_name']} {poc['last_name']}, {poc['position']}\n"
            f"COMPANY FACTS:\n" + "\n".join(f"- {k}: {v}" for k, v in facts.items() if v not in (None, [], "")))
    return call_json(PAIN_SYSTEM, user, PAIN_SCHEMA, effort="medium")["pain_points"][:2]


def draft_email(facts: Dict, poc: Dict, pains: List[Dict], why_now: str, feedback: str = "") -> Dict:
    user = (f"TO: {poc['first_name']} {poc['last_name']}, {poc['position']} at {facts.get('company_name')}\n"
            f"WHY NOW (from qualification): {why_now}\n"
            f"PAIN POINTS:\n" + "\n".join(f"- {p['pain']} (evidence: {p['evidence']})" for p in pains) +
            f"\nHOOK CANDIDATES (pick the most specific and recent):\n" +
            "\n".join(f"- {h}" for h in (facts.get("hook_facts") or []) + (facts.get("compliance_pressure_signals") or [])) +
            (f"\n\nYOUR PREVIOUS DRAFT FAILED THESE CHECKS, FIX THEM: {feedback}" if feedback else ""))
    return call_json(EMAIL_SYSTEM, user, EMAIL_SCHEMA, effort="high")


def check_email(email: Dict, poc: Dict) -> List[str]:
    problems = []
    body = email["body"]
    if not body.strip().lower().startswith(f"hi {poc['first_name'].lower()}"):
        problems.append(f"does not open with 'Hi {poc['first_name']},'")
    words = len(re.findall(r"\w+", body.split(config.SENDER_NAME)[0]))
    if not 60 <= words <= 140:
        problems.append(f"body is {words} words (target 70-120)")
    if body.count("?") != 1:
        problems.append(f"needs exactly one question as the CTA, found {body.count('?')}")
    if "!" in body:
        problems.append("contains an exclamation mark")
    hit = [b for b in BANNED if b in body.lower()]
    if hit:
        problems.append(f"banned phrases: {hit}")
    if not email.get("hook_fact_used"):
        problems.append("no hook fact recorded")
    return problems


def write_email(facts: Dict, poc: Dict, pains: List[Dict], why_now: str, log: List[str]) -> Dict:
    """Draft, check in code, redraft once with the failures as feedback. Cap = 2 drafts."""
    email = draft_email(facts, poc, pains, why_now)
    problems = check_email(email, poc)
    if problems:
        log.append(f"draft 1 failed checks: {problems} - redrafting once")
        email = draft_email(facts, poc, pains, why_now, feedback="; ".join(problems))
        problems = check_email(email, poc)
    email["check_problems"] = problems
    if problems:
        log.append(f"draft 2 still has: {problems} - flagged for human edit")
    return email
