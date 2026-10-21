"""The nightly job: every SACCO's collections queue rebuilt from the latest arrears, before staff start work.

Vercel calls GET /cron/daily on the schedule in vercel.json, with "Authorization: Bearer <CRON_SECRET>". On the Docker
kit, a crontab line does the same with curl. Without a secret set, the job refuses to run: it must never be open.
"""
from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .auth import Principal
from .db import get_session
from .engine.collections import build_queue
from .models import Institution

router = APIRouter(tags=["cron"])


def _secret() -> str:
    return os.getenv("SAWAZI_CRON_SECRET") or os.getenv("CRON_SECRET") or ""


@router.get("/cron/daily", include_in_schema=False)
def daily(authorization: str | None = Header(default=None), s: Session = Depends(get_session)):
    secret = _secret()
    if not secret:
        raise HTTPException(503, "the nightly job is not set up (no CRON_SECRET)")
    if not hmac.compare_digest((authorization or "").encode(), f"Bearer {secret}".encode()):
        raise HTTPException(401, "not allowed")
    done = {}
    for inst in s.scalars(select(Institution).order_by(Institution.id)):
        result = build_queue(s, inst.id)  # commits
        audit.record(s, Principal("system", None, inst.id, "system", "Nightly job"), "collections.queue",
                     note="nightly rebuild", after={"queued": result["queued"]})
        s.commit()
        done[inst.id] = result["queued"]
    return {"queued": done}
