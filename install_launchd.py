#!/usr/bin/env python3
"""Install (or remove) the 04:00 launchd job.

Generates ~/Library/LaunchAgents/com.<user>.watchlist.plist and bootstraps it.

launchd rather than cron, deliberately: if the Mac is asleep at 04:00 cron skips
the run silently, launchd queues it and fires on wake.

Every path is derived at runtime — the interpreter from sys.executable, the repo
from this file's location, the user from getpass. Nothing here hardcodes a
username, so the generator is safe to keep in a public repo even though the
plist it writes (outside the repo) necessarily contains absolute paths.
"""

import argparse
import getpass
import os
import plistlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
AGENTS = Path.home() / "Library" / "LaunchAgents"
LABEL = f"com.{getpass.getuser()}.watchlist"
PLIST = AGENTS / f"{LABEL}.plist"
HOUR, MINUTE = 4, 0


def build_plist():
    log = REPO / "launchd.log"  # gitignored by *.log
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(REPO / "sync.py")],
        "WorkingDirectory": str(REPO),
        # Queued and fired on wake if the Mac is asleep at 04:00.
        "StartCalendarInterval": {"Hour": HOUR, "Minute": MINUTE},
        # False on purpose: loading the agent should not trigger a sync, and
        # neither should a reboot at an arbitrary hour.
        "RunAtLoad": False,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "ProcessType": "Background",
    }


def launchctl(*args, check=False):
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=check, timeout=60
    )


def domain():
    return f"gui/{os.getuid()}"


def uninstall():
    result = launchctl("bootout", f"{domain()}/{LABEL}")
    if result.returncode == 0:
        print(f"booted out {LABEL}")
    elif "No such process" in (result.stderr or ""):
        print(f"{LABEL} was not loaded")
    else:
        print(f"bootout: {(result.stderr or '').strip()}")
    if PLIST.exists():
        PLIST.unlink()
        print(f"removed {PLIST}")


def install():
    AGENTS.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(build_plist()))
    print(f"wrote {PLIST}")

    # Replacing an existing job requires booting the old one out first;
    # bootstrap on an already-loaded label fails with "service already loaded".
    launchctl("bootout", f"{domain()}/{LABEL}")

    result = launchctl("bootstrap", domain(), str(PLIST))
    if result.returncode != 0:
        raise SystemExit(f"bootstrap failed: {(result.stderr or '').strip()}")
    print(f"bootstrapped {LABEL} into {domain()}")

    check = launchctl("print", f"{domain()}/{LABEL}")
    if check.returncode != 0:
        raise SystemExit("job did not register; check the plist")
    for line in check.stdout.splitlines():
        if any(k in line for k in ("state =", "program =", "runs =", "last exit")):
            print("  " + line.strip())
    print(f"\nnext run: {HOUR:02d}:{MINUTE:02d} daily. Log: {REPO / 'launchd.log'}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uninstall", action="store_true", help="bootout and delete the plist")
    args = ap.parse_args(argv)
    uninstall() if args.uninstall else install()
    return 0


if __name__ == "__main__":
    sys.exit(main())
