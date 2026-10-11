"""Fra rå Spond-JSON til små, trygge verktøysvar.

Alt som går ut av MCP-serveren bygges her fra hvitelister over felter, som
``byttehjelp_oppmote.HENDELSESFELTER``. Vi kopierer aldri Spond-dicts videre:
de bærer telefon, e-post, fødselsdato og adresse (gruppas ``contactPerson``,
medlemsfelter, foresattes profiler). Id-er oversettes til navn via
``Directory``, og tider vises i Oslo-tid med ukedag.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from ._base import SVARLISTER
from ._base import chat_msg_text as _chat_msg_text
from ._base import chat_msg_ts as _chat_msg_ts
from ._base import flatten_comments as _flatten_comments
from ._base import location_str as _location_str
from ._base import normalize_child_name as _normalize_child_name
from ._base import parse_iso as _parse_iso

OSLO = ZoneInfo("Europe/Oslo")
WEEKDAYS = ("man", "tir", "ons", "tor", "fre", "lør", "søn")

LIST_TEXT_MAX = 300
DETAIL_TEXT_MAX = 2000
NAMES_MAX = 60

# Svarliste → kort norsk status.
STATUS = {
    "acceptedIds": "ja",
    "declinedIds": "nei",
    "unansweredIds": "ikke svart",
    "waitinglistIds": "venteliste",
    "unconfirmedIds": "ubekreftet",
}
assert set(STATUS) == set(SVARLISTER)


class LookupFailed(ValueError):
    """Brukerrettet feil: fant ikke gruppe/person/arrangement."""


# --------------------------------------------------------------------------- #
# Små hjelpere
# --------------------------------------------------------------------------- #
def to_oslo(value) -> datetime | None:
    dt = value if isinstance(value, datetime) else _parse_iso(value)
    return dt.astimezone(OSLO) if dt is not None else None


def fmt_dt(value) -> str | None:
    """«2026-10-14 (tir) 17:30» — ukedagen med, fordi modeller regner den feil."""
    dt = to_oslo(value)
    if dt is None:
        return None
    return f"{dt:%Y-%m-%d} ({WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"


def fmt_time(value) -> str | None:
    dt = to_oslo(value)
    return f"{dt:%H:%M}" if dt else None


def clip(text, limit: int) -> str | None:
    if not text:
        return None
    text = str(text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def prune(obj):
    """Fjern None, "", [] og {} rekursivt (False og 0 beholdes)."""
    if isinstance(obj, dict):
        out = {k: prune(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(obj, list):
        return [v for v in (prune(x) for x in obj) if v not in (None, "", [], {})]
    return obj


def dumps(obj) -> str:
    return json.dumps(prune(obj), ensure_ascii=False, separators=(",", ":"), default=str)


def cap(items: list, limit: int) -> tuple[list, bool]:
    return items[:limit], len(items) > limit


def _full_name(person: dict) -> str:
    return " ".join(x for x in (person.get("firstName"), person.get("lastName")) if x).strip()


# --------------------------------------------------------------------------- #
# Directory: hvem er hvem, og hva er min rolle
# --------------------------------------------------------------------------- #
@dataclass
class Child:
    name: str
    member_uid: str


@dataclass
class GroupInfo:
    id: str
    name: str
    short: str
    my_roles: list[str] = field(default_factory=list)
    children: list[Child] = field(default_factory=list)
    member_names: dict[str, str] = field(default_factory=dict)       # member_id → navn
    member_roles: dict[str, list[str]] = field(default_factory=dict)  # member_id → rollenavn
    member_profile: dict[str, str] = field(default_factory=dict)      # member_id → profile_id
    guardians: dict[str, list[tuple[str | None, str]]] = field(default_factory=dict)  # member_id → [(profile_id, navn)]

    @property
    def coach(self) -> bool:
        return bool(self.my_roles)

    @property
    def child_ids(self) -> set[str]:
        return {c.member_uid for c in self.children}


@dataclass
class Directory:
    me: str | None
    groups: dict[str, GroupInfo]
    profile_names: dict[str, str]  # profile_id → visningsnavn

    def group(self, query: str | None) -> GroupInfo:
        """Slå opp gruppe på id, short-navn eller del av navnet."""
        if not query:
            raise LookupFailed("Oppgi gruppe. " + self._group_hint())
        q = query.strip().casefold()
        if query in self.groups:
            return self.groups[query]
        exact = [g for g in self.groups.values() if q in (g.short.casefold(), g.name.casefold())]
        if len(exact) == 1:
            return exact[0]
        partial = [g for g in self.groups.values()
                   if q in g.short.casefold() or q in g.name.casefold()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            raise LookupFailed(
                f"«{query}» passer flere grupper: " + ", ".join(g.short for g in partial)
            )
        raise LookupFailed(f"Fant ingen gruppe «{query}». " + self._group_hint())

    def _group_hint(self) -> str:
        return "Grupper: " + ", ".join(g.short for g in self.groups.values())

    def profile_name(self, profile_id: str | None) -> str:
        if not profile_id:
            return "ukjent"
        if profile_id == self.me:
            return "deg"
        return self.profile_names.get(profile_id, "ukjent")

    def member_name(self, gi: GroupInfo, member_id: str) -> str:
        return gi.member_names.get(member_id, "ukjent medlem")


def build_directory(profile: dict, groups: list[dict]) -> Directory:
    """Bygg navne- og rolleoppslag fra ``get_profile`` + ``get_groups``.

    Mine barn = medlemmer der en av foresatte er min profil, altså nøyaktig
    de Spond selv lar meg svare for. Ingen konfigurasjon trengs.
    """
    me = profile.get("id")
    profile_names: dict[str, str] = {}
    infos: dict[str, GroupInfo] = {}

    for g in groups:
        gid = g.get("id")
        if not gid:
            continue
        name = g.get("name") or gid
        gi = GroupInfo(id=gid, name=name, short=name[:32])
        role_names = {r.get("id"): r.get("name") or "rolle" for r in g.get("roles") or []}
        children: dict[str, Child] = {}

        for m in g.get("members") or []:
            mid = m.get("id")
            if not mid:
                continue
            mname = _full_name(m) or "uten navn"
            gi.member_names[mid] = mname
            roles = [role_names.get(rid, "rolle") for rid in m.get("roles") or []]
            if roles:
                gi.member_roles[mid] = roles
            pid = (m.get("profile") or {}).get("id")
            if pid:
                gi.member_profile[mid] = pid
                profile_names.setdefault(pid, mname)
                if pid == me:
                    gi.my_roles = sorted(set(gi.my_roles) | set(roles))
            gl = []
            for gd in m.get("guardians") or []:
                gpid = (gd.get("profile") or {}).get("id")
                gname = _full_name(gd) or "foresatt"
                gl.append((gpid, gname))
                if gpid:
                    first = mname.split()[0] if mname else "?"
                    profile_names.setdefault(gpid, f"{gname} (forelder til {first})")
                if gpid and gpid == me:
                    children[mid] = Child(name=mname, member_uid=mid)
            if gl:
                gi.guardians[mid] = gl

        gi.children = list(children.values())
        infos[gid] = gi

    return Directory(me=me, groups=infos, profile_names=profile_names)


# --------------------------------------------------------------------------- #
# Arrangementer
# --------------------------------------------------------------------------- #
def event_group_id(event: dict) -> str | None:
    return ((event.get("recipients") or {}).get("group") or {}).get("id")


def member_status(event: dict, member_id: str) -> str | None:
    responses = event.get("responses") or {}
    for key, label in STATUS.items():
        if member_id in (responses.get(key) or []):
            return label
    return None


def _counts(event: dict) -> dict:
    r = event.get("responses") or {}
    return {
        "ja": len(r.get("acceptedIds") or []),
        "nei": len(r.get("declinedIds") or []),
        "ikke_svart": len(r.get("unansweredIds") or []),
        "venteliste": len(r.get("waitinglistIds") or []),
    }


def kids_status(event: dict, gi: GroupInfo) -> list[dict]:
    out = []
    for c in gi.children:
        status = member_status(event, c.member_uid)
        if status:
            out.append({"name": c.name.split()[0], "status": status})
    return out


def event_row(event: dict, gi: GroupInfo, now: datetime) -> dict:
    """Kompakt rad for lister."""
    start = to_oslo(event.get("startTimestamp"))
    meetup = None
    prior = event.get("meetupPrior")
    if isinstance(prior, int) and prior > 5:  # 5 min er Sponds standardverdi
        meetup = fmt_time(event.get("meetupTimestamp"))
    deadline = _parse_iso(event.get("rsvpDate"))
    invite = _parse_iso(event.get("inviteTime"))
    return {
        "id": event.get("id"),
        "group": gi.short,
        "title": event.get("heading"),
        "start": fmt_dt(start),
        "end": fmt_time(event.get("endTimestamp")),
        "meetup": meetup,
        "where": _location_str(event) or None,
        "match": True if event.get("matchEvent") is True else None,
        "cancelled": (event.get("cancelledReason") or True) if event.get("cancelled") is True else None,
        "deadline": fmt_dt(deadline),
        "deadline_passed": True if deadline and deadline < now else None,
        "invite_pending": True if invite and invite > now else None,
        "counts": None if event.get("participantsHidden") is True and not gi.coach else _counts(event),
        "kids": kids_status(event, gi),
    }


def can_see_names(event: dict, gi: GroupInfo) -> bool:
    return gi.coach or event.get("participantsHidden") is not True


def responses_by_name(event: dict, gi: GroupInfo) -> dict:
    r = event.get("responses") or {}
    out = {}
    for key, label in STATUS.items():
        names = sorted(gi.member_names.get(mid, "ukjent") for mid in r.get(key) or [])
        if names:
            shown, more = cap(names, NAMES_MAX)
            out[label] = shown + ([f"… og {len(names) - NAMES_MAX} til"] if more else [])
    return out


def event_detail(event: dict, gi: GroupInfo, d: Directory, now: datetime) -> dict:
    out = event_row(event, gi, now)
    out["description"] = clip(event.get("description"), DETAIL_TEXT_MAX)
    out["type"] = event.get("type")
    out["attachments"] = [
        {"name": a.get("title") or "vedlegg", "url": a.get("media")}
        for a in event.get("attachments") or [] if a.get("media")
    ]
    out["organizers"] = sorted({d.profile_name(o.get("id")) for o in event.get("owners") or []})
    if can_see_names(event, gi):
        out["responses"] = responses_by_name(event, gi)
        if gi.coach:
            msgs = (event.get("responses") or {}).get("declineMessages") or {}
            if isinstance(msgs, dict):
                out["decline_messages"] = {
                    gi.member_names.get(mid, "ukjent"): clip(text, LIST_TEXT_MAX)
                    for mid, text in msgs.items() if text
                }
    if event.get("registered") is True:
        att = (event.get("responses") or {}).get("registeredAttendance") or {}
        if isinstance(att, dict):
            out["registered_attendance"] = {
                "møtte": sorted(gi.member_names.get(m, "ukjent") for m, v in att.items() if v == "ATTENDED"),
                "andre": sorted(f"{gi.member_names.get(m, 'ukjent')}: {v}" for m, v in att.items() if v != "ATTENDED"),
            }
    return out


def comments_view(comments: list[dict], d: Directory, limit: int = 30) -> list[dict]:
    flat = _flatten_comments(comments)
    flat.sort(key=lambda c: c.get("timestamp") or "")
    rows = [
        {"who": d.profile_name(c.get("fromProfileId")), "when": fmt_dt(c.get("timestamp")),
         "text": clip(c.get("text"), LIST_TEXT_MAX), "reply": True if c.get("depth") else None,
         "image": c.get("image_url")}
        for c in flat
    ]
    return rows[-limit:]


# --------------------------------------------------------------------------- #
# Innlegg og chat
# --------------------------------------------------------------------------- #
def post_row(post: dict, gi: GroupInfo, d: Directory, include_comments: bool) -> dict:
    files = [{"name": a.get("title") or "vedlegg", "url": a.get("media")}
             for a in post.get("attachments") or [] if a.get("media")]
    files += [{"name": "bilde", "url": m.get("url")} for m in post.get("media") or [] if m.get("url")]
    comments = post.get("comments") or []
    row = {
        "id": post.get("id"),
        "group": gi.short,
        "when": fmt_dt(post.get("timestamp")),
        "author": d.profile_name(post.get("ownerId")),
        "title": post.get("title"),
        "text": clip(post.get("body"), 500),
        "n_comments": len(_flatten_comments(comments)) or None,
        "files": files,
    }
    if include_comments:
        row["comments"] = comments_view(comments, d, limit=15)
    return row


def chat_row(chat: dict, d: Directory) -> dict:
    gi = d.groups.get(chat.get("groupId") or "")
    msg = chat.get("message") or {}
    spond = msg.get("spond") or {}
    others = [d.profile_name(p) for p in chat.get("participants") or []
              if isinstance(p, str) and p != d.me]
    shown, more = cap(others, 8)
    return {
        "chat_id": chat.get("id"),
        "type": chat.get("type"),
        "group": gi.short if gi else None,
        "name": chat.get("name"),
        "with": shown + ([f"… og {len(others) - 8} til"] if more else []),
        "last": {"who": d.profile_name(msg.get("user")), "when": fmt_dt(msg.get("timestamp")),
                 "text": clip(_chat_msg_text(msg), 200)} if msg else None,
        "unread": True if chat.get("unread") else None,
        "event": {"id": spond.get("id"), "title": spond.get("heading")} if spond.get("id") else None,
    }


def chat_messages_view(messages: list[dict], d: Directory, limit: int) -> list[dict]:
    rows = sorted(messages, key=lambda m: str(_chat_msg_ts(m) or ""))
    return [
        {"who": d.profile_name(m.get("user")), "when": fmt_dt(_chat_msg_ts(m)),
         "text": clip(_chat_msg_text(m), LIST_TEXT_MAX)}
        for m in rows[-limit:]
    ]


def normalize_name(name: str | None) -> str:
    return _normalize_child_name(name)
