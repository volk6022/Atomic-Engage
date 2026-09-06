"""Смещение зоны прокси спрашивают в двух местах — и ответы расходились (план 3.6).

`schedule_service` спрашивал безопасно (`getattr(proxy, "tz_offset", 0) or 0`), а
`working_hours._flat_check` — так:

    account.proxy.tz_offset if hasattr(account, "proxy") else 0

Это выглядит защитой и защитой не является. У модели SQLAlchemy атрибут связи
существует всегда, поэтому `hasattr` истинен и при `proxy_id IS NULL`; ветка `else`
недостижима, а `None.tz_offset` роняет `AttributeError`. При этом NULL у `proxy_id` —
не поломка данных, а **поддерживаемый режим**: «работать с собственного IP машины»,
и так прямо написано в модели.

Итог: один и тот же аккаунт в одном месте молча считался живущим по UTC, а в другом
ронял задачу. Здесь проверяется не «не падает», а **согласие двух читателей**: пока
источник один, разойтись им негде.

⚠️ Отдельно стоит помнить, откуда берётся само число: `geo_match.get_proxy_info`
читает GeoIP по ХОСТУ прокси, то есть по шлюзу провайдера, а не по выходу. На живом
флоте это `-18000` (US Eastern) у аккаунтов, работающих по России. Сейчас безвредно
только потому, что у всех `work_start=0, work_end=24`.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.schedule_service import proxy_tz_offset
from app.services.working_hours import WorkingHoursGuard

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _account(proxy):
    # id=None — это и есть путь плоской проверки (`_flat_check`); с id гвардия уходит
    # в расписание, и сломанный читатель никогда не исполнялся бы.
    return SimpleNamespace(id=None, proxy=proxy, work_start=0, work_end=24)


class _FakeProxyAttr:
    """Прокси без `tz_offset` — так выглядит частично заполненная строка."""


@pytest.mark.parametrize("proxy,expected", [
    (SimpleNamespace(tz_offset=10800), 10800),
    (SimpleNamespace(tz_offset=None), 0),
    (SimpleNamespace(tz_offset=0), 0),
    (None, 0),
    (_FakeProxyAttr(), 0),
])
def test_the_single_reader_answers_for_every_shape_of_proxy(proxy, expected):
    assert proxy_tz_offset(_account(proxy)) == expected


def test_the_flat_gate_survives_an_account_without_a_proxy():
    """NULL `proxy_id` — режим «свой IP машины», а не повод уронить задачу."""
    ok, _ = WorkingHoursGuard().check(_account(None), NOW)
    assert ok is True


def test_both_readers_agree_on_the_same_account():
    """Согласие проверяется напрямую, а не по каждому месту отдельно.

    Обе стороны порознь можно объявить «правильными» — расхождение видно только
    если спросить их об одном и том же аккаунте (тот же приём, что с подписью
    аккаунта в Радаре 03.09).
    """
    for proxy in (SimpleNamespace(tz_offset=10800), None, _FakeProxyAttr()):
        account = _account(proxy)
        from app.services import working_hours as wh

        # Плоская проверка обязана считать час по тому же смещению, что и общий
        # читатель: сравниваем результат гвардии с ручным расчётом по нему.
        offset = proxy_tz_offset(account)
        local_hour = ((NOW.hour * 3600 + NOW.minute * 60 + NOW.second + offset)
                      // 3600) % 24
        account.work_start, account.work_end = local_hour, (local_hour + 1) % 24 or 24
        ok, _ = wh.WorkingHoursGuard().check(account, NOW)
        assert ok is True, (
            f"плоская проверка не согласна с общим читателем при смещении {offset}")
