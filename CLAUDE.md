# CLAUDE.md — Atomic Engage (prod)

Карта кода для LLM-агентов и контрибьюторов. **Публичный прод-репозиторий.** Telegram-
автоматизация через живой юзер-аккаунт (kurigram/pyrogram) — «fleet manager».

## Что это

FastAPI-шлюз + arq-воркеры + watchers, управляющие парком Telegram-аккаунтов
(онбординг, действия, прогрев, безопасность/гуманизация). БД — PostgreSQL (asyncpg,
alembic); брокер/лимиты — Redis. Прод-изоляция: **инстанс на клиента** (свои
Postgres/Redis/API_KEY/volume/прокси-пул; см. M4 в плане экосистемы).

## Стек

- Python 3.11+ (asyncio throughout)
- FastAPI · SQLAlchemy[asyncio] + asyncpg · alembic · arq · Redis · kurigram + TgCrypto
- GeoIP: MaxMind GeoLite2 (ASN/City) — качается при сборке, не коммитится

## Карта кода (`fleet_manager/`)

```
fleet_manager/
├── app/
│   ├── main.py             # FastAPI-энтрипоинт
│   ├── api/
│   │   ├── deps.py         # API_KEY gate (M4: per-instance ключ)
│   │   └── v1/
│   │       ├── accounts.py         # онбординг/жизненный цикл аккаунтов
│   │       ├── actions.py          # действия в TG (send_message, join, react…)
│   │       ├── proxies.py          # управление прокси (M4: per-client пул)
│   │       ├── api_credentials.py  # api_id/api_hash аккаунтов
│   │       ├── fleet.py            # /fleet/status, /health
│   │       ├── admin.py            # админ-операции
│   │       └── webhook_events.py   # доставка вебхуков
│   ├── core/               # config (Settings), clock.py (виртуальное время TIME_SCALE)
│   ├── db/                 # models.py (Account/Proxy/ApiCredential/Task/…), session.py
│   ├── services/           # telemetry.py и пр. доменные сервисы
│   ├── watchers/           # фоновые наблюдатели
│   └── workers/            # base_task.py (единый путь исполнения с гейтами безопасности)
├── config/safety.yaml      # профили безопасности/лимитов
├── data/device_combos.json # сид device-fingerprints
├── migrations/             # alembic
├── scripts/                # утилиты сессий (convert/gen/migrate/tdata_to_session)
└── tests/
```

## Безопасность выполнения (ENFORCED, не просто описано)

Единый путь воркера `app/workers/base_task.run_task` применяет по порядку: гейты
аккаунта (`prepare`: banned/sleeping/geo/datacenter-ASN/flood/working-hours) → строгий
per-account FIFO advisory-lock → дневной бюджет (`budget.check_and_consume`, per-account
+ api_id + /24-subnet) → человеческий пейсинг (`_humanize_before`, гейт `HUMANIZE_ACTIONS`)
→ вызов kurigram → телеметрия. Всё поведенческое время идёт через
`app/core/clock.get_clock()` и сжимается под `TIME_SCALE` (инвариант R2,
`tests/unit/test_time_invariant.py`).

- `HUMANIZE_ACTIONS` (Settings, default **True**; тесты выключают, чтобы не спать
  60–300 с между действиями при scale 1).
- `TelemetryEvent` (`app/services/telemetry.py`) — исследовательский инструмент
  (onboarded/action/flood/banned/warmup_tier + `accounts.first_seen_at`/`banned_at`/
  `cohort`). Без контента сообщений и PII.

## Запуск и тесты (нужны PG + Redis)

```bash
cd fleet_manager
docker compose up -d postgres redis            # postgres :5434, redis :6379
# один раз создать тест-БД: docker exec <pg> psql -U fleet_user -d fleet_db -c "CREATE DATABASE fleet_test;"
DATABASE_URL=postgresql+asyncpg://fleet_user:fleet_password@localhost:5434/fleet_test \
  REDIS_URL=redis://localhost:6379 API_KEY=change_me_in_production \
  uv run pytest tests/ -m "not accelerated"     # accelerated исключать обязательно
```

`-m "not accelerated"` обязателен: `test_full_virtual_week` — канонический ~3.5
**реальных часа** цикл (`TIME_SCALE=48`), гонять отдельно ночью. Conftest строит схему
`alembic upgrade head`; между прогонами сбрасывать public-схему `fleet_test`.

## Правила для агентов

1. Прод-клон; полный функционал — в приватном upstream `kurigram_for_n8n`.
2. Секреты/сессии (`*.session`, `tdata/`, `.env`, прокси) — только локально, в `.gitignore`.
3. Поток изменений: issue → branch → PR → ревью владельца (`contribute.md`).
