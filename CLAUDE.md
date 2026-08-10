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
├── common.py              # paths, .env, TMDB client, schema, atomic write
├── resolver.py            # step 1: text -> tmdb ids
├── sync_providers.py      # step 2: availability
├── render.py              # step 3: the page
├── sync.py                # runs 1-3 in order, then commits and pushes
├── template.html
├── watchlist.log          # every run, timestamped (gitignored)
└── .env                   # TMDB_API_KEY (gitignored)
```

Stdlib only. No requirements.txt, nothing to install, nothing to keep current —
a 4am job that depends on a virtualenv is a 4am job that breaks silently.

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
movies(tmdb_id PK, title, year, runtime, poster_path)
favorites(tmdb_id FK)
watchlist(tmdb_id FK, added_at)
availability(tmdb_id FK, provider, kind, seen_on DATE)
poll_log(tmdb_id FK, polled_on DATE)
```

`poster_path` was added 2026-08-10, after `movies` already had 67 rows.
`CREATE TABLE IF NOT EXISTS` can't add a column to a table that already exists,
so `common.connect()` runs a small migration after the schema script —
`PRAGMA table_info`, then `ALTER TABLE ... ADD COLUMN` if it's missing. Any new
column joins this way; the schema script alone only ever handles a fresh db.

Pre-existing rows are backfilled without a separate script: `upsert_movie()`'s
short-circuit (skip re-fetching TMDB details for a row already filled in) now
requires `poster_path IS NOT NULL` too, not just `runtime`. The very next
resolver run re-fetches every old row once and fills it in. A title TMDB
genuinely has no poster for keeps re-fetching every run — accepted, same
tradeoff already made for `runtime IS NULL`, and rare enough not to matter.

`favorites` and `watchlist` are separate tables sharing `movies`.

`availability` gets one row per movie/provider/day. This history is the entire
reason the "new since yesterday" and "gone" sections can exist. Never overwrite
it; only append.

`poll_log` records which ids were *successfully* polled on a given day. Without
it, a movie whose provider fetch failed has no availability rows for today,
which is indistinguishable from a movie that genuinely left every service — and
the diff invents a departure on every network hiccup. Anything absent from
`poll_log` for a date is unknown for that date, not gone.

Membership in `favorites` and `watchlist` is decided by the text files and only
by the text files. Never let the success of an API call decide whether a row
survives: a transient failure would silently delete a title that is still
listed. Titles are removed by deleting the line, and by nothing else.

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
  threshold, write to `unresolved.txt` and skip. Don't guess. Only the top
  result is considered: a second-guess pulled from further down the result list
  is exactly the silent wrong answer this is meant to avoid.
- Also reject on year: if the top result's release year disagrees with the year
  the entry is listed under, refuse it. Don't trust TMDB's `year` search
  parameter to be a hard filter. This is what caught `Blade Runner` under 2017
  returning the 1982 film.
- Normalization before fuzzy matching (casefold, strip accents/punctuation, drop
  a leading article, and compare with spaces removed too) resolves most of the
  sloppy entries on its own — `Wolf of wallstreet`, `Imitation game`,
  `Dont breathe`, `Tick tick boom`, `Brawl in cell block` all match. What
  genuinely needs a human is shorthand (`EEAAO`, `Django`, `Mad Max`), missing
  spaces that defeat TMDB's own search (`Wolf of wallstreet`, `22 jumpstreet`
  return nothing at all), misspellings (`Cacher` → *Caché*), and real ambiguity
  (`Blade Runner`, `Jungle`). 12 of the first 59 needed pinning.
- `Mother!` — the exclamation mark is part of the title. Keep it.
- Unreleased films (2026 entries) may have no TMDB record. Log and skip; don't
  raise.
- Dedupe on `tmdb_id` after resolution, not on title string.

## Providers

Use `/movie/{id}/watch/providers`, US region, `flatrate` for subscription.
Subscribed services: Netflix, Hulu, Peacock, Prime Video, Paramount+.

Store *every* US flatrate provider, not just the subscribed five, and filter at
render time through `common.subscription_for()`. Changing which services are
subscribed to then costs nothing and needs no re-fetch. The corollary is a rule:
every read of `availability` goes through `subscription_for()`. TMDB ships
variants ("Netflix Standard with Ads", "Paramount Plus Essential") that must
match, and reseller add-ons ("Paramount+ Amazon Channel", "Starz Apple TV
Channel") that must not — they are separate paid subscriptions, not the service.

**YouTube's free-with-ads tier counts as streaming too**, added 2026-08-10 on
request. TMDB lists it under the `ads`/`free` kinds, not `flatrate` — a
different bucket that also contains Tubi, Pluto TV, The Roku Channel, Cineverse
and others nobody asked for. `common.free_tier_for()` matches only YouTube's
entry (`FREE_TIERS`, needle `"youtube free"`) and is checked separately from
`subscription_for()`, never merged into `SUBSCRIBED` — the badge it produces
reads "YouTube (free)" so it's never mistaken for a paid subscription. Two
enforcement points, not one: `sync_providers.py` only stores an `ads`/`free`
row if `free_tier_for()` recognizes it, and `render.py` re-checks on the way
out, so a stray non-YouTube row can't reach the page even if one somehow
reached the table. Every read of `availability` now goes through
`subscription_for()` *or* `free_tier_for()` — update both when adding a rule
that touches what counts as watchable.

TMDB does not expose expiration dates. "Leaving soon" is not implementable and
shouldn't be attempted. Daily diffing catches departures the day after they
happen, which is the accepted tradeoff.

## Page sections

In order: summary counts (streaming / new / gone), new since yesterday,
watchlist streaming, favorites streaming.

Each streaming section is followed by a collapsed "not currently streaming (N)"
block listing the rest of that list. A title that simply vanishes from the page
reads as "not on the list" — the count is what makes the page feel complete
rather than thin. These use native `<details>`/`<summary>`, which collapse
without JavaScript. Membership drives them, not the poll, so a title whose fetch
failed still appears rather than disappearing.

**The look (2026-08-10) is ported from a Claude Design project** ("Organic",
claude.ai/design), not designed from scratch. Fonts (Caprasimo/Figtree), the
warm palette, and the rounded card/tag components come from there; the dark
theme doesn't — that system is light-only, so the dark values in `template.html`
are hand-derived, keeping the same hue family and copying the source system's
own documented elevation strategy (shadow on light, a hairline border on dark)
rather than inventing one. The source mock also included a tap-a-row detail
dialog built on `window.React`/`window.ReactDOM` with click-driven state — that
is JavaScript by construction and there is no CSS-only equivalent, so it was
not ported. Zero JS is a hard constraint, not a style preference; a future
"can we get the detail view back" has the same answer.

Each streaming title's card shows a real TMDB poster (`/t/p/w92{poster_path}`,
hotlinked from TMDB's own CDN, no image is stored or proxied) or a decorative
placeholder when TMDB has none. The collapsed "not currently streaming" rows
deliberately carry no poster and no service tags — title and meta only, dimmed
— so the two states read as visually distinct at a glance, not just by list
position. Service tags are colored per `common.SERVICE_COLORS`; a service with
no entry there falls back to `common.TAG_FALLBACK` rather than breaking.

The counts and the first two sections are watchlist-only. The favorites section
is additive, is deduped against the watchlist so nothing renders twice, and is
the one place availability touches favorites — they are still never *filtered*
by it, and the list stays complete elsewhere as a dedupe check and as something
to paste into a chat when looking for recommendations.

**Rental prices are not implementable.** `/watch/providers` returns provider
lists per type (`flatrate`, `rent`, `buy`, `free`, `ads`) and no price field at
all. Prices come from JustWatch, whose API is not free. A "cheap rentals under
$N" section cannot be built on the free tier — same category as "leaving soon".
Don't attempt it; don't approximate it with a made-up price.

"New since yesterday" diffs against the previous poll date, not literally
yesterday. If the Mac slept for two days, yesterday holds no rows and every
title would look new. Only ids present in both polls are compared; an id missing
from either is unknown, not changed.

There was an "under 100 minutes" section. It was removed on 2026-08-09 — the
runtime limit was its only reason to exist. Runtime still shows per title.

A `--kid-awake` equivalent (G/PG via the release-dates endpoint, runtime under
40m) is planned but not needed until 2027. Don't build it yet.

## Reliability

- Write output to a temp file, then `os.replace()` into place. Never leave a
  half-written page.
- Log every run with a timestamp and a movie count. A silently broken sync that
  shows stale data forever is the failure mode to design against.
- Read all paths from `.env` or `Path.home()`. Never hardcode `/Users/<name>/`
  — this is a public repo.
- **One fault is one fault, not N.** Five consecutive API failures aborts the
  run (`common.CONSECUTIVE_FAILURE_LIMIT`). A missing CA bundle once turned one
  dead connection into 59 per-title failures that took seven minutes and still
  exited 0, with every title filed under "needs a human".
- **A systemic failure must not wear a per-title costume.** A 401/403 raises
  `TMDBAuthError` and aborts rather than being caught by per-title handling.
- **Full-rewrite files must not be written from a partial pass.** `unresolved.txt`
  is rewritten wholesale each run, so an aborted run leaves the previous file
  alone and logs that it is stale. Writing it early would delete every title the
  run never reached and leave a worklist that looks nearly clean.
- **Say so on the page when the data is thin.** The page carries a banner when
  the latest poll isn't today, and when either list was polled below
  `MIN_POLL_COVERAGE`. A sparse page that looks authoritative is the enemy.
- A corrupt `resolved.json` crashes the run. It never resets, and never quietly
  drops the affected title.

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

`sync.py` ends with add/commit/push. That's the only network write. It commits
named files, never a directory — `docs/index.html`, `data/resolved.json`,
`data/unresolved.txt`. `git add docs` would stage anything that ever lands in
`docs/` and push it to a public repo. `resolved.json` rides along because it
carries every hand-pinned id and GitHub is the backup; committing only the page
would leave the repo permanently dirty and the hand-fixes unbacked-up.

Source changes are a separate, ordinary commit. `sync.py` does not commit code.

## Build order

1. ~~Resolver~~ — done. `resolver.py`.
2. ~~Provider sync~~ — done. `sync_providers.py`.
3. ~~Renderer~~ — done. `render.py`, plus `sync.py` to chain and publish.
4. ~~launchd plist~~ — done. `install_launchd.py` generates and bootstraps it;
   `--uninstall` removes it.

Built and live 2026-08-09: 67 titles resolved, 0 unresolved, publishing to
GitHub Pages on a 04:00 schedule.

## launchd

`~/Library/LaunchAgents/com.<user>.watchlist.plist`, `StartCalendarInterval` at
04:00, `RunAtLoad` false, `StandardErrorPath` set to a log file.

launchd is used over cron deliberately: if the Mac is asleep at 04:00, cron
skips the run silently, launchd queues it and fires on wake.

Run `python3 install_launchd.py` to generate and bootstrap it. The generator
derives every path at runtime (`sys.executable`, `Path(__file__)`, `getpass`),
so no username is hardcoded in the repo even though the plist it writes contains
absolute paths — the plist lives outside the repo and is not versioned.

Verify a change with `launchctl kickstart -p gui/$(id -u)/com.<user>.watchlist`
rather than waiting for 04:00. launchd runs with a minimal environment, so
"works in my shell" proves nothing about whether `git` and its credentials
resolve inside the job.
