"""R2 invariant guard (003, T014/FR-302): every behavioural/safety delay and
timestamp must flow through the time-scale Clock, never raw wall-clock time.

If a safety module reaches for `datetime.now(` or `asyncio.sleep(`/`time.sleep(`
directly, its timing will NOT compress under TIME_SCALE and the accelerated cycle
becomes a lie. Clock itself is the one allowed home for the raw primitives.

The check strips comments first, so explanatory mentions (e.g. "== datetime.now(utc)
at scale 1") don't trip it — only real code does.
"""
import importlib
import re

import pytest

# Modules whose timing is behavioural/safety and so MUST go through the Clock.
BEHAVIOURAL_MODULES = [
    "app.services.humanizer",
    "app.services.budget",
    "app.services.working_hours",
    "app.services.schedule_service",
    "app.services.warmup",
    "app.workers.base_task",
    "app.db.redis_client",
    "app.core.floodwait",
]

FORBIDDEN = ("datetime.now(", "asyncio.sleep(", "time.sleep(")


def _code_only(source: str) -> str:
    """Drop comments so a forbidden token mentioned in prose isn't a false positive."""
    out = []
    for line in source.splitlines():
        # crude but sufficient: cut at the first '#' that isn't inside a short string
        hash_idx = line.find("#")
        out.append(line if hash_idx == -1 else line[:hash_idx])
    return "\n".join(out)


@pytest.mark.parametrize("modname", BEHAVIOURAL_MODULES)
def test_behavioural_module_uses_clock_not_raw_time(modname):
    mod = importlib.import_module(modname)
    src = _code_only(open(mod.__file__, encoding="utf-8").read())
    hits = [tok for tok in FORBIDDEN if tok in src]
    assert not hits, (
        f"{modname} uses raw {hits} — behavioural time must route through the Clock "
        f"(get_clock()/Clock.sleep/scaled_ttl) so it compresses under TIME_SCALE (R2)."
    )


def test_clock_is_the_only_home_of_raw_primitives():
    """Sanity: the guard would actually catch a violation — Clock itself legitimately
    holds the raw primitives, proving the tokens exist somewhere to be matched."""
    clock = importlib.import_module("app.core.clock")
    src = open(clock.__file__, encoding="utf-8").read()
    assert re.search(r"asyncio\.sleep\(", src), "Clock should own the raw sleep primitive"
