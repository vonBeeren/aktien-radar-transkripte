# Aktien-Radar: YouTube-Transkripte

GitHub Actions holt täglich (04:30 UTC, also 5:30/6:30 Uhr in Berlin) neue Videos der Kanäle in `kanaele.json` und speichert die Untertitel. Shorts sind nicht dabei.

- `transkripte/<datum>_<kanal>_<videoid>.json`: id, kanal, quelle, titel, datum, veroeffentlicht, beschreibung, link, sprache, automatisch, segmente `[{start, text}]`
- `index.json`: alle bekannten Videos mit `status` (`ok`, `kein_transkript`, `blockiert`, `fehler`, `offen`), `versuche` und `datei`
- `letzter_lauf.json`: Bericht des letzten Laufs (u. a. `blockiert: true`, wenn YouTube die IP sperrt)

Videos ohne Transkript werden bis zu 5 Tage lang erneut versucht, weil automatische Untertitel oft erst Stunden nach dem Upload erscheinen.

Lauf von Hand: Actions → „Transkripte holen“ → Run workflow.
