# Instrukcje dla agentów

To repo jest wyłącznie orkiestratorem lokalnego środowiska Event Map.

- Sesję bez przydzielonego skilla zacznij od
  [indeksu wiki](https://github.com/Adinas92/event-map-docs/blob/main/docs/llm/index.md),
  a następnie wybierz właściwego specjalistę z jego routingu.
- Przed zmianą przeczytaj `../event-map-docs/AGENTS.md` (lub jego
  [wersję w wiki](https://github.com/Adinas92/event-map-docs/blob/main/AGENTS.md))
  oraz [projektowy runbook lokalnego stacku](https://github.com/Adinas92/event-map-docs/blob/main/docs/runbook/local-full-stack.md).
- Nie zapisuj sekretów, tokenów, haseł ani realnych connection stringów.
- W `.env` umieszczaj tylko ustawienia niesekretne.
- Sekrety obsługuj wyłącznie przez `bin/secrets`; nie odczytuj ich zawartości.
- Nie wypisuj środowiska kontenerów ani zawartości `/run/secrets`.
- Zmiana kontraktu konfiguracji wymaga równoległej aktualizacji dokumentacji.
- Nie usuwaj wolumenów bez jawnego `CONFIRM=yes` od użytkownika.

Stosuj [wspólny standard bezpieczeństwa danych i sekretów](https://github.com/Adinas92/event-map-docs/blob/main/docs/standards/bezpieczenstwo-danych-i-sekretow.md), także dla danych syntetycznych, logów, fixture i artefaktów CI. Kontrolę offline uruchom przez `python3 scripts/security_scan.py` po zainstalowaniu `detect-secrets==1.5.0`; wynik zawiera tylko lokalizacje.
