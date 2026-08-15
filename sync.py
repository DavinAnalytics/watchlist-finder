#!/usr/bin/env python3
"""The daily run: resolve -> poll providers -> render -> push.

Chains the three scripts and then commits the result to GitHub Pages. The git
push at the end is the only network write this project makes.

Each stage must succeed before the next one starts. A stale page that looks
current is the failure this ordering exists to prevent: if the provider sync
dies, the render never runs, docs/index.html keeps yesterday's content, and
nothing is pushed — so the page's own timestamp stays honest.
"""

import argparse
import datetime as dt
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

import common
import recommend
import render
import resolver
import sync_providers

# How long to wait for GitHub Pages to actually serve what was just pushed, and
# how often to re-check. A Pages build normally lands in 30-90s.
VERIFY_TIMEOUT = 240
VERIFY_INTERVAL = 10
VERIFY_NUDGE_TIMEOUT = 180

# Named files, never a directory: `git add docs` would stage anything that ever
# lands in docs/ — a debug dump, a stray export — and push it to a public repo.
# resolved.json and unresolved.txt ride along because they are the real data:
# resolved.json carries every hand-pinned id, and CLAUDE.md makes GitHub the
# backup. Without them the daily run leaves the repo permanently dirty and the
# hand-fixes unbacked-up.
COMMIT_PATHS = ["docs/index.html", "data/resolved.json", "data/unresolved.txt"]

# (name, entry point, required). A required stage that fails stops the run:
# every one of them is something the page would otherwise lie about.
#
# "recommend" is the only optional stage. It runs after providers — a dead
# network or key aborts above it, so nothing is spent picking recommendations
# for a page that was never going to be published — and by the time it runs,
# the watchlist page is already fully computed. Losing a whole day's page over
# a bonus section would trade a small failure for a much bigger one, so any
# exit code or unhandled exception from it is logged and stepped over.
# recommend.py guards its own known failure modes; `required=False` is what
# covers the unknown ones (a bug in this file's own logic, an unexpected TMDB
# response shape), which is exactly where a blanket try/except inside
# recommend.py would be least trustworthy.
STAGES = [
    ("resolver", resolver.main, True),
    ("providers", sync_providers.main, True),
    ("recommend", recommend.main, False),
    ("render", render.main, True),
]


def git(*args, check=True):
    """Run git in the repo. List args, no shell, no interpolation."""
    return subprocess.run(
        ["git", "-C", str(common.DIR), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=check,
    )


def in_git_repo():
    return git("rev-parse", "--git-dir", check=False).returncode == 0


def publish(logger, push=True):
    """Commit and push the generated page. -> True if anything was published."""
    if not in_git_repo():
        logger.error("%s is not a git repository; skipping publish", common.DIR)
        return False

    existing = [p for p in COMMIT_PATHS if (common.DIR / p).exists()]
    git("add", "--", *existing)

    staged = git("diff", "--cached", "--name-only").stdout.strip()
    if not staged:
        logger.info("nothing changed; not committing")
        return False
    logger.info("staged: %s", ", ".join(staged.splitlines()))

    stamp = dt.date.today().isoformat()
    git("commit", "-m", f"watchlist: sync {stamp}")

    if not push:
        logger.info("--no-push given; committed locally only")
        return True

    if not git("remote", check=False).stdout.strip():
        logger.warning("no git remote configured; committed locally only")
        return True

    result = git("push", check=False)
    if result.returncode != 0:
        # The commit is already safe locally, so this is recoverable by hand.
        logger.error("git push failed: %s", (result.stderr or "").strip()[:300])
        return False

    logger.info("pushed to %s", "origin")
    return True


REMOTE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?$"
)


def pages_url():
    """-> the GitHub Pages URL for this repo's origin, or None.

    Derived from the remote at runtime rather than written down: CLAUDE.md
    forbids hardcoding anything user-specific into a public repo, and that
    applies to the owner's account name just as much as to /Users/<name>/.
    """
    remote = git("remote", "get-url", "origin", check=False)
    if remote.returncode != 0:
        return None
    m = REMOTE_RE.match(remote.stdout.strip())
    if not m:
        return None
    owner, repo = m.group("owner"), m.group("repo")
    # A repo literally named <owner>.github.io is served at the domain root,
    # not under a path segment.
    if repo.casefold() == f"{owner.casefold()}.github.io":
        return f"https://{owner.casefold()}.github.io/"
    return f"https://{owner.casefold()}.github.io/{repo}/"


STAMP_RE = re.compile(r'<p class="stamp">([^<]*)</p>')


def local_stamp():
    """-> the generated-at line from the page just written, or None.

    This is the marker verify_published() looks for: it carries the run's
    wall-clock minute, so it is different on every run and can't accidentally
    match a previously deployed page the way a date-only marker could.
    """
    try:
        text = common.OUTPUT_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    m = STAMP_RE.search(text)
    return m.group(1) if m else None


def _serves_stamp(url, stamp, logger):
    """-> True if the live page already carries `stamp`."""
    # Cache-busting query: GitHub's CDN serves with max-age=600, so without
    # this a fresh deploy can stay invisible here for ten minutes and the
    # check would report a failure that isn't one.
    bust = f"{url}?_={int(time.time())}"
    req = urllib.request.Request(bust, headers={"User-Agent": common.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=common.HTTP_TIMEOUT) as resp:
            return stamp in resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.warning("could not read %s: %s", url, exc)
        return False


def _wait_for(url, stamp, logger, timeout):
    deadline = time.monotonic() + timeout
    while True:
        if _serves_stamp(url, stamp, logger):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(VERIFY_INTERVAL)


def verify_published(logger, nudge=True):
    """Confirm GitHub Pages actually serves the page that was just pushed.

    This exists because it demonstrably does not always happen. On 2026-08-14 a
    push landed on main correctly — raw.githubusercontent.com served the new
    file — and GitHub queued no Pages build for it at all, so the site kept
    serving a four-hour-old page. `git push` had exited 0 and the run logged
    success. Nothing in the pipeline noticed; it was found by refreshing a
    phone. A push that reports success while the page silently stays stale is
    exactly the failure this project is built against, so the push is no longer
    treated as proof of publication.

    Note this is deliberately *not* the "cancelled build" pattern also visible
    in that history: GitHub cancels a Pages build when another push lands
    seconds later, and every one of those was immediately followed by a
    successful build of the newer commit. That coalescing is correct and needs
    no handling. The dropped build is the real fault.

    -> True if the live page matches. On failure, optionally pushes one empty
    commit to re-trigger Pages (a later real push is what unstuck it last
    time), then re-checks once.
    """
    url = pages_url()
    stamp = local_stamp()
    if not url:
        logger.info("no github.com origin; skipping publish verification")
        return True
    if not stamp:
        logger.warning("no timestamp found in %s; cannot verify", common.OUTPUT_PATH.name)
        return True

    logger.info("verifying %s serves this run (up to %ds)", url, VERIFY_TIMEOUT)
    if _wait_for(url, stamp, logger, VERIFY_TIMEOUT):
        logger.info("verified: the live page is this run's")
        return True

    logger.error(
        "PAGE IS STALE: %s did not serve this run within %ds. The commit is "
        "pushed and correct — this is GitHub Pages not publishing it.",
        url, VERIFY_TIMEOUT,
    )
    if not nudge:
        return False

    logger.info("pushing an empty commit to re-trigger the Pages build")
    try:
        git("commit", "--allow-empty", "-m", "watchlist: re-trigger pages build")
        pushed = git("push", check=False)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.error("could not push the re-trigger commit: %s", exc)
        return False
    if pushed.returncode != 0:
        logger.error("re-trigger push failed: %s", (pushed.stderr or "").strip()[:300])
        return False

    if _wait_for(url, stamp, logger, VERIFY_NUDGE_TIMEOUT):
        logger.info("verified after re-trigger: the live page is this run's")
        return True
    logger.error(
        "STILL STALE after a re-trigger. Check the repo's Pages settings and "
        "build history by hand; docs/index.html on main is correct."
    )
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-push", action="store_true", help="commit locally, don't push")
    ap.add_argument("--no-publish", action="store_true", help="skip git entirely")
    ap.add_argument(
        "--no-verify",
        action="store_true",
        help="don't wait to confirm GitHub Pages served the new page",
    )
    args = ap.parse_args(argv)

    logger = common.setup_logging("sync")
    started = dt.datetime.now()
    logger.info("=== daily sync starting ===")

    for name, stage, required in STAGES:
        try:
            code = stage([])
        except SystemExit as exc:
            # Stages abort this way on a dead key or a dead network. Both mean
            # the data behind the page is untrustworthy, so stop here.
            code = exc.code if isinstance(exc.code, int) else 1
            logger.error("stage %s aborted: %s", name, exc)
        except Exception:
            logger.exception("stage %s raised", name)
            code = 1

        if code:
            if not required:
                # Loud in the log — this is still a fault, just not one worth
                # withholding the whole page over.
                logger.error(
                    "stage %s failed (exit %s); continuing without it", name, code
                )
                continue
            logger.error(
                "stopping after %s (exit %s); docs/index.html left untouched", name, code
            )
            return code

    published = False
    if not args.no_publish:
        try:
            published = publish(logger, push=not args.no_push)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            # Same structured logging the stages get: a bad run has to be
            # diagnosable from watchlist.log alone. Nothing was pushed, and the
            # generated page is still on disk, so this is recoverable by hand.
            stderr = getattr(exc, "stderr", "") or ""
            logger.error("publish failed: %s %s", exc, stderr.strip()[:300])
            return 1

    # Only when something was actually pushed: there is nothing to verify after
    # a run that published nothing (page unchanged) or one held local.
    verified = None
    if published and not args.no_push and not args.no_verify:
        verified = verify_published(logger)

    logger.info(
        "=== daily sync complete in %.1fs, published=%s, verified=%s ===",
        (dt.datetime.now() - started).total_seconds(),
        published,
        verified,
    )
    # A page that never went live is a failed run, even though every stage
    # above it succeeded — the whole point of the run is the page.
    return 0 if verified is not False else 1


if __name__ == "__main__":
    sys.exit(main())
