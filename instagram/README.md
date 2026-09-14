# instagram/

Datenpipeline für den wöchentlichen TimTebowTournament-Instagram-Post.
Berührt nichts Bestehendes im Repo – alles liegt in diesem Ordner.

- `build_week.py` – holt Sleeper + nflverse-PBP, schreibt `data/<season>/weekNN.json` (~0,5–1 MB) und `data/<season>/latest.json`
- `takes/<season>/weekNN.md` – Trash-Talk, Cover-Text, Caption (manuell pflegen)
- `.github/workflows/instagram-weekly.yml` – läuft dienstags 12:00 MESZ, manuell startbar (auch mit fester Week)

Das Post-Template lädt
`https://raw.githubusercontent.com/KoBeWa/Scrape/master/instagram/data/<season>/weekNN.json`.
Fehlt die Datei, fällt es auf Live-Sleeper zurück (ohne Win-Probability-Kurve).

Hinweis: nflverse aktualisiert PBP montags/nachts; dienstags 12 Uhr ist die Woche vollständig.
Bei Zeitumstellung Ende Oktober `TZ_OFFSET_H` in `build_week.py` auf 1 setzen.
