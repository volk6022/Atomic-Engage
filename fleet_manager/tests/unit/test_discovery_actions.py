"""Два действия разведки каналов: похожие каналы и поиск публичных чатов.

Постановка Ивана от 04.09.2026 — автоматический поиск похожих каналов и групп как
отдельный сценарий Радара. Просьба не новая: Андрей просил look-alike каналы ещё
19.08 («в неспешном режиме, чтобы не искать их руками»).

Радару искать нечем. В Engage сейчас есть только чтения по уже известному адресу
(`get_chat_info`, `get_chat_history`, `get_chat_admins`, `get_dialogs`,
`resolve_username`) — ни одного действия, которое возвращало бы НОВЫЕ адреса.

⚠️ При этом в лимитах уже больше года лежит строка `search_public_chat: 50`, под
которую нет ни воркера, ни типа задачи, ни белого списка — то есть объявлен потолок
для действия, которого не существует. Это ровно та форма, на которой мы обжигались
не раз: табличка есть, мира за ней нет. Здесь она либо получает код, либо уходит.

Возможности самой библиотеки проверены на kurigram 2.2.22, установленной в этом
репозитории:

* `client.get_similar_channels(chat_id)` → `channels.GetChannelRecommendations`,
  отдаёт список `Chat`; для не-канала бросает `ValueError`;
* `raw.functions.contacts.Search` — поиск публичных чатов и людей по строке.

Тесты структурные: они проверяют, что действие ЗАРЕГИСТРИРОВАНО во всех местах, где
его отсутствие делает его недоступным. Живая проба против Telegram — отдельный шаг
приёмки, её тестом не заменить.
"""
from __future__ import annotations

import inspect

import pytest

from app.core import safety_defaults
from app.core.constants import READ_ACTIONS, TaskType
from app.workers import arq_settings

NEW_ACTIONS = ["get_similar_channels", "search_public_chats"]


@pytest.mark.parametrize("action", NEW_ACTIONS)
def test_action_has_a_task_type(action):
    values = {t.value for t in TaskType}
    assert action in values, (
        f"{action} нет в TaskType — задачу такого типа невозможно завести")


@pytest.mark.parametrize("action", NEW_ACTIONS)
def test_action_is_read_only_and_warmup_exempt(action):
    """Оба действия — чтения: аккаунт ничего не публикует и никуда не вступает.

    Значит гейт прогрева их не касается (как и остальные `get_*`), но дневной бюджет
    чтений — касается: разведка по своей природе бурстовая, и без потолка одна
    неудачная настройка выест лимиты всего флота за час.
    """
    values = {t.value for t in READ_ACTIONS}
    assert action in values, f"{action} должен быть в READ_ACTIONS"
    assert action in safety_defaults.READ_LIMITS, (
        f"у {action} нет дневного потолка в READ_LIMITS")
    assert safety_defaults.READ_LIMITS[action] > 0, (
        f"потолок {action} нулевой — действие фактически выключено")


def test_the_orphan_limit_is_gone():
    """`search_public_chat` в единственном числе — потолок без действия.

    Он лежит в лимитах с самого начала и не применяется ни к чему: воркера с таким
    именем нет. Строка обязана либо получить код, либо исчезнуть — иначе следующий
    читатель снова примет её за существующую возможность.
    """
    assert "search_public_chat" not in safety_defaults.READ_LIMITS, (
        "лимит search_public_chat (ед. ч.) остался сиротой: действие называется "
        "search_public_chats")


@pytest.mark.parametrize("action", NEW_ACTIONS)
def test_worker_is_registered_in_arq(action):
    names = {getattr(f, "__name__", "") for f in arq_settings.FUNCTIONS}
    assert action in names, (
        f"воркер {action} не зарегистрирован в arq_settings.FUNCTIONS — задача будет "
        f"вечно висеть в queued")


@pytest.mark.parametrize("action", NEW_ACTIONS)
def test_action_is_accepted_by_the_gateway(action):
    """Белый список в `create_action` закрытый: незнакомое действие отвергается 422.

    Проверяем по исходнику ручки, а не запросом: поднимать ради этого приложение с
    базой значит превратить структурную проверку в интеграционную.
    """
    from app.api.v1 import actions

    source = inspect.getsource(actions.create_action)
    assert f'"{action}"' in source, (
        f"{action} нет в белом списке create_action — гейт ответит 422")


@pytest.mark.parametrize("action", NEW_ACTIONS)
def test_worker_charges_its_own_read_budget(action):
    """Бюджет списывается по имени действия, а не «как-нибудь».

    `run_task(..., read_action=...)` — единственное место, где чтение попадает под
    свой потолок. Забыть этот аргумент значит получить действие без лимита вовсе,
    и заметить это можно будет только по забаненному аккаунту.
    """
    module = __import__(f"app.workers.{action}", fromlist=[action])
    source = inspect.getsource(module)
    assert f'read_action="{action}"' in source or f"read_action='{action}'" in source, (
        f"воркер {action} не списывает свой бюджет чтений через read_action")


def test_similar_channels_returns_addresses_we_can_act_on():
    """Ответ обязан нести то, чем Радар потом пользуется.

    Кандидат без `username` бесполезен: вступить в него нельзя (только заявкой по
    ссылке), карточку по peer_id чужого канала аккаунт тоже не спросит. Поэтому в
    ответе обязаны быть и адрес, и подпись, и размер — по ним кандидат отсеивается
    ДО того, как его покажут человеку или отдадут модели.
    """
    from app.workers import get_similar_channels as mod

    source = inspect.getsource(mod)
    for field in ("username", "title", "members", "peer_id"):
        assert field in source, (
            f"в ответе get_similar_channels нет поля {field}: кандидат без него "
            f"нельзя ни отсеять, ни подключить")


def test_search_takes_a_query_and_bounds_it():
    """Поиск обязан ограничивать выдачу и не звать Telegram с пустой строкой."""
    from app.workers import search_public_chats as mod

    source = inspect.getsource(mod)
    assert "query" in source, "поиск без строки запроса — это не поиск"
    assert "limit" in source, (
        "выдача поиска обязана быть ограничена: без потолка один запрос вернёт "
        "столько кандидатов, сколько отдаст Telegram, и все они пойдут в проверки")
