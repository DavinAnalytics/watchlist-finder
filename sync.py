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
import subprocess
import sys

import common
import render
import resolver
import sync_providers

# Named files, never a directory: `git add docs` would stage anything that ever
# lands in docs/ — a debug dump, a stray export — and push it to a public repo.
# resolved.json and unresolved.txt ride along because they are the real data:
# resolved.json carries every hand-pinned id, and CLAUDE.md makes GitHub the
# backup. Without them the daily run leaves the repo permanently dirty and the
# hand-fixes unbacked-up.
COMMIT_PATHS = ["docs/index.html", "data/resolved.json", "data/unresolved.txt"]

STAGES = [
    ("resolver", resolver.main),
    ("providers", sync_providers.main),
    ("render", render.main),
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-push", action="store_true", help="commit locally, don't push")
    ap.add_argument("--no-publish", action="store_true", help="skip git entirely")
    args = ap.parse_args(argv)

    logger = common.setup_logging("sync")
    started = dt.datetime.now()
    logger.info("=== daily sync starting ===")

    for name, stage in STAGES:
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

    logger.info(
        "=== daily sync complete in %.1fs, published=%s ===",
        (dt.datetime.now() - started).total_seconds(),
        published,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
