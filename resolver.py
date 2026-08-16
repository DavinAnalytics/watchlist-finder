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


BACKFILL_COLUMNS = (
    "runtime", "poster_path", "overview", "director", "genres", "vote_average", "vote_count",
    "top_cast", "trailer_key", "status", "release_date", "certification", "similar_count",
)
ALL_COLUMNS = ("tmdb_id", "title", "year") + BACKFILL_COLUMNS

# Statuses a title will never move on from — safe to stop re-fetching for.
# Everything else ("In Production", "Post Production", "Planned", "Rumored")
# can still become "Released", so a row in one of those states keeps
# re-fetching every run until it settles. "" means TMDB never gave a status
# at all, which release_badge() already treats as "assume released".
SETTLED_STATUSES = {"", "Released", "Canceled"}

DIRECTOR_JOBS = {"Director", "Co-Director"}
TOP_CAST_LIMIT = 4
# Preference order when a title has several videos: an official trailer, any
# trailer, then a teaser as a last resort. Featurettes/clips are never picked
# — they're promotional cutdowns, not "should I watch this" material.
TRAILER_TYPE_PRIORITY = ("Trailer", "Teaser")


def _director(details):
    crew = (details.get("credits") or {}).get("crew") or []
    names = [c["name"] for c in crew if c.get("job") in DIRECTOR_JOBS and c.get("name")]
    return ", ".join(dict.fromkeys(names))  # de-duped, order preserved


def _top_cast(details, limit=TOP_CAST_LIMIT):
    cast = (details.get("credits") or {}).get("cast") or []
    # .get("order", 999) only substitutes when the key is absent, not when
    # it's present-but-null; a bare `order: null` from TMDB would sort None
    # against int and raise TypeError, which isn't a TMDBError — it wouldn't
    # be caught per-title and would abort the whole run. Same class of
    # unverified-response-shape assumption already fixed for vote_average.
    ranked = sorted(
        (c for c in cast if c.get("name")),
        key=lambda c: c.get("order") if c.get("order") is not None else 999,
    )
    # Dedupe before truncating, not after — a duplicate name shouldn't crowd
    # out a distinct, correctly-billed name just outside the raw top N.
    names = list(dict.fromkeys(c["name"] for c in ranked))
    return ", ".join(names[:limit])


def _trailer_key(details):
    """-> a YouTube video id, or "" if nothing suitable. Only YouTube is
    considered — that's the only site this project ever links out to."""
    videos = (details.get("videos") or {}).get("results") or []
    youtube = [v for v in videos if v.get("site") == "YouTube" and v.get("key")]
    for wanted in TRAILER_TYPE_PRIORITY:
        matches = [v for v in youtube if v.get("type") == wanted]
        # An official one first, but an unofficial trailer still beats a teaser.
        matches.sort(key=lambda v: not v.get("official", False))
        if matches:
            return matches[0]["key"]
    return ""


def _certification(details):
    """-> a US content rating ("R", "PG-13", ...) or "" if TMDB has none.

    TMDB carries one release_dates entry per release event (festival,
    premiere, theatrical, digital, physical), and the certification isn't
    reliably attached to any particular one — some carry it, some don't, with
    no consistent pattern across titles. Take the first non-empty value found,
    in whatever order TMDB lists them.
    """
    results = (details.get("release_dates") or {}).get("results") or []
    us = next((r for r in results if r.get("iso_3166_1") == "US"), None)
    if not us:
        return ""
    for entry in us.get("release_dates") or []:
        cert = (entry.get("certification") or "").strip()
        if cert:
            return cert
    return ""


def _similar_entries(details):
    """-> [(similar_id, title, year, poster_path, rank)] for the "More like
    this" pool, read off the appended recommendations payload.

    rank preserves TMDB's own relevance order. The page picks five at random
    rather than taking the top five, so rank isn't what decides what shows —
    but storing the list unordered would throw away the only quality signal
    TMDB gives, and it costs one integer to keep.

    Skips anything without an int id or a usable title, and anything flagged
    adult: unlike the rest of the pipeline these titles were never vetted by
    appearing on a hand-written list, so this is the only gate they pass
    through. Nothing else is filtered here — see the `similar` table comment
    in common.py for why the year floor lives at render time instead.
    """
    results = (details.get("recommendations") or {}).get("results") or []
    out = []
    for entry in results:
        if not isinstance(entry, dict) or entry.get("adult"):
            continue
        similar_id = entry.get("id")
        title = entry.get("title") or entry.get("original_title") or ""
        if not isinstance(similar_id, int) or isinstance(similar_id, bool) or not title:
            continue
        out.append(
            (similar_id, title, release_year(entry), entry.get("poster_path"), len(out))
        )
    return out


def upsert_movie(conn, tmdb, tmdb_id, logger):
    """Fill in the movies row. Details are fetched once per id and then left
    alone — except that a row missing any of BACKFILL_COLUMNS (grows as
    fields get added: poster_path, then overview/director/genres/
    vote_average/vote_count, then top_cast/trailer_key/status/release_date,
    then certification, all on 2026-08-10/11, then similar_count on
    2026-08-16) gets one more fetch to backfill
    what's missing, and except that a row whose status isn't in
    SETTLED_STATUSES keeps re-fetching every run regardless of what's already
    filled in — see SETTLED_STATUSES for why status/release_date can't join
    the usual "fetch once" treatment the way everything else here does.

    overview/director/genres/top_cast/status/certification are stored as ""
    rather than left NULL when TMDB genuinely has nothing, specifically so the
    short-circuit can tell "fetched, empty" from "never fetched" and doesn't
    re-request forever. trailer_key gets the same treatment for the same
    reason — plenty of older or obscure titles genuinely have no YouTube
    trailer in TMDB's data. vote_average/vote_count get no such treatment —
    TMDB always returns them as numbers (0 for an unrated title), so a real
    fetch never leaves them NULL. release_date is stored even when empty
    (unreleased titles sometimes carry one, sometimes don't) since an empty
    string there is itself meaningful, not a sign the fetch never happened.
    poster_path is the one field that can legitimately stay NULL after a real
    fetch; a title TMDB has no poster for re-fetches every run. Accepted, rare.
    """
    row = conn.execute(
        f"SELECT {', '.join(BACKFILL_COLUMNS)} FROM movies WHERE tmdb_id = ?", (tmdb_id,)
    ).fetchone()
    if (
        row is not None
        and all(row[c] is not None for c in BACKFILL_COLUMNS)
        and row["status"] in SETTLED_STATUSES
    ):
        return True
    # Falls through and re-fetches everything otherwise — including when
    # every other column is already filled. status/release_date aren't a
    # fetch-quality problem the way an empty overview is; they're a real
    # fact that changes over time for exactly the titles this whole
    # unreleased-badge feature exists for. Freezing them the same way as
    # everything else meant a title backfilled while still "Post Production"
    # (or one TMDB hadn't assigned a release_date to yet at all) would keep
    # showing "not yet released" forever, even after it actually released and
    # started streaming — permanently hiding real availability behind a
    # snapshot taken before the fact was settled. Caught in review before
    # this shipped; release_badge()'s "compares the date fresh every render"
    # fix from earlier only covered the case where a release_date existed at
    # all, not this one.

    details = tmdb.movie(tmdb_id)
    if not details:
        logger.warning("no TMDB detail record for id %s", tmdb_id)
        return False

    date = (details.get("release_date") or "")
    genres = ", ".join(g["name"] for g in details.get("genres") or [] if g.get("name"))
    similar = _similar_entries(details)
    values = {
        "tmdb_id": tmdb_id,
        "title": details.get("title") or details.get("original_title") or f"tmdb:{tmdb_id}",
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "runtime": details.get("runtime"),
        "poster_path": details.get("poster_path"),
        "overview": details.get("overview") or "",
        "director": _director(details),
        "genres": genres,
        # Not left raw: "TMDB always returns these as numbers" is an
        # assumption about response shape, not a guarantee. If a response
        # ever omits them, a raw None here would never satisfy
        # BACKFILL_COLUMNS and the row would re-fetch forever — same failure
        # this 0/0.0 default already avoids for the string-typed fields.
        "vote_average": (
            details.get("vote_average") if details.get("vote_average") is not None else 0.0
        ),
        "vote_count": details.get("vote_count") if details.get("vote_count") is not None else 0,
        "top_cast": _top_cast(details),
        "trailer_key": _trailer_key(details),
        "status": details.get("status") or "",
        "release_date": date,
        "certification": _certification(details),
        # 0, never NULL, on a real fetch — same "fetched, empty" vs "never
        # fetched" distinction the string fields above make. A title TMDB has
        # no recommendations for genuinely exists (obscure and unreleased ones
        # especially), and leaving this NULL would fail the BACKFILL_COLUMNS
        # check and re-fetch that title every single run, forever.
        "similar_count": len(similar),
    }

    # Built from ALL_COLUMNS rather than three hand-written, hand-counted SQL
    # fragments: a column list, a VALUES placeholder count, and an ON
    # CONFLICT SET clause, kept in sync purely by careful editing. That
    # pattern already needed fixing once this session — a miscounted
    # positional tuple would silently shift values into the wrong columns
    # with no error, exactly the kind of quiet-wrong-data failure this
    # project is built against. Adding a column now only means adding one key
    # to `values` above and one line to SCHEMA/_migrate/BACKFILL_COLUMNS.
    cols = ", ".join(ALL_COLUMNS)
    placeholders = ", ".join("?" * len(ALL_COLUMNS))
    updates = ", ".join(f"{c} = excluded.{c}" for c in ALL_COLUMNS if c != "tmdb_id")
    conn.execute(
        f"INSERT INTO movies ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(tmdb_id) DO UPDATE SET {updates}",
        tuple(values[c] for c in ALL_COLUMNS),
    )

    # Replace, not append. Every other history in this schema (availability,
    # poll_log, recommendations) is append-only because the history is itself
    # the feature — it's what "new since yesterday" and "don't re-pick for a
    # fortnight" are computed from. Nothing reads a *past* state of this list,
    # so keeping one would only let a title TMDB has since dropped keep
    # surfacing forever. Must run after the movies upsert above: similar.tmdb_id
    # is a real foreign key and PRAGMA foreign_keys is ON.
    conn.execute("DELETE FROM similar WHERE tmdb_id = ?", (tmdb_id,))
    conn.executemany(
        "INSERT INTO similar (tmdb_id, similar_id, title, year, poster_path, rank) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(tmdb_id, sid, t, y, p, r) for sid, t, y, p, r in similar],
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
