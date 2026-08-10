#!/usr/bin/env python3
"""Step 3: db -> docs/index.html.

Queries availability, diffs the latest poll against the one before it, fills
template.html, and atomically replaces docs/index.html.

Two rules this file exists to honour:

* Zero JavaScript in the output. Every sort, group and filter happens here, in
  Python. The page that lands in docs/ is HTML and CSS.
* Nothing counts as "streaming" unless it is on one of the subscribed services.
  Every read of the availability table goes through common.subscription_for(),
  which is what keeps Max and Disney+ rows out of the counts.
"""

import argparse
import datetime as dt
import html
import string
import sys

import common

MIN_POLL_COVERAGE = 0.9  # below this share of the watchlist, say so on the page


def poll_dates(conn, on=None):
    """-> (latest, previous). Either may be None.

    The diff is against the previous poll, not literally yesterday: if the Mac
    was asleep for two days, "yesterday" holds no rows and every title would
    look brand new.
    """
    if on:
        latest = conn.execute(
            "SELECT MAX(polled_on) AS d FROM poll_log WHERE polled_on <= ?", (on,)
        ).fetchone()["d"]
    else:
        latest = conn.execute("SELECT MAX(polled_on) AS d FROM poll_log").fetchone()["d"]
    if latest is None:
        return None, None
    prev = conn.execute(
        "SELECT MAX(polled_on) AS d FROM poll_log WHERE polled_on < ?", (latest,)
    ).fetchone()["d"]
    return latest, prev


LIST_TABLES = ("watchlist", "favorites")  # the only names allowed into a query


def snapshot(conn, on, table="watchlist"):
    """-> {tmdb_id: sorted[subscribed service]} for movies on `table` polled on `on`.

    Only ids with a poll_log row are included, so a movie whose fetch failed is
    absent rather than being reported as available nowhere.
    """
    if table not in LIST_TABLES:
        raise ValueError(f"unknown list table {table!r}")

    rows = conn.execute(
        "SELECT p.tmdb_id AS tmdb_id, a.provider AS provider "
        "FROM poll_log p "
        f"JOIN {table} l ON l.tmdb_id = p.tmdb_id "  # noqa: S608 - whitelisted above
        "LEFT JOIN availability a "
        "  ON a.tmdb_id = p.tmdb_id AND a.seen_on = p.polled_on AND a.kind = 'flatrate' "
        "WHERE p.polled_on = ?",
        (on,),
    ).fetchall()

    snap = {}
    for row in rows:
        services = snap.setdefault(row["tmdb_id"], set())
        service = common.subscription_for(row["provider"])
        if service:
            services.add(service)
    return {k: sorted(v) for k, v in snap.items()}


def movie_rows(conn):
    return {
        r["tmdb_id"]: r
        for r in conn.execute("SELECT tmdb_id, title, year, runtime FROM movies")
    }


def esc(text):
    return html.escape(str(text), quote=True)


def render_list(items, movies, empty_text):
    """items: [(tmdb_id, [services])] -> an HTML <ul>, or an empty-state note."""
    if not items:
        return f'<p class="empty">{esc(empty_text)}</p>'

    out = ["<ul>"]
    for tmdb_id, services in items:
        movie = movies.get(tmdb_id)
        title = movie["title"] if movie else f"tmdb:{tmdb_id}"
        year = movie["year"] if movie else None
        runtime = movie["runtime"] if movie else None

        meta = []
        if year:
            meta.append(esc(year))
        if runtime:
            meta.append(f"{esc(runtime)} min")
        line = " · ".join(meta)
        on = f'<span class="on">{esc(", ".join(services))}</span>' if services else ""
        if line and on:
            line = f"{line} · {on}"
        else:
            line = line or on

        out.append(
            f'<li><div class="title">{esc(title)}</div>'
            f'<div class="meta">{line}</div></li>'
        )
    out.append("</ul>")
    return "\n".join(out)


def sort_key(movies):
    def key(item):
        movie = movies.get(item[0])
        return ((movie["title"] if movie else "").casefold(), item[0])

    return key


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="render as of this ISO date instead of the latest poll")
    ap.add_argument("--out", help="write here instead of docs/index.html")
    args = ap.parse_args(argv)

    logger = common.setup_logging("render")
    started = dt.datetime.now()

    template_text = common.TEMPLATE_PATH.read_text(encoding="utf-8")
    template = string.Template(template_text)

    conn = common.connect()
    try:
        latest, prev = poll_dates(conn, args.date)
        if latest is None:
            # Refuse to publish a page built from nothing; the previous
            # docs/index.html stays exactly where it is.
            logger.error("no poll data in the db; run sync_providers.py first")
            return 1

        movies = movie_rows(conn)
        watchlist_size = conn.execute("SELECT COUNT(*) AS n FROM watchlist").fetchone()["n"]
        favorites_size = conn.execute("SELECT COUNT(*) AS n FROM favorites").fetchone()["n"]
        today = snapshot(conn, latest)
        yesterday = snapshot(conn, prev) if prev else {}

        streaming = {i: s for i, s in today.items() if s}

        # new/gone are only computed for ids polled on both days. An id missing
        # from either poll is unknown, not changed, and guessing would turn a
        # network blip into a departure.
        comparable = set(today) & set(yesterday)
        new = {i: today[i] for i in comparable if today[i] and not yesterday[i]}
        gone = sorted(i for i in comparable if yesterday[i] and not today[i])

        # CLAUDE.md's section list includes an "under 100 minutes" section.
        # Removed on the owner's instruction, 2026-08-09: the runtime limit was
        # the section's only reason to exist, so dropping the limit drops the
        # section. Runtime is still shown per title.

        # Deliberate, explicitly authorised departure from CLAUDE.md's
        # "favorites are never filtered by streaming availability" rule: the
        # owner asked for a favorites-streaming section on 2026-08-09. It is
        # additive only — the counts and the sections above stay
        # watchlist-only, and anything already on the watchlist is excluded
        # here so it can't appear twice on the page.
        fav_today = snapshot(conn, latest, "favorites")
        fav_streaming = {i: s for i, s in fav_today.items() if s and i not in today}

        key = sort_key(movies)
        prev_label = prev or "the last run"
        banners = []
        if latest != dt.date.today().isoformat():
            banners.append(f"Stale: last successful provider sync was {esc(latest)}.")
            logger.warning("latest poll is %s, not today", latest)

        # A sync where most fetches failed still dates its poll_log rows today,
        # so the staleness check above won't catch it. Without this the page
        # just looks emptier than it should, which is the quiet kind of wrong.
        # Both lists are checked: a sparse favorites section is just as
        # misleading as a sparse watchlist one.
        for label, polled, total in (
            ("watchlist", len(today), watchlist_size),
            ("favorites", len(fav_today), favorites_size),
        ):
            if total and polled < total * MIN_POLL_COVERAGE:
                banners.append(
                    f"Incomplete: only {polled} of {total} {label} titles "
                    f"were polled on {esc(latest)}."
                )
                logger.warning("only %d of %d %s titles polled", polled, total, label)

        banner = "\n  ".join(f'<p class="stale">{b}</p>' for b in banners)

        page = template.substitute(
            generated=esc(
                f"{dt.datetime.now().strftime('%a %-d %b, %H:%M')} · data from {latest}"
            ),
            stale_banner=banner,
            count_streaming=len(streaming),
            count_new=len(new),
            count_gone=len(gone),
            prev_label=esc(prev_label),
            section_new=render_list(
                sorted(new.items(), key=key), movies, "Nothing new since the last run."
            ),
            section_streaming=render_list(
                sorted(streaming.items(), key=key),
                movies,
                "Nothing on the watchlist is streaming right now.",
            ),
            section_favorites=render_list(
                sorted(fav_streaming.items(), key=key),
                movies,
                "None of your favorites are streaming right now.",
            ),
            footer=esc(
                f"{len(today)} of {watchlist_size} watchlist titles polled on {latest}"
            ),
        )
    finally:
        conn.close()

    if "<script" in page.lower():  # the output is HTML and CSS, and stays that way
        logger.error("refusing to write: rendered page contains a script tag")
        return 1

    out_path = args.out or common.OUTPUT_PATH
    common.atomic_write(out_path, page)

    elapsed = (dt.datetime.now() - started).total_seconds()
    logger.info(
        "run complete in %.1fs: wrote %s — %d streaming, %d new, %d gone, "
        "%d favorites streaming (%s vs %s)",
        elapsed, out_path, len(streaming), len(new), len(gone), len(fav_streaming),
        latest, prev,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
