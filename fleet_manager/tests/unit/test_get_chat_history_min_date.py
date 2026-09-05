"""The `min_date` early-stop must survive kurigram's naive datetimes (§2 contract).

`min_date` has been declared in `GetChatHistoryPayload`, documented in the worker
docstring and listed in the read-action contract since the action was written — and
it has never once worked. kurigram hands back `Message.date` as a **naive** datetime,
`_parse_iso` always returns an **aware** one, and `mdate < min_date` on that pair
raises `TypeError: can't compare offset-naive and offset-aware datetimes` on the very
first message of the very first page.

Found live on 2026-09-05, not by reading: the first automatic backfill from Radar's
queue asked for a month of history, and task 1294 came back
`{'error': "can't compare offset-naive and offset-aware datetimes"}` two seconds
later. Nothing above noticed — a failed task sends no webhook, so the requester's
queue item simply sat in `running` until its lease expired.

The comparison therefore moves out of the closure into a named helper, because a
branch that cannot be called from a test is a branch that ships broken: this one
passed review, image build and deploy while being dead on arrival.
"""
from datetime import datetime, timezone

from app.workers.get_chat_history import _reached_min_date

AWARE = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def test_naive_message_date_is_compared_as_utc():
    """The real shape: kurigram's date is naive, the payload's boundary is aware."""
    assert _reached_min_date(datetime(2026, 8, 5, 12, 0), AWARE) is True
    assert _reached_min_date(datetime(2026, 8, 7, 12, 0), AWARE) is False


def test_aware_message_date_still_works():
    """Should kurigram ever start returning aware dates, nothing here changes."""
    assert _reached_min_date(
        datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc), AWARE) is True
    assert _reached_min_date(
        datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc), AWARE) is False


def test_no_boundary_means_never_stop():
    """No `min_date` in the payload: the scan runs to the requested limit."""
    assert _reached_min_date(datetime(2026, 1, 1, 0, 0), None) is False


def test_missing_message_date_does_not_stop_the_scan():
    """A post without a date must not end the page early — losing the rest of it
    is a worse answer than keeping one undated post."""
    assert _reached_min_date(None, AWARE) is False


def test_the_boundary_itself_is_kept():
    """`min_date` is inclusive: a post exactly on the boundary is inside the window,
    and the caller asking for 'the last month' means the month, not month-minus-one."""
    assert _reached_min_date(datetime(2026, 8, 6, 12, 0), AWARE) is False
