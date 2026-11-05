# Projekcja indeksu własnego fixture UI

`bin/index-fixture` jest osobnym narzędziem lokalnego setupu. Korzysta z istniejącego
`EventService.index_event` w działającym kontenerze `event-ai`. Nie zalicza zachowania
produktu. Nie uruchamia się automatycznie razem z `test-e2e`.

Operator suity najpierw przygotowuje własne konta i wydarzenia przez UI, odczytuje
ich dokładne UUID z DOM i eksportuje prywatny `index-input.json` bez poświadczeń.
Indeksację wykonuje dopiero w uzgodnionym oknie, bez równoległego setupu, testów lub
cleanupu tego namespace. Narzędzie nie potwierdza samodzielnie pochodzenia UUID z DOM;
jest to odpowiedzialność eksportera i operatora.

## Wejście

Zwykły plik należący do operatora, `0600`, bez dowiązania i maksymalnie 1 MB:

```text
schemaVersion: 1
baseURL: jawny http://localhost:4200/ albo http://127.0.0.1:4200/
namespace: e2e-<12–32 znaki hex>
datasetSha256: SHA-256 UTF-8 JSON.stringify({namespace,events})
timezone: Europe/Warsaw
location: {city,address,latitude,longitude}
events: {primary,secondary}
validUntil: termin UTC
```

Każde wydarzenie ma dokładnie `id,title,description,start,end,address,latitude,
longitude,category,access`. Dwa różne kanoniczne UUID muszą pochodzić z własnych kart
UI. Tytuły zaczynają się od namespace i spacji, opisy zawierają namespace.
`primary` ma `MUSIC/FREE`, `secondary` `THEATER/PAID`; lokalizacja odpowiada wspólnej
lokalizacji fixture. Współrzędne są ciągami liczbowymi. Daty wydarzeń muszą być
przyszłe i późniejsze niż dzień końca ważności fixture w strefie produktu.

Hash zachowuje kolejność kluczy oryginalnego JSON; nie sortuje ich ani nie zamienia
Unicode na sekwencje ASCII. Nie przekazuj całego `fixture.json`: dodatkowe pola,
w tym konta i hasła, są odrzucane. Hash sprawdza spójność eksportu, nie własność
zasobów w aplikacji. Origin musi odpowiadać jawnemu argumentowi; narzędzie go nie
przepisuje i nie obsługuje dev.

## Polecenie

Po przygotowaniu eksportu i uzgodnieniu okna operator uruchamia:

```sh
./bin/index-fixture index --engine docker \
  --input /prywatna/sciezka/index-input.json \
  --base-url http://localhost:4200 --owner local-fixture-operator
```

Dostępne są `docker|podman`, `--project` (domyślnie `event-map-local`),
`--state-dir`, `--lock-dir` i `--worker-timeout` (1–900 s, domyślnie 180 s).
Silnik i zdrowy kontener `event-ai` muszą już działać; polecenie ich nie uruchamia.
Zgodność Podmana wymaga osobnego rzeczywistego dowodu.

Blokada namespace ma dokładnie ten sam klucz co E2E: SHA-256 originu znormalizowanego
do `http://127.0.0.1:4200`, znaku nowej linii i namespace. Dodatkowo blokowane są oba
UUID. Katalog `E2E_LOCK_DIR`, domyślnie `event-map-e2e-locks` w katalogu tymczasowym
systemu, musi być współdzielony z hostem i kontenerami runnera i mieć `0700`.
To blokada jednego hosta; nie zastępuje operacyjnej wyłączności między hostami.

## Zakres operacji

Worker korzysta z normalnych ustawień serwisu wewnątrz kontenera. Nie odczytuje
środowiska innych procesów, nie kopiuje ani nie wypisuje sekretów. Nie wykonuje
pełnego bootstrapa usług, połączeń do bazy, LLM, crawl, geokodowania ani re-ingestu.
Akceptuje wyłącznie lokalny Qdrant na porcie 6333 (`qdrant`, `localhost`, `127.0.0.1`)
i istniejącą kolekcję. Nie tworzy ani nie odtwarza kolekcji i indeksów payload.

Plan punktów powstaje z rzeczywistej logiki chunkowania `EventService`, z tymi samymi
kanonicznymi UUID i deterministycznymi UUID5 chunków. Jest zapisany trwale na hoście
przed pierwszą mutacją. Właściwa indeksacja używa prawdziwego `SentenceTransformerAdapter`
z modelem i wymiarem konfiguracji runtime; nie zastępuje go stałymi wektorami.
Rejestr jest związany z silnikiem, projektem Compose, ID kontenerów i obrazów AI/Qdrant
oraz lokalnym endpointem i nazwą kolekcji. Zmiana któregokolwiek celu blokuje ponowienie
i cleanup bez zastępowania starego rejestru; nie ma automatycznej migracji punktów.
Plik, atomowa podmiana i wpisy katalogów są utrwalane przez `fsync` przed mutacją.
Metadane dowodu obejmują także hash implementacji, model, wymiar, device i wersje
pakietów; rewizja wag modelu pozostaje `unknown`, jeśli cache jej nie poświadcza.

Adapter dopuszcza wyłącznie zaplanowane ID. Payload otrzymuje marker
`e2e_setup` z namespace i hashami datasetu/eksportu. Istniejący punkt bez zgodnego
markera i własnego event ID/tytułu blokuje nadpisanie i cleanup. Nie jest stosowany
szeroki delete po filtrze, nazwa kolekcji ani sam kanoniczny UUID jako lista chunków.
Syntetyczny `urn:event-map:e2e:...` w projekcji jest identyfikatorem fixture,
a nie nowym publicznym adresem produktu.

## Wynik i odbiór UI

Kod 0 dla `index` oznacza `INDEXED_NOT_UI_VERIFIED`. Potwierdzenie `0600` powstaje w:

```text
<state-dir>/datasets/<namespace-key>-attestation.json
```

Domyślny `state-dir` to `index-state` obok wejścia. Potwierdzenie zawiera wyłącznie
`baseURL,namespace,datasetSha256,eventIds,owner,evidenceRef,validUntil`.
`evidenceRef` wskazuje `fixture-index/<run-id>/evidence.json` względem `state-dir`.
Dowody i stan są prywatne (`0700/0600`) i nie zawierają kont, haseł ani wektorów.

Dopiero osobny scenariusz E2E przez UI potwierdza znalezienie obu dokładnych ID
oraz powrót z zapytania bez wyników. Wyłącznie właściciel suity zapisuje capability
`searchIndex` po takim dowodzie. Samo potwierdzenie indeksacji nie odblokowuje AC,
nie zmienia manifestu i nie oznacza PASS testu ani gotowości dev.

Capability wiąże się z aktywnym slotem `index-state/datasets/<namespace-key>-attestation.json`
względem katalogu fixture. E2E odczytuje go pod wspólną blokadą i porównuje wszystkie
pola, w tym `evidenceRef` bieżącej generacji. Brak pliku po cleanup albo zastąpienie
po reindeksacji daje `BLOCKED` do ponownej weryfikacji UI. Nie kopiuj starego
potwierdzenia ani nie przywracaj capability ręcznie.

Kontenerowy adapter `business/discovery` montuje tylko katalog `datasets` RO do
`/run/index-state/datasets`; fixture pozostaje osobnym plikiem RO. Mount katalogu
zapewnia widoczność atomowego unlink/replace; mount pojedynczego potwierdzenia byłby
nieaktualnym inode. Producent E2E deklaruje w publicznym `config/dependencies.json`
`activeAttestationContract: "fixture-index-slot-v1"`; adapter odrzuca starszy lub
nieznany kontrakt przed startem stacku. Host wymaga obu katalogów `index-state` i `datasets` jako `0700`,
własności operatora i bez dowiązań. Ten adapter obsługuje standardowy `state-dir`
obok fixture; niestandardowa ścieżka poza tym slotem nie jest mapowana automatycznie.


## Ponowienie, błąd i cleanup

Powtórzenie tego samego eksportu i właściciela aktualizuje te same własne punkty.
Zmiana datasetu lub planu chunków wymaga najpierw potwierdzonego `CLEANED` starego
planu. Ten sam właściciel może następnie indeksować nowy eksport w tym samym celu;
stary rejestr trafia do prywatnego `history/` przed zastąpieniem. Zmiana właściciela
pozostaje blokowana i wymaga osobnego przekazania operacyjnego. Nie wolno usuwać
starego inventory w celu obejścia odmowy.
Po częściowym błędzie dokładny plan pozostaje w `datasets/<namespace-key>.json`,
a nowe potwierdzenie nie jest wystawiane. Zachowaj oryginalny eksport oraz ten stan.

Cleanup jest osobnym jawnym poleceniem operatora po zakończeniu zależnych runów:

```sh
./bin/index-fixture cleanup --engine docker \
  --input /prywatna/sciezka/index-input.json \
  --base-url http://localhost:4200 --owner local-fixture-operator
```

Sprawdza każdy zapisany punkt/chunk i marker własności przed pierwszym usunięciem.
Usuwa tylko dokładne ID z ledgeru; brakujący własny punkt jest bezpiecznym powtórzeniem.
Nie usuwa wydarzeń, kont, wiadomości, cudzych punktów ani całej kolekcji. Cleanup
akceptuje przeterminowany eksport, ale nadal wymaga poprawnej struktury, hashów i ID.
Stan cleanupu oraz dowód pozostają, a potwierdzenie indeksacji jest unieważniane.

Kod 2 oznacza `BLOCKED`, 130/143 przerwanie procesu. Worker ma limit egzekwowany
sygnałem systemowym; host czeka dodatkowe 30 s. Gdy zakończenia workera nie można
potwierdzić, stan ma `UNCERTAIN`, a blokady namespace i UUID pozostają. Operator musi
najpierw ustalić, że żaden worker już nie działa, i sprawdzić własny ledger; nie usuwaj
blokad automatycznie ani nie zaczynaj równoległego seeda. Nie wypisuj raw stderr.
Dowody usuń po odbiorze, najpóźniej po siedmiu dniach; ledger i eksport zachowaj do
cleanup wszystkich własnych punktów i bezpiecznego przekazania ich właścicielowi.
