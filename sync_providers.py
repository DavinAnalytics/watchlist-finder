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
"""

import argparse
import datetime as dt
import sys

import common

REGION = "US"


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


def providers_for(tmdb, tmdb_id):
    """-> sorted list of (kind, provider_name), possibly empty.

    Every flatrate provider is kept. Ad-supported entries are kept only when
    common.free_tier_for() recognizes them — see the module docstring.
    """
    data = tmdb.watch_providers(tmdb_id)
    region = ((data or {}).get("results") or {}).get(REGION) or {}

    pairs = set()
    for entry in region.get(common.KIND_FLATRATE) or []:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("provider_name") or "").strip()
        if name:
            pairs.add((common.KIND_FLATRATE, name))

    for kind in common.FREE_KINDS:
        for entry in region.get(kind) or []:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("provider_name") or "").strip()
            if name and common.free_tier_for(name):
                pairs.add((kind, name))

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
        logger.info("polling providers for %d movies (%s)", len(ids), seen_on)

        polled = 0
        failed = 0
        consecutive_failures = 0
        rows = 0
        streaming = 0

        for tmdb_id in ids:
            try:
                pairs = providers_for(tmdb, tmdb_id)
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
            "%d on a subscribed service",
            elapsed, polled, failed, rows, streaming,
        )
        # A run where nothing at all could be polled is a failure, not a day
        # on which every movie left every service.
        return 1 if polled == 0 else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
