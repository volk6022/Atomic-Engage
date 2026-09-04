"""Пауза хьюманайзера не должна превышать таймаут задачи arq.

Найдено на живом проде 04.09.2026 первым же прогоном вступлений в группы (Radar
run #173, пять `join_group` на пяти аккаунтах):

    22:03:01  0.25s → join_group(task_id=1281)
    22:08:01  300.01s ! join_group failed, TimeoutError

Три соседних вступления заняли 191, 220 и 226 секунд и прошли; четвёртое упёрлось
ровно в 300.01 с — это `WorkerSettings.job_timeout`, а не Telegram. Арифметика
объясняет всё:

    inter_action_base_s   = 300 с
    inter_action_jitter   = 0.40          → пауза равномерна на [180, 420] с
    job_timeout           = 300 с         → всё, что выпало больше ~295, убивается

То есть примерно половина вступлений и приглашений обречена, и это не «иногда
Telegram тормозит», а гарантированный хвост распределения, заложенный настройками.

**Форма отказа хуже самой потери.** Убитая по таймауту задача остаётся в статусе
`executing` навсегда: закрыть её некому. А очередь аккаунта строго FIFO
(`BaseTask._claim_for_execution`), поэтому аккаунт после такой задачи **не выполняет
больше ничего** — ни вступлений, ни чтений. 04.09 так встал аккаунт 2, и разблокировал
его только ручной `docker restart` воркера.

Лечится `recover_orphaned_tasks` — она для этого и написана, — но она зовётся
**только из `on_startup`**. Пока никто не перезапустит воркер, аккаунт стоит.
Отсюда второе требование этого файла: восстановление обязано идти по расписанию.

Тесты структурные и инфраструктуры не требуют: проверяется согласованность
констант между собой, а не поведение Telegram.
"""
from __future__ import annotations

import pytest

from app.core.humanizer_config import HumanizerConfig
from app.workers import recovery
from app.workers.arq_settings import WorkerSettings


def _max_inter_action_seconds(cfg: HumanizerConfig) -> float:
    """Верхняя граница паузы: база плюс джиттер (пол снизу на неё не влияет)."""
    return cfg.inter_action_base_s * (1.0 + cfg.inter_action_jitter)


def test_job_timeout_outlives_the_longest_humanizer_pause():
    """Задача обязана переживать самую длинную паузу, которую сама же и берёт.

    Запас нужен сверх паузы: после неё идёт подключение клиента через прокси и сам
    вызов Telegram — на проде это ещё десятки секунд.
    """
    longest = _max_inter_action_seconds(HumanizerConfig())
    assert WorkerSettings.job_timeout > longest, (
        f"пауза перед действием доходит до {longest:.0f} с, а job_timeout "
        f"{WorkerSettings.job_timeout} с — задача убивается своей же паузой")
    assert WorkerSettings.job_timeout >= longest + 60, (
        "после паузы ещё подключение через прокси и вызов Telegram; таймаут без "
        "запаса просто сдвигает ту же аварию на минуту")


def test_recovery_lease_outlives_the_job_timeout():
    """Лиза восстановления обязана быть длиннее таймаута задачи.

    Иначе `recover_orphaned_tasks` сбросит в очередь задачу, которая прямо сейчас
    честно работает, и одно и то же действие уйдёт в Telegram дважды. Условие
    записано в докстринге `recovery.py` («The lease MUST exceed job_timeout»), но
    ничем не проверялось — а обе константы правятся независимо.
    """
    assert recovery.DEFAULT_LEASE_SECONDS > WorkerSettings.job_timeout, (
        f"лиза {recovery.DEFAULT_LEASE_SECONDS} с не больше таймаута "
        f"{WorkerSettings.job_timeout} с — восстановление будет дублировать "
        f"работающие задачи")


def test_orphan_recovery_runs_on_schedule_and_not_only_at_startup():
    """Задача, убитая таймаутом, блокирует очередь аккаунта до перезапуска воркера.

    `recover_orphaned_tasks` умеет это чинить и делает это идемпотентно (лиза плюс
    запертый переход), но зовётся только из `on_startup`. Значит между двумя
    выкатками аккаунт может простоять сколько угодно — 04.09 простоял десять минут
    и не сдвинулся бы вовсе, если бы не ручной рестарт.
    """
    scheduled = {getattr(job, "name", None) or getattr(job.coroutine, "__name__", "")
                 for job in WorkerSettings.cron_jobs}
    assert any("recover_orphaned" in name for name in scheduled), (
        "recover_orphaned_tasks нет среди cron_jobs: осиротевшая задача держит "
        f"очередь аккаунта до перезапуска. Сейчас по расписанию: {sorted(scheduled)}")


@pytest.mark.parametrize("task_type", ["join_group", "invite_to_group"])
def test_the_delay_applies_to_the_actions_that_actually_hit_the_ceiling(task_type):
    """Проверка, что тест сторожит именно те действия, на которых пауза берётся.

    Если пауза перед действием однажды переедет на другие типы задач, этот файл
    обязан об этом узнать: он опирается на то, что потолок таймаута трогают
    `join_group` и `invite_to_group` (`base_task._humanize_before`).
    """
    import inspect

    from app.workers import base_task

    source = inspect.getsource(base_task._humanize_before)
    assert task_type in source, (
        f"{task_type} больше не берёт inter_action_delay — перечитай, какие "
        f"действия упираются в job_timeout, и поправь этот файл")
