# Watchlist

A personal movie watchlist dashboard. Runs locally on macOS once a day via
launchd, pre-renders a static HTML page, pushes it to GitHub Pages, and gets
read on a phone in bed.

## Hard constraints

These are decisions, not preferences. Don't relitigate them in code.

- **Free only.** No paid APIs, no paid tiers, no hosting bills. TMDB's free API
  key covers everything needed.
- **Local execution only.** All work happens on this Mac via launchd. GitHub is
  a dumb file host — there is no GitHub Actions workflow and there never will
  be. If the Mac is off, nothing runs. That's fine.
- **Read-only output.** The page displays; it does not accept input. There is no
  "add to watchlist" button, no write-back path, no server. New titles are added
  by editing `data/watchlist.txt` directly.
- **Zero JavaScript in the output.** `sync.py` emits finished HTML. All sorting,
  grouping, and filtering happens in Python. The page is HTML and CSS only.
- **Once a day is enough.** No real-time updates, no polling, no webhooks.
- **The repo lives outside any synced folder.** Not iCloud Drive, not Dropbox,
  not Google Drive — file-sync services corrupt `.git`. GitHub is the sync and
  the backup.

## Layout

```
watchlist/
├── data/
│   ├── favorites.txt      # year-grouped, exported from Apple Notes
│   ├── watchlist.txt      # things to watch; edited by hand or by Claude Code
│   ├── resolved.json      # raw input string -> tmdb_id cache
│   ├── unresolved.txt     # titles that need a human
│   └── movies.db          # SQLite (gitignored, regenerable)
├── docs/
│   └── index.html         # generated; served by GitHub Pages
├── sync.py
├── template.html
└── .env                   # TMDB_API_KEY (gitignored)
```

GitHub Pages serves from `/docs` on main.

## Input format

Both `favorites.txt` and `watchlist.txt` use year-grouped plain text:

```
2017
Get out
Blade Runner
Mother!

2018
Annihilation
Burning
```

A line matching `^\d{4}$` sets the current year. Any other non-blank line is a
film under that year. Blank lines are ignored.

The year matters: it's passed to TMDB search and is what disambiguates
"Blade Runner" under 2017 (*Blade Runner 2049*) from the 1982 film. Never
search without it.

## Schema

```sql
movies(tmdb_id PK, title, year, runtime)
favorites(tmdb_id FK)
watchlist(tmdb_id FK, added_at)
availability(tmdb_id FK, provider, kind, seen_on DATE)
```

`favorites` and `watchlist` are separate tables sharing `movies`. Favorites are
never filtered by streaming availability — they exist as a dedupe check and as
something to paste into a chat when looking for recommendations.

`availability` gets one row per movie/provider/day. This history is the entire
reason the "new since yesterday" and "gone" sections can exist. Never overwrite
it; only append.

## Resolution rules

**Never overwrite an existing `resolved.json` entry.** Hand-pinned IDs are
permanent. A run that re-guesses a title that was already corrected is the worst
possible failure mode here — it's silent and it undoes human work.

The cache is append-only. Load it, skip any key already present, and only search
for what's missing:

```python
resolved = load_json_or_empty(RESOLVED_PATH)
for raw, year in entries:
    key = raw.strip()
    if key in resolved:
        continue
    resolved[key] = tmdb_search(key, year)
save_json(RESOLVED_PATH, resolved)
```

Do not build the dict fresh each run and write it out — that pattern silently
reverts every hand-fix on the next 4am sync. Re-resolving a title is done by
deleting that one entry from the cache, deliberately, by hand.

- Key the cache on the raw input string, but `.strip()` before hashing. Several
  entries have trailing whitespace and would otherwise cache twice.
- If the top TMDB result's title doesn't fuzzy-match the input above a
  threshold, write to `unresolved.txt` and skip. Don't guess.
- Titles that won't resolve cleanly on first run include shorthand (`EEAAO`,
  `Django`, `Martian`, `Imitation game`, `Brawl in cell block`), missing
  punctuation (`Dont breathe`, `Tick tick boom`), and genuinely ambiguous ones
  (`Jungle`). These get fixed once, by hand, in the cache.
- `Mother!` — the exclamation mark is part of the title. Keep it.
- Unreleased films (2026 entries) may have no TMDB record. Log and skip; don't
  raise.
- Dedupe on `tmdb_id` after resolution, not on title string.

## Providers

Use `/movie/{id}/watch/providers`, US region, `flatrate` for subscription.
Subscribed services: Netflix, Hulu, Peacock, Prime Video, Paramount+.

TMDB does not expose expiration dates. "Leaving soon" is not implementable and
shouldn't be attempted. Daily diffing catches departures the day after they
happen, which is the accepted tradeoff.

## Page sections

In order: summary counts (streaming / new / gone), new since yesterday, under
100 minutes, everything currently streaming.

A `--kid-awake` equivalent (G/PG via the release-dates endpoint, runtime under
40m) is planned but not needed until 2027. Don't build it yet.

## Reliability

- Write output to a temp file, then `os.replace()` into place. Never leave a
  half-written page.
- Log every run with a timestamp and a movie count. A silently broken sync that
  shows stale data forever is the failure mode to design against.
- Read all paths from `.env` or `Path.home()`. Never hardcode `/Users/<name>/`
  — this is a public repo.

## Git

Gitignore before the first commit, not after:

```
.env
*.log
data/movies.db
```

The database is regenerable and would produce a binary diff daily. The text
files and `resolved.json` are the real data and should be versioned.

If the TMDB key ever lands in a commit, don't rewrite history — revoke it in the
TMDB dashboard and issue a new one.

`sync.py` ends with add/commit/push to `docs/`. That's the only network write.

## Build order

1. Resolver — parse both text files, hit TMDB search with year, populate
   `resolved.json` and `unresolved.txt`. Run it, hand-fix the outliers.
2. Provider sync — fetch availability, append to `availability`.
3. Renderer — query, diff against yesterday, fill `template.html`, atomic write
   to `docs/index.html`.
4. launchd plist — only after the script runs clean by hand.

## launchd

`~/Library/LaunchAgents/com.<user>.watchlist.plist`, `StartCalendarInterval` at
04:00, `RunAtLoad` false, `StandardErrorPath` set to a log file.

launchd is used over cron deliberately: if the Mac is asleep at 04:00, cron
skips the run silently, launchd queues it and fires on wake.

Load with `launchctl bootstrap gui/$(id -u) <plist>`.
