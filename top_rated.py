#!/usr/bin/env python3
"""Step 2c: the highest-rated films on each subscribed service.

Fills the `top_rated` table that docs/top-rated.html is built from. One list of
TOP_N per service, ordered by TMDB user score, excluding anything already on
the watchlist or in favorites — the page is for finding something you haven't
already written down.

Like recommend.py, this must not be able to take the main page down. It runs
after sync_providers.py, so a dead key or dead network has already aborted the
run before anything is spent here, both of its loops stop on
CONSECUTIVE_FAILURE_LIMIT, and sync.py carries it as a `required=False` stage.
Finding nothing is a legitimate outcome; an empty section renders honestly.
"""

import argparse
import datetime as dt
import sys

import common
import resolver

# How many titles each service's list shows.
TOP_N = 10

# Minimum TMDB votes before a rating is trusted. Sorting by vote_average with
# no floor is meaningless: measured on Netflix when this shipped, a floor of
# 100 put a 114-vote concert film and a comedy special above Schindler's List,
# and 300 still let a 246-vote documentary through. 1000 is where the list
# stops being novelty items. It leaves plenty to choose from — 579 Netflix
# titles, 345 on Max, and 38 even on Apple TV+, whose catalog is smallest.
MIN_VOTES = 1000

# Pages of 20 to walk per service before giving up on reaching TOP_N. Two is
# enough for 10 slots unless a service's whole first page is already on a list,
# which would itself be worth noticing rather than papering over.
MAX_PAGES = 2

# Deliberately NOT filtered by recommend.MIN_YEAR. That rule exists because
# TMDB's *recommendations* skew toward a film's era and kept surfacing 70s/80s
# titles nobody asked for. This page is a factual "what is rated highest on
# this service" — quietly dropping Grave of the Fireflies from it would make
# the list wrong rather than tidier. If the owner wants a floor here, it should
# be a visible choice, not a silent one.


def excluded_ids(conn):
    """Titles already on a list. The page is for discovery, so a film the owner
    has already written down is a wasted slot — and it has a better home on the
    main page, where its real availability is computed."""
    rows = conn.execute(
        "SELECT tmdb_id FROM favorites UNION SELECT tmdb_id FROM watchlist"
    )
    return {r["tmdb_id"] for r in rows}


def candidates(tmdb, service, exclude, logger):
    """-> [tmdb_id] for one service, best-rated first, already filtered.

    Walks pages only until TOP_N survive the filter, so a service whose first
    page is mostly unseen costs exactly one call.
    """
    kept = []
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        data = tmdb.discover_top_rated(common.PROVIDER_IDS[service], MIN_VOTES, page)
        results = (data or {}).get("results") or []
        if not results:
            break
        for entry in results:
            if not isinstance(entry, dict) or entry.get("adult"):
                continue
            tmdb_id = entry.get("id")
            if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool):
                continue
            if tmdb_id in exclude or tmdb_id in seen:
                continue
            seen.add(tmdb_id)
            kept.append(tmdb_id)
            if len(kept) >= TOP_N:
                return kept
        if page >= (data or {}).get("total_pages", 1):
            break
    return kept


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args(argv)

    logger = common.setup_logging("top_rated")
    started = dt.datetime.now()

    conn = common.connect()
    tmdb = common.TMDB(common.api_key(), logger)
    try:
        exclude = excluded_ids(conn)
        logger.info(
            "%d services, excluding %d titles already on a list",
            len(common.PROVIDER_IDS), len(exclude),
        )

        picked = {}
        consecutive_failures = 0
        for service in common.PROVIDER_IDS:
            try:
                ids = candidates(tmdb, service, exclude, logger)
            except common.TMDBAuthError:
                # Systemic, not per-service. Let it abort rather than filing a
                # dead key under "this service has no titles today".
                raise
            except common.TMDBError as exc:
                logger.error("discover failed for %s: %s", service, exc)
                consecutive_failures += 1
                if consecutive_failures >= common.CONSECUTIVE_FAILURE_LIMIT:
                    logger.error(
                        "aborting: %d discover calls in a row failed (%s). One "
                        "broken connection, not %d broken services.",
                        consecutive_failures, exc, len(common.PROVIDER_IDS),
                    )
                    break
                continue
            consecutive_failures = 0
            picked[service] = ids
            logger.info("%s: %d titles", service, len(ids))

        if args.dry_run:
            for service, ids in picked.items():
                print(f"{service}\t{len(ids)}\t{ids}")
            return 0

        # Details for every title that will be shown. upsert_movie() caches
        # forever, so this is expensive once and ~free afterwards: these lists
        # barely move day to day, and only a new entrant costs a call. It also
        # satisfies the foreign key below, and is what gives these titles the
        # same cards and dialogs as everything else on the site.
        known = set()
        fetch_failures = 0
        with conn:
            for tmdb_id in sorted({i for ids in picked.values() for i in ids}):
                try:
                    if resolver.upsert_movie(conn, tmdb, tmdb_id, logger):
                        known.add(tmdb_id)
                        fetch_failures = 0
                except common.TMDBAuthError:
                    raise
                except common.TMDBError as exc:
                    logger.error("detail fetch failed for id %s: %s", tmdb_id, exc)
                    fetch_failures += 1
                    if fetch_failures >= common.CONSECUTIVE_FAILURE_LIMIT:
                        logger.error(
                            "aborting detail fetches: %d in a row failed (%s); "
                            "keeping whatever is already stored",
                            fetch_failures, exc,
                        )
                        break

            # Replace per service rather than appending: nothing reads a past
            # state of this list. Scoped per service so a service whose fetch
            # failed above keeps yesterday's rows instead of going blank —
            # stale-but-labelled beats empty.
            for service, ids in picked.items():
                conn.execute("DELETE FROM top_rated WHERE service = ?", (service,))
                conn.executemany(
                    "INSERT INTO top_rated (service, tmdb_id, rank) VALUES (?, ?, ?) "
                    "ON CONFLICT(service, tmdb_id) DO UPDATE SET rank = excluded.rank",
                    [(service, i, r) for r, i in enumerate(ids) if i in known],
                )
    finally:
        conn.close()

    stored = sum(len(v) for v in picked.values())
    logger.info(
        "run complete in %.1fs: %d services, %d titles stored",
        (dt.datetime.now() - started).total_seconds(), len(picked), stored,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
