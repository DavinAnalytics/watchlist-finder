#!/usr/bin/env python3
"""Step 2: today's streaming availability.

For every resolved movie, fetch /movie/{id}/watch/providers (US) and append one
row per movie/provider/day into the availability table.

availability is append-only. Nothing here updates or deletes a row: that history
is the only reason the "new since yesterday" and "gone" sections can exist, and
a rewritten past makes both of them lie.

All US flatrate providers are stored, not just the subscribed five. Filtering to
the subscription happens at render time, so changing which services are
subscribed to doesn't require re-fetching anything.

TMDB's ad-supported bucket ('ads'/'free' kinds) is not stored wholesale — it
also carries Tubi, Pluto TV, The Roku Channel, Cineverse, and more that were
never asked for. Only entries common.free_tier_for() recognizes (YouTube's free
tier, currently) are kept, so the availability table doesn't silently grow to
track services nobody subscribed to or asked about.

TMDB's /watch/providers is a delayed copy of JustWatch. A title can be free
on YouTube today (Prisoners, 2026-09-02) and still missing from TMDB for
hours or days. After the TMDB fetch, this stage asks JustWatch whether the
title is on YouTube Free (package 235, ads/free, US) and adds that row if
TMDB didn't already have it. JustWatch is enrichment, not a second source
of truth for the rest of the catalog: a failure is logged and skipped, the
TMDB rows still write, and five failures in a row disable JustWatch for
the rest of the run. It must never abort a provider sync.

Rent/buy providers ('rent'/'buy' kinds) are stored wholesale, unlike ads/free
— that bucket is already clean storefronts, no reseller-channel noise to
filter out. Shown on the page as *where* a non-streaming title can be
rented, never *for how much*; TMDB's free API has no price field.
"""

import argparse
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request

import common

REGION = "US"
JW_GRAPHQL = "https://apis.justwatch.com/graphql"
JW_SEARCH_FIRST = 8


class JustWatchError(RuntimeError):
    """JustWatch failed for one title. Never raised out of providers_for()."""


# Search by title, then accept only the edge whose TMDB id matches. Taking
# the first result would be the silent-wrong-answer this project is built
# against — "Prisoners" is also a 2017 show (tmdb 71129) and a 2006 film.
_JW_SEARCH = """
query($country: Country!, $language: Language!, $first: Int!, $filter: TitleFilter) {
  popularTitles(country: $country, first: $first, filter: $filter) {
    edges {
      node {
        content(country: $country, language: $language) {
          externalIds { tmdbId }
        }
        offers(country: $country, platform: WEB) {
          monetizationType
          package { packageId }
        }
      }
    }
  }
}
"""


def resolved_ids(conn):
    """Ids currently on either list.

    Scoped to favorites/watchlist rather than all of movies: movies keeps every
    id ever resolved, so polling it would spend a TMDB call a day, forever, on
    titles deleted from the text files years ago. Reading from the db (not
    resolved.json) also keeps the availability foreign key satisfied — an id
    whose detail fetch failed has no movies row to hang a row off.
    """
    return [
        r["tmdb_id"]
        for r in conn.execute(
            "SELECT tmdb_id FROM favorites "
            "UNION SELECT tmdb_id FROM watchlist ORDER BY tmdb_id"
        )
    ]


def movie_titles(conn):
    """-> {tmdb_id: (title, year)} for JustWatch search.

    Title is what JustWatch can look up; tmdb_id is what we accept a hit on.
    Year travels with the title for the caller's convenience and is not a
    search parameter: JustWatch's ranking is not a hard year filter, so
    trusting it would be the same mistake as trusting TMDB's `year` param.
    """
    return {
        r["tmdb_id"]: (r["title"], r["year"])
        for r in conn.execute("SELECT tmdb_id, title, year FROM movies")
    }


def _free_ads_name(entry):
    """-> provider name to store for an ads/free entry, or None.

    Recognizes YouTube's free tier by name *or* by TMDB provider id. Id 235
    is "YouTube Free"; id 192 is bare "YouTube", which TMDB sometimes files
    in ads/free instead of the dedicated catalog. A name free_tier_for()
    already accepts is stored as-is; an id-only hit is stored as
    common.YOUTUBE_FREE_PROVIDER so render.py's name check still fires.
    """
    if not isinstance(entry, dict):
        return None
    name = (entry.get("provider_name") or "").strip()
    if name and common.free_tier_for(name):
        return name
    try:
        provider_id = int(entry.get("provider_id"))
    except (TypeError, ValueError):
        return None
    if provider_id in common.YOUTUBE_FREE_PROVIDER_IDS:
        # Name didn't match free_tier_for() or was empty. Store the
        # canonical TMDB name so render.py's name check still fires.
        return common.YOUTUBE_FREE_PROVIDER
    return None


def pairs_from_region(region):
    """TMDB US watch/providers region dict -> set of (kind, provider_name).

    Split out of providers_for() so the store rules can be tested against a
    fixture without a TMDB round-trip.
    """
    region = region or {}
    pairs = set()
    for entry in region.get(common.KIND_FLATRATE) or []:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("provider_name") or "").strip()
        if name:
            pairs.add((common.KIND_FLATRATE, name))

    for kind in common.FREE_KINDS:
        for entry in region.get(kind) or []:
            name = _free_ads_name(entry)
            if name:
                pairs.add((kind, name))

    for kind in common.RENT_BUY_KINDS:
        for entry in region.get(kind) or []:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("provider_name") or "").strip()
            if name:
                pairs.add((kind, name))
    return pairs


class YouTubeFreeLookup:
    """JustWatch: is this title on YouTube Free (ads) in the US?

    One search per title, title-string in, tmdb_id match required. Failures
    disable the rest of the run after CONSECUTIVE_FAILURE_LIMIT — the same
    "one fault is one fault" rule as TMDB, except the consequence here is
    "stop asking JustWatch", not "abort the provider sync".
    """

    def __init__(self, logger=None, pause=0.25):
        self._log = logger
        self._pause = pause
        self._last_call = 0.0
        self._consecutive_failures = 0
        self.disabled = False
        self.filled = 0
        self.checked = 0

    def has(self, tmdb_id, title, year=None):
        if self.disabled or not (title or "").strip():
            return False
        try:
            found = self._query(int(tmdb_id), title.strip())
        except JustWatchError as exc:
            self._consecutive_failures += 1
            if self._log:
                self._log.warning(
                    "JustWatch YouTube Free check failed for id %s: %s",
                    tmdb_id, exc,
                )
            if self._consecutive_failures >= common.CONSECUTIVE_FAILURE_LIMIT:
                self.disabled = True
                if self._log:
                    self._log.error(
                        "disabling JustWatch for this run: %d checks in a row "
                        "failed (%s). TMDB rows still write.",
                        self._consecutive_failures, exc,
                    )
            return False
        self._consecutive_failures = 0
        self.checked += 1
        return found

    def _query(self, tmdb_id, title):
        data = self._post({
            "query": _JW_SEARCH,
            "variables": {
                "country": REGION,
                "language": "en",
                "first": JW_SEARCH_FIRST,
                "filter": {"searchQuery": title},
            },
        })
        edges = ((data.get("data") or {}).get("popularTitles") or {}).get("edges") or []
        want = str(tmdb_id)
        for edge in edges:
            node = (edge or {}).get("node") or {}
            content = node.get("content") or {}
            ext = content.get("externalIds") or {}
            if str(ext.get("tmdbId") or "") != want:
                continue
            for offer in node.get("offers") or []:
                if not isinstance(offer, dict):
                    continue
                package = offer.get("package") or {}
                try:
                    package_id = int(package.get("packageId"))
                except (TypeError, ValueError):
                    continue
                kind = (offer.get("monetizationType") or "").strip().casefold()
                if package_id == common.YOUTUBE_FREE_PACKAGE_ID and kind in ("ads", "free"):
                    return True
            return False
        return False

    def _post(self, payload):
        body = json.dumps(payload).encode("utf-8")
        gap = time.monotonic() - self._last_call
        if gap < self._pause:
            time.sleep(self._pause - gap)
        req = urllib.request.Request(
            JW_GRAPHQL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": common.USER_AGENT,
            },
            method="POST",
        )
        try:
            self._last_call = time.monotonic()
            with urllib.request.urlopen(req, timeout=common.HTTP_TIMEOUT) as resp:
                raw = resp.read(4 * 1024 * 1024).decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise JustWatchError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise JustWatchError(str(exc)) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JustWatchError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise JustWatchError("expected a JSON object")
        if data.get("errors"):
            first = data["errors"][0] if data["errors"] else "GraphQL error"
            if isinstance(first, dict):
                msg = first.get("message") or "GraphQL error"
            else:
                msg = str(first)
            raise JustWatchError(msg)
        return data


def add_youtube_free(pairs, present):
    """Add the canonical YouTube Free ads row when JustWatch (or a test) says so."""
    if present:
        pairs.add((common.FREE_KINDS[0], common.YOUTUBE_FREE_PROVIDER))
    return pairs


def providers_for(tmdb, tmdb_id, youtube_free=None, title=None, year=None):
    """-> sorted list of (kind, provider_name), possibly empty.

    Every flatrate and rent/buy provider is kept. Ad-supported entries are
    kept only when they are YouTube's free tier — see the module docstring
    for why the three groups are treated differently.

    `youtube_free`, when given, is a YouTubeFreeLookup. recommend.py calls
    this without one (candidates have no movies row yet, so no title to
    search), which is fine: list titles are the ones the owner is looking
    at, and a missed YouTube-only recommendation is an advert we didn't
    make, not a title on the page showing the wrong tag.
    """
    data = tmdb.watch_providers(tmdb_id)
    region = ((data or {}).get("results") or {}).get(REGION) or {}
    pairs = pairs_from_region(region)
    if youtube_free is not None:
        filled = youtube_free.has(tmdb_id, title, year)
        already = any(
            kind in common.FREE_KINDS and common.free_tier_for(name)
            for kind, name in pairs
        )
        add_youtube_free(pairs, filled)
        if filled and not already:
            youtube_free.filled += 1
    return sorted(pairs)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="record under this ISO date instead of today")
    args = ap.parse_args(argv)

    logger = common.setup_logging("providers")
    started = dt.datetime.now()

    if args.date:
        try:
            seen_on = dt.date.fromisoformat(args.date).isoformat()
        except ValueError:
            raise SystemExit(f"--date must be ISO yyyy-mm-dd, got {args.date!r}")
    else:
        seen_on = dt.date.today().isoformat()

    tmdb = common.TMDB(common.api_key(), logger)
    conn = common.connect()
    try:
        ids = resolved_ids(conn)
        if not ids:
            logger.error("no movies in the db; run resolver.py first")
            return 1
        titles = movie_titles(conn)
        youtube_free = YouTubeFreeLookup(logger)
        logger.info("polling providers for %d movies (%s)", len(ids), seen_on)

        polled = 0
        failed = 0
        consecutive_failures = 0
        rows = 0
        streaming = 0

        for tmdb_id in ids:
            title, year = titles.get(tmdb_id, (None, None))
            try:
                pairs = providers_for(
                    tmdb, tmdb_id, youtube_free=youtube_free, title=title, year=year,
                )
            except common.TMDBAuthError:
                logger.error("aborting run: TMDB rejected the API key")
                raise
            except common.TMDBError as exc:
                # Record nothing at all for this id. A failed fetch must never
                # be written down as "available nowhere" — that would show up
                # tomorrow as a departure that never happened.
                logger.error("provider fetch failed for id %s: %s", tmdb_id, exc)
                failed += 1
                consecutive_failures += 1
                if consecutive_failures >= common.CONSECUTIVE_FAILURE_LIMIT:
                    msg = (
                        f"aborting run: {consecutive_failures} provider fetches "
                        f"in a row failed ({exc}). Stopping before a broken "
                        "connection gets written down as a day with no availability."
                    )
                    logger.error(msg)  # the log is the run record, not just stderr
                    raise SystemExit(msg)
                continue
            consecutive_failures = 0

            # One transaction per movie, so a crash mid-run leaves the ids
            # already polled recorded and consistent with their poll_log entry.
            with conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO availability (tmdb_id, provider, kind, seen_on) "
                    "VALUES (?, ?, ?, ?)",
                    [(tmdb_id, name, kind, seen_on) for kind, name in pairs],
                )
                conn.execute(
                    "INSERT OR IGNORE INTO poll_log (tmdb_id, polled_on) VALUES (?, ?)",
                    (tmdb_id, seen_on),
                )
            polled += 1
            rows += len(pairs)
            if any(common.subscription_for(n) or common.free_tier_for(n) for _, n in pairs):
                streaming += 1

        elapsed = (dt.datetime.now() - started).total_seconds()
        logger.info(
            "run complete in %.1fs: %d polled, %d failed, %d availability rows, "
            "%d on a subscribed service; JustWatch YouTube Free: %d checked, "
            "%d filled in where TMDB had nothing",
            elapsed, polled, failed, rows, streaming,
            youtube_free.checked, youtube_free.filled,
        )
        # A run where nothing at all could be polled is a failure, not a day
        # on which every movie left every service.
        return 1 if polled == 0 else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
