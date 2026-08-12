#!/usr/bin/env python3
"""Step 2b: pick today's "Because you liked X" recommendations.

Samples a few favorites at random, asks TMDB what people who liked those also
liked, and keeps the first RECOMMEND_COUNT candidates that are actually
streaming on a subscribed service (or free on YouTube). A recommendation that
can't be watched tonight is an advert, not a recommendation.

Runs after sync_providers.py, deliberately:

* If the network or the key is dead, providers.py aborts the run first and
  nothing is spent here.
* Candidates aren't on either list, so sync_providers.resolved_ids() will
  never poll them. This step does its own polling, writing the same
  availability/poll_log rows in the same shape for the same date — so
  render.py reads a recommendation's services exactly the way it reads a
  watchlist title's, with no special case.

Those poll_log rows are for titles on neither list, which is safe only because
every watchlist/favorites read joins its list table and filters them straight
back out (verified: a day's counts are identical with and without this stage).
The one real coupling is render.poll_dates(), which takes MAX(polled_on) across
the whole table: running this stage alone on a date sync_providers.py never
reached would make that date "latest" and render a page whose watchlist is
empty. It can't happen through sync.py — providers runs first and must succeed
— and if it ever did, the coverage banners say so loudly rather than the page
quietly looking thin. Keep that ordering.

The seed is the date, so re-running by hand on the same day re-picks the same
titles rather than reshuffling the page. A day that already has a full set of
picks is skipped outright, before any API call.

Nothing here is allowed to take the page down with it: a run that finds
nothing (or gives up after repeated fetch failures) logs loudly and exits 0,
and the page simply carries no recommendations that day. The watchlist is the
product; this section is a bonus on top of it.
"""

import argparse
import datetime as dt
import random
import sys

import common
import resolver
import sync_providers

# How many picks the page shows, and how many favorites to draw them from.
# More sources than picks on purpose: sources get used round-robin, so the
# picks come from as many different favorites as possible, and the spares
# cover sources whose recommendations are all already-seen or unwatchable.
RECOMMEND_COUNT = 5
SOURCE_SAMPLE = 8

# Hard ceiling on provider polls per run. Recommendation candidates are not
# curated the way the watchlist is, so the streaming hit rate is much lower
# and an uncapped search would keep calling TMDB until it got lucky. Hitting
# this just means fewer picks today, which the page renders honestly.
POLL_BUDGET = 40

# Don't re-pick something picked in the last fortnight. Without this a title
# that several favorites all recommend would win the round-robin every single
# day and the section would never change.
RECENT_DAYS = 14


def sampled_sources(conn, seed, limit=SOURCE_SAMPLE):
    """-> a seeded-random sample of favorite ids, as the recommendation seeds.

    Ordered by id before sampling so the result depends only on the seed and
    the membership set, never on the order sqlite happens to return rows in.
    """
    ids = sorted(r["tmdb_id"] for r in conn.execute("SELECT tmdb_id FROM favorites"))
    if not ids:
        return []
    rng = random.Random(f"recommend-sources:{seed}")
    return rng.sample(ids, min(limit, len(ids)))


def excluded_ids(conn, seen_on):
    """-> ids that must never be picked in this run.

    Anything already on either list (recommending something the owner already
    logged is noise), plus anything picked in the last RECENT_DAYS — today
    included. Excluding today's own picks is what makes a re-run *top up* to
    RECOMMEND_COUNT instead of re-walking them: paired with seeding `picked`
    from the count already stored, a partial day converges on exactly
    RECOMMEND_COUNT rows no matter how many times it runs, and without
    re-polling anything it already decided on.
    """
    cutoff = (dt.date.fromisoformat(seen_on) - dt.timedelta(days=RECENT_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT tmdb_id FROM favorites "
        "UNION SELECT tmdb_id FROM watchlist "
        "UNION SELECT tmdb_id FROM recommendations WHERE picked_on >= ?",
        (cutoff,),
    )
    return {r["tmdb_id"] for r in rows}


def candidates_for(tmdb, source_id):
    """-> [tmdb_id] from TMDB's recommendations for one movie, in its order."""
    data = tmdb.recommendations(source_id)
    out = []
    for entry in (data or {}).get("results") or []:
        if not isinstance(entry, dict):
            continue
        tmdb_id = entry.get("id")
        # Adult titles are filtered out of search elsewhere; this endpoint has
        # no include_adult parameter, so it's done by hand here.
        if isinstance(tmdb_id, int) and not entry.get("adult"):
            out.append(tmdb_id)
    return out


def interleave(by_source):
    """[(source_id, [candidate...])] -> [(candidate, source_id)], round-robin.

    Takes each source's first candidate, then each source's second, and so on.
    That way the five picks come from five different favorites where possible,
    while still preferring each source's most relevant suggestions — draining
    one source at a time would make the whole section "because you liked" a
    single film. First source to offer a candidate keeps it; a title several
    favorites recommend is attributed to one of them, not repeated.
    """
    seen = set()
    out = []
    for rank in range(max((len(c) for _, c in by_source), default=0)):
        for source_id, cands in by_source:
            if rank >= len(cands):
                continue
            tmdb_id = cands[rank]
            if tmdb_id in seen:
                continue
            seen.add(tmdb_id)
            out.append((tmdb_id, source_id))
    return out


def is_streaming(pairs):
    """(kind, provider) pairs -> is any of them something we can watch?

    Kind-checked, not just name-checked: common.RENT_BUY_KINDS entries are
    real storefronts that must never count as streaming, and matching on the
    name alone would depend on no rent/buy provider ever being named like a
    subscription.
    """
    return any(
        common.subscription_for(name) or common.free_tier_for(name)
        for kind, name in pairs
        if kind in common.STREAMING_KINDS
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="record under this ISO date instead of today")
    args = ap.parse_args(argv)

    logger = common.setup_logging("recommend")
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
        already = conn.execute(
            "SELECT COUNT(*) AS n FROM recommendations WHERE picked_on = ?", (seen_on,)
        ).fetchone()["n"]
        if already >= RECOMMEND_COUNT:
            logger.info("%d recommendations already picked for %s; nothing to do",
                        already, seen_on)
            return 0

        sources = sampled_sources(conn, seen_on)
        if not sources:
            logger.warning("no favorites to draw recommendations from")
            return 0

        skip = excluded_ids(conn, seen_on)

        by_source = []
        consecutive_failures = 0
        for source_id in sources:
            try:
                cands = candidates_for(tmdb, source_id)
            except common.TMDBAuthError:
                logger.error("aborting run: TMDB rejected the API key")
                raise
            except common.TMDBError as exc:
                logger.error("recommendations fetch failed for id %s: %s", source_id, exc)
                consecutive_failures += 1
                if consecutive_failures >= common.CONSECUTIVE_FAILURE_LIMIT:
                    # Same rule as everywhere else: one dead network is one
                    # fault, not eight. Without this the loop just runs out of
                    # sources and reports "0 candidates from 0 of 8 favorites"
                    # — a legitimate-looking result for a systemic failure,
                    # which is the exact costume this project forbids.
                    logger.error(
                        "giving up on recommendations: %d recommendation fetches in a "
                        "row failed (%s). This is one broken connection, not a day "
                        "with nothing to suggest.",
                        consecutive_failures, exc,
                    )
                    break
                continue
            consecutive_failures = 0
            by_source.append((source_id, [c for c in cands if c not in skip]))

        queue = interleave(by_source)
        logger.info(
            "%d candidates from %d of %d favorites (%s)",
            len(queue), len(by_source), len(sources), seen_on,
        )

        # Seeded from what's already stored for today, not from zero: with
        # today's picks excluded from the queue (see excluded_ids), a run that
        # was interrupted after 2 tops up by exactly 3. Counting inserts from
        # zero instead would only land on 5 while the queue reproduced
        # identically — which stops being true the moment favorites.txt is
        # edited between two runs on the same day.
        picked = already
        polled = 0
        consecutive_failures = 0

        for tmdb_id, source_id in queue:
            if picked >= RECOMMEND_COUNT or polled >= POLL_BUDGET:
                break

            try:
                pairs = sync_providers.providers_for(tmdb, tmdb_id)
            except common.TMDBAuthError:
                logger.error("aborting run: TMDB rejected the API key")
                raise
            except common.TMDBError as exc:
                logger.error("provider fetch failed for candidate %s: %s", tmdb_id, exc)
                consecutive_failures += 1
                if consecutive_failures >= common.CONSECUTIVE_FAILURE_LIMIT:
                    # Unlike the other stages this does NOT abort the run. The
                    # watchlist page is already fully computed by this point;
                    # taking it down over a bonus section would trade a real
                    # failure for a much bigger one. Stop searching, keep what
                    # was found, and say so.
                    logger.error(
                        "giving up on recommendations: %d provider fetches in a row "
                        "failed (%s). Keeping the %d already picked; the page will "
                        "show only those.",
                        consecutive_failures, exc, picked,
                    )
                    break
                continue
            consecutive_failures = 0
            polled += 1

            if not is_streaming(pairs):
                continue

            # Only now is the detail fetch worth spending: this one is going on
            # the page, and needs a movies row for its card and dialog (and to
            # satisfy the availability/recommendations foreign keys).
            try:
                if not resolver.upsert_movie(conn, tmdb, tmdb_id, logger):
                    continue
            except common.TMDBAuthError:
                logger.error("aborting run: TMDB rejected the API key")
                raise
            except common.TMDBError as exc:
                logger.error("detail fetch failed for candidate %s: %s", tmdb_id, exc)
                continue

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
                conn.execute(
                    "INSERT OR IGNORE INTO recommendations (tmdb_id, source_id, picked_on) "
                    "VALUES (?, ?, ?)",
                    (tmdb_id, source_id, seen_on),
                )
            picked += 1
            logger.info("picked %s (because of %s)", tmdb_id, source_id)

        elapsed = (dt.datetime.now() - started).total_seconds()
        logger.info(
            "run complete in %.1fs: %d picks for %s (%d new this run), "
            "%d candidates polled (budget %d)",
            elapsed, picked, seen_on, picked - already, polled, POLL_BUDGET,
        )
        # Finding nothing is a legitimate outcome, not a failure: it means
        # nothing TMDB suggested today is on a subscribed service.
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
