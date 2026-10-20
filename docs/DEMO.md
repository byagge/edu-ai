# Как показать демо

Порты сдвинуты относительно clinic-ai, чтобы оба стека стояли на одной машине.
Живые Telegram, Telegram Business, WhatsApp и Claude в этой среде не
подключались: нет токенов и нет Docker-демона. Команда `make demo` рассчитана
на машину с Docker.

## Вариант 1. Оффлайн, без каналов и без ключей

```bash
cp .env.example .env
make demo
```

Показывать:

1. **Панель** — http://localhost:3002, вход `owner@demo-language.local` /
   `demo12345`. Интерфейс панели пока клинический (задача P1-4); API уже
   учебное: курсы, группы, счета, пробные.
2. **API** — http://localhost:8001/docs. Заголовок сервиса: «ИИ-менеджер
   учебного центра».
3. **Стартовый eval** — пять диалогов, которые не должны зависеть от модели
   (возврат, торг, «гарантируете 7.0?», цена курса, неизвестная цена):
   ```bash
   cd backend && python -m arix.eval.runner
   ```
   Это не пилотная приёмка из 60 диалогов. Её ещё нет (задача P1-1).
4. **Одно место в группе** — `cd backend && python -m pytest
   tests/test_enrollment.py -q -k fifty` (50 одновременных зачислений в группу
   на 1 место → ровно одно).

## Вариант 2. С живым Telegram

Нужен публичный HTTPS-адрес для вебхуков.

```bash
cloudflared tunnel --url http://localhost:8001
```

1. В `.env`: `PUBLIC_BASE_URL=https://<адрес туннеля>`,
   `TELEGRAM_SHARED_BOT_TOKEN=<токен от @BotFather>`,
   `TELEGRAM_WEBHOOK_SECRET=<любая строка>`.
2. `make up`
3. Подключить канал:
   ```bash
   cd backend
   python -m arix.cli connect-telegram --slug demo-language --token <BOT_TOKEN>
   ```
   Команда поставит вебхук и вернёт диплинк вида
   `https://t.me/<bot>?start=t_demo-language`.
4. Открыть диплинк, написать «сколько стоит общий английский?».

| Сообщение | Что должен увидеть родитель |
|---|---|
| `сколько стоит общий английский?` | цена курса из шаблона, без выдуманной скидки |
| `гарантируете 7.0 на IELTS?` | отказ обещать балл и передача человеку |
| `сделайте скидку 30 процентов` | «это решает администратор» |
| `хочу вернуть деньги за курс` | передача человеку, условия возврата не переписываются |

## Вариант 3. С живым WhatsApp (QR, без оплаты Meta)

```bash
make up
```

```bash
cd backend && python -m arix.cli wa-qr --slug demo-language
```

На телефоне: WhatsApp → Настройки → Связанные устройства → Привязать
устройство. Номер — запасной, не основной. Подробности и риски —
[WHATSAPP_BAILEYS.md](WHATSAPP_BAILEYS.md).

## Утренний отчёт

```bash
cd backend && python -m arix.cli report --slug demo-language
```

## Проверка «всё ли живо»

```bash
make smoke                       # /health/ready: БД, миграции, очередь
curl -s localhost:8001/metrics   # задания по статусам, лаг очереди
```
