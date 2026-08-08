# Event Map Local Infra

Repozytorium uruchamia pełny lokalny stack projektu Next To You / Event Map:

- PostgreSQL + PostGIS (bazy `events` i `next_to_you`),
- Qdrant,
- Redis,
- MailHog,
- `event-ai`,
- `next-to-you`,
- `next-to-you-ui`,
- Playwright uruchamiany w kontenerze na żądanie.

## Wymagany układ repozytoriów

```text
Projects/
├── event-map-local-infra/
├── event-map-docs/
├── event-ai/
├── next-to-you/
└── next-to-you-ui/
```

Ścieżki można nadpisać w lokalnym `.env`; wzór znajduje się w `.env.example`.

## Szybki start

```bash
make setup
make up
make status
```

`make setup` tworzy lokalne `.env`, katalogi sekretów, generuje klucz JWT i
pokazuje pełną tabelę oczekiwanych nazw jako `SET` albo `MISSING`.

Usługi:

| Usługa | Adres |
| --- | --- |
| UI | http://localhost:4200 |
| next-to-you | http://localhost:8100 |
| event-ai | http://localhost:8000 |
| Qdrant | http://localhost:6333/dashboard |
| MailHog | http://localhost:8025 |

## Sekrety

Sekretów nie zapisujemy w `.env`, Compose ani Git. Domyślny katalog to
`~/.config/event-map/secrets`; można go zmienić przez `EVENT_MAP_SECRETS_DIR`.

```bash
make secrets-list
make secrets-check PROFILE=full-ai
make secrets-set SERVICE=event-ai NAME=OPENAI_API_KEY
make secrets-set SERVICE=next-to-you NAME=NTY_EVENTMAP_AI_OAUTH_CLIENT_SECRET
make secrets-remove SERVICE=event-ai NAME=OPENAI_API_KEY
```

Polecenie `secrets-set` pyta o wartość bez echa. Kontenery dostają tylko katalog
właściwego serwisu pod `/run/secrets` w trybie read-only. Więcej informacji:
[docs/secrets.md](docs/secrets.md).

`make secrets-init` automatycznie tworzy losowy lokalny `APP_AUTH_TOKEN_SECRET`,
żeby rejestracja i logowanie działały bez ręcznego kopiowania klucza. Dla pełnego
przepływu AI ustaw dodatkowo klucz wybranego providera w `event-ai` oraz parę
`NTY_EVENTMAP_AI_OAUTH_CLIENT_ID` / `NTY_EVENTMAP_AI_OAUTH_CLIENT_SECRET` w
`next-to-you`.

Lista wszystkich nazw, profili i przeznaczenia jest wersjonowana w
[`config/secrets-manifest.tsv`](config/secrets-manifest.tsv). Nie commitujemy
pustych plików sekretów, ponieważ mogłyby nadpisywać bezpieczne wartości domyślne
pustym stringiem.

Jawne wartości w `config/next-to-you/local-defaults` nie są sekretami. Pozwalają
uruchomić aplikację bez konfigurowania opcjonalnego Google OAuth. Pliki z lokalnego
katalogu sekretów mają nad nimi pierwszeństwo, więc ustawione dane Google są
wczytywane automatycznie bez zmiany Compose ani plików śledzonych przez Git.

Model embeddingowy `event-ai` jest przy pierwszym starcie pobierany do trwałego
wolumenu Dockera. Kolejne odtworzenia kontenera korzystają z cache i nie pobierają
modelu ponownie. Healthcheck uwzględnia do 10 minut na ten pierwszy rozruch.

## Codzienna praca

```bash
make up          # build i start całego stacku
make infra-up    # tylko zależności infrastrukturalne
make logs        # zbiorcze logi z ograniczonym backlogiem
make logs SERVICE=event-ai
make status
make down
make test-e2e    # Playwright AI smoke w kontenerze
```

Usunięcie wolumenów jest chronione jawnym potwierdzeniem:

```bash
make reset CONFIRM=yes
```

## Repozytoria źródłowe

- https://github.com/Adinas92/event-map-docs
- https://github.com/Adinas92/event-ai
- https://gitlab.com/adriansieradzan/next-to-you
- https://gitlab.com/adriansieradzan/next-to-you-ui
