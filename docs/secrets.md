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
make setup
make secrets-status
make secrets-check PROFILE=full-ai
make secrets-set SERVICE=event-ai NAME=OPENAI_API_KEY
make secrets-remove SERVICE=event-ai NAME=OPENAI_API_KEY
```

`secrets-list` pokazuje wyłącznie nazwy plików, nigdy wartości.
Zmienne z konfiguracji IntelliJ `next-to-you [bootRunDev]` można przechowywać
pod nazwami z manifestu w katalogu `next-to-you`. Wszystkie nazwy z tego
profilu są oznaczone jako `optional`; ich status `SET` oznacza zapis na hoście,
nie potwierdza użycia przez aplikację. Compose jawnie ustawia własne wartości
dla lokalnej bazy, MailHog, AWS i CORS, więc te ustawienia mają pierwszeństwo
nad wartościami zapisanymi w plikach. `APP_AUTH_TOKENSECRET` z IDE ma inną
pisownię niż używane przez aplikację `APP_AUTH_TOKEN_SECRET`; lokalny klucz
JWT pozostaje pod właściwą nazwą.

Dla logowania Google w lokalnym UI klient OAuth musi mieć w Google Cloud
dokładnie autoryzowany URI przekierowania
`http://localhost:4200/login/oauth2/code/google`. Samo zapisanie identyfikatora
i sekretu klienta nie usuwa błędu `redirect_uri_mismatch`.
Przy pierwszym `secrets-init` helper sam generuje lokalny `APP_AUTH_TOKEN_SECRET`
dla backendu i również nigdy go nie wypisuje.

Źródłem prawdy dla wymaganych nazw jest wersjonowany
`config/secrets-manifest.tsv`. Profile:

- `core` — minimalny start i lokalne logowanie;
- `full-ai` — komunikacja S2S oraz co najmniej jeden provider LLM;
- `e2e-auth` — konto dla testów Playwright wymagających logowania;
- `optional` — integracje niewymagane do podstawowego startu.

Na nowym komputerze `make setup` pokazuje komplet brakujących nazw bez tworzenia
pustych plików i bez wyświetlania jakiejkolwiek wartości.

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
