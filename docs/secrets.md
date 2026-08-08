# Sekrety lokalnego stacku

## Model

- wartości nie trafiają do Git ani `.env`,
- helper przyjmuje wartość interaktywnie bez echa,
- pliki i katalogi mają uprawnienia odpowiednio `0600` i `0700`,
- każdy kontener dostaje tylko katalog własnego serwisu,
- `event-ai` czyta pliki przez Pydantic `secrets_dir`,
- `next-to-you` czyta pliki przez Spring `configtree`,
- Playwright ładuje tylko jawnie dozwolone pliki auth w procesie testowym.

Nazwa pliku jest nazwą ustawienia, np.:

```text
~/.config/event-map/secrets/
├── event-ai/
│   └── OPENAI_API_KEY
├── next-to-you/
│   └── NTY_EVENTMAP_AI_OAUTH_CLIENT_SECRET
└── playwright/
    └── PLAYWRIGHT_AUTH_PASSWORD
```

## Obsługa

```bash
make secrets-init
make secrets-list
make secrets-set SERVICE=event-ai NAME=OPENAI_API_KEY
make secrets-remove SERVICE=event-ai NAME=OPENAI_API_KEY
```

`secrets-list` pokazuje wyłącznie nazwy plików, nigdy wartości.
Przy pierwszym `secrets-init` helper sam generuje lokalny `APP_AUTH_TOKEN_SECRET`
dla backendu i również nigdy go nie wypisuje.

## Granica bezpieczeństwa

To rozwiązanie ogranicza przypadkowe ujawnienie w Git, konfiguracji Compose,
historii powłoki i `docker inspect`. Nie stanowi izolacji przed procesem działającym
na tym samym koncie użytkownika z prawem odczytu plików ani przed podmiotem z
dostępem do Docker socketa lub możliwością wykonania komendy w kontenerze.

Agentowi LLM należy udostępniać repozytoria i bezpieczne komendy (`make status`,
`make logs`), ale nie katalog sekretów, nieograniczony Docker socket ani polecenia
odczytujące środowisko procesu. Przy wymaganej twardej izolacji stack powinien być
uruchamiany przez osobne konto systemowe lub zewnętrzny runner bez dostępu agenta
do magazynu sekretów.
