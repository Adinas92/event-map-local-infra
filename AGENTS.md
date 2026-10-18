# Instrukcje dla agentów

To repo jest wyłącznie orkiestratorem lokalnego środowiska Event Map.

- Przed zmianą przeczytaj `/Users/adrian/Projects/event-map-docs/AGENTS.md` oraz
  projektowy runbook lokalnego środowiska.
- Nie zapisuj sekretów, tokenów, haseł ani realnych connection stringów.
- W `.env` umieszczaj tylko ustawienia niesekretne.
- Sekrety obsługuj wyłącznie przez `bin/secrets`; nie odczytuj ich zawartości.
- Nie wypisuj środowiska kontenerów ani zawartości `/run/secrets`.
- Zmiana kontraktu konfiguracji wymaga równoległej aktualizacji dokumentacji.
- Nie usuwaj wolumenów bez jawnego `CONFIRM=yes` od użytkownika.
