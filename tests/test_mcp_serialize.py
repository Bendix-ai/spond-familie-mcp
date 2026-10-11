"""Spond-MCP: tidsformat, beskjæring og forhåndsvisningslager."""

from __future__ import annotations

import pytest

from spond_familie.safety import PreviewStore, SafetyError
from spond_familie.serialize import cap, clip, fmt_dt, prune


def test_fmt_dt_oslo_summer_and_winter():
    assert fmt_dt("2026-06-01T20:33:00Z") == "2026-06-01 (man) 22:33"
    assert fmt_dt("2026-12-24T16:00:00Z") == "2026-12-24 (tor) 17:00"
    assert fmt_dt(None) is None


def test_prune_keeps_false_and_zero():
    assert prune({"a": None, "b": "", "c": [], "d": {}, "e": False, "f": 0,
                  "g": [{"x": None}, 1]}) == {"e": False, "f": 0, "g": [1]}


def test_clip_and_cap():
    assert clip("x" * 10, 5) == "xxxxx …"
    assert clip("  kort ", 5) == "kort"
    assert cap([1, 2, 3], 2) == ([1, 2], True)


def test_preview_store_is_single_use_and_param_bound():
    store = PreviewStore()
    pid = store.create("rsvp", {"a": 1})
    with pytest.raises(SafetyError, match="endret"):
        store.consume(pid, "rsvp", {"a": 2})
    pid = store.create("rsvp", {"a": 1})
    with pytest.raises(SafetyError, match="endret"):
        store.consume(pid, "send_message", {"a": 1})
    pid = store.create("rsvp", {"a": 1})
    store.consume(pid, "rsvp", {"a": 1})
    with pytest.raises(SafetyError, match="brukt"):
        store.consume(pid, "rsvp", {"a": 1})
    with pytest.raises(SafetyError, match="preview_id"):
        store.consume(None, "rsvp", {"a": 1})
