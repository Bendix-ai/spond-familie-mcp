"""Svar- og oppmøtestatistikk per medlem.

Reglene er målt på et ekte barnefotballag (august 2026):

- Bare avsluttede (``expired``), ikke avlyste arrangementer telles.
- ``participantsHidden`` holdes utenfor: tom svarliste betyr ikke «ingen møtte».
- «Invitert» er unionen av de fem svarlistene, ikke hele gruppa.
- «Møtte» finnes bare der oppmøte faktisk er ført (``registered is True``).
  Mange lag fører det nesten aldri (3 av 126 i målingen), så hovedtallet er
  «svart ja», ikke «deltatt».
- ``autoAccept`` gjør «ja» til en standardverdi; antallet flagges.
"""

from __future__ import annotations

import re
from collections import defaultdict

from ._base import SVARLISTER

from .serialize import GroupInfo

# «Hjemmelag 2 – Bortelag 2». Tankestrek, kort bindestrek og en-dash.
KAMPMONSTER = re.compile(r"^\s*\S.*\s[–—-]\s.*\S\s*$")

NOTE = (
    "«ja» betyr svart ja, ikke at personen møtte. «møtte» vises bare for "
    "arrangementer der treneren har ført oppmøte i Spond."
)


def is_match(event: dict) -> bool:
    if event.get("matchEvent") is True:
        return True
    return bool(KAMPMONSTER.match(event.get("heading") or ""))


def invited(event: dict) -> set[str]:
    out: set[str] = set()
    responses = event.get("responses") or {}
    for key in SVARLISTER:
        out.update(responses.get(key) or [])
    return out


def attendance(events: list[dict], gi: GroupInfo, *, kind: str = "all",
               only_members: set[str] | None = None) -> dict:
    """Beregn per-medlem-statistikk. ``only_members`` begrenser radene (ikke-trener)."""
    if kind not in ("all", "match", "training"):
        raise ValueError("kind må være all, match eller training")

    finished = [e for e in events if e.get("expired")]
    cancelled = [e for e in finished if e.get("cancelled") is True]
    hidden = [e for e in finished if e.get("cancelled") is not True
              and e.get("participantsHidden") is True]
    usable = [e for e in finished if e.get("cancelled") is not True
              and e.get("participantsHidden") is not True]
    if kind == "match":
        usable = [e for e in usable if is_match(e)]
    elif kind == "training":
        usable = [e for e in usable if not is_match(e)]

    registered = [e for e in usable if e.get("registered") is True]
    rows: dict[str, dict] = defaultdict(lambda: {"invited": 0, "yes": 0, "no": 0, "none": 0,
                                                 "reg_invited": 0, "attended": 0})
    for e in usable:
        r = e.get("responses") or {}
        yes = set(r.get("acceptedIds") or [])
        no = set(r.get("declinedIds") or [])
        none = set(r.get("unansweredIds") or [])
        att = r.get("registeredAttendance") or {}
        for mid in invited(e):
            if only_members is not None and mid not in only_members:
                continue
            row = rows[mid]
            row["invited"] += 1
            row["yes"] += mid in yes
            row["no"] += mid in no
            row["none"] += mid in none
            if e.get("registered") is True:
                row["reg_invited"] += 1
                if isinstance(att, dict) and att.get(mid) == "ATTENDED":
                    row["attended"] += 1

    players = []
    for mid, row in rows.items():
        out = {
            "name": gi.member_names.get(mid, "ukjent medlem"),
            "role": ", ".join(gi.member_roles.get(mid, [])) or None,
            "invited": row["invited"],
            "yes": row["yes"],
            "no": row["no"],
            "none": row["none"],
            "yes_pct": round(100 * row["yes"] / row["invited"]) if row["invited"] else None,
        }
        if row["reg_invited"]:
            out["attended"] = f"{row['attended']}/{row['reg_invited']}"
        players.append(out)
    players.sort(key=lambda p: (p["role"] is not None, -p["invited"], -(p["yes_pct"] or 0), p["name"]))

    return {
        "events_counted": len(usable),
        "excluded": {"cancelled": len(cancelled), "hidden": len(hidden),
                     "not_finished": len(events) - len(finished)},
        "registered_coverage": f"{len(registered)}/{len(usable)}",
        "autoaccept_events": sum(1 for e in usable if e.get("autoAccept") is True),
        "matches": sum(1 for e in usable if is_match(e)),
        "note": NOTE,
        "players": players,
    }
