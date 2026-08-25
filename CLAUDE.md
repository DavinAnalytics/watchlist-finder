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
- **No client-side logic that duplicates what render.py already computes.**
  Sorting, grouping, filtering, diffing, and deciding what's available all
  happen once, in Python, at generation time. The browser never re-derives any
  of that. Pure presentation — toggling the visibility of HTML render.py already
  emitted, with nothing recomputed and no data that wasn't already on the page
  — is fine; the collapsed "not currently streaming" sections already do this
  with native `<details>`/`<summary>`, no script required. Clarified
  2026-08-10: the original wording ("zero JavaScript, HTML and CSS only") was
  read as an absolute during that day's design-import work and cost a real
  feature (a tap-to-expand detail view) that this looser, more precise rule
  would actually have allowed.
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
│   ├── owned.txt          # films bought outright, grouped by store
│   ├── resolved.json      # raw input string -> tmdb_id cache
│   ├── unresolved.txt     # titles that need a human
│   └── movies.db          # SQLite (gitignored, regenerable)
├── docs/
│   └── index.html         # generated; served by GitHub Pages
├── common.py              # paths, .env, TMDB client, schema, atomic write
├── resolver.py            # step 1: text -> tmdb ids
├── sync_providers.py      # step 2: availability
├── recommend.py           # step 2b: today's "because you liked" picks
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

A line starting with `#` is a comment, in all three files (added 2026-08-18
with `owned.txt`, which needs a header explaining its own format to whoever
edits it next). No film title in any list starts with one.

`owned.txt` (2026-08-18) adds one dimension on top of that format: a
`[Store]` line sets the current store the way a bare year line sets the
current year.

```
[Amazon Prime]

1999
Fight Club

[YouTube]

1999
The Matrix
```

A new `[Store]` **resets the year to None**. Without that, the first titles
under a store would silently inherit the previous store's last year, and a
wrong year is a wrong TMDB search — the one failure this format exists to
prevent. A title before any store line is skipped and logged, exactly as one
before any year line is: ownership with no store attached can't be displayed,
and guessing the store would invent a fact about where the film actually is.

`common.parse_owned()` is a separate function from `parse_entries()`, not a
flag on it, specifically because the return shape differs (three-tuples, not
two) — a caller that forgot to unpack the third value would otherwise get a
silent mis-assignment instead of an error.

## Schema

```sql
movies(tmdb_id PK, title, year, runtime, poster_path,
       overview, director, genres, vote_average, vote_count,
       top_cast, trailer_key, status, release_date, certification,
       similar_count)
favorites(tmdb_id FK)
watchlist(tmdb_id FK, added_at)
availability(tmdb_id FK, provider, kind, seen_on DATE)
poll_log(tmdb_id FK, polled_on DATE)
recommendations(tmdb_id FK, source_id FK, picked_on DATE)
similar(tmdb_id FK, similar_id, title, year, poster_path, rank)
owned(tmdb_id FK, store)
```

Ten of the eleven non-original columns were added 2026-08-10/11, after `movies` already
had 67 rows, in four passes for the detail dialog: `poster_path` first, then
`overview`/`director`/`genres`/`vote_average`/`vote_count`, then `top_cast`/
`trailer_key`/`status`/`release_date`, then `certification`. The eleventh,
`similar_count`, followed on 2026-08-16 for the dialog's "More like this" row
(104 rows by then). `CREATE TABLE IF
NOT EXISTS` can't add a column to a table that already exists, so
`common.connect()` runs a small migration after the schema script —
`PRAGMA table_info`, then `ALTER TABLE ... ADD COLUMN` for whatever's missing.
Any new column joins this way; the schema script alone only ever handles a
fresh db. `cast` was avoided as a column name — it's a reserved SQL keyword —
in favor of `top_cast`.

`upsert_movie()`'s INSERT is built from `resolver.ALL_COLUMNS`
(`("tmdb_id", "title", "year") + BACKFILL_COLUMNS`) rather than three
hand-written SQL fragments — a column list, a VALUES placeholder count, an ON
CONFLICT SET clause — kept in sync by careful editing. That pattern needed
fixing after two hand-edits in one session; a miscounted positional tuple
would silently shift values into the wrong columns with no error. Adding a
column now means one line in SCHEMA/`_migrate`/`BACKFILL_COLUMNS` and one key
in the `values` dict inside `upsert_movie()` — nothing else to keep in sync.

Pre-existing rows are backfilled without a separate script: `upsert_movie()`'s
short-circuit (skip re-fetching TMDB details for a row already filled in) now
requires every column in `resolver.BACKFILL_COLUMNS` to be non-NULL, not just
`runtime`. The very next resolver run re-fetches every old row once and fills
in whatever's missing — confirmed five times now, 70 or 71/71 each time
(71 once "No Country for Old Men" joined the watchlist). `overview`/
`director`/`genres`/`top_cast`/`status`/`trailer_key`/`certification` are
stored as `""` rather than left NULL when TMDB genuinely has nothing,
specifically so the short-circuit can tell "fetched, empty" from "never
fetched" — without that, a title with no listed director (or, for
`trailer_key`, plenty of older or obscure titles with no YouTube trailer in
TMDB's data) would re-fetch forever.
`vote_average`/`vote_count` default to `0.0`/`0` instead of staying raw for
the same reason — "TMDB always returns them as numbers" turned out to be an
assumption about response shape, not a guarantee, and trusting it blindly
risked the identical failure mode. `release_date` is stored even when empty,
since empty is itself meaningful there, not a sign the fetch never happened.
`poster_path` is the one column that can legitimately stay NULL after a real
fetch — a title TMDB has no poster for keeps re-fetching every run, accepted,
same tradeoff already made for `runtime IS NULL`, and rare enough not to
matter. `director`/`top_cast`/`trailer_key` come from `credits`/`videos` via
`append_to_response=credits,videos` on the existing `/movie/{id}` call
(`common.TMDB.movie()`) — two extra query params, not two more API calls per
movie. Trailer selection prefers an official `Trailer`, then any `Trailer`,
then a `Teaser` as a last resort, YouTube only — see `resolver._trailer_key()`
for why the full video list has to be scanned rather than assuming a trailer
comes first (it doesn't, reliably). `certification` (the US content rating —
"R", "PG-13", ...) comes the same way, via `release_dates` added to the same
`append_to_response` — and `recommendations` joined them on 2026-08-16 for the
dialog's "More like this" row: four query params total now, still one API
call. That last one is worth noting as a cost that never appeared — the
obvious build would have been a `/movie/{id}/recommendations` call per title,
77 extra requests a run forever; appending it to a request already being made
made the whole feature free, and it returns the identical page-1/20-result
payload the standalone endpoint does.
TMDB carries a separate release_dates entry per release event (festival,
premiere, theatrical, digital, physical) and the rating isn't reliably
attached to any particular one, so `resolver._certification()` takes the
first non-empty value found in whatever order TMDB lists them.

`favorites` and `watchlist` are separate tables sharing `movies`.

`availability` gets one row per movie/provider/day. This history is the entire
reason the "new since yesterday" and "gone" sections can exist. Never overwrite
it; only append.

`poll_log` records which ids were *successfully* polled on a given day. Without
it, a movie whose provider fetch failed has no availability rows for today,
which is indistinguishable from a movie that genuinely left every service — and
the diff invents a departure on every network hiccup. Anything absent from
`poll_log` for a date is unknown for that date, not gone.

`recommendations` is append-only for the same reason `availability` is: the
history is what lets `recommend.py` refuse to re-pick anything chosen in the
last `RECENT_DAYS` (14). Without it, a title several favorites all recommend
would win every day and the section would never change. It was added as a new
*table*, so `CREATE TABLE IF NOT EXISTS` covers both a fresh db and the
existing one — no `_migrate()` entry, unlike the eleven added columns above.

`similar` (2026-08-16) is the one table here that is **not** append-only, and
is deliberately the opposite of `recommendations` on every axis:

- **It is replaced, not appended.** `upsert_movie()` deletes a title's rows
  and rewrites them. Every other history in this schema exists because
  something reads the *past* — "new since yesterday", "don't re-pick for a
  fortnight". Nothing reads a past state of this list, so keeping one would
  only let a title TMDB has since dropped keep surfacing forever.
- **`similar_id` carries no foreign key**, unlike every other `tmdb_id`
  column. These are explicitly titles *not* on either list — that's the whole
  point of a discovery row — and `PRAGMA foreign_keys` is ON, so a FK would
  make the insert fail for exactly the rows the feature exists to store.
  `title`/`year`/`poster_path` are denormalized for the same reason: there is
  no `movies` row to join to, and fetching one per suggestion would cost
  ~1500 API calls a run instead of zero.
- **Nothing is filtered on the way in.** The table is a faithful cache of what
  TMDB returned; which entries the page shows is decided at render time. This
  is the reverse of `recommend.py`'s `MIN_YEAR`, and the difference is not
  stylistic: there, rejecting a candidate early saves a real provider poll, so
  filtering at collection has a cost to save. Here every stored candidate is
  free, so filtering early would only bake a reversible display choice into
  the data and force a re-fetch to undo.

`similar_count` on `movies` is what makes the backfill work: it is the
"fetched, empty" marker for a table rather than a column, exactly as `""`
serves `overview`/`trailer_key`. NULL means never fetched, `0` means fetched
and TMDB had nothing. Without it the short-circuit would have to test "has
`similar` rows", and any title with genuinely no recommendations would
re-fetch every run forever.

**`owned` (2026-08-18) is the one kind of availability TMDB cannot know.**
`/watch/providers` reports what a *service* is carrying; it has no idea what
sits in a personal library. So `owned` mirrors `owned.txt` and only
`owned.txt`, under the same contract as `favorites`/`watchlist` — an API
call's success must never decide whether a row survives — with the row key
one dimension wider, `(tmdb_id, store)`, because the same film can be bought
on more than one store and the page names where. *Alien: Covenant* is
currently the live case, owned on both Amazon Prime and YouTube.

The important consequence: **owned is not date-scoped and must never enter
the poll/diff machinery.** An owned film is owned every day until the text
file says otherwise. If ownership were written into `availability` instead, a
film would go "new" the day it was typed into the text file and "gone" the
day it was deleted — neither of which is a change in what any service is
doing, and both of which would be a lie in the one section of the page whose
whole value is that it only reports real changes. `render.merge_owned()`
therefore runs *after* new/gone are computed and after `len(today)` is fixed
for the coverage banner, and it returns a copy rather than mutating the
poll-derived snapshot. All four of those properties are worth re-verifying if
that code is ever touched; they were verified when it shipped.

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
Subscribed services: Netflix, Hulu, Peacock, Prime Video, Paramount+. Also
tracked, added 2026-08-11, not yet actually subscribed to: Max, Apple TV+ —
the owner asked for them ahead of time, "might subscribe in the future," so
the day a real subscription starts the page reflects it immediately with no
code change. Until then their titles show as streaming on the page whether
or not the subscription actually exists yet; that's the accepted tradeoff of
adding a service preemptively rather than something to fix.

Store *every* US flatrate provider, not just the subscribed seven, and filter
at render time through `common.subscription_for()`. Changing which services
are subscribed to then costs nothing and needs no re-fetch. The corollary is a
rule: every read of `availability` goes through `subscription_for()`. TMDB
ships variants ("Netflix Standard with Ads", "Paramount Plus Essential") that
must match.

**Needles match as a prefix, not a substring** — changed 2026-08-25, replacing
a blanket `"channel" in name` rejection. TMDB names a reseller entry
`<content service> <reseller> Channel`, so the catalog a title actually sits
in is always the *leading* name, and reading the prefix answers the only
question that matters ("is this in the catalog of something already paid
for?"):

```
"Paramount+ Amazon Channel"  -> Paramount+   subscribed, so it counts
"Starz Apple TV channel"     -> None         Starz isn't subscribed — and
                                             this is not Apple TV+ either
```

The old rule got the second case right and the first case wrong, throwing away
every reseller row including ones whose content service *is* subscribed. The
symptom: *Strange Darling* read as "not currently streaming" while sitting on
Paramount+, because TMDB listed it only under the Amazon and Roku channels and
never under a direct entry. Found by the owner on the actual service, not by
the pipeline.

The evidence for the change was measured against the live table, not assumed:
of the 14 titles then carrying a direct Paramount+ row, **14 also carried a
channel row and 0 carried only a direct row** — the channel catalogs mirror
the real one, so a channel-only listing is a TMDB data gap rather than a
genuinely different catalog. Exactly four names changed classification across
all 74 ever stored (`Paramount+ Amazon Channel`, `Paramount+ Roku Premium
Channel`, `HBO Max Amazon Channel`, `Apple TV Amazon Channel`), and every
unsubscribed reseller — Starz, Cinemax, AMC+, Shudder, MGM+ — was re-verified
as still excluded. Re-run that diff if the needles are ever touched.

The `"store"` guard stays, and is now the only global exclusion:
TMDB's raw name for Apple TV+ is bare `"Apple TV"`, which would otherwise
prefix-match the unrelated rent/buy storefront `"Apple TV Store"`. TMDB's raw
name for Max is `"HBO Max"`, never bare `"Max"` — that needle would also
catch `"Cinemax"`. Both raw names were confirmed live against real API
responses before being hardcoded, not assumed from memory.

**Known, unhandled: Paramount+ Essential vs Premium.** TMDB emits these as
separate provider names and `subscription_for()` maps both to `Paramount+`.
Measured 2026-08-25: Essential is an exact subset of Premium (12 titles on
both, 0 Essential-only, 2 Premium-only). So the collapse is harmless *on a
Premium subscription* and would produce false positives on Essential — the
worse direction of error, since it sends you to a title that isn't there.
Left as-is deliberately, because the owner is on Premium. If that ever
changes, split the needles rather than widening them.

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

**Rent/buy providers are stored too**, added 2026-08-11, but on a different
rule than flatrate/ads: no filtering. TMDB's `rent`/`buy` kinds are already
clean storefronts (Amazon Video, Apple TV Store, Google Play Movies, ...),
not the reseller-channel noise the ads/free bucket carries, so every entry is
kept (`common.RENT_BUY_KINDS`). These never count as "watchable" — they don't
go through `subscription_for()`/`free_tier_for()`, don't affect the counts,
and don't appear as a service tag. They only ever show as a "Rentable on ..."
line in the detail dialog for a released title that isn't streaming anywhere
subscribed (`render.rentable_snapshot()`), and only *where*, never for how
much — TMDB's free API still has no price field, same as ever.

TMDB does not expose expiration dates. "Leaving soon" is not implementable and
shouldn't be attempted. Daily diffing catches departures the day after they
happen, which is the accepted tradeoff.

## Page sections

In order: summary counts (streaming / new / gone), new since yesterday,
watchlist streaming, recommended for you, favorites streaming.

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
dialog, built on `window.React`/`window.ReactDOM` with click-driven state, and
it was not ported that day — read against the pre-2026-08-10 wording of the
JS constraint, that whole component looked disallowed. Under the rule as
clarified since, it isn't: the dialog's content (title, year, runtime,
services) is all data render.py already computes and would already be sitting
in the page's HTML, hidden; opening it needs only a visibility toggle, no
recomputation. Built 2026-08-10, the same day as the clarification: a CSS
`:target` toggle in `render.py`'s `render_dialogs()`. Tapping a row (every
row, streaming or hidden) opens a bottom sheet with a larger poster, genre and
director, top-billed cast, TMDB's user score and vote count, a synopsis, and
a closing line with a trailer link (when TMDB has one) alongside an outbound
link to the title's TMDB page for full cast, reviews, and rental prices the
free API doesn't expose (same day, same schema additions — see Schema).
Every field a title might legitimately be missing (mid-backfill, or TMDB
genuinely has nothing) degrades to just not showing that line, not a blank
or a crash.

**"More like this"**, added 2026-08-16: a horizontally scrolling strip of
five TMDB recommendations at the bottom of every dialog, poster + title +
year. Five *at random* from a pool of up to 20, seeded by poll date and
tmdb_id together — so they rotate daily but hold still across repeat renders
of the same day, the same contract `tonight_pick()` has, and two titles never
draw correlated positions from their pools. `random.sample()` over the whole
pool rather than the top five, or TMDB's relevance order would pin the same
five forever and make the daily rotation invisible.

Two filters, both at render time (`render.similar_snapshot()`): a
`SIMILAR_MIN_YEAR` of 1990, mirroring the owner's standing rule for
`recommend.py`, and anything already on the watchlist or favorites — a
"discovery" row suggesting something already listed is a wasted slot, and it
would link out to TMDB for a title with a perfectly good dialog two taps
away. A candidate with no year is dropped, same reasoning as
`recommend.candidates_for()`: unknown is not the same as recent. Live, those
two filters take 1526 cached rows to 1243 eligible, and every one of the 77
titles still clears five.

The tiles link to TMDB, not to `#m{id}`: these titles are on neither list, so
no dialog exists for them and an internal link would silently do nothing.

Cost is the part worth remembering. Zero API calls (see `append_to_response`
under Schema), and over the wire the page went 32KB → 50KB gzipped — the raw
HTML roughly doubles, but that number is not the one that matters. The ~380
extra posters cost *no* requests at all until a dialog is opened: they sit
inside `.dialog-target { display: none }` and carry `loading="lazy"`, and
browsers don't fetch images in a display:none subtree.

A recommendation pick stored before this shipped has `similar_count` NULL and
renders with no strip until the next day's picks are chosen — correct
degradation, self-healing, not worth spending calls to force.

The tag row shows the real service list, or "not currently streaming" — except
for a title that hasn't released yet, which gets "Releases {date}" instead
(`render.release_badge()`). "Not currently streaming" is technically true but
misleading for something that was never eligible to stream at all. As of
2026-08-10 nothing on either list is actually still unreleased, so this sits
dormant; it activates the next time a genuinely upcoming title is added by
hand.

Two layers guard against this badge going stale, fixed in two review passes
on the same day the feature shipped:

1. `resolver.SETTLED_STATUSES = {"", "Released", "Canceled"}`. Every other
   `BACKFILL_COLUMNS` field is fetched once and frozen forever — that's the
   whole point of the backfill mechanism. `status`/`release_date` can't join
   that treatment: they're a real fact that changes over time, not a
   fetch-quality problem the way an empty `overview` is. `upsert_movie()`'s
   short-circuit now also requires `status` to be in `SETTLED_STATUSES`, so a
   row backfilled as "Post Production" keeps re-fetching — cheaply, only for
   the small number of titles genuinely still pending — until it actually
   settles. Confirmed: 0 extra fetches against the real, all-"Released" db.
   First caught the day this shipped, in the case where `release_date` had
   already been captured; a second review pass the same day found the gap
   still open for a title whose `release_date` had *never* been captured at
   all (empty string, no date to compare against) — that title fell through
   to trusting a frozen `status` string with no freshness check whatsoever.
2. `release_badge()` still compares a captured `release_date` against
   *today's real date*, fresh, every render, rather than trusting `status`
   even when it is fresh — a second, independent check on top of the first,
   not a replacement for it.

Without layer 1, a title added long before release would show a stale
"Releases {date-in-the-past}" badge (or, in the no-`release_date` case, a
static "Not yet released" tag) forever after it actually came out and started
streaming — permanently hiding real, correctly-computed availability. Both
are exactly the "wrong result that looks right" failure this project is
built against.

Two accepted tradeoffs of doing this with no JavaScript, worth knowing rather
than rediscovering:

- Both opening and closing are ordinary anchor navigations, so each pushes a
  browser-history entry. A few titles opened and closed in one session means
  a few back-taps to actually leave the page. The close links point at
  `#_close`, a fragment matching no element, specifically so closing doesn't
  also scroll the page back to the top — a bare `#` would, per the
  fragment-navigation spec, and that's a real bug this shipped with and fixed
  before it went out.
- The dialog carries `role="dialog" aria-modal="true"` but nothing moves
  keyboard/screen-reader focus into it on open — CSS can't do that. This
  asserts slightly more than a no-JS implementation can deliver. Accepted as
  a known gap, not silently ignored: fixing it for real needs the very
  JavaScript this project is built to avoid.

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

**Rental prices are not implementable — *where* to rent is, and that shipped
2026-08-11.** `/watch/providers` returns provider lists per type (`flatrate`,
`rent`, `buy`, `free`, `ads`) and no price field at all. Prices come from
JustWatch, whose API is not free. A "cheap rentals under $N" section cannot
be built on the free tier — same category as "leaving soon". Don't attempt
it; don't approximate it with a made-up price. The dialog's "Rentable on ..."
line (see Providers) is the honest version of this: which storefronts, never
for how much.

"New since yesterday" diffs against the previous poll date, not literally
yesterday. If the Mac slept for two days, yesterday holds no rows and every
title would look new. Only ids present in both polls are compared; an id missing
from either is unknown, not changed.

There was an "under 100 minutes" section. It was removed on 2026-08-09 — the
runtime limit was its only reason to exist.

**Row cards show genre, not runtime — changed 2026-08-11, owner's call.**
Runtime originally showed on every card; it's now genre instead ("2024 ·
Science Fiction, Adventure" rather than "2024 · 167 min"), on every row-card
variant (streaming and not-currently-streaming alike) and on the "Tonight's
Pick" hero. Runtime didn't disappear from the page — it moved to the detail
dialog, which was deliberately left alone by this change and still shows
year, runtime, and certification together on its own meta line.

A `--kid-awake` equivalent (G/PG via the release-dates endpoint, runtime under
40m) is planned but not needed until 2027. Don't build it yet. The dialog's
`certification` badge (2026-08-11) is not that feature — it's display only, no
new filtering or section, and doesn't move the 2027 timeline.

**"Tonight's Pick"**, added 2026-08-11: a featured hero card above the
summary counts, one title chosen from `new` if anything's new today, else
from `streaming` (`render.tonight_pick()`). The choice is seeded by the poll
date, not truly random — stable across repeat renders on the same day,
changes only when the date does. Links to the same `#m{id}` dialog every
other card for that title would use; no separate dialog markup for it.

**"Recommended for you"**, added 2026-08-12: five daily "Because you liked X"
picks, sitting between watchlist streaming and favorites streaming.
`recommend.py` samples `SOURCE_SAMPLE` (8) favorites at random, pulls TMDB's
`/movie/{id}/recommendations` for each, and walks the candidates
**round-robin** across sources rather than draining one at a time — otherwise
the whole section reads "because you liked" a single film. First source to
offer a title keeps it, so nothing is attributed twice.

**A pick must be streaming on a subscribed service (or free on YouTube).** A
recommendation you can't watch tonight is an advert, not a recommendation. That
means candidates need availability data, and they are on neither list, so
`sync_providers.resolved_ids()` will never poll them — `recommend.py` does its
own polling and writes the same `availability`/`poll_log` rows, same date, same
shape. `render.recommendation_snapshot()` then reads a pick's services through
the identical `subscription_for()`/`free_tier_for()` pass as everything else.
Note the consequence: because Max and Apple TV+ are in `SUBSCRIBED` ahead of
any real subscription (see Providers), a pick can be surfaced on a service not
actually paid for yet. That's the same accepted tradeoff, applied consistently
— a title must not read as streaming in one section and not in another.

**Nothing older than `MIN_YEAR` (1990)**, owner's call 2026-08-12. TMDB's
recommendations lean hard on a film's era, so a favorites list with any older
entry pulls up a steady supply of 70s/80s titles — the first live run surfaced
*52 Pick-Up* (1986) off The Wolf of Wall Street. The rule is applied when
candidates are collected, reading the `release_date` already in the
recommendations response, so a rejected year costs no extra call and never
burns a provider poll. It typically removes about a third of the pool (153 →
105 candidates on the day it shipped). A candidate whose `release_date` TMDB
doesn't carry is dropped too: unknown is not the same as recent, and there are
enough candidates to afford skipping the ambiguous ones. `recommend.py` is the
only place this rule lives — `render.py` deliberately does not re-check it, so
a pick stored before the rule existed stays until it ages out. The one that
existed was deleted by hand and the day topped back up to five, which is what
that mechanism is for.

Costs and bounds, all deliberate:

- `POLL_BUDGET` (40) caps provider polls per run. Candidates aren't curated the
  way the watchlist is, so the hit rate is lower and an uncapped search would
  keep calling until it got lucky. Hitting the cap just means fewer picks,
  which the page renders honestly. In practice the first real run needed only
  8 polls for 5 picks.
- The detail fetch (`resolver.upsert_movie()`) happens *only* for a title that
  already passed the streaming check — it's what gives the pick its card and
  dialog, and satisfies the `movies` foreign key. Rejected candidates cost one
  provider call and nothing else, and get re-polled on a later day; paying a
  detail call per candidate would double the run for data thrown away.
- Seeded by the date, like Tonight's Pick. A day that already has a full set is
  skipped **before any API call**, so re-running `sync.py` by hand doesn't
  reshuffle the page or spend calls. A partial day *tops up*: today's picks are
  excluded from the candidate queue and the counter starts from how many are
  already stored, so an interrupted run adds exactly the shortfall and polls
  only for it. Counting inserts from zero instead would land on five only while
  the queue reproduced identically — which stops being true the moment
  `favorites.txt` is edited between two runs on the same day. Verified:
  dropping 3 of 5 and re-running restored exactly 5 with 3 polls.

**`recommend.py` is the one stage that must not take the page down.** It runs
after `sync_providers.py` (so a dead key or network aborts the run before
anything is spent here), and both of its fetch loops stop on
`CONSECUTIVE_FAILURE_LIMIT`, log loudly, keep whatever was already picked, and
exit 0. Finding nothing is a legitimate outcome, not a failure — it means
nothing TMDB suggested is on a subscribed service today.

That promise is enforced in `sync.py`, not just intended: `STAGES` entries
carry a third `required` field and `recommend` is the only `False`. Any exit
code *or unhandled exception* from it is logged and stepped over, and `render`
still runs. Guarding inside `recommend.py` alone would have covered only the
failure modes already thought of — the point is to survive the ones that
aren't. Verified both ways: a raising stage and a nonzero-exit stage each let
the page render, while a failing *required* stage still stops the run with
`docs/index.html` untouched.

**`poll_log` is now shared between two writers**, and that had a sharp edge.
`render.poll_dates()` took a bare `MAX(polled_on)` over the whole table, so a
`recommend.py` run for a date `sync_providers.py` hadn't reached would become
the page's "latest poll" and render a watchlist that polled nothing. Both of
its queries are now scoped to rows for titles actually on a list
(`render.POLLED_LIST_MEMBER`). The stage order already prevented this; the
scope makes the page's dates independent of that ordering instead of quietly
dependent on it. Every other watchlist/favorites read already joined its list
table, so the counts, the new/gone diff, and the coverage banners were never
exposed — verified, the first run's counts were byte-identical to the same
day's run without the feature.

`render.main()` re-filters picks against current membership before rendering,
since a title can join the watchlist between the pick and a later hand-run of
`render.py`; that also keeps the five dialog groups disjoint.

**Owned films count as watchable**, added 2026-08-18. A film sitting in a
personal library is *more* reliably available than one on Netflix, which can
leave — so filing it under "not currently streaming" was the page stating
something confidently false. Owned titles now get a tag (`Owned · Amazon
Prime`), sit in the streaming sections rather than the collapsed ones, and
are counted in the summary's streaming figure so the number can't contradict
the list beneath it.

**An owned tag takes the colour of the service that storefront belongs to**
(owner's call, same day, replacing a single shared owned colour): `Owned ·
Amazon Prime` renders in Prime Video's blue, `Owned · YouTube` in YouTube's
yellow. Scanning the page, the eye groups by *where the film lives* before it
reads the words, and a film bought on Prime and a film streaming on Prime are
the same errand.

`common.STORE_COLORS` derives those values from `SERVICE_COLORS` rather than
repeating the hex, so a service colour and its owned counterpart cannot drift
apart. `common.OWNED_COLOR` survives as the fallback for a storefront with no
subscription counterpart to borrow from (Apple TV Store, Google Play) —
without it, a store added to `owned.txt` tomorrow would be a `KeyError`
instead of just rendering in the neutral tone.

`common.tag_color()` is the single lookup for services, owned tags and
anything unrecognised, so a caller can't colour one kind correctly and
silently fall back on another.

The word "streaming" in those section headers is now doing slightly loose
duty — an owned film isn't streaming — but the tag says so on every row, and
the alternative (a fourth count, or sections that disagree with their own
counts) is worse on a phone.

**Filter by service**, added 2026-08-11: a pill row (`render.render_filter_bar()`)
tapping "Netflix" and showing only Netflix titles across New/Watchlist
streaming/Favorites streaming. Pure CSS — hidden radio inputs
(`render.render_filter_radios()`) sit as the first children of `<body>`,
before `<main>`, so the `~` sibling combinator can reach in and hide any
`.row-card` whose `data-services` attribute doesn't contain the checked
service's slug (`render._slug()` — must stay in sync with the hardcoded
slugs in template.html's filter CSS, verified against each other, not just
assumed). Owned stores join the same pill row, but their rules are
**generated** by `render.render_owned_css()` rather than hand-written —
stores are user data, not a fixed list, so the slug-drift hazard the service
rules carry is removed outright the same way `render_alpha_css()` removes it.
`render.owned_stores()` reads the distinct stores from the table, so a store
whose last title was sold back stops offering a pill that matches nothing.
The counts at the top stay fixed at their true unfiltered totals;
recomputing them per filter isn't something CSS can do. Collapsed "not
currently streaming" sections are untouched by the filter on purpose — they
carry no service tag to filter by, and hiding some of a `<details>` whose
`<summary>` states a fixed count (e.g. "(6)") would make that count read
wrong while filtered. A filter with zero matching titles just leaves that
section visually blank; there's no CSS way to detect "everything in this
list is hidden" and swap in a fallback message without JavaScript. Accepted,
same spirit as the dialog's other no-JS tradeoffs.

**A–Z title index**, added 2026-08-14 in place of a search box. **Free-text
search is not implementable without JavaScript** — CSS cannot compare what's
typed in an input against the text of other elements, because typing changes
the DOM *property*, not the `value` *attribute* an attribute selector reads.
Same category as "leaving soon" and rental prices: don't attempt it, don't
approximate it. Offered as a choice, owner picked the letter index over
amending the JS constraint.

Mechanics mirror the service filter — hidden radios before `<main>`, the `~`
combinator, `data-initial` on each card — with three deliberate differences:

1. **A separate radio group** (`name="alpha"`, not `name="filter"`), so a
   letter and a service can both be active and *intersect* rather than
   resetting each other.
2. **It reaches the dimmed "not currently streaming" cards**, which the
   service filter deliberately does not. Over half the library lives inside
   those collapsed blocks; an index that couldn't reach them would miss most
   of the list, which defeats the point. The consequence is that their
   `<summary>` states an unfiltered "(N)" — so the count is wrapped in
   `<span class="hidden-count">` and the generated CSS hides it whenever any
   letter is active. The page never shows a number contradicted by the list
   under it. The `<details>` still starts collapsed, so a match inside is one
   extra tap away; CSS can't force a `<details>` open.
3. **The CSS is generated by `render.render_alpha_css()`, not hand-written in
   `template.html`.** There are ~60 rules and the set changes with the
   library. The service rules above them carry a standing "slugs must match
   `_slug()` exactly" hazard that generation removes outright — the rules and
   the markup come from the same values and cannot drift.

Only letters that actually have a title get a pill, so the bar never offers a
dead option. Buckets come from the **raw** title, the same basis `sort_key()`
uses, so the bar agrees with the order on screen: *The Rip* sorts under T on
the page, so it indexes under T. Stripping a leading article (as
`resolver.normalize()` does for fuzzy matching) would file it under R and leave
the bar disagreeing with the list beneath it. The cost is a heavy T bucket —
11 of 80 titles — accepted for that consistency. Digits and any script with no
a–z form go to "#" (id-safe slug `num`), bucketed rather than dropped: a title
reachable from no pill is exactly the silent-wrong-result this project is built
against.

`_initial()` returning **exactly one character from `[a-z#]`** is a security
property, not just a tidiness one: its output is interpolated into the `<style>`
block, where `html.escape()` would not save anything. It was fuzzed over 30,000
inputs — deliberate CSS breakout strings, `$`-template payloads, null bytes,
combining marks, non-Latin scripts, multi-char casefolds (`ß`→`ss`,
`İ`→`i̇`), NFKD ligatures — with zero violations. Keep that guarantee if it is
ever touched.

**The rendered page is no longer fully self-contained.** The reskin's fonts
load from `fonts.googleapis.com`, and posters hotlink `image.tmdb.org` — new
client-side network dependencies for a page whose whole point is reading it on
a phone that might not have a strong connection. Both degrade gracefully
(system font, missing image), so this isn't a functional break, just a
tradeoff to know about if the page ever looks plain or image-less on bad wifi.

## Reliability

- Write output to a temp file, then `os.replace()` into place. Never leave a
  half-written page.
- Log every run with a timestamp and a movie count. A silently broken sync that
  shows stale data forever is the failure mode to design against — including
  when the break is on GitHub's side and every local step reported success.
  See `verify_published()` under Git.
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

`sync.py` ends with add/commit/push, then *verifies the page actually went
live*. The push is the only network write. It commits named files, never a
directory — `docs/index.html`, `data/resolved.json`, `data/unresolved.txt`.
`git add docs` would stage anything that ever lands in `docs/` and push it to a
public repo. `resolved.json` rides along because it carries every hand-pinned
id and GitHub is the backup; committing only the page would leave the repo
permanently dirty and the hand-fixes unbacked-up.

Source changes are a separate, ordinary commit. `sync.py` does not commit code.

**A successful `git push` is not proof the page published.** On 2026-08-14 a
push landed on `main` correctly — `raw.githubusercontent.com` served the new
file — and GitHub queued **no Pages build for it at all**. The site kept
serving a four-hour-old page while `git push` exited 0 and the run logged
success. Nothing noticed; it was found by refreshing a phone. So
`sync.publish()` is now followed by `sync.verify_published()`:

- Polls the live Pages URL (up to `VERIFY_TIMEOUT`, 240s) for the exact
  `<p class="stamp">` line of the page just written. That marker carries the
  run's wall-clock minute, so it can't accidentally match a previously
  deployed page the way a date-only marker could. The request is
  cache-busted — GitHub's CDN serves `max-age=600`, long enough to make a
  fresh deploy look like a failure.
- On failure, pushes **one** empty commit to re-trigger the build (a later
  real push is what unstuck it), waits again, and logs loudly either way.
- Makes the run exit non-zero if the page never went live. Every stage can
  succeed and the run still be a failure — the page is the point.
- The Pages URL is derived from `git remote get-url origin` at runtime, never
  written down: the no-hardcoded-user rule covers the account name too.
- `--no-verify` skips it, for quick manual runs.

**Do not "fix" the cancelled Pages builds.** That history shows a recurring
pattern — a `watchlist: sync` build cancelled seconds before a `data:`/`feat:`
build succeeds — which looks like a race and is not one. GitHub coalesces
pushes that land within seconds, and in all four occurrences the surviving
build published everything correctly. Batching the two pushes was proposed and
rejected for this reason: the actual outage was a *dropped* build on a push
that was already a single push. Chasing the cancellations would have shipped a
fix for a non-problem while the real one stayed silent.

## Build order

1. ~~Resolver~~ — done. `resolver.py`.
2. ~~Provider sync~~ — done. `sync_providers.py`.
2b. ~~Recommendations~~ — done 2026-08-12. `recommend.py`.
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
