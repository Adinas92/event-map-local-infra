COMPOSE := ./bin/compose
SERVICE ?=
NAME ?=
PROFILE ?=core
CONTAINER_ENGINE ?=podman
E2E_REPO ?=../event-map-e2e
SUITE ?=entry
STACK_MODE ?=start
BASE_URL ?=http://127.0.0.1:4200
export CONTAINER_ENGINE

.PHONY: setup secrets-init secrets-list secrets-status secrets-check secrets-set secrets-remove config build up infra-up down reset logs status test-e2e test-e2e-mocked test-runner

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

test-e2e:
	@./bin/test-ui --engine "$(CONTAINER_ENGINE)" --e2e-repo "$(E2E_REPO)" --suite "$(SUITE)" --stack-mode "$(STACK_MODE)" --base-url "$(BASE_URL)" $(if $(FIXTURE),--fixture "$(FIXTURE)") $(if $(TEST_TIMEOUT),--test-timeout "$(TEST_TIMEOUT)")

test-runner:
	@python3 -m unittest discover -s tests -v

test-e2e-mocked: secrets-init
	@$(COMPOSE) --profile test run --rm playwright
