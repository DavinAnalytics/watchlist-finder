#!/usr/bin/env python3
"""Step 1: text files -> TMDB ids.

Parses data/favorites.txt and data/watchlist.txt, resolves each title against
TMDB search (always with the year), and maintains data/resolved.json.

The cache is APPEND-ONLY. An entry already in resolved.json is never re-searched
and never rewritten — hand-pinned ids are permanent. To re-resolve a title,
delete that one key from resolved.json by hand.

Anything the top search result doesn't fuzzy-match above MATCH_THRESHOLD is
written to data/unresolved.txt for a human, and skipped. It is never guessed.
"""

import argparse
import datetime as dt
import re
import sys
import unicodedata
from difflib import SequenceMatcher

import common

MATCH_THRESHOLD = 0.85
LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+")
PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
SPACES = re.compile(r"\s+")


def normalize(title):
    """Casefold, strip accents/punctuation and a leading article, collapse spaces."""
    text = unicodedata.normalize("NFKD", title)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = PUNCT.sub(" ", text.casefold())
    text = SPACES.sub(" ", text).strip()
    return LEADING_ARTICLE.sub("", text)


def similarity(a, b):
    """Fuzzy ratio, also compared with spaces removed.

    The despaced comparison is what lets 'Wolf of wallstreet' match
    'The Wolf of Wall Street' without loosening the threshold for real
    mismatches like 'EEAAO'.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    spaced = SequenceMatcher(None, na, nb).ratio()
    despaced = SequenceMatcher(None, na.replace(" ", ""), nb.replace(" ", "")).ratio()
    return max(spaced, despaced)


def release_year(result):
    date = (result.get("release_date") or "").strip()
    if len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def resolve_one(tmdb, raw, year, logger):
    """-> (tmdb_id, None) on a confident match, or (None, reason) to skip.

    Only the top search result is considered, per the resolution rules: a
    second-guess picked out of the result list is exactly the silent wrong
    answer this is meant to avoid.
    """
    data = tmdb.search_movie(raw, year)
    results = (data or {}).get("results") or []
    if not results:
        return None, "no TMDB results"

    top = results[0]
    tmdb_id = top.get("id")
    title = top.get("title") or top.get("original_title") or ""
    if not isinstance(tmdb_id, int) or not title:
        return None, "malformed TMDB result"

    score = similarity(raw, title)
    if score < MATCH_THRESHOLD:
        return None, f'low match {score:.2f} vs "{title}" (id {tmdb_id}, {release_year(top)})'

    # Don't lean on TMDB's year filter alone. If the top result disagrees with
    # the year the title was listed under, a text match is not enough.
    got = release_year(top)
    if got is not None and got != year:
        return None, f'year mismatch: "{title}" is {got}, listed under {year} (id {tmdb_id})'

    logger.info("resolved %r (%s) -> %s %r [%.2f]", raw, year, tmdb_id, title, score)
    return tmdb_id, None


def upsert_movie(conn, tmdb, tmdb_id, logger):
    """Fill movies(title, year, runtime). Details are fetched once per id."""
    row = conn.execute(
        "SELECT runtime FROM movies WHERE tmdb_id = ?", (tmdb_id,)
    ).fetchone()
    if row is not None and row["runtime"] is not None:
        return True

    details = tmdb.movie(tmdb_id)
    if not details:
        logger.warning("no TMDB detail record for id %s", tmdb_id)
        return False

    date = (details.get("release_date") or "")
    conn.execute(
        "INSERT INTO movies (tmdb_id, title, year, runtime) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(tmdb_id) DO UPDATE SET title = excluded.title, "
        "year = excluded.year, runtime = excluded.runtime",
        (
            tmdb_id,
            details.get("title") or details.get("original_title") or f"tmdb:{tmdb_id}",
            int(date[:4]) if date[:4].isdigit() else None,
            details.get("runtime"),
        ),
    )
    return True


def load_resolved():
    """Load the cache, refusing to run on a corrupt one.

    A non-int value can't be matched to a title, so that title would drop out of
    this run's membership set and get deleted from favorites/watchlist — a
    corrupt cache quietly undoing hand-pinned work is exactly what must not
    happen. Crash instead; the fix is one line of JSON.
    """
    resolved = common.load_json_or_empty(common.RESOLVED_PATH)
    bad = {k: v for k, v in resolved.items() if not isinstance(v, int) or isinstance(v, bool)}
    if bad:
        raise SystemExit(
            f"{common.RESOLVED_PATH} has {len(bad)} entries whose value is not a TMDB id: "
            + ", ".join(f"{k!r} -> {v!r}" for k, v in sorted(bad.items())[:5])
            + ("..." if len(bad) > 5 else "")
        )
    return resolved


def write_unresolved(unresolved, started):
    """Rewrite the report of titles that need a human. Not a cache — a fixed
    title simply stops appearing here on the next run."""
    report = [
        "# Titles TMDB could not resolve confidently. Fix by adding the correct",
        "# id to data/resolved.json by hand, keyed on the exact raw string below.",
        f"# regenerated {started.isoformat(timespec='seconds')}",
        "",
    ]
    for name, raw, year, reason in unresolved:
        report.append(f"{name}\t{year}\t{raw}\t{reason}")
    common.atomic_write(common.UNRESOLVED_PATH, "\n".join(report) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and report only: no API calls, no writes",
    )
    args = ap.parse_args(argv)

    logger = common.setup_logging("resolver")
    started = dt.datetime.now()

    sources = [("favorites", common.FAVORITES_PATH), ("watchlist", common.WATCHLIST_PATH)]
    entries = {}  # list name -> [(raw, year)]
    readable = {}  # list name -> was the source file actually there
    for name, path in sources:
        readable[name] = path.exists()
        if not readable[name]:
            # A missing input is a broken setup, not "the list is empty now".
            # Loud, and the table for it is left alone further down.
            logger.error("input file missing, leaving %s table untouched: %s", name, path)
        entries[name] = common.parse_entries(path, logger)
        logger.info("parsed %d entries from %s", len(entries[name]), path.name)

    if args.dry_run:
        for name, items in entries.items():
            for raw, year in items:
                print(f"{name}\t{year}\t{raw}")
        return 0

    resolved = load_resolved()
    before = len(resolved)
    tmdb = common.TMDB(common.api_key(), logger)

    unresolved = []  # (list_name, raw, year, reason)
    membership = {"favorites": [], "watchlist": []}
    searched = 0
    consecutive_failures = 0
    finished_pass = False
    seen_year = {}  # cache key -> the year it was first seen under

    try:
        for name, items in entries.items():
            for raw, year in items:
                key = raw.strip()

                # The cache is keyed on the raw string alone, so the same text
                # under two different years would silently reuse one id — the
                # exact ambiguity the year param exists to prevent.
                if seen_year.setdefault(key, year) != year:
                    logger.warning(
                        "%r appears under both %s and %s; the cached id for %s "
                        "will be reused for both — check this by hand",
                        key, seen_year[key], year, seen_year[key],
                    )
                    unresolved.append(
                        (name, key, year, f"duplicate title, also listed under {seen_year[key]}")
                    )

                if key in resolved:
                    # load_resolved() has already guaranteed these are ints.
                    membership[name].append(resolved[key])
                    continue

                searched += 1
                try:
                    tmdb_id, reason = resolve_one(tmdb, key, year, logger)
                except common.TMDBAuthError:
                    # Not a per-title problem. Filing the rest of the list under
                    # "needs a human" would hide a dead key behind normal-looking
                    # output, so stop the run instead.
                    logger.error("aborting run: TMDB rejected the API key")
                    raise
                except common.TMDBError as exc:
                    # A network failure is not a resolution decision: leave the
                    # title out of the cache so the next run retries it.
                    logger.error("search failed for %r (%s): %s", key, year, exc)
                    unresolved.append((name, key, year, f"search failed: {exc}"))
                    consecutive_failures += 1
                    if consecutive_failures >= common.CONSECUTIVE_FAILURE_LIMIT:
                        msg = (
                            f"aborting run: {consecutive_failures} searches in a "
                            f"row failed ({exc}). This is one broken connection, "
                            "not a list that needs hand-fixing."
                        )
                        # Into watchlist.log too, not just stderr: the log is the
                        # run record, and a silent gap is what we design against.
                        logger.error(msg)
                        raise SystemExit(msg)
                    continue

                consecutive_failures = 0
                if tmdb_id is None:
                    logger.info("unresolved %r (%s): %s", key, year, reason)
                    unresolved.append((name, key, year, reason))
                    continue

                resolved[key] = tmdb_id  # only ever adds keys, never replaces
                membership[name].append(tmdb_id)
        finished_pass = True
    finally:
        # resolved.json is safe to persist partially: it only ever gains keys.
        if len(resolved) != before:
            # The cache must only ever grow. If it ever shrinks, something has
            # eaten hand-pinned ids — say so loudly rather than let sync.py
            # commit the damage to a public repo unremarked.
            if len(resolved) < before:
                logger.error(
                    "resolved.json SHRANK %d -> %d entries; hand-pinned ids may "
                    "have been lost — check git history before pushing",
                    before, len(resolved),
                )
            common.save_json(common.RESOLVED_PATH, resolved)
            logger.info("resolved.json: %d -> %d entries", before, len(resolved))

        # unresolved.txt is not. It's a full rewrite, so writing it from a
        # half-finished pass would delete every title the run never reached --
        # leaving a worklist that looks nearly clean precisely when the run
        # checked almost nothing. Keep the previous file and say so.
        if finished_pass:
            write_unresolved(unresolved, started)
        else:
            logger.error(
                "run aborted before checking every title; leaving %s from the "
                "previous run in place — it is now stale",
                common.UNRESOLVED_PATH.name,
            )

    # Dedupe on tmdb_id, not on the title string.
    fav_ids = sorted(set(membership["favorites"]))
    watch_ids = sorted(set(membership["watchlist"]))

    conn = common.connect()
    try:
        with conn:
            known = set()
            for tmdb_id in sorted(set(fav_ids) | set(watch_ids)):
                try:
                    if upsert_movie(conn, tmdb, tmdb_id, logger):
                        known.add(tmdb_id)
                except common.TMDBAuthError:
                    raise
                except common.TMDBError as exc:
                    # One flaky detail lookup must not cost the whole rebuild.
                    logger.error("detail fetch failed for id %s: %s", tmdb_id, exc)

            # favorites/watchlist mirror the text files, so they are rebuilt --
            # but only when the file they mirror was actually readable.
            # availability is never touched here — that history is append-only.
            today = dt.date.today().isoformat()

            # Membership is decided by the text file, and only by the text file.
            # `known` decides what is safe to INSERT this run (an id with no
            # movies row would break the foreign key); it must never decide what
            # to DELETE, or a transient detail-fetch failure would drop a title
            # that is still listed. Removal happens by editing the text file.
            if readable["favorites"]:
                keep = set(fav_ids)
                current = {r["tmdb_id"] for r in conn.execute("SELECT tmdb_id FROM favorites")}
                conn.executemany(
                    "DELETE FROM favorites WHERE tmdb_id = ?",
                    [(i,) for i in sorted(current - keep)],
                )
                conn.executemany(
                    "INSERT INTO favorites (tmdb_id) VALUES (?) "
                    "ON CONFLICT(tmdb_id) DO NOTHING",
                    [(i,) for i in sorted(keep & known)],
                )

            if readable["watchlist"]:
                # watchlist also keeps added_at for rows still in the text file.
                keep = set(watch_ids)
                current = {r["tmdb_id"] for r in conn.execute("SELECT tmdb_id FROM watchlist")}
                conn.executemany(
                    "DELETE FROM watchlist WHERE tmdb_id = ?",
                    [(i,) for i in sorted(current - keep)],
                )
                conn.executemany(
                    "INSERT INTO watchlist (tmdb_id, added_at) VALUES (?, ?) "
                    "ON CONFLICT(tmdb_id) DO NOTHING",
                    [(i, today) for i in sorted(keep & known)],
                )
    finally:
        conn.close()

    elapsed = (dt.datetime.now() - started).total_seconds()
    logger.info(
        "run complete in %.1fs: %d searched, %d newly resolved, %d unresolved, "
        "%d favorites, %d watchlist",
        elapsed,
        searched,
        len(resolved) - before,
        len(unresolved),
        len(fav_ids),
        len(watch_ids),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
