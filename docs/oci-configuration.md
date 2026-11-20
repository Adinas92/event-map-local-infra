# Konfiguracja aplikacji z OCI Vault

To opt-in integracja deploy/start dla LOCAL i DEV. Nie zmienia hostingu i nie
potwierdza migracji istniejącego środowiska. Nie uruchamia się przy `make up`.
Pełna procedura IAM, migracji i odbioru jest w
[wiki konfiguracji OCI](https://github.com/Adinas92/event-map-docs/blob/main/docs/runbook/oci-konfiguracja.md).

## Katalog i przypięcie

[`config/oci-config-catalog.json`](../config/oci-config-catalog.json) zawiera
nazwy, klasy, konsumentów, właścicieli, źródła i docelowe nazwy plików dla trzech
aplikacji. `excluded` wyłącza niepotwierdzony alias IDE i kontrolowane ustawienia
bootstrapu. `migratable` oznacza kandydata, nie potwierdzenie aktywnej konfiguracji.
Każdy descriptor wybiera konkretny zestaw `names` i jego podzbiór `required`.
Włącz nazwy rzeczywiście potrzebne wybranym integracjom. Brakujące opcjonalne
wartości nie tworzą pustych plików. Wartość pusta, nieznana nazwa lub zła klasa
zatrzymuje import. Parametry wymagające pustej wartości reprezentuj pominięciem
opcjonalnej nazwy; nie rób jej `required`.

Descriptor zawiera `schema_version: 1`, `environment: local|dev`, `service`,
`region`, SHA-256 dokładnych bajtów katalogu `catalog_sha256` oraz `bundles`.
Każdy bundle ma `class`, `secret_id`, dodatni `version_number`, `names`, `required`.
Przykłady w [`config/oci`](../config/oci) mają syntetyczne referencje i są
szablonami, nie gotowym ustawieniem środowiska. Przypnij również commit narzędzia.
Zmiana katalogu wymaga świadomego odświeżenia descriptorów.
Zastąp `REPLACE_WITH_SHA256_OF_CATALOG` wynikiem
`shasum -a 256 config/oci-config-catalog.json` oraz syntetyczne OCID prawdziwymi
referencjami właściwych bundle. Szablon przed uzupełnieniem jest odrzucany.

Vault przechowuje JSON osobno per środowisko, serwis i klasę (`secret`,
`parameter`; UI wyłącznie `public`). Payload ma dokładnie pola `environment`,
`service`, `class`, `values`; `values` mapuje dozwolone nazwy na niepuste stringi.
Maksymalny rozmiar dekodowanego dokumentu to konserwatywnie 25 000 bajtów.
Nie publikuj payloadów z rzeczywistymi wartościami w repo ani argumentach CLI.
Operator importuje je prywatnym, zatwierdzonym kanałem do Vault.

## Pobranie

Wymagane Python3.10+, OCI CLI, dostęp do Vault i prywatny katalog poza repo.
Profil CLI wybiera się jawnie; nazwa profilu nie dowodzi uprawnień IAM.

```sh
./bin/secrets oci sync \
  --deployment /prywatna-konfiguracja/local/event-ai.json \
  --root /prywatny-katalog/oci-config \
  --environment local --service event-ai \
  --profile EVENT-MAP-INSPECT --auth security_token
```

Powtórz osobno dla `next-to-you` i `next-to-you-ui` z ich descriptorami i
odpowiednimi profilami. Do stałego deployu użyj zatwierdzonej tożsamości:
`api_key` lub `instance_principal` tylko tam, gdzie rzeczywiście działa.
Fetcher nie przekazuje poświadczeń OCI do kontenerów aplikacji.

Sukces zwraca tylko `READY`, środowisko, serwis i bezwzględną ścieżkę generacji.
Katalogi mają `0700`, pliki konfiguracji `0600`; wyłącznie publiczny JS UI ma
`0644`, aby worker nginx mógł go czytać po montażu pojedynczego pliku. Katalog
bazowy musi należeć do operatora i mieć `0700`. Nie wskazuj repo ani symlinka.
Root twórz pod prywatnym, kontrolowanym katalogiem operatora, nie w publicznym
katalogu współdzielonym. Zawartości plików nie wyświetlaj.

## Odtworzenie kontenerów

```sh
./bin/secrets oci compose --environment local \
  --release /prywatny-katalog/oci-config/local/event-ai/releases/GEN_AI \
  --release /prywatny-katalog/oci-config/local/next-to-you/releases/GEN_BE \
  --release /prywatny-katalog/oci-config/local/next-to-you-ui/releases/GEN_UI \
  -- up -d --force-recreate --no-build event-ai next-to-you next-to-you-ui
```

Wymagany Docker Compose >=2.24.4 (`!override`); inne providery wymagają osobnej
walidacji. Wrapper korzysta z istniejącego `bin/compose` i blokady stacku. Nie
uruchamiaj go podczas cudzych testów. Obrazy zbuduj wcześniej przez właściwy
workflow; `--no-build` nie produkuje nowych obrazów. Adresy i credentials lokalnych
dependencji muszą występować w odpowiednich bundle, bo overlay usuwa environment
z podstawowego Compose. Pozostają wyłącznie kontrolowane ścieżki bootstrapu.
Nie przekazuj dodatkowego overlayu przywracającego stare env. Runner Playwright
pozostaje poza migracją aplikacji; jego osobne dane nie są pobierane przez importer.

Podaj konkretne ścieżki `releases/<id>`, nie `current`. Hostowy symlink jest tylko
lokalnym wskaźnikiem; nie przełącza bind mountu istniejącego kontenera. Montaże
nie tworzą automatycznie brakujących źródeł. Backend ma wymagany `configtree`,
AI istniejące `secrets_dir`. UI dostaje tylko publiczny JS, a entrypoint uruchamia
nginx bez skryptu generującego stary config z environment. Nie ma hot reload.
DEV może użyć tego wariantu wyłącznie na potwierdzonym celu Compose; nie oznacza
to istnienia ani provisioningu takiego celu.

## Awaria, rotacja i rollback

Błąd OCI, nieprawidłowy JSON, wersja, środowisko, serwis, klasa lub wymagane nazwy
zwraca stały kod `BLOCKED` i nie przełącza `current`. Nie odtwarzaj kontenerów po
nieudanym `sync`. Wrapper nie wybiera automatycznie poprzedniej konfiguracji.
Działający wcześniej proces może pozostać na swojej generacji.

Rotacja: nowa wersja Vault → nowy descriptor → `sync` → recreate → smoke.
Rollback: poprzedni descriptor z dokładnymi wersjami → ponowny `sync` → recreate
ze zwróconymi generacjami → smoke. Stare prywatne generacje nie są automatycznie
usuwane; operator wyznacza okno retencji i usuwa nieużywane generacje po odbiorze.
Cofnięcie wersji Vault nie przywraca odwołanego klucza u zewnętrznego dostawcy.

Testy offline: `python3 -m unittest discover -s tests -p test_oci_config.py -v`.
Nie zastępują live IAM ani startu aplikacji. Nie usuwaj starych wartości CI przed
odbiorem działającego środowiska i rozwiązaniem bootstrapu dostępu do OCI.
