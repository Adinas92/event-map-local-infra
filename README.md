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

To repo odpowiada za lokalne uruchomienie usług. [Wiki Event Map](https://github.com/Adinas92/event-map-docs) jest źródłem wiedzy o produkcie, architekturze, ADR, standardach i procesach; zacznij od [indeksu wiki](https://github.com/Adinas92/event-map-docs/blob/main/docs/README.md), a przy pracy agentowej od [indeksu LLM](https://github.com/Adinas92/event-map-docs/blob/main/docs/llm/index.md).

## Wymagania

- Docker Engine/Desktop z działającym `docker compose`.
  Podman pozostaje opcjonalnym silnikiem wymagającym osobnej walidacji runtime.
- Python 3.10+ i Git do nowego adaptera E2E.
- `make` oraz powłoka zgodna ze skryptami `bin/`.
- Sąsiednie checkouty trzech repozytoriów kodu (lub ich ścieżki w `.env`).
- Wolne porty z `.env.example`; pierwszy build i pobranie modelu embeddingowego wymagają połączenia z siecią.

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
`CARTO_BASEMAPS_API_KEY` w `.env` jest wyjątkiem nazewniczym: trafia do
przeglądarki i żądań kafelków, więc jest kluczem publicznym ograniczanym do
lokalnego hosta, a nie sekretem serwerowym. Nie commituj jego wartości.

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
wolumenu silnika kontenerowego. Kolejne odtworzenia kontenera korzystają z cache i nie pobierają
modelu ponownie. Healthcheck uwzględnia do 10 minut na ten pierwszy rozruch.

## Codzienna praca

```bash
make up          # build i start całego stacku
make infra-up    # tylko zależności infrastrukturalne
make logs        # zbiorcze logi z ograniczonym backlogiem
make logs SERVICE=event-ai
make status
make down
make test-e2e E2E_REPO=/sciezka/event-map-e2e  # nowa suita entry
make test-e2e-mocked  # zachowane regresje UI z mockami
```

`make test-e2e` korzysta z czystego checkoutu osobnego `event-map-e2e`.
Domyślnie buduje/startuje stack, czeka do 900 s na readiness i uruchamia `entry`
przez istniejący UI, bez drugiego Angular dev servera. Podczas cudzych runów używaj
`STACK_MODE=existing`, który nie zmienia usług. Wybierz jawnie
`CONTAINER_ENGINE=docker|podman` (domyślnie Docker); `bin/compose` respektuje ten sam wybór.

```sh
make test-e2e STACK_MODE=existing E2E_REPO=/sciezka/event-map-e2e
# Opcjonalny silnik wymagający osobnej walidacji:
make test-e2e CONTAINER_ENGINE=podman STACK_MODE=existing E2E_REPO=/sciezka/event-map-e2e
make test-runner
```

Raport i ograniczona diagnostyka pozostają prywatnie w `reports/<run>/` na hoście.
Dla jawnie wybranego Podmana wrapper używa `podman-compose`.
Inny provider Podmana można wskazać przez `PODMAN_COMPOSE_PROVIDER`.
Brak silnika, readiness lub raportu daje `BLOCKED` z niezerowym statusem; brak Podmana
to `NOT RUN` dla zgodności tego silnika. Testy kontraktu nie dowodzą zgodności runtime.
`entry` rozpoznający bramkę beta nie potwierdza Discovery/loginu/AI. `SUITE=business`
lub `discovery` wymaga dostarczającej go wersji E2E i prywatnej `FIXTURE=/sciezka/fixture.json`
(0600, mount read-only). Wymaga też prywatnych katalogów `index-state/datasets`
obok fixture (oba 0700, właściciel operatora, bez dowiązań). Adapter montuje katalog
aktualnych potwierdzeń RO. Publiczny `config/dependencies.json` w czystym E2E musi
deklarować `activeAttestationContract: "fixture-index-slot-v1"`; starszy kontrakt daje
`BLOCKED` przed startem stacku. E2E sprawdza aktywny slot po atomowym usunięciu
lub zastąpieniu; nie czyta poświadczeń fixture. `BASE_URL` wybiera localhost albo127.0.0.1 na porcie4200;
origin musi odpowiadać fixture. Dane przygotowuje osobno właściciel, bez resetu bazy.

`make test-e2e-mocked` zachowuje stare `npm run test:e2e:ai` z repo UI. Ten zestaw
podstawia auth/beta/eventy/NDJSON; jego sukces jest dowodem regresji z mockami.
Nowy adapter usuwa tylko własny kontener i nie zatrzymuje stacku ani wolumenów.
Wersje usług bez manifestu deploymentu pozostają `unknown`.

Pełna konfiguracja, rootless/UID, DNS/proxy/secure context, kody, wspólny
`E2E_LOCK_DIR`, retencja i ograniczenia:
[runbook runnera UI](https://github.com/Adinas92/event-map-docs/blob/main/docs/runbook/ui-suite-runner.md).

Osobny setup własnej projekcji wyszukiwania udostępnia `bin/index-fixture index|cleanup`.
Przyjmuje wyłącznie prywatny eksport dwóch UUID z UI, blokuje namespace/ID i zapisuje
dokładny plan punktów/chunków. Nie wykonuje się automatycznie z testami; potwierdzenie
indeksacji nie zastępuje weryfikacji przez UI. Kontrakt, ograniczenia i cleanup opisuje
[procedura własnego indeksu fixture](docs/fixture-index.md).

Usunięcie wolumenów jest chronione jawnym potwierdzeniem:

```bash
make reset CONFIRM=yes
```

## Repozytoria źródłowe

- https://github.com/Adinas92/event-map-docs
- https://github.com/Adinas92/event-ai
- https://gitlab.com/adriansieradzan/next-to-you
- https://gitlab.com/adriansieradzan/next-to-you-ui
