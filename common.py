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


def parse_entries(path, logger=None):
    """Year-grouped plain text -> [(raw_title, year)].

    A bare 4-digit line sets the current year; every other non-blank line is a
    film under it. Lines appearing before any year line are skipped and logged
    (the Apple Notes export starts with a 'Movies' header).
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
        if not raw:
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
        return self.get(f"/movie/{int(tmdb_id)}", language="en-US")

    def watch_providers(self, tmdb_id):
        return self.get(f"/movie/{int(tmdb_id)}/watch/providers")


def _retry_after(exc, attempt):
    header = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return min(float(header), 60.0)
    except (TypeError, ValueError):
        return float(2**attempt)


# --- database ----------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    tmdb_id     INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    year        INTEGER,
    runtime     INTEGER,
    poster_path TEXT
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
}


def subscription_for(provider_name):
    """-> the subscribed service this TMDB provider name is, or None."""
    name = (provider_name or "").strip().casefold()
    if not name or "channel" in name:
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
# distinct, free-to-anyone tier, not to be brand-accurate.
SERVICE_COLORS = {
    "Netflix": "#c8342f",
    "Prime Video": "#2c93bd",
    "Hulu": "#1f9d63",
    "Paramount+": "#3b5fd6",
    "Peacock": "#8b5cf6",
    "YouTube (free)": "#c9a227",
}
TAG_FALLBACK = "#8a8478"


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
    if "poster_path" not in cols:
        conn.execute("ALTER TABLE movies ADD COLUMN poster_path TEXT")
        conn.commit()
