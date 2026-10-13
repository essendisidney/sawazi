"""The guarantee network across SACCOs on Sawazi: who guarantees here and at other SACCOs too.

A member who has pledged deposits at several SACCOs is stretched further than any one SACCO can see. SACCOs that
join the network see, for their own members, how many other network SACCOs that member guarantees at and the
total pledged there. Never which SACCOs, never another SACCO's figures for a member, never arrears (sharing
repayment behaviour is credit reference business). Members are linked only by national ID number. Joining is a
deliberate admin act that records the SACCO's confirmation that its data processing terms allow the sharing.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, guarantors
from .auth import Principal, require
from .db import get_session
from .models import CoreGuarantee, Guarantee, Institution, Member

router = APIRouter(tags=["network"])


def id_key(id_number: str | None) -> str | None:
    """National ID as digits without leading zeros; None when there is nothing safe to link on."""
    digits = re.sub(r"\D", "", id_number or "").lstrip("0")
    return digits if len(digits) >= 6 else None


def _peers(s: Session, inst: Institution) -> list[int]:
    """Other network SACCOs. Demo institutions only ever see other demos, real ones only real ones."""
    return list(s.scalars(select(Institution.id).where(Institution.network_opt_in.is_(True), Institution.id != inst.id,
                                                       Institution.is_demo.is_(inst.is_demo))))


def elsewhere(s: Session, inst: Institution, members: list[Member]) -> dict[int, dict]:
    """member id -> {"saccos": n, "pledged_cents": total} for members who guarantee at other network SACCOs."""
    if not inst.network_opt_in:
        return {}
    peers = _peers(s, inst)
    keys = {}
    for m in members:
        k = id_key(m.id_number)
        if k:
            keys.setdefault(k, []).append(m.id)
    if not peers or not keys:
        return {}
    out: dict[int, dict] = {}
    others = s.scalars(select(Member).where(Member.institution_id.in_(peers), Member.id_number.is_not(None)))
    for o in others:
        k = id_key(o.id_number)
        if k not in keys:
            continue
        pledged = guarantors.pledged_cents(s, o.institution_id, o.id)
        if not pledged:
            continue
        for mid in keys[k]:
            row = out.setdefault(mid, {"saccos": set(), "pledged_cents": 0})
            row["saccos"].add(o.institution_id)
            row["pledged_cents"] += pledged
    return {mid: {"saccos": len(r["saccos"]), "pledged_cents": r["pledged_cents"]} for mid, r in out.items()}


@router.get("/institutions/{institution_id}/network/guarantees", dependencies=[Depends(require("read"))])
def network_guarantees(institution_id: int, s: Session = Depends(get_session)):
    """This SACCO's guarantors who also guarantee at other network SACCOs, largest first."""
    inst = s.get(Institution, institution_id)
    if inst is None:
        raise HTTPException(404, "institution not found")
    if not inst.network_opt_in:
        return {"opted_in": False, "saccos": 0, "members": []}
    ids = set(s.scalars(select(Guarantee.guarantor_member_id).where(Guarantee.institution_id == institution_id,
                                                                    Guarantee.status == "accepted")))
    ids |= set(s.scalars(select(CoreGuarantee.guarantor_member_id).where(CoreGuarantee.institution_id == institution_id,
                                                                         CoreGuarantee.status == "active")))
    members = list(s.scalars(select(Member).where(Member.institution_id == institution_id, Member.id.in_(ids)))) if ids else []
    found = elsewhere(s, inst, members)
    rows = []
    for m in members:
        if m.id in found:
            here = guarantors.pledged_cents(s, institution_id, m.id)
            rows.append({"member_no": m.member_no, "name": m.name, "pledged_here_kes": here / 100,
                         "deposits_here_kes": None if m.deposits_cents is None else m.deposits_cents / 100,
                         "saccos_elsewhere": found[m.id]["saccos"],
                         "pledged_elsewhere_kes": found[m.id]["pledged_cents"] / 100,
                         "total_pledged_kes": (here + found[m.id]["pledged_cents"]) / 100})
    rows.sort(key=lambda r: -r["pledged_elsewhere_kes"])
    return {"opted_in": True, "saccos": len(_peers(s, inst)) + 1, "members": rows[:50]}


class NetworkIn(BaseModel):
    opt_in: bool
    confirm: str | None = Field(default=None, max_length=200,
                                description="to join: the reference of the data processing terms that allow sharing")


@router.put("/institutions/{institution_id}/network")
def set_network(institution_id: int, body: NetworkIn, s: Session = Depends(get_session),
                who: Principal = Depends(require("manage_users"))):
    inst = s.get(Institution, institution_id)
    if inst is None:
        raise HTTPException(404, "institution not found")
    if body.opt_in and not (body.confirm or "").strip():
        raise HTTPException(422, "to join, give the reference of the member consent or data processing terms that allow sharing")
    before = inst.network_opt_in
    inst.network_opt_in = body.opt_in
    audit.record(s, who, "network.join" if body.opt_in else "network.leave", "institution", inst.id,
                 before={"network_opt_in": before}, after={"network_opt_in": body.opt_in, "confirm": body.confirm})
    s.commit()
    return {"opted_in": inst.network_opt_in}
