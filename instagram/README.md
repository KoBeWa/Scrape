# instagram/

Datenpipeline für den wöchentlichen TimTebowTournament-Instagram-Post.
Berührt nichts Bestehendes im Repo – alles liegt in diesem Ordner.

- `build_week.py` – holt Sleeper + nflverse-PBP, schreibt `data/<season>/weekNN.json` (~0,5–1 MB) und `data/<season>/latest.json`
- `fetch_pbp.py` – täglich 08:00 MESZ: nflverse-PBP der Season laden, auf Scoring-Spalten reduzieren → `pbp/<season>/weekNN.csv.gz`
- `render_post.py` – rendert `post/TTT Weekly Post.dc.html` headless, schreibt `export/<season>/weekNN/*.png` (2160×2700) + `caption.txt`
- `post/` – Kopie des Templates (+ support.js, pbp.js, sleeper.js, assets); bei Design-Änderungen neu kopieren
- `takes/<season>/weekNN.md` – Trash-Talk, Cover-Text, Caption (manuell pflegen)
- `.github/workflows/instagram-pbp-daily.yml` – Cron täglich 06:00 UTC
- `.github/workflows/instagram-weekly.yml` – läuft dienstags 12:00 MESZ, manuell startbar (auch mit fester Week)

Das Post-Template lädt
`https://raw.githubusercontent.com/KoBeWa/Scrape/master/instagram/data/<season>/weekNN.json`.
Fehlt die Datei, fällt es auf Live-Sleeper zurück (ohne Win-Probability-Kurve).

Caption: wird automatisch gebaut (Ergebnisse, H2H-Bilanz aus data/processed, Karriere-Facts). Text aus `## Caption` in der Takes-Markdown wird angehängt.

Hinweis: nflverse aktualisiert PBP montags/nachts; dienstags 12 Uhr ist die Woche vollständig.
Bei Zeitumstellung Ende Oktober `TZ_OFFSET_H` in `build_week.py` auf 1 setzen.
