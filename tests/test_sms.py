import json

import httpx
import pytest
from sqlalchemy import select

from sawazi import sms
from sawazi.api import app
from sawazi.models import AuditEvent, Loan, Member, Reminder, SmsMessage, SmsOptOut, SmsSettings
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)

TOKEN = "cb-secret-token"


# ---------------------------------------------------------------- Taifa protocol

@pytest.mark.parametrize("text,error", [
    ('[{"status_code":"1000","status_desc":"Success","message_id":42,"credit_balance":"6.33"}]', None),
    ('{"status_code":1000,"status_desc":"Success"}', None),  # bare object, numeric code
    ('[{"status_code":"1004","status_desc":"Low credits"}]', "Taifa 1004: Low credits"),
    ('{"status_code":"1001"}', 'Taifa 1001: {"status_code":"1001"}'),
    ("<html>404 Not Found</html>", "Bad response from Taifa: <html>404 Not Found</html>"),
])
def test_taifa_rejection(text, error):
    assert sms.taifa_rejection(text) == error


def taifa(handler):
    return sms.TaifaProvider("the-api-key-1234567", transport=httpx.MockTransport(handler))


def test_taifa_request_shape_and_success():
    seen = {}

    def handler(request):
        seen["url"], seen["headers"] = str(request.url), request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[{"status_code": "1000", "status_desc": "Success", "message_id": 9876,
                                          "credit_balance": "6.33"}])

    res = taifa(handler).send("254712345678", "Dear Achieng, pay via Paybill 522900.", "UFANISI")
    assert seen["url"] == "https://api.taifamobile.co.ke/v2/sms/sendsms"
    assert seen["headers"]["h_api_key"] == "the-api-key-1234567" and "api-key" not in seen["headers"]
    assert seen["headers"]["content-type"] == "application/json"
    assert seen["body"] == {"mobile": "254712345678", "sender_name": "UFANISI",
                            "message": "Dear Achieng, pay via Paybill 522900.", "response_type": "json"}
    assert (res.status, res.provider_message_id, res.provider_status) == ("sent", "9876", "1000")


def test_taifa_sender_falls_back_to_sidnet():
    seen = []
    ok = lambda r: seen.append(json.loads(r.content)) or httpx.Response(200, json=[{"status_code": "1000"}])  # noqa: E731
    taifa(ok).send("254712345678", "m", None)
    sms.TaifaProvider("k", transport=httpx.MockTransport(ok), sender="OTHER").send("254712345678", "m", "  ")
    assert [b["sender_name"] for b in seen] == ["SIDNET", "OTHER"]


@pytest.mark.parametrize("code,desc", [("1004", "Low credits"), ("1001", "Invalid short code"),
                                       ("1006", "Invalid API key")])
def test_taifa_error_codes(code, desc):
    res = taifa(lambda r: httpx.Response(200, json=[{"status_code": code, "status_desc": desc}])).send(
        "254712345678", "m", None)
    assert res.status == "failed" and res.description == f"Taifa {code}: {desc}" and res.provider_status == code


def test_taifa_timeout_is_unknown_not_failed():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    res = taifa(handler).send("254712345678", "m", None)
    assert res.status == "unknown" and "before resending" in res.description


def test_taifa_unreachable_and_garbage():
    def refused(request):
        raise httpx.ConnectError("no route", request=request)
    assert taifa(refused).send("254712345678", "m", None).status == "failed"
    garbage = taifa(lambda r: httpx.Response(404, text="<html>Not Found")).send("254712345678", "m", None)
    assert garbage.status == "failed" and garbage.description == "Bad response from Taifa: <html>Not Found"


def test_stop_words():
    for t in ["STOP", "stop", " Stop please", "ACHA", "Sitisha.", "unsubscribe"]:
        assert sms.is_stop(t), t
    for t in ["", None, "I will pay on Friday", "nonstop"]:
        assert not sms.is_stop(t), t


def test_provider_from_env(monkeypatch):
    monkeypatch.delenv("SAWAZI_SMS_PROVIDER", raising=False)
    assert sms.provider_from_env().name == "simulate"
    monkeypatch.setenv("SAWAZI_SMS_PROVIDER", "taifa")
    monkeypatch.delenv("SAWAZI_TAIFA_API_KEY", raising=False)
    monkeypatch.delenv("TAIFA_API_KEY", raising=False)
    monkeypatch.delenv("TAIFA_SENDER_ID", raising=False)
    with pytest.raises(RuntimeError, match="TAIFA_API_KEY"):
        sms.provider_from_env()
    monkeypatch.setenv("SAWAZI_TAIFA_API_KEY", "k")
    assert sms.provider_from_env().name == "taifa"
    monkeypatch.setenv("TAIFA_API_KEY", ' "your-taifa-api-key" ')
    p = sms.provider_from_env()
    assert (p.api_key, p.sender, p.url) == ("your-taifa-api-key", "SIDNET", sms.TAIFA_URL)


# ---------------------------------------------------------------- approving and sending

class FakeProvider:
    def __init__(self, name="simulate", result=None, error=None):
        self.name, self.result, self.error, self.sent = name, result, error, []

    def send(self, phone, text, service_name):
        self.sent.append((phone, text, service_name))
        if self.error:
            raise self.error
        return self.result or sms.SendResult("simulated", f"id-{len(self.sent)}", None, "Simulated")


@pytest.fixture()
def world(env, monkeypatch):
    """SACCO A: 4 loans in arrears with queued reminders. SACCO B: one."""
    c, Session = env
    monkeypatch.setenv("SAWAZI_SMS_CALLBACK_TOKEN", TOKEN)
    with Session() as s:
        rows = [  # member id, institution, phone, channel
            (11, 1, "254711000011", "sms"),
            (12, 1, "254711000012", "call"),
            (13, 1, None, "sms"),
            (14, 1, "254711000014", "recovery"),
            (21, 2, "254711000021", "sms"),
        ]
        for mid, iid, phone, channel in rows:
            s.add(Member(id=mid, institution_id=iid, member_no=f"M{mid}", name=f"Member {mid}", phone=phone))
            s.add(Loan(id=mid, institution_id=iid, member_id=mid, loan_no=f"LN{mid}", principal_cents=1_000_000,
                       balance_cents=500_000, installment_cents=50_000, arrears_cents=50_000, days_in_arrears=5))
            s.flush()  # parents before children: PostgreSQL checks foreign keys
            s.add(Reminder(id=mid, institution_id=iid, loan_id=mid, channel=channel, priority_score=50,
                           message=f"Dear Member{mid}, KES 500 on loan LN{mid} is due. Pay via Paybill 522900."))
        s.commit()
    fake = FakeProvider()
    app.dependency_overrides[sms.get_provider] = lambda: fake
    yield c, Session, fake
    app.dependency_overrides.pop(sms.get_provider, None)


def send(c, h, *ids, iid=1):
    r = c.post(f"/institutions/{iid}/reminders/send", headers=h, json={"reminder_ids": list(ids)})
    assert r.status_code == 200, r.text
    return {x["reminder_id"]: x for x in r.json()["results"]}


def test_send_records_approval_and_result(world):
    c, Session, fake = world
    h = login(c, "credit_officer@a.test")
    me = c.get("/auth/me", headers=h).json()
    res = send(c, h, 11)
    assert res[11]["result"] == "simulated" and res[11]["phone"] == "254711000011"
    assert fake.sent == [("254711000011", "Dear Member11, KES 500 on loan LN11 is due. Pay via Paybill 522900.", None)]
    with Session() as s:
        m = s.scalar(select(SmsMessage))
        assert (m.status, m.approved_by_user_id, m.loan_id, m.provider) == ("simulated", me["id"], 11, "simulate")
        assert s.get(Reminder, 11).status == "sent"
        ev = s.scalar(select(AuditEvent).where(AuditEvent.action == "sms.approve"))
        assert ev.actor_id == me["id"] and ev.entity_id == m.id and ev.after["member_no"] == "M11"
    listed = c.get("/institutions/1/sms", headers=login(c, "viewer@a.test")).json()
    assert listed[0]["status"] == "simulated"


def test_never_sends_twice(world):
    c, Session, fake = world
    h = login(c, "accountant@a.test")
    res = send(c, h, 11, 11, 11)  # double click in one request
    assert list(res) == [11] and len(fake.sent) == 1
    assert send(c, h, 11)[11]["reason"] == "already sent"
    # the queue is rebuilt and the same loan comes back as a new reminder
    with Session() as s:
        s.add(Reminder(id=99, institution_id=1, loan_id=11, channel="sms", priority_score=50, message="again"))
        s.commit()
    assert "already went out" in send(c, h, 99)[99]["reason"]
    assert len(fake.sent) == 1


def test_checks_before_sending(world):
    c, Session, fake = world
    h = login(c, "accountant@a.test")
    with Session() as s:
        s.add(SmsOptOut(institution_id=1, phone="254711000012", source="staff", created_at=sms_now()))
        s.commit()
    res = send(c, h, 12, 13, 14, 21, 404)
    assert res[12]["reason"] == "member has opted out of SMS"
    assert res[13]["reason"] == "member has no valid phone number"
    assert "note for staff" in res[14]["reason"]
    assert res[21]["reason"] == "reminder not found"  # SACCO B's reminder
    assert res[404]["reason"] == "reminder not found"
    assert fake.sent == []


def sms_now():
    from sawazi.auth import utcnow
    return utcnow()


def test_who_may_send(world):
    c, _, fake = world
    assert c.post("/institutions/1/reminders/send", headers=login(c, "viewer@a.test"),
                  json={"reminder_ids": [11]}).status_code == 403
    key = c.post("/institutions/1/api-keys", headers=login(c, "admin@a.test"),
                 json={"name": "sync", "role": "accountant"}).json()["key"]
    r = c.post("/institutions/1/reminders/send", headers={"Authorization": f"Bearer {key}"}, json={"reminder_ids": [11]})
    assert r.status_code == 403  # a machine never messages members
    assert c.post("/institutions/2/reminders/send", headers=login(c, "admin@a.test"),
                  json={"reminder_ids": [21]}).status_code == 404
    assert fake.sent == []


def test_real_provider_needs_institution_switched_on(world):
    c, _, _ = world
    real = FakeProvider(name="taifa", result=sms.SendResult("sent", "tm-1", "1000", "Success"))
    app.dependency_overrides[sms.get_provider] = lambda: real
    admin = login(c, "admin@a.test")
    r = c.post("/institutions/1/reminders/send", headers=admin, json={"reminder_ids": [11]})
    assert r.status_code == 409 and real.sent == []
    c.put("/institutions/1/sms/settings", headers=admin,
          json={"enabled": True, "service_name": "UFANISI", "opt_out_text": "To stop SMS call 0700000000."})
    assert send(c, admin, 11)[11]["result"] == "sent"
    assert real.sent[0][1].endswith("Paybill 522900. To stop SMS call 0700000000.") and real.sent[0][2] == "UFANISI"


def test_failed_send_can_be_retried(world):
    c, Session, _ = world
    broke = FakeProvider(result=sms.SendResult("failed", None, "1004", "Taifa 1004: Low credits"))
    app.dependency_overrides[sms.get_provider] = lambda: broke
    h = login(c, "accountant@a.test")
    res = send(c, h, 11)[11]
    assert res.pop("sms_id")
    assert res == {"reminder_id": 11, "result": "failed", "phone": "254711000011",
                   "detail": "Taifa 1004: Low credits"}
    with Session() as s:
        assert s.get(Reminder, 11).status == "failed"
    ok = FakeProvider()
    app.dependency_overrides[sms.get_provider] = lambda: ok
    assert send(c, h, 11)[11]["result"] == "simulated"  # topped up, sent again
    assert len(ok.sent) == 1


def test_crash_while_sending_is_unknown_and_not_retried(world):
    c, Session, _ = world
    app.dependency_overrides[sms.get_provider] = lambda: FakeProvider(error=RuntimeError("boom"))
    h = login(c, "accountant@a.test")
    assert send(c, h, 11, 12)[11]["result"] == "unknown"
    with Session() as s:
        assert s.get(Reminder, 11).status == "sent"  # not offered again automatically
        assert s.get(Reminder, 12).status == "sent"  # the rest of the batch still ran


# ---------------------------------------------------------------- opt-outs and settings

def test_staff_opt_out_and_opt_back_in(world):
    c, Session, _ = world
    officer, admin = login(c, "credit_officer@a.test"), login(c, "admin@a.test")
    r = c.post("/institutions/1/sms/opt-outs", headers=officer, json={"phone": "0711 000 011", "note": "called in"})
    assert r.json() == {"phone": "254711000011", "opted_out": True, "already": False}
    assert c.post("/institutions/1/sms/opt-outs", headers=officer, json={"phone": "254711000011"}).json()["already"]
    assert c.post("/institutions/1/sms/opt-outs", headers=officer, json={"phone": "12"}).status_code == 422
    assert send(c, officer, 11)[11]["reason"] == "member has opted out of SMS"

    assert c.delete("/institutions/1/sms/opt-outs/254711000011", headers=officer,
                    params={"note": "asked again"}).status_code == 403  # admin only
    assert c.delete("/institutions/1/sms/opt-outs/254711000011", headers=admin).status_code == 422  # reason required
    r = c.delete("/institutions/1/sms/opt-outs/0711000011", headers=admin, params={"note": "member asked in branch"})
    assert r.status_code == 200
    with Session() as s:
        actions = [e.action for e in s.scalars(select(AuditEvent).where(AuditEvent.action.like("sms.opt%")))]
    assert actions == ["sms.opt_out", "sms.opt_in"]


def test_settings_admin_only_and_audited(world):
    c, Session, _ = world
    assert c.get("/institutions/1/sms/settings", headers=login(c, "accountant@a.test")).status_code == 403
    admin = login(c, "admin@a.test")
    assert c.get("/institutions/1/sms/settings", headers=admin).json()["enabled"] is False
    c.put("/institutions/1/sms/settings", headers=admin, json={"enabled": True, "service_name": "UFANISI"})
    c.put("/institutions/1/sms/settings", headers=admin, json={"enabled": False, "service_name": "UFANISI"})
    with Session() as s:
        evs = list(s.scalars(select(AuditEvent).where(AuditEvent.action == "sms.settings").order_by(AuditEvent.id)))
    assert evs[0].before is None and evs[1].before["enabled"] is True and evs[1].after["enabled"] is False


# ---------------------------------------------------------------- callbacks

def cb(c, kind, body, token=TOKEN):
    return c.post(f"/callbacks/taifa/{token}/{kind}", json=body)


def sent_via_taifa(c, Session, reminder_id=11):
    real = FakeProvider(name="taifa", result=sms.SendResult("sent", "tm-abc", "1000", "Success"))
    app.dependency_overrides[sms.get_provider] = lambda: real
    admin = login(c, "admin@a.test")
    c.put("/institutions/1/sms/settings", headers=admin, json={"enabled": True, "service_name": "UFANISI"})
    send(c, admin, reminder_id)


def test_callbacks_need_the_secret(world, monkeypatch):
    c, _, _ = world
    assert cb(c, "delivery", {}, token="wrong").status_code == 404
    monkeypatch.delenv("SAWAZI_SMS_CALLBACK_TOKEN")
    assert cb(c, "delivery", {}).status_code == 404  # closed when not configured


def test_delivery_reports(world):
    c, Session, _ = world
    sent_via_taifa(c, Session)
    assert cb(c, "delivery", {"messageId": "nope", "status": "DeliveredToTerminal"}).json()["matched"] is False
    r = cb(c, "delivery", {"timestamp": "2026-10-06 10:00:00", "phoneNumber": "254711000011",
                           "messageId": "tm-abc", "status": "DeliveredToTerminal"})
    assert r.json()["matched"]
    with Session() as s:
        m = s.scalar(select(SmsMessage))
        assert (m.status, m.delivery_status) == ("delivered", "DeliveredToTerminal") and m.delivered_at


def test_undelivered_and_blocked_sender(world):
    c, Session, _ = world
    sent_via_taifa(c, Session)
    cb(c, "delivery", {"messageId": "tm-abc", "status": "sender_ID blacklisted by user"})
    with Session() as s:
        assert s.scalar(select(SmsMessage)).status == "undelivered"
        o = s.scalar(select(SmsOptOut))
        assert (o.institution_id, o.phone, o.source) == (1, "254711000011", "sender_blocked")
        ev = s.scalar(select(AuditEvent).where(AuditEvent.action == "sms.opt_out"))
        assert ev.actor_kind == "provider" and ev.institution_id == 1


def test_member_replies_stop(world):
    c, Session, _ = world
    with Session() as s:
        s.add(SmsSettings(institution_id=2, enabled=True, service_name="SACCOB"))
        s.commit()
    assert cb(c, "incoming", {"message": "I will pay Friday", "phone_number": "254711000021",
                              "service": {"service_name": "SACCOB"}}).json()["opted_out"] == 0
    r = cb(c, "incoming", {"message": "Acha", "phone_number": "254711000021", "link_id": "1",
                           "service": {"service_name": "SACCOB", "keyword": "ACHA"}})
    assert r.json()["opted_out"] == 1
    with Session() as s:
        assert [(o.institution_id, o.source) for o in s.scalars(select(SmsOptOut))] == [(2, "member_sms")]


def test_stop_for_unknown_service_applies_wherever_we_messaged_them(world):
    c, Session, _ = world
    sent_via_taifa(c, Session)  # SACCO A messaged 254711000011
    r = cb(c, "incoming", {"message": "STOP", "phone_number": "254711000011", "service": {"service_name": "OTHER"}})
    assert r.json()["opted_out"] == 1
    assert cb(c, "incoming", {"message": "STOP", "phone_number": "254711000011"}).json()["opted_out"] == 0  # idempotent


def test_unsubscribe_callback(world):
    c, Session, _ = world
    sent_via_taifa(c, Session)
    body = {"date": "2026-10-06 10:00:00", "phone_number": "254711000011",
            "service": {"service_name": "UFANISI", "keyword": "X"}}
    assert cb(c, "subscription", {**body, "update_description": "ACTIVATION"}).json()["opted_out"] == 0
    assert cb(c, "subscription", {**body, "update_description": "DEACTIVATION"}).json()["opted_out"] == 1
