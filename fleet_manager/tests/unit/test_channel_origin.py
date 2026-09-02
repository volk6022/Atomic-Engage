"""The channel post behind an auto-relayed thread root, on both ingest paths.

Why this field exists. A lead found in a linked discussion group is a comment under a
channel post. Linking to the comment alone opens the *group* — and for a reader who is
not a member of that group, that is a dead end. To offer "open the post this comments
on" the consumer needs the id of the post inside the channel, and that is exactly what
kurigram hides behind `forward_origin` rather than a flat `forward_from_message_id`:
reading the flat name yields None forever, silently.

Realtime and history land in one table downstream, so the two builders are tested
together, field for field. A field present on only one path is a field the consumer
cannot rely on.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.watchers._message_payload import build_incoming_message_payload
from app.workers._read_helpers import build_post

CHANNEL_ID = -1001627075588
POST_ID = 4242


def _origin(kind: str, *, chat_id=CHANNEL_ID, message_id=POST_ID):
    """A `MessageOrigin` double. `.type.value` is what kurigram's AutoName enum yields."""
    return SimpleNamespace(type=SimpleNamespace(value=kind),
                           chat=SimpleNamespace(id=chat_id),
                           message_id=message_id)


def _msg(**kw):
    chat = SimpleNamespace(id=-1002, type=SimpleNamespace(value="supergroup"),
                           username="chat", title="Обсуждение")
    base = dict(from_user=None, sender_chat=None, chat=chat, id=7,
                text="комментарий", caption=None,
                date=datetime.now(timezone.utc), edit_date=None,
                media=None, views=None, outgoing=False,
                reply_to_message=None, forward_origin=None)
    base.update(kw)
    return SimpleNamespace(**base)


BUILDERS = [
    pytest.param(lambda m: build_incoming_message_payload(7, m), id="realtime"),
    pytest.param(build_post, id="history"),
]


@pytest.mark.parametrize("build", BUILDERS)
def test_channel_origin_is_surfaced(build):
    p = build(_msg(forward_origin=_origin("channel"), automatic_forward=True))
    assert p["forward_from_chat_id"] == CHANNEL_ID
    assert p["forward_from_message_id"] == POST_ID


@pytest.mark.parametrize("build", BUILDERS)
def test_a_plain_message_carries_the_field_as_none_not_absent(build):
    """Отсутствующий ключ и ключ со значением None — разные вещи для потребителя:
    первый читается как «старая версия Engage», второй как «пересылки не было»."""
    p = build(_msg())
    assert p["forward_from_chat_id"] is None
    assert p["forward_from_message_id"] is None


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("kind", ["user", "chat", "hidden_user", "import"])
def test_a_non_channel_origin_is_ignored(build, kind):
    """Пересылка от человека — не пост канала. Ссылку на пост из неё не построить,
    и подставлять туда чужой id опаснее, чем не дать ссылки вовсе."""
    p = build(_msg(forward_origin=_origin(kind)))
    assert p["forward_from_chat_id"] is None
    assert p["forward_from_message_id"] is None


@pytest.mark.parametrize("build", BUILDERS)
def test_a_malformed_origin_does_not_crash_ingest(build):
    """Приём не имеет права падать из-за формы одного поля: одно сообщение уронило бы
    весь пакет истории."""
    p = build(_msg(forward_origin=SimpleNamespace(type=None)))
    assert p["forward_from_chat_id"] is None
    assert p["forward_from_message_id"] is None


@pytest.mark.parametrize("build", BUILDERS)
def test_the_flat_legacy_attribute_is_not_what_is_read(build):
    """Регрессия на саму ловушку: у объекта есть плоское поле и нет forward_origin.

    Если кто-то однажды «упростит» чтение до `forward_from_message_id`, тест на живых
    объектах останется зелёным, а здесь станет видно, что читается не то.
    """
    p = build(_msg(forward_from_message_id=999, forward_from_chat_id=-1))
    assert p["forward_from_message_id"] is None
