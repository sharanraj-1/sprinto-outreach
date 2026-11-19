"""Web app for Vercel: the same 7-step pipeline, one company per request.

  GET  /            the page
  POST /api/run     {domain}                      -> steps 1-6, returns decisions + draft (nothing sent)
  POST /api/send    {email, poc, company, reason} -> step 7, after a human has reviewed/edited the draft

If APP_PASSWORD is set, every API call needs a matching X-App-Password header. It is
currently unset, so the app is open to anyone with the link.
"""
import hmac
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import config  # noqa: E402
from run import run_one  # noqa: E402
from send import SendAuthError, send  # noqa: E402

app = FastAPI(title="Sprinto outreach")


def _check(pw: str) -> None:
    if not config.APP_PASSWORD:   # no password set = open app (owner's choice)
        return
    if not hmac.compare_digest((pw or "").encode(), config.APP_PASSWORD.encode()):
        raise HTTPException(401, "Wrong password")


class RunReq(BaseModel):
    domain: str


class SendReq(BaseModel):
    email: dict
    poc: dict
    company: str
    reason: str


@app.get("/", response_class=HTMLResponse)
def page():
    return (ROOT / "web" / "index.html").read_text(encoding="utf-8")


@app.post("/api/run")
def api_run(req: RunReq, x_app_password: str = Header("")):
    _check(x_app_password)
    domain = req.domain.strip().lower().replace("https://", "").replace("http://", "").split("/")[0]
    if not domain or "." not in domain:
        raise HTTPException(400, "Give a domain like acme.com")
    rec = run_one(domain, do_send=False, state={})
    rec.pop("trace", None)
    return rec


@app.post("/api/send")
def api_send(req: SendReq, x_app_password: str = Header("")):
    _check(x_app_password)
    for k in ("subject", "body"):
        if not req.email.get(k):
            raise HTTPException(400, f"email.{k} is empty")
    try:
        status = send(req.email, req.poc, req.company, req.reason)
    except SendAuthError as e:
        raise HTTPException(502, str(e))
    return {"send_status": status, "to": config.SEND_TO}
