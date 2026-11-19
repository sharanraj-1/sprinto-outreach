"""One helper for every model call: JSON in a fixed schema, or a clear failure.

Provider is set by LLM_PROVIDER in .env:
  gemini (default) - free key from aistudio.google.com
  claude           - needs an Anthropic Console key with credit

Either way:
- Structured output (a JSON schema) so every step returns named fields, which is what
  lets the code - not the model - check whether a field is filled (L4 design rule).
- "effort" maps to how much the model may think: low for extraction, high for the email.
- Rate limits (free tier!) are transient -> wait and retry up to 4 times.
- A safety block / refusal is permanent -> LLMRefused, the company goes to manual review.
"""
import json
import logging
import time
from typing import Dict

import config

# google-genai warns on every thinking response ("non-text parts: thought_signature"); harmless.
logging.getLogger("google_genai.types").setLevel(logging.ERROR)


class LLMRefused(Exception):
    pass


class LLMError(Exception):
    pass


# ---------------- Gemini ------------------------------------------------------
_gemini = None
GEMINI_THINKING = {"low": 0, "medium": 2048, "high": 8192}


def _call_gemini(system: str, user: str, schema: Dict, effort: str, max_tokens: int) -> Dict:
    global _gemini
    from google import genai
    from google.genai import errors, types

    if _gemini is None:
        if not config.GEMINI_API_KEY:
            raise LLMError("GEMINI_API_KEY is not set in .env")
        # v1beta: the API version where gemini-flash-latest and JSON-schema output both exist
        _gemini = genai.Client(api_key=config.GEMINI_API_KEY,
                               http_options=types.HttpOptions(api_version="v1beta"))

    cfg = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema,
        max_output_tokens=max_tokens,
        thinking_config=types.ThinkingConfig(thinking_budget=GEMINI_THINKING.get(effort, 2048)),
    )
    for attempt in range(5):
        try:
            resp = _gemini.models.generate_content(model=config.GEMINI_MODEL, contents=user, config=cfg)
            break
        except errors.ClientError as e:
            if e.code == 429 and attempt < 4:          # free-tier per-minute limit
                time.sleep(15 * (attempt + 1))
                continue
            raise LLMError(f"Gemini {e.code}: {str(e)[:200]}")
        except errors.ServerError as e:
            if attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise LLMError(f"Gemini server error: {str(e)[:200]}")

    fb = getattr(resp, "prompt_feedback", None)
    if fb is not None and getattr(fb, "block_reason", None):
        raise LLMRefused(f"Gemini blocked the prompt ({fb.block_reason})")
    cand = resp.candidates[0] if resp.candidates else None
    reason = str(getattr(cand, "finish_reason", "")) if cand else "NO_CANDIDATE"
    if "SAFETY" in reason or "PROHIBITED" in reason or "BLOCKLIST" in reason:
        raise LLMRefused(f"Gemini declined ({reason})")
    if "MAX_TOKENS" in reason:
        raise LLMRefused("hit the output limit before finishing the JSON")
    if not resp.text:
        raise LLMError(f"Gemini returned no text (finish reason {reason})")
    return json.loads(resp.text)


# ---------------- Claude ------------------------------------------------------
_claude = None


def _call_claude(system: str, user: str, schema: Dict, effort: str, max_tokens: int) -> Dict:
    global _claude
    import anthropic

    if _claude is None:
        _claude = anthropic.Anthropic()
    try:
        resp = _claude.beta.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.APIError as e:
        raise LLMError(f"Claude API error: {type(e).__name__}: {e}")
    if resp.stop_reason == "refusal":
        cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
        raise LLMRefused(f"model declined (category={cat})")
    if resp.stop_reason == "max_tokens":
        raise LLMRefused("hit max_tokens before finishing the JSON")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def call_json(system: str, user: str, schema: Dict, effort: str = "medium", max_tokens: int = 16000) -> Dict:
    if config.LLM_PROVIDER == "claude":
        return _call_claude(system, user, schema, effort, max_tokens)
    return _call_gemini(system, user, schema, effort, max_tokens)


# ---------------- schema helpers ----------------------------------------------
def obj(props: Dict, required=None) -> Dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()) if required is None else required,
        "additionalProperties": False,
    }


S = {"type": "string"}
B = {"type": "boolean"}
I = {"type": "integer"}
NS = {"type": ["string", "null"]}
NB = {"type": ["boolean", "null"]}
NI = {"type": ["integer", "null"]}
LS = {"type": "array", "items": {"type": "string"}}
