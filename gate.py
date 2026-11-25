"""A pre-run script gate for the desk's scheduled jobs: no model call unless there is work.

Installed once per routine as ``<home>/scripts/stoa_gate_<blueprint>.py`` (or run as ``gate.py <blueprint>``) and given
to the scheduled job as its pre-run ``--script``. It reads the desk files read-only and prints ``{"wakeAgent": false}``
when the routine has nothing to work on, so the job ends without a model call. Otherwise it prints one line of counts
for the run. A desk that cannot be read wakes the job so the failure is reported. Standard library only.

    morning-brief, notes-sweep   open records on any pipeline
    activity-sweep               open records with an email address to match activity against
    market-watch                 at least one market map
    sheet-mirror                 the desk changed since the last mirror run
    friday-review                anything on the ledger in the last seven days

sheet-pull is not gated: what it looks for is in the Sheet, which only the model can read.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

SKIP = json.dumps({"wakeAgent": False})
GATED = ("morning-brief", "notes-sweep", "activity-sweep", "market-watch", "sheet-mirror", "friday-review")
PREFIX = "stoa_gate_"


def data_dir() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugin-data" / "stoa"


def desks(data: Path) -> list:
    """Every pipeline's file: desk.db and pipe-*.db."""
    if not data.is_dir():
        return []
    return [p for p in [data / "desk.db", *sorted(data.glob("pipe-*.db"))] if p.is_file()]


def _read(path: Path, sql: str, args: tuple = ()) -> int:
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        row = conn.execute(sql, args).fetchone()
        return int((row or [0])[0] or 0)
    finally:
        conn.close()


def _table(path: Path, name: str) -> bool:
    return _read(path, "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)) > 0


OPEN = "SELECT COUNT(*) FROM deals WHERE status = 'open' AND archived_at = ''"


def measure(check: str, files: list, now: dt.datetime) -> tuple:
    """``(work, detail)`` for one routine over every pipeline file."""
    if check in ("morning-brief", "notes-sweep"):
        n = sum(_read(f, OPEN) for f in files if _table(f, "deals"))
        return n > 0, "%d open records" % n
    if check == "activity-sweep":
        n = sum(_read(f, OPEN + " AND email != ''") for f in files if _table(f, "deals"))
        return n > 0, "%d open records with an email address" % n
    if check == "market-watch":
        n = sum(_read(f, "SELECT COUNT(*) FROM markets") for f in files if _table(f, "markets"))
        return n > 0, "%d market maps" % n
    if check == "friday-review":
        since = (now - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        n = sum(_read(f, "SELECT COUNT(*) FROM ledger WHERE at >= ?", (since,)) for f in files if _table(f, "ledger"))
        return n > 0, "%d changes this week" % n
    if check == "sheet-mirror":
        mark = ",".join("%s:%d" % (f.name, _read(f, "SELECT COALESCE(MAX(seq), 0) FROM ledger")) for f in files if _table(f, "ledger"))
        return bool(mark), mark
    raise ValueError("not a gated routine: %s" % check)


def run(check: str, data: Path, now: dt.datetime) -> str:
    try:
        files = desks(data)
        work, detail = measure(check, files, now) if files else (False, "")
        if check == "sheet-mirror" and work:
            state = data / ("gate-%s.json" % check)
            try:
                last = json.loads(state.read_text()).get("mark", "")
            except (OSError, ValueError, AttributeError):
                last = ""
            if detail == last:
                return SKIP
            state.write_text(json.dumps({"mark": detail}))
            detail = "the desk changed since the last mirror"
    except ValueError:
        raise
    except Exception as exc:        # a desk that cannot be read: wake, so the run reports it
        return "The desk could not be read (%s); say so in one line." % type(exc).__name__
    return ("Desk check: %s." % detail) if work else SKIP


def check_name(argv: list) -> str:
    if len(argv) > 1:
        return argv[1]
    stem = Path(argv[0]).stem
    return stem[len(PREFIX):] if stem.startswith(PREFIX) else stem


def main(argv: list) -> int:
    check = check_name(argv)
    if check not in GATED:
        print("usage: gate.py <%s>" % "|".join(GATED), file=sys.stderr)
        return 2
    print(run(check, data_dir(), dt.datetime.now(dt.timezone.utc)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
