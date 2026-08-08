# Contributing — Atomic Engage

Прод-репозиторий (Telegram-автоматизация через личные аккаунты). Все изменения — через ревью
владельца (@volk6022). Прямой push в `main` запрещён.

## 1. Поток работы

1. **Issue.** Задача/баг — отдельным issue до начала. Ссылайся в PR.
2. **Branch.** От свежего `main`: `feat/<коротко>`, `fix/<коротко>`, `docs/<коротко>`, `chore/<коротко>`.
3. **Commits.** Атомарные, [Conventional Commits](https://www.conventionalcommits.org): `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`.
4. **Pull Request.** PR в `main` со ссылкой на issue и описанием (что / зачем / как проверял). CI зелёный.
5. **Review.** Обязательный аппрув владельца. Merge — squash, владельцем.

## 2. Definition of Done (чеклист PR)

- [ ] `cd fleet_manager && docker compose up -d postgres redis` + запуск воркера не падает.
- [ ] `pytest -m "not accelerated"` зелёный (нужны PG + Redis, см. `CLAUDE.md`).
- [ ] Нет секретов/сессий в диффе (`git diff --staged` проверен — см. §4).
- [ ] Гейты безопасности аккаунтов (`base_task`) не ослаблены без явного решения.
- [ ] Доки обновлены при смене поведения гейтвея/провижнера.

## 3. Безопасность аккаунтов — критично

Это сервис, который **действует от лица живых Telegram-аккаунтов**. Бан аккаунта = потеря
актива и риск для клиента.

- **Не ослабляй гейты воркера** (`base_task`): троттлинг, задержки, лимиты касаний. Любое
  изменение rate/поведения — только с явного решения владельца.
- Гео-защита (GeoLite mmdb, блок датацентр-ASN) — не отключай в проде.
- Прод ≠ дев: полный функционал в приватном upstream `kurigram_for_n8n`.

## 4. Секреты и сессии

- **Только локально, в `.gitignore`:** `.env`, `*.session`, `tdata/`, `raw-tg-sessions/`,
  прокси-листы, API-ключи, bot-токены.
- Перед `git add -A` — обязательно `git diff --staged` и проверка, что живые TG-сессии не
  утекают (частая ошибка: `raw-tg-sessions/` не в `.gitignore`).
- Уязвимость → напрямую владельцу (Telegram), не в публичный issue.

## 5. Локальный старт

```bash
cd fleet_manager
cp .env.example .env
docker compose up -d postgres redis
```

⚠️ Один bot-токен = один long-poll консюмер. Локальный стек с прод-токеном даст **409** —
используй отдельный dev-токен.

## 6. Что должно быть настроено (для мейнтейнера)

- Branch protection на `main`: require PR + CI, no direct/force push.
- `CODEOWNERS` → владелец.
- Секрет-сканер в CI (gitleaks) + отдельная проверка на `*.session`/`tdata`.
- Шаблоны Issue/PR в `.github/`.
