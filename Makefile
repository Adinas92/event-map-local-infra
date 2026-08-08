COMPOSE := ./bin/compose
SERVICE ?=
NAME ?=
PROFILE ?=core

.PHONY: setup secrets-init secrets-list secrets-status secrets-check secrets-set secrets-remove config build up infra-up down reset logs status test-e2e

setup:
	@test -f .env || cp .env.example .env
	@./bin/secrets init
	@./bin/secrets status

secrets-init:
	@./bin/secrets init

secrets-list:
	@./bin/secrets status

secrets-status:
	@./bin/secrets status

secrets-check:
	@./bin/secrets check "$(PROFILE)"

secrets-set:
	@./bin/secrets set "$(SERVICE)" "$(NAME)"

secrets-remove:
	@./bin/secrets remove "$(SERVICE)" "$(NAME)"

config: secrets-init
	@$(COMPOSE) config --quiet

build: secrets-init
	@$(COMPOSE) build

up: secrets-init
	@$(COMPOSE) up -d --build

infra-up: secrets-init
	@$(COMPOSE) up -d postgres qdrant redis mailhog

down:
	@$(COMPOSE) down --remove-orphans

reset:
	@test "$(CONFIRM)" = "yes" || (echo "Odmowa: użyj 'make reset CONFIRM=yes', aby usunąć lokalne wolumeny."; exit 1)
	@$(COMPOSE) down --volumes --remove-orphans

logs:
	@$(COMPOSE) logs --follow --tail=200 $(SERVICE)

status:
	@$(COMPOSE) ps

test-e2e: secrets-init
	@$(COMPOSE) --profile test run --rm playwright
