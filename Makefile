# ARIX edu — частые операции.
SHELL := /bin/bash
COMPOSE := docker compose
BACKEND := cd backend && PYTHONPATH=$$PWD

.DEFAULT_GOAL := help
.PHONY: help demo up down logs ps migrate revision seed eval test test-fast lint fmt \
        shell psql reset tunnel-hint smoke

help: ## Показать это сообщение
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

demo: ## Полный демо-стенд: поднять всё, применить миграции, засеять демо-центр
	@test -f .env || cp .env.example .env
	$(COMPOSE) up -d --build postgres redis
	$(COMPOSE) run --rm migrate
	$(COMPOSE) up -d --build backend worker waweb frontend
	$(COMPOSE) run --rm seed
	@set -a; . ./.env; set +a; \
	  echo; \
	  echo "Готово."; \
	  echo "  API:         http://localhost:$${BACKEND_PORT:-8001}/docs"; \
	  echo "  Панель:      http://localhost:$${FRONTEND_PORT:-3002}"; \
	  echo "  WhatsApp QR: http://localhost:$${WAWEB_PORT:-3003}/health"; \
	  echo "  Дальше:      docs/DEMO.md"

up: ## Поднять стек
	$(COMPOSE) up -d --build

down: ## Остановить стек (данные сохраняются)
	$(COMPOSE) down

reset: ## Остановить и удалить данные (ВСЁ сотрётся)
	$(COMPOSE) down -v

logs: ## Логи бэкенда и воркера
	$(COMPOSE) logs -f backend worker

ps: ## Статус сервисов
	$(COMPOSE) ps

migrate: ## Применить миграции
	$(COMPOSE) run --rm migrate

revision: ## Создать миграцию: make revision m="add x"
	$(BACKEND) python -m alembic revision --autogenerate -m "$(m)"

seed: ## Засеять демо-центр (курсы, группы, ученики, счета)
	$(COMPOSE) run --rm seed

eval: ## Прогнать стартовый eval (полный набор 60 диалогов — задача P1-1)
	$(BACKEND) python -m arix.eval.runner

test: ## Все тесты
	$(BACKEND) python -m pytest -q

test-fast: ## Тесты без eval-набора
	$(BACKEND) python -m pytest -q -m "not slow"

lint: ## Проверить стиль
	$(BACKEND) python -m ruff check arix tests

fmt: ## Автоформат
	$(BACKEND) python -m ruff check --fix arix tests && cd backend && python -m ruff format arix tests

shell: ## Python-консоль в контейнере бэкенда
	$(COMPOSE) exec backend python

psql: ## psql в базу
	$(COMPOSE) exec postgres psql -U postgres -d $${POSTGRES_DB:-arix}

smoke: ## Проверить, что живое API отвечает
	@set -a; [ -f .env ] && . ./.env; set +a; \
	  curl -fsS "http://localhost:$${BACKEND_PORT:-8001}/health/ready" | python3 -m json.tool

tunnel-hint: ## Как отдать вебхуки наружу для реального Telegram/WhatsApp
	@set -a; [ -f .env ] && . ./.env; set +a; \
	  echo "1) cloudflared tunnel --url http://localhost:$${BACKEND_PORT:-8001}"
	@echo "2) PUBLIC_BASE_URL=<адрес туннеля> в .env"
	@echo "3) make up && см. docs/DEMO.md про подключение каналов"
