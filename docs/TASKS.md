# Что осталось сделать — edu-ai

Контекст: [STATUS.md](STATUS.md). ТЗ: [tz/TZ_edu.md](tz/TZ_edu.md).
**P0** — чтобы модуль вообще запускался; **P1** — для пилота; **P2** — v2/v3.

Эталон для всего, что ещё не написано под edu, — одноимённые файлы в
[clinic-ai](https://github.com/byagge/clinic-ai) (ветка `claude/focused-planck-t7zylo`).

## P0 — собрать работающий модуль

- [ ] **P0-1. Миграция БД.**
  *Как:* `cd backend && DATABASE_URL=... python -m alembic revision --autogenerate -m init`; переименовать в `0001_init.py`; перенести из `clinic-ai/backend/alembic/versions/0001_init.py` три ручные части: `CREATE EXTENSION btree_gist/pg_trgm`, функцию `_enable_rls()` (список `TENANT_TABLES` заменить на таблицы ядра + edu: `courses, teachers, groups, students, guardians, trial_lessons, trial_bookings, enrollments, invoices, lessons, attendance, level_tests, test_results, referrals, waitlist`), `_search_indexes()` (без индекса по `services`). Заменить `arix.db.base.*` типы на `postgresql.JSONB`/`UUID`. Проверить: `alembic upgrade head`, затем RLS-тест (взять `clinic-ai/backend/tests/test_tenancy.py`).
- [ ] **P0-2. Перенести API-слой на edu.**
  *Как:* файлы `api/webhooks.py`, `api/panel.py`, `api/miniapp.py`, `api/tenants.py`, `bots/staff.py`, `main.py`, `eval/runner.py`, `cli.py`, `seed/imports.py` содержат импорты `arix.verticals.clinic.*`.
  — `webhooks.py`: заменить `from arix.verticals.clinic.adapter import build_runtime` на `...edu.adapter`; убрать клиническую оплату, добавить закрытие счёта по вебхуку (см. P0-5).
  — `panel.py`: эндпоинты `/courses`, `/groups`, `/students`, `/enrollments`, `/invoices` (отметка оплаты наличными/переводом — кнопка админа), `/lessons`, `/trials`, `/attendance`, вместо `/services`, `/doctors`, `/visits`.
  — `bots/staff.py`: роль `teacher` (`/today`, кнопки `tb:*` вердикт пробного и `at:*` посещаемость — обработчики уже в `verticals/edu/adapter.py`), владелец `/debts`, `/groups`, `/trials`, `/risk`, `/pause_bot`.
  — `main.py`/`worker.py` уже выбирают вертикаль по `settings.vertical` — задать `VERTICAL=edu` в `.env.example` и compose.
  Критерий: `python -c "import arix.main"` проходит, `GET /health/ready` зелёный.
- [ ] **P0-3. Шаблоны центров.**
  *Как:* создать `backend/arix/seed/packs/edu_language.yaml`, `edu_exam_prep.yaml`, `edu_kids.yaml`, `edu_it.yaml` по образцу `clinic-ai/.../packs/clinic_dental.yaml`. Секции: `meta` (code, vertical: edu, rubrics), `config` (валюта UZS/KZT, `billing`, `trial`, `features`, тексты), `courses`, `teachers`, `groups` (с `schedule: {days: [0,2,4], time: "18:00"}`, `capacity`, `min_to_start`, `starts_in_days`), `level_test` (`questions: [{q, options, answer}]`, `bands: [{min,max,level}]` — 12–15 вопросов, A1–C1), `kb` (40–60 вопросов: оплата, рассрочка, возврат дословно с `verbatim: true`, пробное, документы, ваучер KZ), `sequences`. Загрузчик `seed/template_packs.py` и `seed/provision.py` уже умеют все эти секции. Критерий: `create_tenant(template_code="edu_language")` создаёт курсы, группы, тест и KB.
- [ ] **P0-4. Тесты вертикали (минимум для P0).**
  *Как:* `tests/test_billing.py` — чистая арифметика без БД: `prorate` (полный месяц, с 20-го, скидка, окончание в середине, вне периода, режим `course`), `due_date_for` (`by_start`, фикс. день, февраль), `build_ladder`/`ladder_runs`. `tests/test_enrollment.py` — **50 параллельных `enroll` в группу на 1 место → ровно один успех**; повтор с тем же `idempotency_key`; `AlreadyEnrolled`. `tests/test_trials.py` — то же для вместимости пробного. `tests/test_attendance.py` — риск ухода: 2 пропуска подряд, 3 за месяц, не чаще раза в неделю. `tests/test_leveltest.py` — подсчёт и уровень. Взять фикстуры из `clinic-ai/backend/tests/conftest.py` (заменить шаблон на `edu_language`).
- [ ] **P0-5. Оплата закрывает счёт и останавливает лестницу.**
  *Как:* в вебхуке платежа (`mark_paid` в `payments.py`) по `payment.entity_ref` найти `Invoice` → `billing.mark_invoice_paid` → `scheduling.cancel_invoice_ladder` и `jobs.cancel("trial_followup:")` для ученика; событие `invoice_paid` с источником (`trial_followup`, `miniapp`, `referral`). Админская кнопка «оплачено наличными» делает то же. Тест: оплата → счёт `paid`, отложенные `pay_reminder` отменены.
- [ ] **P0-6. Подключить ежедневные задания.**
  *Как:* в `scheduler/worker.py::Worker._arm_daily` не должно быть клинических допущений; добавить для edu `lessons_rollout` (ежедневно 03:00), `group_start_announce` (ежедневно 10:00), `reactivation` (1 августа и 1 декабря), `parent_report` (1-е число, если `parent_reports`), `invoice_create` по зачислению (уже ставится из `schedule_enrollment_jobs`), `next_level_offer` (за 14 дней до конца уровня — ставить из `schedule_enrollment_jobs`, когда известен `end_date`). Реализовать через реестр: добавить в `scheduler/registry.py` функцию `register_daily(arm)` и вызывать вертикальные `arm` из `housekeeping`, чтобы файл `worker.py` остался общим с `clinic-ai`.
- [ ] **P0-7. Запустить `docker compose up` и пройти демо** (см. P0-1…P0-4 в [clinic-ai/docs/TASKS.md](https://github.com/byagge/clinic-ai/blob/claude/focused-planck-t7zylo/docs/TASKS.md)) — они общие для обоих репозиториев и ещё не выполнены.

## P1 — для пилота

- [ ] **P1-1. Eval-набор ≥ 60 диалогов (RU/UZ/KZ).**
  *Как:* создать `arix/eval/cases.py` по образцу клиники. Категории из ТЗ B11: подбор курса, тест уровня, пробный, торг, «гарантируете 7.0?», возврат денег, родитель спрашивает про чужого ребёнка, промпт-инъекции, долги и баланс. Приёмка: 0 обещаний баллов, 0 выдуманных цен и скидок, 0 утечек данных другого ученика, 100% handoff на возврат и торг. Для кейсов «чужой ученик» нужны диалоги с двумя контактами (расширить `EvalCase` полем `contact`). Адаптировать `eval/runner.py` под `edu.adapter` и `_reset_calendar` (очищать пробные/зачисления между кейсами).
- [ ] **P1-2. Бот учителя и кнопки урока.**
  *Как:* команды `/today`, `/lessons`; «урок отменён/перенесён» → `attendance.cancel_lesson` + задание `lesson_cancelled_notice` (уведомление всей группе ≤ 1 мин, ТЗ У11). Посещаемость: сейчас накопление «кого не было» хранится в памяти процесса (`EduVertical._pending`) — перенести в `conversation.scratch` или таблицу, иначе при нескольких репликах/перезапуске отметки теряются.
- [ ] **P1-3. Импорт Excel для edu.**
  *Как:* переписать `seed/imports.py` под колонки ТЗ B8: `courses.xlsx`, `groups.xlsx`, `students.xlsx` (ученик, родитель, телефон, группа, долг). Импортёр должен возвращать список проблем (как в клинике), создавать `Student` + `Guardian` + `Enrollment` + открытый `Invoice` на сумму долга. Команда `python -m arix.cli import-prices` заменить на `import-courses/-groups/-students`. Тесты на разбор колонок.
- [ ] **P1-4. Панель.**
  *Как:* заменить страницы `frontend/src/pages/*` на: «Сводка» (отчёт + воронка), «Группы» (наполненность, `min_to_start`), «Ученики», «Долги» (список с отметкой оплаты), «Пробные», «Диалоги», «База знаний», «Каналы». Каркас (`api.ts`, `ui.tsx`, `Dialogs`, `Knowledge`, `Channels`, `Login`) переиспользуется.
- [ ] **P1-5. Mini App (родитель/ученик/учитель).**
  *Как:* `frontend/src/pages/MiniApp.tsx` — сейчас клиническая запись. Нужны: расписание и ближайший урок, баланс и счета, оплата, посещаемость, результат теста, запись на пробный; для учителя — группы на сегодня и посещаемость. Эндпоинты `api/miniapp.py` — по принципу: контакт всегда из подписи `initData`, ученики только из `students_of_contact`.
- [ ] **P1-6. Платежи Click / Payme / Kaspi** — как P2-1 в клинике; плюс связка «счёт ↔ платёж» из P0-5. Click: `Prepare/Complete`; Payme: JSON-RPC Merchant API (автосписания Subscribe API — v3).
- [ ] **P1-7. Проверка текстов UZ/KZ носителем**, особенно лестницы долгов и дожима (`templates/defaults.py`). Языки по умолчанию для UZ-центра — `uz`, `ru`; детерминированный движок сейчас отвечает по-русски — вынести реплики `playbook.py` в `templates/defaults.py` с языками.
- [ ] **P1-8. Eval на Claude и правка промпта** (`agent/prompt.py`, блок `SAFETY_BLOCK_EDU`); проверить, что `postcheck` с `vertical="edu"` ловит обещания баллов (`SCORE_PROMISE_RE`) и не блокирует нормальные ответы о цене.
- [ ] **P1-9. Документация под edu:** переписать `docs/ARCHITECTURE.md`, `DEMO.md`, `ONBOARDING.md`, `OPERATIONS.md`, `TESTING.md` (скопированы из клиники и местами описывают врачей и визиты); вернуть полноценный CI (миграция + все тесты + eval).
- [ ] **P1-10. Демо-генератор для центров:** `api/tenants.py::generate_demo` выбирает шаблон по рубрикам 2ГИС — добавить рубрики для образовательных центров (языковые курсы, репетиторы, подготовка к экзаменам, IT-школы, детские центры) и шаблон по умолчанию `edu_language`; в демо — курсы-заглушки с пометкой «цены примерные».

## P2 — v2 / v3

- [ ] **P2-1. Отчёт родителю раз в месяц** (У8) — задание `parent_report` есть; добавить оценки и комментарий учителя (поле в `Lesson`/`Attendance`), включение `features.parent_reports`.
- [ ] **P2-2. Реферальная программа** (У10) — личная ссылка `?start=ref_<code>` → `Referral`, награда по первой оплате друга (`reward` из конфига), статистика в отчёте.
- [ ] **P2-3. Продление и следующий уровень** (У9) — довести `next_level_offer` до подбора по `level_to` курса.
- [ ] **P2-4. Лист ожидания → уведомление** при освобождении места/открытии группы.
- [ ] **P2-5. Автосписания с карт** (Payme Subscribe, токенизация) — v3.
- [ ] **P2-6. Интеграции:** Modme (уточнить API у вендора; до этого — ежедневный Excel-обмен), AlfaCRM REST (клиенты, группы, уроки, оплаты) — v3.
- [ ] **P2-7. Ваучер на допобразование (KZ, Шымкент)** — тексты в KB; интеграция с платформой ваучера не нужна сейчас.
- [ ] **P2-8. Инстанс в РК для KZ-центров**, 2FA в панели, автоматическая анонимизация — как P1 в клинике.

## Как проверять

`make lint`, `make test`, `make eval` зелёные; для каждой новой логики — тест в `backend/tests/`, для диалогов — кейс в eval. Изменения общего ядра (`agent/`, `core/`, `channels/`, `domain/`, `scheduler/`, `kb/`) делать в одном репозитории и переносить `scripts/sync-core.sh` (см. [CORE_SYNC.md](CORE_SYNC.md)).
