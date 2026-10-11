"""Syntetiske Spond-data og en FakeSession for MCP-testene.

Ingen ekte id-er, navn eller nettverk. Kontaktfeltene er fylt med
gjenkjennelige verdier slik at lekkasjetestene kan lete etter dem.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spond_familie._base import Settings

NOW = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)

ME = "PROFILEME0000000000000000000000001"
PARTNER = "PROFILEPARTNER000000000000000002"
OTHER_PARENT = "PROFILEOTHERPARENT00000000000003"

COACH_G = "GROUPCOACH00000000000000000000001"
PARENT_G = "GROUPPARENT0000000000000000000002"

CHILD_C = "MEMBERCHILDC00000000000000000001"   # mitt barn i trenergruppa
PLAYER_C = "MEMBERPLAYERC0000000000000000002"  # andres barn i trenergruppa
ME_C = "MEMBERMEC000000000000000000000003"     # meg som trener
CHILD_P = "MEMBERCHILDP00000000000000000004"   # mitt barn i foreldregruppa
PLAYER_P = "MEMBERPLAYERP0000000000000000005"

ROLE_TRENER = "ROLETRENER0000000000000000000001"

SECRET_PW = "SUPERSECRETpassword-mcp-42"
LEAKY_EMAIL = "lekk@example.com"
LEAKY_PHONE = "+4799999999"
LEAKY_DOB = "2015-01-02"
LEAKY_ADDRESS = "Hemmeligveien 7"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def config(root: Path | None = None) -> Settings:
    return Settings(spond_username="user@example.com", spond_password=SECRET_PW)


def _member(mid, first, last, *, profile=None, roles=(), guardians=()):
    m = {
        "id": mid, "firstName": first, "lastName": last, "roles": list(roles),
        "email": LEAKY_EMAIL, "phoneNumber": LEAKY_PHONE, "dateOfBirth": LEAKY_DOB,
        "address": {"streetAddress": LEAKY_ADDRESS},
        "guardians": [
            {"id": f"G{pid[:8]}", "firstName": gf, "lastName": gl,
             "email": LEAKY_EMAIL, "phoneNumber": LEAKY_PHONE,
             "profile": {"id": pid, "contactMethod": "app", "phoneNumber": LEAKY_PHONE}}
            for pid, gf, gl in guardians
        ],
    }
    if profile:
        m["profile"] = {"id": profile, "phoneNumber": LEAKY_PHONE}
    return m


def groups() -> list[dict]:
    return [
        {
            "id": COACH_G, "name": "Testlag J15",
            "contactPerson": {"email": LEAKY_EMAIL, "phoneNumber": LEAKY_PHONE},
            "roles": [{"id": ROLE_TRENER, "name": "Trener", "permissions": ["events"]}],
            "members": [
                _member(CHILD_C, "Kari", "Test", guardians=[(ME, "Ola", "Test"), (PARTNER, "Eva", "Test")]),
                _member(PLAYER_C, "Per", "Annen", guardians=[(OTHER_PARENT, "Siri", "Annen")]),
                _member(ME_C, "Ola", "Test", profile=ME, roles=[ROLE_TRENER]),
            ],
        },
        {
            "id": PARENT_G, "name": "Turngruppe",
            "roles": [],
            "members": [
                _member(CHILD_P, "Kari", "Test", guardians=[(ME, "Ola", "Test")]),
                _member(PLAYER_P, "Lise", "Annen", guardians=[(OTHER_PARENT, "Siri", "Annen")]),
            ],
        },
    ]


def _event(eid, gid, start, *, heading="Trening", accepted=(), declined=(), unanswered=(), **extra):
    e = {
        "id": eid, "heading": heading, "type": "EVENT",
        "startTimestamp": iso(start), "endTimestamp": iso(start + timedelta(hours=1)),
        "recipients": {"group": {"id": gid, "name": "x"}},
        "responses": {"acceptedIds": list(accepted), "declinedIds": list(declined),
                      "unansweredIds": list(unanswered), "waitinglistIds": [], "unconfirmedIds": []},
        "owners": [{"id": ME, "response": "accepted"}],
        "location": {"feature": "Banen", "address": "Veien 1"},
        "description": "Ta med drikkeflaske.",
    }
    e.update(extra)
    return e


def events() -> list[dict]:
    d = timedelta
    return [
        _event("EVFUTURE", COACH_G, NOW + d(days=2), unanswered=[CHILD_C, PLAYER_C], accepted=[ME_C],
               rsvpDate=iso(NOW + d(days=1))),
        _event("EVPENDING", COACH_G, NOW + d(days=3), unanswered=[CHILD_C], inviteTime=iso(NOW + d(days=1))),
        _event("EVCANCEL", COACH_G, NOW + d(days=4), unanswered=[CHILD_C], cancelled=True,
               cancelledReason="Regn"),
        _event("EVMATCH", COACH_G, NOW - d(days=5), heading="Lag A – Lag B", accepted=[CHILD_C],
               declined=[PLAYER_C], expired=True, matchEvent=True,
               registered=True),
        _event("EVPAST", COACH_G, NOW - d(days=10), accepted=[CHILD_C, PLAYER_C], expired=True),
        _event("EVHIDDEN", COACH_G, NOW - d(days=12), accepted=[PLAYER_C], expired=True,
               participantsHidden=True),
        _event("EVPARENT", PARENT_G, NOW + d(days=1), unanswered=[CHILD_P, PLAYER_P],
               participantsHidden=True),
        _event("EVPARENTPAST", PARENT_G, NOW - d(days=3), accepted=[CHILD_P], declined=[PLAYER_P],
               expired=True),
    ]


class FakeSession:
    """Delmengde av SpondSession med registrering av skrivekall."""

    def __init__(self, cfg=None):
        self.events = {e["id"]: e for e in events()}
        self.groups = groups()
        self.writes: list[tuple] = []
        self.reads: list[str] = []
        self.logins = 1
        self.login_age_s = 60.0
        self._spond = None
        self.xlsx_bytes = b""

    async def close(self):
        pass

    def auth_status(self):
        return {"mode": "test", "source": "refresh", "access_expires_min": 600,
                "refresh_expires_days": 89.5}

    async def fetch_profile(self):
        self.reads.append("profile")
        return {"id": ME, "firstName": "Ola", "lastName": "Test", "phoneNumber": LEAKY_PHONE,
                "primaryEmail": LEAKY_EMAIL, "dateOfBirth": LEAKY_DOB}

    async def fetch_groups(self):
        self.reads.append("groups")
        return copy.deepcopy(self.groups)

    async def fetch_events_window(self, group_id, start, end):
        self.reads.append(f"events:{group_id}")
        out = []
        for e in self.events.values():
            if e["recipients"]["group"]["id"] != group_id:
                continue
            ts = datetime.fromisoformat(e["startTimestamp"].replace("Z", "+00:00"))
            if start <= ts < end:
                out.append(copy.deepcopy(e))
        return out

    async def fetch_event(self, event_id):
        self.reads.append(f"event:{event_id}")
        if event_id not in self.events:
            raise RuntimeError("HTTP 404")
        return copy.deepcopy(self.events[event_id])

    async def fetch_event_comments(self, event_ids):
        return {eid: [{"id": "C1", "fromProfileId": OTHER_PARENT, "timestamp": iso(NOW),
                       "text": "Hvem kjører?", "children": [
                           {"id": "C2", "fromProfileId": ME, "timestamp": iso(NOW), "text": "Jeg"}]}]
                for eid in event_ids}, []

    async def fetch_group_posts(self, group_id, max_posts=50, include_comments=True):
        return [{"id": "P1", "groupId": group_id, "ownerId": OTHER_PARENT, "timestamp": iso(NOW - timedelta(days=1)),
                 "title": "Dugnad", "body": "Husk dugnad lørdag.", "comments": []}]

    async def fetch_chats(self, max_chats=50):
        return [
            {"id": "CHATCOACH", "type": "DM", "groupId": COACH_G, "participants": [ME, OTHER_PARENT],
             "newestTimestamp": iso(NOW), "unread": True,
             "message": {"user": OTHER_PARENT, "text": "Per er syk", "timestamp": iso(NOW)}},
            {"id": "CHATPARENT", "type": "GROUP", "groupId": PARENT_G, "participants": [ME, OTHER_PARENT],
             "newestTimestamp": iso(NOW - timedelta(days=1)),
             "message": {"user": OTHER_PARENT, "text": "Hei", "timestamp": iso(NOW)}},
        ]

    async def fetch_chat_messages(self, chat_id, max_messages=40):
        return [{"chatId": chat_id, "user": OTHER_PARENT, "text": "Per er syk", "timestamp": iso(NOW)}]

    async def export_xlsx(self, event_id):
        self.reads.append(f"export:{event_id}")
        return self.xlsx_bytes

    async def put_response(self, event_id, member_uid, accepted, decline_message=None):
        self.writes.append(("put_response", event_id, member_uid, accepted)
                           + ((decline_message,) if decline_message else ()))
        r = self.events[event_id]["responses"]
        if decline_message:
            r.setdefault("declineMessages", {})[member_uid] = decline_message
        for key in ("acceptedIds", "declinedIds", "unansweredIds"):
            if member_uid in r[key]:
                r[key].remove(member_uid)
        r["acceptedIds" if accepted else "declinedIds"].append(member_uid)
        return r

    async def send_chat(self, recipient_profile_id, group_id, text):
        self.writes.append(("send_chat", recipient_profile_id, group_id, text))
        return {"ok": True}

    async def reply_chat(self, chat_id, text):
        self.writes.append(("reply_chat", chat_id, text))
        return {"ok": True}


def runtime(tmp_path: Path, session: FakeSession | None = None):
    from spond_familie.runtime import Runtime

    sess = session or FakeSession()
    rt = Runtime(config=config(), session_factory=lambda cfg: sess, now=lambda: NOW)
    return rt, sess
