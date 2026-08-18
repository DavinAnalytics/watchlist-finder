"""Shared config, TMDB client, and db helpers for the watchlist scripts.

Stdlib only, on purpose: no pip install step, nothing to keep up to date.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Paths are anchored to this file, never to a hardcoded /Users/<name>/.
# WATCHLIST_DIR in .env overrides, for when the repo moves.
ROOT = Path(__file__).resolve().parent

TMDB_BASE = "https://api.themoviedb.org/3"

# Consecutive failed API calls before a run gives up. One dead network (or a
# missing CA bundle) is a single fault, not N per-title faults: without this a
# script retries every title with backoff, takes minutes, and still exits 0
# having filed the whole list under "needs a human".
CONSECUTIVE_FAILURE_LIMIT = 5
HTTP_TIMEOUT = 15
USER_AGENT = "watchlist/1.0 (personal, local)"


def load_env(path=None):
    """Parse a KEY=VALUE .env into a dict. No shell, no exec, no interpolation."""
    path = Path(path) if path else ROOT / ".env"
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # Strip one layer of matching quotes, if present.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


ENV = load_env()


def config_dir():
    override = ENV.get("WATCHLIST_DIR") or os.environ.get("WATCHLIST_DIR")
    return Path(override).expanduser() if override else ROOT


DIR = config_dir()
DATA_DIR = DIR / "data"
DOCS_DIR = DIR / "docs"
FAVORITES_PATH = DATA_DIR / "favorites.txt"
WATCHLIST_PATH = DATA_DIR / "watchlist.txt"
OWNED_PATH = DATA_DIR / "owned.txt"
RESOLVED_PATH = DATA_DIR / "resolved.json"
UNRESOLVED_PATH = DATA_DIR / "unresolved.txt"
DB_PATH = DATA_DIR / "movies.db"
TEMPLATE_PATH = DIR / "template.html"
OUTPUT_PATH = DOCS_DIR / "index.html"
LOG_PATH = DIR / "watchlist.log"


def api_key():
    key = ENV.get("TMDB_API_KEY") or os.environ.get("TMDB_API_KEY")
    if not key:
        raise SystemExit(
            "TMDB_API_KEY is not set. Put it in .env as TMDB_API_KEY=... "
            f"({DIR / '.env'}); .env is gitignored."
        )
    return key


def setup_logging(name):
    """Log to stdout and to watchlist.log, timestamped. Returns a logger."""
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    try:
        f = logging.FileHandler(LOG_PATH, encoding="utf-8")
        f.setFormatter(fmt)
        logger.addHandler(f)
    except OSError as exc:  # log file unavailable is not worth aborting a run
        logger.warning("could not open log file %s: %s", LOG_PATH, exc)
    return logger


# --- atomic writes -----------------------------------------------------------


def atomic_write(path, text):
    """Write via temp file in the same dir, then os.replace. Never a half file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Leave no debris and, critically, leave the previous file intact.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json_or_empty(path):
    path = Path(path)
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    data = json.loads(raw)  # a corrupt cache must crash, not silently reset
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def save_json(path, data):
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


# --- input parsing -----------------------------------------------------------

YEAR_RE = re.compile(r"^\d{4}$")
# "[Amazon Prime]" in owned.txt. Anchored and non-greedy about nothing — a
# real film title starting with "[" and ending with "]" would be misread as a
# store, which is a trade accepted for a format that has to stay hand-editable
# on a phone.
STORE_RE = re.compile(r"^\[(.+)\]$")


def parse_entries(path, logger=None):
    """Year-grouped plain text -> [(raw_title, year)].

    A bare 4-digit line sets the current year; every other non-blank line is a
    film under it. Lines appearing before any year line are skipped and logged
    (the Apple Notes export starts with a 'Movies' header). A line starting
    with '#' is a comment — no film title in either list starts with one, and
    owned.txt needs a header explaining its own format to whoever edits it
    next.
    """
    path = Path(path)
    if not path.exists():
        if logger:
            logger.warning("input file missing: %s", path)
        return []

    entries = []
    year = None
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if YEAR_RE.match(raw):
            year = int(raw)
            continue
        if year is None:
            if logger:
                logger.info("%s:%d: skipping %r before any year line", path.name, lineno, raw)
            continue
        entries.append((raw, year))
    return entries


def parse_owned(path, logger=None):
    """owned.txt -> [(raw_title, year, store)].

    parse_entries() plus a store dimension: a "[Store]" line sets the current
    store the way a bare year line sets the current year. Kept as its own
    function rather than a flag on parse_entries() because the return shape
    differs — a caller that forgot to unpack three values would otherwise get
    a silent mis-assignment rather than an error.

    A title before any store line is skipped and logged, exactly as one before
    any year line is: ownership with no store attached can't be displayed and
    guessing a store would invent a fact about where the film actually is.
    """
    path = Path(path)
    if not path.exists():
        if logger:
            logger.warning("input file missing: %s", path)
        return []

    entries = []
    year = None
    store = None
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        match = STORE_RE.match(raw)
        if match:
            store = match.group(1).strip()
            # A new store restarts the year grouping. Without this, the first
            # titles under a store would silently inherit the previous store's
            # last year — a wrong year is a wrong TMDB search.
            year = None
            continue
        if YEAR_RE.match(raw):
            year = int(raw)
            continue
        if store is None or year is None:
            if logger:
                missing = "store" if store is None else "year"
                logger.info("%s:%d: skipping %r before any %s line", path.name, lineno, raw, missing)
            continue
        entries.append((raw, year, store))
    return entries


# --- TMDB --------------------------------------------------------------------


class TMDBError(RuntimeError):
    pass


class TMDBAuthError(TMDBError):
    """Bad or revoked API key. Never caught per-title: it aborts the run.

    Swallowing this would file every remaining title under 'needs a human',
    which is a config failure wearing the costume of a normal result.
    """


class TMDB:
    """Minimal TMDB v3 client: timeouts, polite pacing, 429 retry."""

    def __init__(self, key, logger=None, pause=0.25):
        self._key = key
        self._log = logger
        self._pause = pause
        self._last_call = 0.0

    def get(self, path, **params):
        params["api_key"] = self._key
        url = f"{TMDB_BASE}{path}?" + urllib.parse.urlencode(params)

        for attempt in range(4):
            gap = time.monotonic() - self._last_call
            if gap < self._pause:
                time.sleep(self._pause - gap)
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                self._last_call = time.monotonic()
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    body = resp.read().decode("utf-8")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise TMDBError(f"{path}: expected a JSON object")
                return data
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                if exc.code in (401, 403):
                    raise TMDBAuthError(
                        f"TMDB rejected the API key (HTTP {exc.code})"
                    ) from exc
                if exc.code == 429:
                    wait = _retry_after(exc, attempt)
                    if self._log:
                        self._log.warning("rate limited, sleeping %.1fs", wait)
                    time.sleep(wait)
                    continue
                if 500 <= exc.code < 600 and attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise TMDBError(f"{path}: HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise TMDBError(f"{path}: {exc}") from exc
        raise TMDBError(f"{path}: gave up after retries")

    def search_movie(self, title, year):
        """Search with the year — never without it; the year is the disambiguator."""
        return self.get(
            "/search/movie",
            query=title,
            year=year,
            include_adult="false",
            language="en-US",
        )

    def movie(self, tmdb_id):
        # append_to_response bundles credits, videos, release_dates and
        # recommendations into this same request — director/cast from
        # credits, the trailer from videos, the US certification from
        # release_dates, and the dialog's "More like this" list from
        # recommendations. Without this it would take four more calls per
        # movie. recommendations joined the list on 2026-08-16 and cost
        # nothing: it is one more query param on a call already being made,
        # not a fifth request, and it returns the same page-1/20-result
        # payload the standalone endpoint does.
        return self.get(
            f"/movie/{int(tmdb_id)}",
            language="en-US",
            append_to_response="credits,videos,release_dates,recommendations",
        )

    def watch_providers(self, tmdb_id):
        return self.get(f"/movie/{int(tmdb_id)}/watch/providers")

    def recommendations(self, tmdb_id):
        """TMDB's own "people who liked this also liked" list for one movie.

        Page 1 only — 20 results, ordered by TMDB's relevance. recommend.py
        never reads deep into this list, so paging would just spend calls on
        candidates it will not reach.
        """
        return self.get(f"/movie/{int(tmdb_id)}/recommendations", language="en-US", page=1)


def _retry_after(exc, attempt):
    header = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return min(float(header), 60.0)
    except (TypeError, ValueError):
        return float(2**attempt)


# --- database ----------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    tmdb_id       INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    year          INTEGER,
    runtime       INTEGER,
    poster_path   TEXT,
    overview      TEXT,
    director      TEXT,
    genres        TEXT,
    vote_average  REAL,
    vote_count    INTEGER,
    top_cast      TEXT,
    trailer_key   TEXT,
    status        TEXT,
    release_date  TEXT,
    certification TEXT,
    similar_count INTEGER
);
CREATE TABLE IF NOT EXISTS favorites (
    tmdb_id INTEGER PRIMARY KEY REFERENCES movies(tmdb_id)
);
CREATE TABLE IF NOT EXISTS watchlist (
    tmdb_id  INTEGER PRIMARY KEY REFERENCES movies(tmdb_id),
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS availability (
    tmdb_id  INTEGER NOT NULL REFERENCES movies(tmdb_id),
    provider TEXT    NOT NULL,
    kind     TEXT    NOT NULL,
    seen_on  DATE    NOT NULL,
    PRIMARY KEY (tmdb_id, provider, kind, seen_on)
);
CREATE INDEX IF NOT EXISTS availability_seen_on ON availability(seen_on);

-- Not in the original four tables, but "gone" is not trustworthy without it:
-- a movie whose provider fetch failed has no rows for today, which is
-- indistinguishable from a movie that genuinely left every service. This
-- records which ids were successfully polled, so the diff can tell those
-- two cases apart instead of inventing a departure.
CREATE TABLE IF NOT EXISTS poll_log (
    tmdb_id   INTEGER NOT NULL REFERENCES movies(tmdb_id),
    polled_on DATE    NOT NULL,
    PRIMARY KEY (tmdb_id, polled_on)
);

-- The daily "Because you liked X" picks (recommend.py). Append-only, like
-- availability: keeping the history is what lets a pick be excluded from the
-- next fortnight's candidates instead of resurfacing every day. source_id is
-- the favorite the pick came from, and is what the page's "Because you liked"
-- line names. A new table rather than a new column, so CREATE TABLE IF NOT
-- EXISTS is enough here and no _migrate() entry is needed.
CREATE TABLE IF NOT EXISTS recommendations (
    tmdb_id   INTEGER NOT NULL REFERENCES movies(tmdb_id),
    source_id INTEGER NOT NULL REFERENCES movies(tmdb_id),
    picked_on DATE    NOT NULL,
    PRIMARY KEY (tmdb_id, picked_on)
);

-- Films owned outright, per store (2026-08-18). Mirrors owned.txt and only
-- owned.txt, exactly as favorites/watchlist mirror their own files — an API
-- call's success must never decide whether a row here survives.
--
-- Ownership is the one kind of availability TMDB cannot know: /watch/providers
-- reports what a *service* carries, never what a person bought. So this table
-- is not derived from `availability` and is not date-scoped — an owned film is
-- owned every day until the text file says otherwise, which is exactly why it
-- can't be folded into the poll/diff machinery. It never makes a title "new"
-- and never makes one "gone".
--
-- (tmdb_id, store) rather than tmdb_id alone: the same film can be bought on
-- more than one store, and the page names where.
CREATE TABLE IF NOT EXISTS owned (
    tmdb_id INTEGER NOT NULL REFERENCES movies(tmdb_id),
    store   TEXT    NOT NULL,
    PRIMARY KEY (tmdb_id, store)
);

-- The dialog's "More like this" pool (2026-08-16): TMDB's recommendations for
-- one movie, cached so the page can show five of them without a daily fetch.
--
-- similar_id deliberately carries NO foreign key to movies. Every other
-- tmdb_id column here references a title on one of the two lists; these are
-- explicitly titles that are not, and never will be unless hand-added. A FK
-- would make the insert fail for exactly the discovery suggestions that are
-- the entire point of the feature.
--
-- title/year/poster_path are denormalized for that same reason — there is no
-- movies row to join to, and re-fetching 20 detail records per title to fill
-- one would cost ~1500 API calls a run. The recommendations payload already
-- carries all three, so they are simply stored as they arrive.
--
-- Nothing here is filtered on the way in: this is a faithful cache of what
-- TMDB returned, and which of them the page actually shows (a year floor,
-- excluding titles already on a list) is decided at render time. That is the
-- opposite of recommend.py's MIN_YEAR, which is applied at collection —
-- there a rejected candidate saves a real provider poll, so filtering early
-- has a cost to save. Here every candidate is free once the row exists, so
-- filtering early would only bake a reversible display choice into the data.
CREATE TABLE IF NOT EXISTS similar (
    tmdb_id     INTEGER NOT NULL REFERENCES movies(tmdb_id),
    similar_id  INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    year        INTEGER,
    poster_path TEXT,
    rank        INTEGER NOT NULL,
    PRIMARY KEY (tmdb_id, similar_id)
);
"""

# TMDB provider names for the services actually subscribed to. Matching is on
# a normalized substring because TMDB ships variants ("Netflix basic with Ads",
# "Peacock Premium Plus"). Reseller add-ons ("... Amazon Channel") are not the
# subscription and are deliberately excluded.
SUBSCRIBED = {
    "Netflix": ("netflix",),
    "Hulu": ("hulu",),
    "Peacock": ("peacock",),
    "Prime Video": ("amazon prime video", "prime video"),
    "Paramount+": ("paramount plus", "paramount+"),
    # Not yet subscribed to as of 2026-08-11 — added ahead of time so the day
    # a real subscription starts, the page reflects it with no code change.
    # TMDB's raw provider_name for each is confirmed live, not assumed:
    # "HBO Max" (never bare "Max" — that needle would also catch "Cinemax"),
    # "Apple TV" (never "Apple TV+" — TMDB doesn't carry the "+").
    "Max": ("hbo max",),
    "Apple TV+": ("apple tv",),
}


def subscription_for(provider_name):
    """-> the subscribed service this TMDB provider name is, or None."""
    name = (provider_name or "").strip().casefold()
    # "store" excludes "Apple TV Store" (a rent/buy storefront, unrelated to
    # the Apple TV+ subscription) from the "apple tv" needle above — the same
    # kind of false-positive "channel" already guards against for resellers.
    # Both guards are global, not per-service: adding a future SUBSCRIBED
    # entry whose real flatrate name happens to contain "channel" or "store"
    # would silently never match anything. Check new needles against both
    # before adding them, the same way "hbo max"/"apple tv" were confirmed
    # live against the real API rather than assumed.
    if not name or "channel" in name or "store" in name:
        return None
    for service, needles in SUBSCRIBED.items():
        if any(n in name for n in needles):
            return service
    return None


# TMDB's watch/providers kinds this project stores. Shared between
# sync_providers.py (what to fetch) and render.py (what to read back), so the
# two can't drift out of sync the way two copies of the same literal would.
KIND_FLATRATE = "flatrate"
FREE_KINDS = ("ads", "free")
# Unlike FREE_KINDS, no filtering is applied to what's stored here: TMDB's
# rent/buy lists are already clean storefronts (Amazon Video, Apple TV Store,
# Google Play Movies, ...), not the reseller-channel noise the ads/free
# bucket carries. No price is available on the free tier — never was, never
# will be — this only ever shows *where*, not *how much*.
RENT_BUY_KINDS = ("rent", "buy")
# The kinds that can make a title count as "watchable" — a subscription, or
# YouTube's free tier. Deliberately excludes RENT_BUY_KINDS: paying per title
# is not the same as it being on something already subscribed to. Shared so
# render.py and recommend.py can't drift on what "streaming" means.
STREAMING_KINDS = (KIND_FLATRATE, *FREE_KINDS)

# Ad-supported "free to watch" tiers, kept separate from SUBSCRIBED: these
# aren't something paid for, they're free to anyone. TMDB lists them under the
# 'ads'/'free' kinds, not 'flatrate'. Scoped to YouTube only, on request —
# TMDB's ad-supported bucket also includes Tubi, Pluto TV, The Roku Channel,
# Cineverse and others that were never asked for and would flood the page.
FREE_TIERS = {
    "YouTube (free)": ("youtube free",),
}


# Brand colors for the service tag on the page. Every SUBSCRIBED and
# FREE_TIERS label needs an entry; TAG_FALLBACK covers anything that doesn't
# (there shouldn't be one, since only those two are ever shown, but a missing
# key here would otherwise be a KeyError at render time over a cosmetic).
# YouTube gets its own hue rather than its real-world red, since Netflix
# already owns red in this set and the point of the badge is to read as a
# distinct, free-to-anyone tier, not to be brand-accurate. Max's real brand
# is also purple, colliding with Peacock already in this set — given a
# straight choice between matching a competitor's badge color or reading as
# a wrong service, that's not really a choice, so Max got pushed to a more
# saturated indigo-violet clearly apart from Peacock's lighter one. Apple
# TV+ has no strong signature color in the wild the way the others do;
# Apple's own recognizable system blue stands in for it.
SERVICE_COLORS = {
    "Netflix": "#c8342f",
    "Prime Video": "#2c93bd",
    "Hulu": "#1f9d63",
    "Paramount+": "#3b5fd6",
    "Peacock": "#8b5cf6",
    "Max": "#5822ff",
    "Apple TV+": "#0071e3",
    "YouTube (free)": "#c9a227",
}
TAG_FALLBACK = "#8a8478"

# An owned tag takes the colour of the service that storefront belongs to
# (owner's call, 2026-08-18): "Owned · Amazon Prime" reads in Prime Video's
# blue, "Owned · YouTube" in YouTube's yellow. Scanning the page, the eye
# groups by *where the film lives* before it reads the words, and a film
# bought on Prime and a film streaming on Prime are the same errand.
#
# Derived from SERVICE_COLORS rather than repeating the hex, so a service
# colour and its owned counterpart can never drift apart.
STORE_COLORS = {
    "Amazon Prime": SERVICE_COLORS["Prime Video"],
    "YouTube": SERVICE_COLORS["YouTube (free)"],
}

# Fallback for a store with no SERVICE_COLORS counterpart — a storefront that
# isn't also a subscription (Apple TV Store, Google Play) has no colour to
# borrow. Keeps the "a store added to owned.txt tomorrow needs no code change
# to look right" property that a bare KeyError would lose.
OWNED_COLOR = "#b5793a"


def owned_label(store):
    """-> the tag text for a film owned on `store`.

    "Owned · Netflix" would be a lie waiting to happen, so the word stays
    first: the tag is primarily a claim about ownership, and the store is the
    qualifier telling you where to go open it.
    """
    return f"Owned · {store}"


OWNED_PREFIX = "Owned · "


def tag_color(label):
    """-> the swatch colour for any tag label the page can show.

    One lookup for services, owned tags and anything unrecognised, so a caller
    can't colour one kind correctly and silently fall back on another.
    """
    if label in SERVICE_COLORS:
        return SERVICE_COLORS[label]
    if label.startswith(OWNED_PREFIX):
        return STORE_COLORS.get(label[len(OWNED_PREFIX):], OWNED_COLOR)
    return TAG_FALLBACK


def free_tier_for(provider_name):
    """-> the free-with-ads label this TMDB provider name is, or None.

    Deliberately not folded into subscription_for(): the badge on the page
    shows whatever this returns verbatim, and "YouTube (free)" is the whole
    point — it must read differently from a service actually paid for.
    """
    name = (provider_name or "").strip().casefold()
    if not name or "channel" in name:
        return None
    for label, needles in FREE_TIERS.items():
        if any(n in name for n in needles):
            return label
    return None


def connect(path=None):
    path = Path(path) if path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add columns CREATE TABLE IF NOT EXISTS can't add to an already-existing
    table. The db is documented as regenerable, but wiping it on every schema
    change would also wipe poll_log and availability history for no reason."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(movies)")}
    additions = {
        "poster_path": "TEXT",
        "overview": "TEXT",
        "director": "TEXT",
        "genres": "TEXT",
        "vote_average": "REAL",
        "vote_count": "INTEGER",
        "top_cast": "TEXT",
        "trailer_key": "TEXT",
        "status": "TEXT",
        "release_date": "TEXT",
        "certification": "TEXT",
        "similar_count": "INTEGER",
    }
    changed = False
    for name, sql_type in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE movies ADD COLUMN {name} {sql_type}")
            changed = True
    if changed:
        conn.commit()
