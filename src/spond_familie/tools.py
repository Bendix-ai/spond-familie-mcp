"""Verktøyene, uten MCP-avhengighet: ``async def verktøy(rt, ...) -> dict``.

``server.py`` pakker dem inn for stdio; ``--cli`` kaller dem direkte. Alle
svar bygges via ``serialize`` (hvitelister, navn i stedet for id-er, Oslo-tid).
Skriveverktøyene (``rsvp``, ``send_message``) går via ``safety``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from importlib.metadata import version

from . import serialize as ser
from . import xlsx
from .runtime import Runtime
from .safety import (
    MAX_RECIPIENTS,
    SafetyError,
    audit,
    audit_path,
    check_message_text,
    rsvp_checks,
)
from .stats import attendance

LookupFailed = ser.LookupFailed


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def _start(event: dict) -> datetime | None:
    return ser._parse_iso(event.get("startTimestamp"))


def _matches(text: str | None, query: str | None) -> bool:
    if not query:
        return True
    hay = (text or "").casefold()
    return all(tok in hay for tok in query.casefold().split())


async def _groups(rt: Runtime, group: str | None) -> tuple[ser.Directory, list[ser.GroupInfo]]:
    d = await rt.directory()
    return d, ([d.group(group)] if group else list(d.groups.values()))


async def _event_and_group(rt: Runtime, event_id: str) -> tuple[dict, ser.GroupInfo, ser.Directory]:
    if not event_id:
        raise LookupFailed("Oppgi event_id (fra list_events eller overview).")
    s = await rt.session()
    d = await rt.directory()
    event = await s.fetch_event(event_id)
    gi = d.groups.get(ser.event_group_id(event) or "")
    if gi is None:
        raise LookupFailed("Arrangementet tilhører en gruppe du ikke ser.")
    return event, gi, d


# --------------------------------------------------------------------------- #
# Lesing
# --------------------------------------------------------------------------- #
async def overview(rt: Runtime, days: int = 7) -> dict:
    days = _clamp(days, 1, 31)
    d = await rt.directory()
    now = rt.now()
    horizon = max(days, 14)
    rows, todo, cancelled, errors = [], [], [], []
    for gi in d.groups.values():
        try:
            events = await rt.events(gi.id, now, now + timedelta(days=horizon))
        except Exception as exc:  # noqa: BLE001 - én gruppe skal ikke velte oversikten
            errors.append(f"{gi.short}: {exc}")
            continue
        for e in events:
            row = ser.event_row(e, gi, now)
            for kid in row.get("kids") or []:
                if kid["status"] == "ikke svart" and not row.get("invite_pending") and not row.get("cancelled"):
                    todo.append({"child": kid["name"], "id": row["id"], "group": gi.short,
                                 "title": row["title"], "start": row["start"],
                                 "deadline": row.get("deadline")})
            if _start(e) < now + timedelta(days=days):
                if row.get("cancelled"):
                    cancelled.append({k: row.get(k) for k in ("id", "group", "title", "start", "cancelled")})
                else:
                    rows.append((e.get("startTimestamp") or "", row))
    rows.sort(key=lambda t: t[0])
    shown, more = ser.cap([r for _, r in rows], 60)
    return {
        "now": ser.fmt_dt(now),
        "days": days,
        "children_todo": todo,
        "events": shown,
        "truncated": more or None,
        "cancelled": cancelled,
        "errors": errors,
        "hint": "Bruk get_event(id) for detaljer. Barnas ubesvarte gjelder de neste 14 dagene.",
    }


async def list_groups(rt: Runtime) -> dict:
    d = await rt.directory()
    out = []
    for gi in d.groups.values():
        players = [m for m in gi.member_names if m not in gi.member_roles]
        out.append({
            "id": gi.id, "short": gi.short, "name": gi.name,
            "my_roles": gi.my_roles, "coach": gi.coach,
            "children": [c.name for c in gi.children],
            "n_members": len(gi.member_names), "n_with_role": len(gi.member_roles),
            "n_players": len(players),
        })
    return {"groups": out, "note": "coach=true betyr at du har en rolle i gruppa (trenerverktøy og meldinger er tillatt)."}


async def list_events(rt: Runtime, group: str | None = None, days_ahead: int = 14, days_back: int = 0,
                      query: str | None = None, only_children: bool = False, limit: int = 50) -> dict:
    days_ahead = _clamp(days_ahead, 0, 365)
    days_back = _clamp(days_back, 0, 365)
    limit = _clamp(limit, 1, 200)
    d, groups = await _groups(rt, group)
    now = rt.now()
    rows, errors = [], []
    for gi in groups:
        if only_children and not gi.children:
            continue
        try:
            events = await rt.events(gi.id, now - timedelta(days=days_back), now + timedelta(days=days_ahead))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{gi.short}: {exc}")
            continue
        for e in events:
            if not _matches(f"{e.get('heading')} {e.get('description')}", query):
                continue
            row = ser.event_row(e, gi, now)
            if only_children and not row.get("kids"):
                continue
            rows.append((e.get("startTimestamp") or "", row))
    rows.sort(key=lambda t: t[0])
    shown, more = ser.cap([r for _, r in rows], limit)
    return {"events": shown, "count": len(rows), "truncated": more or None, "errors": errors}


async def get_event(rt: Runtime, event_id: str, include_comments: bool = True,
                    include_chat: bool = False) -> dict:
    event, gi, d = await _event_and_group(rt, event_id)
    s = await rt.session()
    now = rt.now()
    out = ser.event_detail(event, gi, d, now)
    errors: list[str] = []
    if include_comments:
        by_event, errs = await s.fetch_event_comments([event_id])
        out["comments"] = ser.comments_view(by_event.get(event_id) or [], d)
        errors += errs
    if include_chat:
        threads = []
        for chat in await rt.chats():
            spond = (chat.get("message") or {}).get("spond") or {}
            if spond.get("id") != event_id:
                continue
            msgs = await s.fetch_chat_messages(chat["id"], 40)
            threads.append({"chat_id": chat["id"], "messages": ser.chat_messages_view(msgs, d, 40)})
            if len(threads) >= 3:
                break
        out["chats"] = threads
    can = []
    for c in gi.children:
        blocks, _ = rsvp_checks(event, c.member_uid, now)
        if not blocks:
            can.append(c.name.split()[0])
    out["can_rsvp_for"] = can
    out["errors"] = errors
    return out


async def list_posts(rt: Runtime, group: str | None = None, days: int = 14, query: str | None = None,
                     include_comments: bool = False, limit: int = 20) -> dict:
    days = _clamp(days, 1, 365)
    limit = _clamp(limit, 1, 50)
    d, groups = await _groups(rt, group)
    since = rt.now() - timedelta(days=days)
    rows, errors = [], []
    for gi in groups:
        try:
            posts = await rt.posts(gi.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{gi.short}: {exc}")
            continue
        for p in posts:
            ts = ser._parse_iso(p.get("timestamp"))
            if ts is None or ts < since or not _matches(f"{p.get('title')} {p.get('body')}", query):
                continue
            rows.append((p.get("timestamp") or "", ser.post_row(p, gi, d, include_comments)))
    rows.sort(key=lambda t: t[0], reverse=True)
    shown, more = ser.cap([r for _, r in rows], limit)
    return {"posts": shown, "count": len(rows), "truncated": more or None, "errors": errors}


async def list_chats(rt: Runtime, limit: int = 20, unread_only: bool = False) -> dict:
    limit = _clamp(limit, 1, 50)
    d = await rt.directory()
    chats = sorted(await rt.chats(), key=lambda c: c.get("newestTimestamp") or "", reverse=True)
    rows = [ser.chat_row(c, d) for c in chats if not unread_only or c.get("unread")]
    shown, more = ser.cap(rows, limit)
    return {"chats": shown, "truncated": more or None}


async def get_chat(rt: Runtime, chat_id: str, limit: int = 40) -> dict:
    limit = _clamp(limit, 1, 100)
    s = await rt.session()
    d = await rt.directory()
    header = next((ser.chat_row(c, d) for c in await rt.chats() if c.get("id") == chat_id), None)
    msgs = await s.fetch_chat_messages(chat_id, limit)
    return {"chat": header, "messages": ser.chat_messages_view(msgs, d, limit)}


async def find_person(rt: Runtime, query: str, group: str | None = None) -> dict:
    if not query or len(query.strip()) < 2:
        raise LookupFailed("Søk med minst to tegn.")
    d, groups = await _groups(rt, group)
    found = []
    for gi in groups:
        for mid, name in gi.member_names.items():
            guardians = gi.guardians.get(mid, [])
            if _matches(name, query):
                found.append({
                    "member_id": mid, "name": name, "group": gi.short,
                    "role": ", ".join(gi.member_roles.get(mid, [])) or "spiller/medlem",
                    "guardians": [g for _, g in guardians],
                    "is_my_child": mid in gi.child_ids or None,
                    "has_profile": mid in gi.member_profile,
                })
            for _, gname in guardians:
                if _matches(gname, query):
                    found.append({"name": gname, "group": gi.short, "guardian_of": name})
    shown, more = ser.cap(found, 25)
    return {"people": shown, "truncated": more or None,
            "note": "Kontaktinfo (telefon/e-post) vises aldri her."}


async def attendance_stats(rt: Runtime, group: str, days_back: int = 90, kind: str = "all",
                           member: str | None = None) -> dict:
    days_back = _clamp(days_back, 7, 365)
    d = await rt.directory()
    gi = d.group(group)
    if gi.coach:
        only = None
        scope = "alle medlemmer"
    elif gi.children:
        only = gi.child_ids
        scope = "bare dine barn (du har ingen rolle i gruppa)"
    else:
        raise SafetyError(f"Du har verken rolle eller barn i {gi.short}.")
    if member:
        ids = {mid for mid, name in gi.member_names.items() if _matches(name, member)}
        if not ids:
            raise LookupFailed(f"Fant ingen «{member}» i {gi.short}.")
        only = ids if only is None else (only & ids)
    now = rt.now()
    events = await rt.events(gi.id, now - timedelta(days=days_back), now)
    res = attendance(events, gi, kind=kind, only_members=only)
    res["players"], more = ser.cap(res["players"], 60)
    return {"group": gi.short, "period_days": days_back, "kind": kind, "scope": scope,
            **res, "truncated": more or None}


async def export_attendance_xlsx(rt: Runtime, event_id: str) -> dict:
    event, gi, _ = await _event_and_group(rt, event_id)
    if not gi.coach:
        raise SafetyError(f"Eksport er bare tillatt i grupper der du har en rolle; ikke i {gi.short}.")
    s = await rt.session()
    data = await s.export_xlsx(event_id)
    start = ser.to_oslo(event.get("startTimestamp"))
    path = xlsx.save(data, xlsx.export_dir(), f"{start:%Y-%m-%d}" if start else "udatert",
                     event.get("heading") or "arrangement")
    audit({"tool": "export_attendance_xlsx", "phase": "executed", "group": gi.short,
           "event_id": event_id, "path": str(path)})
    return {"path": str(path), "bytes": len(data), **xlsx.summarize(data),
            "note": "Fila på disk inneholder kontaktdata fra Spond (telefon, e-post, adresse). "
                    "Sammendraget her har bare status og navn."}


async def status(rt: Runtime) -> dict:
    sess = await rt.session()
    spond = getattr(sess, "_spond", None)
    return {
        "mode": rt.mode,
        "logged_in": bool(spond and spond.token),
        "spond_auth": sess.auth_status() if hasattr(sess, "auth_status") else None,
        "cache_entries": rt.cache_size,
        "readonly": rt.readonly,
        "audit_log": str(audit_path()),
        "export_dir": str(xlsx.export_dir()),
        "spond_lib": version("spond"),
        "last_errors": list(rt.errors),
    }


# --------------------------------------------------------------------------- #
# Skriving
# --------------------------------------------------------------------------- #
_YES = {"accept", "ja", "yes", "kommer", "true"}
_NO = {"decline", "nei", "no", "kommer ikke", "false"}


def _resolve_child(gi: ser.GroupInfo, child: str) -> ser.Child:
    if not gi.children:
        raise SafetyError(f"Du har ingen barn i {gi.short}; RSVP er bare tillatt for egne barn.")
    q = (child or "").strip().casefold()
    hits = [c for c in gi.children
            if q and (q == c.name.split()[0].casefold() or q == c.name.casefold()
                      or q == ser.normalize_name(c.name))]
    if len(hits) != 1:
        names = ", ".join(c.name.split()[0] for c in gi.children)
        raise SafetyError(f"Fant ikke barnet «{child}» i {gi.short}. Dine barn der: {names}.")
    return hits[0]


DECLINE_MESSAGE_MAX = 200


def _decline_message(event: dict, member_uid: str) -> str | None:
    msgs = (event.get("responses") or {}).get("declineMessages") or {}
    return msgs.get(member_uid) if isinstance(msgs, dict) else None


async def rsvp(rt: Runtime, event_id: str, child: str, answer: str, confirm: bool = False,
               preview_id: str | None = None, message: str | None = None) -> dict:
    a = (answer or "").strip().casefold()
    if a in _YES:
        accepted, target = True, "ja"
    elif a in _NO:
        accepted, target = False, "nei"
    else:
        raise SafetyError("answer må være accept (ja) eller decline (nei).")
    message = (message or "").strip() or None
    if message and accepted:
        raise SafetyError("message (avmeldingsgrunn) kan bare brukes sammen med nei.")
    if message and len(message) > DECLINE_MESSAGE_MAX:
        raise SafetyError(f"Avmeldingsgrunnen kan være maks {DECLINE_MESSAGE_MAX} tegn.")

    event, gi, _ = await _event_and_group(rt, event_id)
    kid = _resolve_child(gi, child)
    now = rt.now()
    blocks, warnings = rsvp_checks(event, kid.member_uid, now)
    current = ser.member_status(event, kid.member_uid)
    current_msg = _decline_message(event, kid.member_uid)
    what = (f"Svare «{target}» for {kid.name} på «{event.get('heading')}» "
            f"({ser.fmt_dt(event.get('startTimestamp'))}, {gi.short})"
            + (f" med melding «{message}»" if message else ""))
    params = {"event_id": event_id, "member": kid.member_uid, "answer": target, "message": message}
    base = {"tool": "rsvp", "group": gi.short, "event_id": event_id, "child": kid.name, "answer": target,
            "message": message}

    if blocks:
        audit({**base, "phase": "refused", "reason": blocks})
        raise SafetyError("Kan ikke svare: " + " ".join(blocks))
    if current == target and (message is None or message == current_msg):
        return {"result": "no_change", "message": f"{kid.name} har allerede svart «{target}»."}

    if not confirm:
        pid = rt.previews.create("rsvp", params)
        audit({**base, "phase": "preview", "preview_id": pid, "current": current})
        return {"preview": True, "will_do": what, "current_state": current,
                "current_decline_message": current_msg, "warnings": warnings,
                "preview_id": pid, "expires_in_s": 600,
                "next": "Vis dette til brukeren. Kall rsvp igjen med samme argumenter, "
                        "confirm=true og preview_id først etter et tydelig ja."}

    rt.previews.consume(preview_id, "rsvp", params)
    s = await rt.session()
    try:
        await s.put_response(event_id, kid.member_uid, accepted, decline_message=message)
    except Exception as exc:
        audit({**base, "phase": "failed", "preview_id": preview_id, "error": str(exc)})
        raise
    rt.invalidate("events", gi.id)
    fresh = await s.fetch_event(event_id)
    now_status = ser.member_status(fresh, kid.member_uid)
    now_msg = _decline_message(fresh, kid.member_uid)
    ok = now_status == target and (message is None or now_msg == message)
    audit({**base, "phase": "executed", "preview_id": preview_id, "before": current,
           "after": now_status, "verified": ok})
    return {"result": "ok" if ok else "uncertain", "done": what, "before": current, "status_now": now_status,
            "decline_message_now": now_msg,
            "warning": None if ok else "Spond viser ikke det nye svaret ennå. Sjekk i appen."}


async def send_message(rt: Runtime, text: str, group: str | None = None, to: str | None = None,
                       audience: str = "guardians", chat_id: str | None = None, confirm: bool = False,
                       preview_id: str | None = None) -> dict:
    text = check_message_text(text)
    d = await rt.directory()
    coach_groups = ", ".join(g.short for g in d.groups.values() if g.coach) or "ingen"

    if chat_id:
        chat = next((c for c in await rt.chats() if c.get("id") == chat_id), None)
        if chat is None:
            raise LookupFailed("Fant ikke chatten blant de 50 nyeste. Bruk list_chats.")
        gi = d.groups.get(chat.get("groupId") or "")
        if gi is None or not gi.coach:
            raise SafetyError(f"Svar i chat er bare tillatt i chatter i grupper der du har en rolle ({coach_groups}).")
        row = ser.chat_row(chat, d)
        params = {"mode": "reply", "chat_id": chat_id, "text": text}
        targets = [("reply", chat_id, ", ".join(row.get("with") or []) or "chatten")]
    else:
        gi = d.group(group)
        if not gi.coach:
            raise SafetyError(f"Meldinger kan bare sendes i grupper der du har en rolle ({coach_groups}).")
        if audience not in ("guardians", "member", "both"):
            raise SafetyError("audience må være guardians, member eller both.")
        if not to:
            raise SafetyError("Oppgi mottaker i «to» (navn på spiller/medlem eller foresatt).")
        members = [mid for mid, name in gi.member_names.items() if _matches(name, to)]
        guardian_hits = [(pid, gname) for mid, gl in gi.guardians.items() for pid, gname in gl
                         if pid and _matches(gname, to)]
        targets = []
        if len(members) == 1:
            mid = members[0]
            mname = gi.member_names[mid]
            if audience in ("member", "both") and gi.member_profile.get(mid):
                targets.append(("dm", gi.member_profile[mid], mname))
            if audience in ("guardians", "both"):
                targets += [("dm", pid, f"{gname} (forelder til {mname.split()[0]})")
                            for pid, gname in gi.guardians.get(mid, []) if pid]
        elif not members and len({p for p, _ in guardian_hits}) == 1:
            targets.append(("dm", guardian_hits[0][0], guardian_hits[0][1]))
        else:
            cands = [gi.member_names[m] for m in members] + [g for _, g in guardian_hits]
            raise LookupFailed(
                f"«{to}» passer {len(cands)} personer i {gi.short}"
                + (": " + ", ".join(cands[:8]) if cands else "") + ". Vær mer presis."
            )
        targets = [t for t in targets if t[1] != d.me]
        if not targets:
            raise SafetyError("Ingen mottakere med Spond-profil (spillere uten profil nås via foresatte).")
        if len(targets) > MAX_RECIPIENTS:
            raise SafetyError(f"Maks {MAX_RECIPIENTS} mottakere per melding.")
        params = {"mode": "dm", "group": gi.id, "recipients": sorted(t[1] for t in targets), "text": text}

    base = {"tool": "send_message", "group": gi.short, "recipients": [t[2] for t in targets], "text": text}
    if not confirm:
        pid = rt.previews.create("send_message", params)
        audit({**base, "phase": "preview", "preview_id": pid})
        return {"preview": True, "group": gi.short,
                "will_send_to": [t[2] for t in targets],
                "separate_dm_each": len(targets) > 1 or None,
                "text": text, "preview_id": pid, "expires_in_s": 600,
                "next": "Vis mottakere og tekst ordrett til brukeren. Kall send_message igjen med "
                        "samme argumenter, confirm=true og preview_id først etter et tydelig ja."}

    rt.previews.consume(preview_id, "send_message", params)
    s = await rt.session()
    results = []
    for kind, target, label in targets:
        try:
            if kind == "reply":
                await s.reply_chat(target, text)
            else:
                await s.send_chat(target, gi.id, text)
            results.append({"to": label, "result": "sendt"})
        except Exception as exc:  # noqa: BLE001 - rapporter per mottaker, ingen retry
            results.append({"to": label, "result": "feilet", "error": str(exc)[:200]})
    rt.invalidate("chats")
    audit({**base, "phase": "executed", "preview_id": preview_id, "results": results})
    return {"results": results,
            "note": "Feilede sendinger prøves ikke på nytt automatisk (unngår dobbel melding)."}


READ_TOOLS = (overview, list_groups, list_events, get_event, list_posts, list_chats, get_chat,
              find_person, attendance_stats, export_attendance_xlsx, status)
WRITE_TOOLS = (rsvp, send_message)
