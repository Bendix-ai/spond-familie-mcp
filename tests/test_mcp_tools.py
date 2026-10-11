"""Spond-MCP: verktøyene mot en FakeSession (ingen nettverk, syntetiske data)."""

from __future__ import annotations

import asyncio
import io
import json

import pytest

pytest.importorskip("openpyxl")

from _mcp_fixtures import (  # noqa: E402
    CHILD_C, CHILD_P, COACH_G, LEAKY_ADDRESS, LEAKY_DOB, LEAKY_EMAIL, LEAKY_PHONE,
    OTHER_PARENT, PARTNER, PLAYER_C, SECRET_PW, FakeSession, runtime,
)

from spond_familie import tools  # noqa: E402
from spond_familie.safety import SafetyError  # noqa: E402
from spond_familie.serialize import LookupFailed, dumps, prune  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOND_MCP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SPOND_MCP_EXPORT_DIR", str(tmp_path / "exports"))


def run(rt, fn, **kw):
    return asyncio.run(rt.run(fn, **kw))


# --------------------------------------------------------------------------- #
# Lesing
# --------------------------------------------------------------------------- #
def test_overview_lists_children_todo_but_not_pending_or_cancelled(tmp_path):
    rt, _ = runtime(tmp_path)
    out = run(rt, tools.overview, days=7)
    todo_ids = {t["id"] for t in out["children_todo"]}
    assert "EVFUTURE" in todo_ids and "EVPARENT" in todo_ids
    assert "EVPENDING" not in todo_ids and "EVCANCEL" not in todo_ids
    assert any(c["id"] == "EVCANCEL" for c in out["cancelled"])
    assert all(e["id"] != "EVCANCEL" for e in out["events"])


def test_list_groups_detects_coach_and_children(tmp_path):
    rt, _ = runtime(tmp_path)
    out = run(rt, tools.list_groups)
    by = {g["id"]: g for g in out["groups"]}
    assert by[COACH_G]["coach"] is True and by[COACH_G]["my_roles"] == ["Trener"]
    assert by[COACH_G]["short"] == "Testlag J15"  # uten konfig: gruppenavnet
    assert by[COACH_G]["children"] == ["Kari Test"]
    parent = next(g for g in out["groups"] if g["id"] != COACH_G)
    assert parent["coach"] is False and parent["children"] == ["Kari Test"]


def test_event_times_are_oslo_with_weekday(tmp_path):
    rt, _ = runtime(tmp_path)
    out = run(rt, tools.list_events, group="J15", days_ahead=7)
    ev = next(e for e in out["events"] if e["id"] == "EVFUTURE")
    assert ev["start"] == "2026-06-03 (ons) 10:00"  # 08:00Z + 2 d → 10:00 Oslo
    assert ev["kids"] == [{"name": "Kari", "status": "ikke svart"}]


def test_get_event_hides_names_in_parent_group_with_hidden_participants(tmp_path):
    rt, _ = runtime(tmp_path)
    parent = run(rt, tools.get_event, event_id="EVPARENT")
    assert parent.get("responses") is None and parent.get("counts") is None
    coach = run(rt, tools.get_event, event_id="EVFUTURE")
    assert coach["responses"]["ikke svart"] == ["Kari Test", "Per Annen"]
    assert coach["can_rsvp_for"] == ["Kari"]
    assert prune(coach["comments"][1]) == {"who": "deg", "when": "2026-06-01 (man) 10:00", "text": "Jeg", "reply": True}


def test_find_person_never_returns_contact_data(tmp_path):
    rt, _ = runtime(tmp_path)
    out = run(rt, tools.find_person, query="annen")
    names = {p["name"] for p in out["people"]}
    assert {"Per Annen", "Lise Annen", "Siri Annen"} <= names


def test_attendance_stats_coach_vs_parent_scope(tmp_path):
    rt, _ = runtime(tmp_path)
    coach = run(rt, tools.attendance_stats, group="J15", days_back=30)
    assert coach["events_counted"] == 2  # EVMATCH + EVPAST; EVHIDDEN holdes utenfor
    assert coach["excluded"]["hidden"] == 1
    assert coach["registered_coverage"] == "1/2"
    per = {p["name"]: p for p in coach["players"]}
    assert per["Per Annen"]["yes"] == 1 and per["Per Annen"]["yes_pct"] == 50
    assert per["Kari Test"]["attended"] == "0/1"
    parent = run(rt, tools.attendance_stats, group="Turngruppe", days_back=30)
    assert [p["name"] for p in parent["players"]] == ["Kari Test"]
    assert parent["scope"].startswith("bare dine barn")
    matches = run(rt, tools.attendance_stats, group="J15", days_back=30, kind="match")
    assert matches["events_counted"] == 1


def test_unknown_group_lists_alternatives(tmp_path):
    rt, _ = runtime(tmp_path)
    with pytest.raises(LookupFailed, match="J15"):
        run(rt, tools.list_events, group="finnes ikke")


LEAKS = (LEAKY_EMAIL, LEAKY_PHONE, LEAKY_DOB, LEAKY_ADDRESS, SECRET_PW, "hmac-secret-mcp-do-not-leak")
LEAKY_KEYS = ("email", "phoneNumber", "dateOfBirth", "address", "contactPerson", "primaryEmail")


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


@pytest.mark.parametrize("fn,kw", [
    (tools.overview, {}),
    (tools.list_groups, {}),
    (tools.list_events, {"days_back": 30, "days_ahead": 30}),
    (tools.get_event, {"event_id": "EVFUTURE", "include_chat": True}),
    (tools.get_event, {"event_id": "EVMATCH"}),
    (tools.list_posts, {"include_comments": True}),
    (tools.list_chats, {}),
    (tools.get_chat, {"chat_id": "CHATCOACH"}),
    (tools.find_person, {"query": "test"}),
    (tools.attendance_stats, {"group": "J15"}),
    (tools.status, {}),
])
def test_no_contact_data_or_secrets_leak(tmp_path, fn, kw):
    rt, _ = runtime(tmp_path)
    out = run(rt, fn, **kw)
    text = dumps(out)
    for needle in LEAKS:
        assert needle not in text, f"{fn.__name__} lekker {needle!r}"
    assert not set(_walk_keys(json.loads(text))) & set(LEAKY_KEYS)


# --------------------------------------------------------------------------- #
# RSVP
# --------------------------------------------------------------------------- #
def test_rsvp_preview_writes_nothing_and_confirm_writes_once(tmp_path):
    rt, sess = runtime(tmp_path)
    prev = run(rt, tools.rsvp, event_id="EVFUTURE", child="kari", answer="accept")
    assert prev["preview"] is True and sess.writes == []
    assert prev["current_state"] == "ikke svart"
    assert any("autoAccept" in w for w in prev.get("warnings", [])) is False

    with pytest.raises(SafetyError, match="preview_id"):
        run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="ja", confirm=True)
    assert sess.writes == []

    done = run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="ja", confirm=True,
               preview_id=prev["preview_id"])
    assert sess.writes == [("put_response", "EVFUTURE", CHILD_C, True)]
    assert done["result"] == "ok" and done["status_now"] == "ja"

    # Engangs: samme preview_id kan ikke brukes igjen (selv for annet svar)
    with pytest.raises(SafetyError, match="brukt"):
        run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="nei", confirm=True,
            preview_id=prev["preview_id"])
    assert len(sess.writes) == 1


def test_rsvp_confirm_rejects_changed_params(tmp_path):
    rt, sess = runtime(tmp_path)
    prev = run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="accept")
    with pytest.raises(SafetyError, match="endret"):
        run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="decline", confirm=True,
            preview_id=prev["preview_id"])
    assert sess.writes == []


def test_rsvp_preview_expires(tmp_path):
    clock = [1000.0]
    from spond_familie.runtime import Runtime
    from _mcp_fixtures import NOW, config

    sess = FakeSession()
    rt = Runtime(config=config(), session_factory=lambda c: sess,
                 now=lambda: NOW, clock=lambda: clock[0])
    prev = run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="accept")
    clock[0] += 601
    with pytest.raises(SafetyError, match="utløpt"):
        run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="accept", confirm=True,
            preview_id=prev["preview_id"])
    assert sess.writes == []


@pytest.mark.parametrize("event_id,child,match", [
    ("EVPENDING", "Kari", "ikke åpnet"),
    ("EVCANCEL", "Kari", "avlyst"),
    ("EVMATCH", "Kari", "startet"),
    ("EVFUTURE", "Per", "Fant ikke barnet"),
])
def test_rsvp_refusals(tmp_path, event_id, child, match):
    rt, sess = runtime(tmp_path)
    with pytest.raises(SafetyError, match=match):
        run(rt, tools.rsvp, event_id=event_id, child=child, answer="accept")
    assert sess.writes == []


def test_rsvp_uses_member_uid_of_the_events_group(tmp_path):
    rt, sess = runtime(tmp_path)
    prev = run(rt, tools.rsvp, event_id="EVPARENT", child="Kari", answer="nei")
    run(rt, tools.rsvp, event_id="EVPARENT", child="Kari", answer="nei", confirm=True,
        preview_id=prev["preview_id"])
    assert sess.writes == [("put_response", "EVPARENT", CHILD_P, False)]


def test_rsvp_no_change_when_already_answered(tmp_path):
    rt, sess = runtime(tmp_path)
    out = run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="accept")
    run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="accept", confirm=True,
        preview_id=out["preview_id"])
    again = run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="accept")
    assert again["result"] == "no_change"


def test_audit_log_records_preview_and_execution(tmp_path):
    rt, _ = runtime(tmp_path)
    prev = run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="accept")
    run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="accept", confirm=True,
        preview_id=prev["preview_id"])
    log = (tmp_path / "home" / "audit.jsonl")
    phases = [json.loads(line)["phase"] for line in log.read_text().splitlines()]
    assert phases == ["preview", "executed"]
    assert oct(log.stat().st_mode & 0o777) == "0o600"
    assert oct(log.parent.stat().st_mode & 0o777) == "0o700"


# --------------------------------------------------------------------------- #
# Meldinger
# --------------------------------------------------------------------------- #
def test_send_message_to_guardians_excludes_me(tmp_path):
    rt, sess = runtime(tmp_path)
    prev = run(rt, tools.send_message, group="J15", to="Kari", text="Husk leggskinn")
    assert prev["will_send_to"] == ["Eva Test (forelder til Kari)"]
    assert sess.writes == []
    done = run(rt, tools.send_message, group="J15", to="Kari", text="Husk leggskinn",
               confirm=True, preview_id=prev["preview_id"])
    assert sess.writes == [("send_chat", PARTNER, COACH_G, "Husk leggskinn")]
    assert done["results"][0]["result"] == "sendt"


def test_send_message_to_other_players_guardian(tmp_path):
    rt, sess = runtime(tmp_path)
    prev = run(rt, tools.send_message, group="J15", to="Per Annen", text="Hei")
    assert prev["will_send_to"] == ["Siri Annen (forelder til Per)"]
    run(rt, tools.send_message, group="J15", to="Per Annen", text="Hei", confirm=True,
        preview_id=prev["preview_id"])
    assert sess.writes == [("send_chat", OTHER_PARENT, COACH_G, "Hei")]


def test_send_message_refused_outside_coach_groups(tmp_path):
    rt, sess = runtime(tmp_path)
    with pytest.raises(SafetyError, match="rolle"):
        run(rt, tools.send_message, group="Turngruppe", to="Lise", text="Hei")
    with pytest.raises(SafetyError, match="rolle"):
        run(rt, tools.send_message, chat_id="CHATPARENT", text="Hei")
    assert sess.writes == []


def test_send_message_reply_in_coach_chat(tmp_path):
    rt, sess = runtime(tmp_path)
    prev = run(rt, tools.send_message, chat_id="CHATCOACH", text="God bedring!")
    run(rt, tools.send_message, chat_id="CHATCOACH", text="God bedring!", confirm=True,
        preview_id=prev["preview_id"])
    assert sess.writes == [("reply_chat", "CHATCOACH", "God bedring!")]


def test_send_message_ambiguous_and_empty(tmp_path):
    rt, _ = runtime(tmp_path)
    with pytest.raises(LookupFailed, match="passer"):
        run(rt, tools.send_message, group="J15", to="Test", text="Hei")
    with pytest.raises(SafetyError, match="tegn"):
        run(rt, tools.send_message, group="J15", to="Per", text="   ")


# --------------------------------------------------------------------------- #
# Eksport
# --------------------------------------------------------------------------- #
def _fake_export() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "For import"
    ws.append(["Trening"])
    ws.append(["man. 1. juni 10:00"])
    ws.append(["Banen"])
    ws.append([])
    ws.append(["Status", "Navn", "E-post", "Mobilnummer", "Foresatt 1's navn", "Svartidspunkt"])
    ws.append(["Kommer", "Kari Test", LEAKY_EMAIL, LEAKY_PHONE, "Ola Test", None])
    ws.append(["Kan ikke komme", "Per Annen", None, None, "Siri Annen", None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_export_summary_drops_contact_columns_and_file_is_private(tmp_path):
    sess = FakeSession()
    sess.xlsx_bytes = _fake_export()
    rt, _ = runtime(tmp_path, sess)
    out = run(rt, tools.export_attendance_xlsx, event_id="EVPAST")
    assert out["by_status"] == {"Kommer": 1, "Kan ikke komme": 1}
    assert out["columns_kept"] == ["Status", "Navn", "Svartidspunkt"]
    text = dumps({k: v for k, v in out.items() if k != "note"})
    assert LEAKY_EMAIL not in text and LEAKY_PHONE not in text and "Ola Test" not in text
    path = tmp_path / "exports" / "spond-oppmote-2026-05-22-trening.xlsx"
    assert out["path"] == str(path) and path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    again = run(rt, tools.export_attendance_xlsx, event_id="EVPAST")
    assert again["path"].endswith("_2.xlsx")


def test_export_refused_outside_coach_groups(tmp_path):
    rt, sess = runtime(tmp_path)
    with pytest.raises(SafetyError, match="rolle"):
        run(rt, tools.export_attendance_xlsx, event_id="EVPARENTPAST")
    assert not any(r.startswith("export:") for r in sess.reads)


def test_rsvp_decline_with_message(tmp_path):
    rt, sess = runtime(tmp_path)
    prev = run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="nei", message="Fotballkamp")
    assert "med melding «Fotballkamp»" in prev["will_do"] and sess.writes == []
    done = run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="nei", message="Fotballkamp",
               confirm=True, preview_id=prev["preview_id"])
    assert sess.writes == [("put_response", "EVFUTURE", CHILD_C, False, "Fotballkamp")]
    assert done["result"] == "ok" and done["decline_message_now"] == "Fotballkamp"
    # Bare ny melding på et nei som allerede står, er en endring (ikke no_change)
    again = run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="nei", message="Kamp")
    assert again["preview"] is True and again["current_decline_message"] == "Fotballkamp"
    same = run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="nei", message="Fotballkamp")
    assert same["result"] == "no_change"


def test_rsvp_message_rules(tmp_path):
    rt, sess = runtime(tmp_path)
    with pytest.raises(SafetyError, match="bare brukes sammen med nei"):
        run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="ja", message="Gleder meg")
    with pytest.raises(SafetyError, match="maks"):
        run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="nei", message="x" * 201)
    prev = run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="nei", message="Syk")
    with pytest.raises(SafetyError, match="endret"):
        run(rt, tools.rsvp, event_id="EVFUTURE", child="Kari", answer="nei", message="Kamp",
            confirm=True, preview_id=prev["preview_id"])
    assert sess.writes == []
