# ARIX — ИИ-менеджер для учебных центров

Отвечает 24/7 в Telegram и WhatsApp, подбирает курс, проводит тест уровня,
записывает на пробный, дожимает после пробного, напоминает об оплате по лестнице
долгов, сообщает родителям о пропусках и каждое утро присылает владельцу отчёт.

Реализация ТЗ «ИИ-менеджер ARIX — ядро + модуль „Учебный центр“» v1.0
([docs/tz/TZ_edu.md](docs/tz/TZ_edu.md)). Ядро общее с репозиторием
[`clinic-ai`](https://github.com/byagge/clinic-ai).

> **Статус: работа в процессе, целиком запустить пока нельзя.**
> Написано ядро (скопировано из `clinic-ai`) и доменная часть модуля
> (`backend/arix/verticals/edu/`). Не написаны: миграция, YAML-шаблоны центров,
> API-слой для edu, eval и тесты вертикали.
> Что сделано по ТЗ — [docs/STATUS.md](docs/STATUS.md);
> что осталось и как делать — [docs/TASKS.md](docs/TASKS.md).

## Что уже есть

- **Ядро** (общее): мультитенантность с RLS, конфиг вместо кода, три слоя
  guardrails, PII-маскирование перед LLM, durable-планировщик, Telegram и
  Telegram Business, WhatsApp через Baileys (без оплаты Meta), два «мозга»
  (Claude / детерминированный).
- **Модуль «Учебный центр»**:
  - группы с вместимостью — место берётся под `SELECT … FOR UPDATE`, последнее
    место не достаётся двоим;
  - счета с пропорцией за неполный месяц, скидкой, режимами `monthly/course`,
    лестница долгов `[-3, 0, 3, 7, 10]` с передачей человеку на последнем шаге;
  - пробные занятия, вердикт учителя в один тап → цепочка дожима;
  - посещаемость «отметь отсутствующих», уведомление родителю, флаг риска ухода;
  - тест уровня кнопками, подбор курса по возрасту и цели;
  - доступ к балансу только у плательщика из `guardians` или у взрослого ученика;
  - утренний отчёт владельцу (группы, долги, риск ухода, пробные).

## Структура

```
backend/arix/
├── core, db, channels, domain, agent, kb, scheduler   общее ядро (см. docs/CORE_SYNC.md)
├── verticals/edu/      модели, billing, enrollment, trials, attendance, leveltest,
│                       tools, playbook, scheduling, handlers, reports, adapter
├── seed/               провижининг (edu), загрузчик шаблонов
├── api/, bots/         ⚠️ пока клинические — требуют переноса на edu
waweb/                  WhatsApp (Baileys) сайдкар
frontend/               ⚠️ пока клиническая версия панели
```

## Разработка

```bash
cd backend
pip install -e ".[dev]"
python -m pytest -q tests/test_guardrails.py tests/test_pii.py tests/test_timeutil.py
```

Остальное (миграции, `make demo`, eval) заработает после задач P0 из
[docs/TASKS.md](docs/TASKS.md). Документы `docs/ARCHITECTURE.md`, `DEMO.md`,
`ONBOARDING.md`, `OPERATIONS.md` скопированы из клиники и будут переписаны под
edu (задача P1-9).
