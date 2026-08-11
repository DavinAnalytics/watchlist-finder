#!/usr/bin/env python3
"""Step 3: db -> docs/index.html.

Queries availability, diffs the latest poll against the one before it, fills
template.html, and atomically replaces docs/index.html.

Two rules this file exists to honour:

* No client-side logic that duplicates what this file already computes. Every
  sort, group, filter and diff happens here, in Python, once, at generation
  time. The per-title detail dialogs are pure presentation — a CSS :target
  toggle revealing HTML this file already rendered, nothing recomputed in the
  browser — which is why they don't cost the project its zero-JS posture.
* Nothing counts as "streaming" unless it is on one of the subscribed services,
  or free with ads on YouTube. Every read of the availability table goes
  through common.subscription_for() and common.free_tier_for(), which is what
  keeps Max, Disney+, Tubi, and Pluto TV rows out of the counts.
"""

import argparse
import datetime as dt
import html
import random
import re
import string
import sys
import urllib.parse

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
    """-> {tmdb_id: sorted[service or free-tier label]} for `table`, polled on `on`.

    Only ids with a poll_log row are included, so a movie whose fetch failed is
    absent rather than being reported as available nowhere.
    """
    if table not in LIST_TABLES:
        raise ValueError(f"unknown list table {table!r}")

    kinds = (common.KIND_FLATRATE, *common.FREE_KINDS)
    placeholders = ", ".join("?" * len(kinds))
    rows = conn.execute(
        "SELECT p.tmdb_id AS tmdb_id, a.provider AS provider "
        "FROM poll_log p "
        f"JOIN {table} l ON l.tmdb_id = p.tmdb_id "  # noqa: S608 - whitelisted above
        "LEFT JOIN availability a "
        f"  ON a.tmdb_id = p.tmdb_id AND a.seen_on = p.polled_on AND a.kind IN ({placeholders}) "
        "WHERE p.polled_on = ?",
        (*kinds, on),
    ).fetchall()

    snap = {}
    for row in rows:
        services = snap.setdefault(row["tmdb_id"], set())
        label = common.subscription_for(row["provider"]) or common.free_tier_for(row["provider"])
        if label:
            services.add(label)
    return {k: sorted(v) for k, v in snap.items()}


def rentable_snapshot(conn, on):
    """-> {tmdb_id: sorted[provider name]} for rent/buy availability on `on`.

    No subscription_for()/free_tier_for() filtering, unlike snapshot(): every
    rent/buy storefront TMDB lists is legitimate to show as-is, there's no
    reseller-channel noise in this bucket the way there is for flatrate/ads.
    No LEFT JOIN against poll_log needed either — a rent/buy row only exists
    because that id was polled that day, so there's nothing to distinguish.
    """
    rows = conn.execute(
        "SELECT tmdb_id, provider FROM availability "
        "WHERE seen_on = ? AND kind IN ('rent', 'buy')",
        (on,),
    ).fetchall()
    snap = {}
    for row in rows:
        snap.setdefault(row["tmdb_id"], set()).add(row["provider"])
    return {k: sorted(v) for k, v in snap.items()}


def movie_rows(conn):
    return {
        r["tmdb_id"]: r
        for r in conn.execute(
            "SELECT tmdb_id, title, year, runtime, poster_path, "
            "overview, director, genres, vote_average, vote_count, "
            "top_cast, trailer_key, status, release_date, certification FROM movies"
        )
    }


def esc(text):
    return html.escape(str(text), quote=True)


POSTER_SIZE = "w92"  # TMDB's smallest useful width; this is a phone-width list, not a gallery
DIALOG_POSTER_SIZE = "w154"


def poster_html(poster_path, *, css_class="poster", size=POSTER_SIZE, width=52, height=78):
    if poster_path:
        src = f"https://image.tmdb.org/t/p/{size}{poster_path}"
        return (
            f'<img class="{css_class}" src="{esc(src)}" alt="" '
            f'loading="lazy" width="{width}" height="{height}">'
        )
    return f'<div class="{css_class} poster-placeholder" aria-hidden="true">poster</div>'


def tag_chips(services):
    if not services:
        return '<span class="tag tag-neutral">Not currently streaming</span>'
    chips = []
    for s in services:
        color = common.SERVICE_COLORS.get(s, common.TAG_FALLBACK)
        chips.append(
            f'<span class="tag" style="--tag-color:{esc(color)}">'
            f'<span class="tag-dot"></span>{esc(s)}</span>'
        )
    return "".join(chips)


def _slug(label):
    """-> a lowercase, hyphenated ASCII token safe for an HTML id/attribute."""
    return re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")


def render_filter_radios():
    """Hidden radio inputs driving the service filter — see the CSS comment
    in template.html for the mechanism. These have to sit as a previous
    sibling of <main>, not inside it: the "~" combinator only reaches
    forward across siblings, and <label for=...> works regardless of where
    in the document the label itself lives, so the visible pills can stay
    inside <main> while these stay outside it, out of the way visually.
    """
    ids = ['<input type="radio" name="filter" id="filter-all" class="filter-radio" checked>']
    for label in common.SERVICE_COLORS:
        ids.append(
            f'<input type="radio" name="filter" id="filter-{_slug(label)}" class="filter-radio">'
        )
    return "\n".join(ids)


def render_filter_bar():
    """The visible pill row. One pill per common.SERVICE_COLORS entry — the
    same six labels a tag can ever actually show, so there's never a filter
    option that couldn't possibly match anything.
    """
    pills = ['<label class="pill" for="filter-all">All</label>']
    for label in common.SERVICE_COLORS:
        color = common.SERVICE_COLORS[label]
        pills.append(
            f'<label class="pill" for="filter-{_slug(label)}" '
            f'style="--tag-color:{esc(color)}">{esc(label)}</label>'
        )
    return f'<div class="filter-bar">{"".join(pills)}</div>'


def render_list(items, movies, empty_text, *, variant="on"):
    """items: [(tmdb_id, [services])] -> an HTML <ul>, or an empty-state note.

    variant="on": elevated card, poster art (real if TMDB has one, a decorative
    placeholder if not), colored service tags — for titles actually streaming.
    variant="off": flat card, dimmed, title/meta only — for the collapsed
    "not currently streaming" lists, deliberately less visually loud.

    Every row is a link to #m{tmdb_id}, opening that title's detail dialog —
    see render_dialogs(). Pure navigation to an anchor already in the page;
    nothing here executes in the browser.
    """
    if not items:
        return f'<p class="empty">{esc(empty_text)}</p>'

    on = variant == "on"
    card_class = "row-card elev-sm" if on else "row-card row-card-off"

    out = ["<ul>"]
    for tmdb_id, services in items:
        movie = movies.get(tmdb_id)
        title = movie["title"] if movie else f"tmdb:{tmdb_id}"
        year = movie["year"] if movie else None
        runtime = movie["runtime"] if movie else None
        poster_path = movie["poster_path"] if movie else None

        meta = []
        if year:
            meta.append(esc(year))
        if runtime:
            meta.append(f"{esc(runtime)} min")
        meta_html = f'<div class="card-meta">{" · ".join(meta)}</div>' if meta else ""

        poster = poster_html(poster_path) if on else ""

        tags_html = ""
        services_attr = ""
        if on and services:
            tags_html = f'<div class="tag-row">{tag_chips(services)}</div>'
            # Drives the CSS-only service filter — see render_filter_radios().
            # Off-variant cards never get this attribute; they're not part of
            # what the filter touches (see template.html's filter-bar CSS).
            slugs = " ".join(_slug(s) for s in services)
            services_attr = f' data-services="{esc(slugs)}"'

        out.append(
            f'<li><a class="{card_class}" href="#m{tmdb_id}"{services_attr}>{poster}'
            f'<div class="row-info"><div class="card-title">{esc(title)}</div>'
            f"{meta_html}{tags_html}</div></a></li>"
        )
    out.append("</ul>")
    return "\n".join(out)


def release_badge(status, release_date):
    """-> an HTML tag for a title that hasn't released yet, or None.

    None means "say nothing here, fall back to the normal streaming/not-
    streaming tag" — which covers a released title, one with no usable date,
    and (deliberately) `status` alone. `status`/`release_date` are fetched
    once, like everything else in BACKFILL_COLUMNS, and never touched again —
    sync_providers.py doesn't refresh them either. Trusting a frozen `status`
    string forever would mean a title that was "Post Production" the day it
    got backfilled shows a stale "Releases {date-in-the-past}" badge forever
    after it actually comes out, permanently hiding real streaming
    availability that's sitting right there computed and correct — the exact
    "wrong result that looks right" failure this project is built against.
    Comparing the *date* against today, freshly, on every render, self-heals
    the moment the calendar catches up — no re-fetch, no cache to invalidate,
    nothing to remember.
    """
    if not release_date:
        # No date at all: status is the only signal, but a stale non-dated
        # "not released" claim can't silently outlive its truth the way a
        # concrete past date could, so trusting it here is safe indefinitely.
        if status in ("", "Released"):
            return None
        return '<span class="tag tag-upcoming">Not yet released</span>'
    try:
        when = dt.date.fromisoformat(release_date)
    except (ValueError, TypeError):
        return None
    if when <= dt.date.today():
        return None
    return f'<span class="tag tag-upcoming">Releases {esc(when.strftime("%b %-d, %Y"))}</span>'


def render_dialogs(items, movies, rentable=None):
    """One collapsed detail overlay per unique tmdb_id, revealed by CSS
    :target when its row is tapped. `items`: {tmdb_id: [services]}, already
    deduped by the caller — dict keys can't collide by construction, so a
    broken disjointness assumption there wouldn't produce invalid HTML; it
    would silently show the wrong services in a dialog (whichever source
    group was merged last wins for that id), which is quieter and worse.
    That disjointness isn't local to this file: it depends on poll_log
    having one row per movie per day shared across both lists (see
    sync_providers.resolved_ids()), which is what makes "not in today"
    actually mean "not a watchlist member" rather than just "wasn't polled".
    Touching poll_log's schema or how ids are polled should come back here.

    `rentable`: {tmdb_id: [provider names]} from rentable_snapshot(), shown
    only for a title that's released and not currently streaming anywhere —
    a title already streaming doesn't need "also rentable on X", and an
    unreleased title gets release_badge() instead, never both.

    The close links point at "#_close" rather than a bare "#": per the
    fragment-navigation spec, an empty fragment means "scroll to top of
    document", which would yank a long page back up every time a dialog
    closes. A fragment matching no element id clears :target with no scroll.
    """
    if not items:
        return ""
    rentable = rentable or {}

    out = []
    for tmdb_id, services in sorted(items.items()):
        movie = movies.get(tmdb_id)
        title = movie["title"] if movie else f"tmdb:{tmdb_id}"
        year = movie["year"] if movie else None
        runtime = movie["runtime"] if movie else None
        poster_path = movie["poster_path"] if movie else None
        # None until the next resolver run backfills a pre-existing row —
        # every field below degrades to "just don't show that line" rather
        # than a blank or a crash.
        overview = (movie["overview"] if movie else "") or ""
        director = (movie["director"] if movie else "") or ""
        genres = (movie["genres"] if movie else "") or ""
        top_cast = (movie["top_cast"] if movie else "") or ""
        trailer_key = (movie["trailer_key"] if movie else "") or ""
        status = (movie["status"] if movie else "") or ""
        release_date = (movie["release_date"] if movie else "") or ""
        certification = (movie["certification"] if movie else "") or ""
        vote_average = movie["vote_average"] if movie else None
        vote_count = movie["vote_count"] if movie else None

        meta = " · ".join(
            esc(v)
            for v in (year, f"{runtime} min" if runtime else None, certification or None)
            if v
        )

        byline = []
        if genres:
            byline.append(esc(genres))
        if director:
            byline.append(f"Directed by {esc(director)}")
        byline_html = f'<div class="card-meta">{" · ".join(byline)}</div>' if byline else ""
        cast_html = f'<div class="card-meta">Starring {esc(top_cast)}</div>' if top_cast else ""

        score_html = ""
        if vote_average is not None and vote_count:
            score_html = (
                f'<div class="score">★ {vote_average:.1f}'
                f'<span class="text-muted"> · {vote_count:,} votes</span></div>'
            )

        overview_html = f'<p class="dialog-body">{esc(overview)}</p>' if overview else ""

        # An unreleased title gets its own badge instead of the usual
        # streaming/not-streaming tag — "not currently streaming" is true but
        # misleading for something that was never eligible to stream at all.
        upcoming = release_badge(status, release_date)
        tags_html = upcoming or tag_chips(services)

        # Only for a released title with nothing streaming: already-streaming
        # doesn't need an alternative, and an unreleased title isn't rentable
        # yet either regardless of what TMDB's rent/buy list might still show
        # from a prior release window (re-releases, festival runs).
        rentable_html = ""
        if not upcoming and not services:
            providers = rentable.get(tmdb_id) or []
            if providers:
                rentable_html = (
                    f'<div class="card-meta">Rentable on {esc(", ".join(providers))}</div>'
                )

        trailer_html = ""
        if trailer_key:
            # quote(), not just esc(): a stray "&" or "#" in a malformed key
            # would be read as query/fragment syntax by the browser and link
            # to the wrong video, quietly, rather than fail loudly. esc()
            # alone only stops it from breaking out of the href attribute.
            v = urllib.parse.quote(trailer_key, safe="")
            trailer_url = esc(f"https://www.youtube.com/watch?v={v}")
            trailer_html = f'<a href="{trailer_url}">▶ Watch trailer</a> · '

        poster = poster_html(
            poster_path,
            css_class="poster dialog-poster",
            size=DIALOG_POSTER_SIZE,
            width=84,
            height=126,
        )
        tmdb_url = f"https://www.themoviedb.org/movie/{tmdb_id}"

        out.append(
            f'<div id="m{tmdb_id}" class="dialog-target">'
            f'<a href="#_close" class="dialog-backdrop" tabindex="-1" aria-hidden="true"></a>'
            f'<div class="dialog elev-lg" role="dialog" aria-modal="true" '
            f'aria-labelledby="m{tmdb_id}-title">'
            f'<a href="#_close" class="dialog-close" aria-label="Close">✕</a>'
            f'<div class="dialog-head">{poster}'
            f'<div><div id="m{tmdb_id}-title" class="dialog-title">{esc(title)}</div>'
            f'<div class="card-meta">{meta}</div>{byline_html}{cast_html}{score_html}'
            f"</div></div>"
            f'<div class="tag-row">{tags_html}</div>'
            f"{rentable_html}"
            f"{overview_html}"
            f'<p class="dialog-link">{trailer_html}<a href="{esc(tmdb_url)}">'
            f"Full cast, reviews, and rental prices on TMDB →</a></p>"
            f"</div></div>"
        )
    return "\n".join(out)


def render_details(summary, items, movies):
    """A collapsed <details> block, or "" when there's nothing to hide.

    <details>/<summary> is native HTML: it collapses and expands with no
    JavaScript, which the zero-JS rule requires.
    """
    if not items:
        return ""
    return (
        f'<details class="hidden-list">\n'
        f"<summary>{esc(summary)} ({len(items)})</summary>\n"
        f"{render_list(items, movies, '', variant='off')}\n"
        f"</details>"
    )


def _truncate(text, limit=140):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def tonight_pick(streaming, new, latest):
    """-> a featured tmdb_id, or None if nothing is streaming.

    Seeded by the poll date, not truly random: stable across repeat renders
    on the same day (this project gets re-run by hand plenty during
    development, and a pick that changes every run would be a strange kind
    of not-broken-but-useless). Prefers `new` over `streaming` when there is
    a `new` title — something that just became available is more worth
    surfacing than an arbitrary pick from the whole list.
    """
    pool = new if new else streaming
    if not pool:
        return None
    return random.Random(f"tonight:{latest}").choice(sorted(pool))


def render_hero(tmdb_id, services, movies):
    """-> the "Tonight's Pick" hero card, or "" if there's nothing to feature."""
    movie = movies.get(tmdb_id) if tmdb_id is not None else None
    if not movie:
        return ""

    title = movie["title"]
    year = movie["year"]
    runtime = movie["runtime"]
    overview = movie["overview"] or ""

    meta = " · ".join(esc(v) for v in (year, f"{runtime} min" if runtime else None) if v)
    poster = poster_html(
        movie["poster_path"], css_class="poster hero-poster", width=72, height=108
    )
    tag_row = f'<div class="tag-row">{tag_chips(services)}</div>' if services else ""
    blurb = f'<p class="hero-blurb">{esc(_truncate(overview))}</p>' if overview else ""

    return (
        f'<a href="#m{tmdb_id}" class="hero-card elev-lg">'
        f'<div class="hero-kicker">Tonight’s pick</div>'
        f'<div class="hero-body">{poster}'
        f'<div class="row-info"><div class="hero-title">{esc(title)}</div>'
        f"<div class=\"card-meta\">{meta}</div>{tag_row}{blurb}</div></div>"
        f"</a>"
    )


def members(conn, table):
    """Every tmdb_id on a list, streaming or not."""
    if table not in LIST_TABLES:
        raise ValueError(f"unknown list table {table!r}")
    return {r["tmdb_id"] for r in conn.execute(f"SELECT tmdb_id FROM {table}")}  # noqa: S608


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
        rentable = rentable_snapshot(conn, latest)

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

        # Everything not currently streaming, collapsed rather than dropped. A
        # title silently missing from the page reads as "not on the list"; the
        # counts here are what make the page feel complete instead of thin.
        # Membership drives these, not the poll, so a title that failed its
        # fetch still appears rather than vanishing.
        watch_hidden = {i: [] for i in members(conn, "watchlist") - set(streaming)}
        fav_hidden = {
            i: []
            for i in members(conn, "favorites") - set(fav_streaming) - members(conn, "watchlist")
        }

        # Every id shown anywhere on the page needs a dialog. The four sets
        # are pairwise disjoint by construction (fav_streaming excludes
        # watchlist ids, *_hidden excludes their *_streaming counterpart), so
        # a plain merge is safe — see render_dialogs()'s own note for the
        # defensive case where that construction ever changes.
        dialog_items = {}
        for group in (streaming, watch_hidden, fav_streaming, fav_hidden):
            dialog_items.update(group)

        pick_id = tonight_pick(streaming, new, latest)
        hero_html = render_hero(pick_id, streaming.get(pick_id, []), movies)

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
            hero=hero_html,
            filter_radios=render_filter_radios(),
            filter_bar=render_filter_bar(),
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
            section_watchlist_hidden=render_details(
                "Not currently streaming", sorted(watch_hidden.items(), key=key), movies
            ),
            section_favorites=render_list(
                sorted(fav_streaming.items(), key=key),
                movies,
                "None of your favorites are streaming right now.",
            ),
            section_favorites_hidden=render_details(
                "Not currently streaming", sorted(fav_hidden.items(), key=key), movies
            ),
            footer=esc(
                f"{len(today)} of {watchlist_size} watchlist titles polled on {latest}"
            ),
            dialogs=render_dialogs(dialog_items, movies, rentable),
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
