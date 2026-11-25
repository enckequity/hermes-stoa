"""The files the talk box handed to the assistant, and removing them.

A file dropped in the page's talk box (a PDF, a Word file, a picture) is given to the assistant through the gateway's
own attach calls, and the assistant keeps a copy of its own in the profile's home: ``attachments/`` for a file,
``images/`` for a picture. The assistant clears what it is sent through a messaging platform within a day and what a
browser stages for upload within a week, but nothing clears these two folders. So Stoa keeps a list of the
copies its own talk box caused, by the path the assistant gave back, and removes them: after a set number of days
(seven unless changed), or when a person presses the button.

Only a file on that list is ever removed, and only while it is still the same file in one of those two
folders: a file from another chat is never on the list, and one that has changed since is left alone.
The list is ``handed-files.json`` in the plugin's data folder. It holds paths, names and sizes, no contents.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any, Optional

from . import store as st

log = logging.getLogger("hermes.plugins.stoa")
LIST = "handed-files.json"
FOLDERS = ("attachments", "images")
KEEP_DAYS = 7            # the usual: what the assistant itself gives a browser's staged upload
MOST_DAYS = 3650
MOST_LISTED = 2000       # the list is trimmed past this, oldest first, once their files are gone


class Handed:
    def __init__(self, desk):
        self.desk = desk
        self._lock = threading.Lock()

    # -- where things are -----------------------------------------------------------------------
    def home(self) -> Path:
        """The assistant profile home these files are in: the one the assistant built the desk for, else the one the data
        folder sits in (``<home>/plugin-data/stoa``)."""
        home = getattr(self.desk, "home", None)
        return Path(home) if home else self.desk.data_dir.parent.parent

    def roots(self) -> list:
        return [self.home() / name for name in FOLDERS]

    def _path(self) -> Path:
        return self.desk.data_dir / LIST

    def _read(self) -> dict:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        files = [entry for entry in data.get("files") or [] if isinstance(entry, dict) and entry.get("path") and entry.get("id")]
        return {"files": files, "elsewhere": int(data.get("elsewhere") or 0), "removed": int(data.get("removed") or 0)}

    def _write(self, data: dict) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        spare = path.with_suffix(".json.tmp")
        spare.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(spare, path)

    def keep_days(self) -> int:
        """After how many days a handed file is removed. 0: never by itself."""
        try:
            days = int(self.desk.setting("files_keep_days", KEEP_DAYS))
        except (TypeError, ValueError):
            days = KEEP_DAYS
        return max(0, min(MOST_DAYS, days))

    def set_keep_days(self, days: Any) -> dict:
        try:
            days = int(days)
        except (TypeError, ValueError):
            return {"ok": False, "error": "a number of days, or 0 to keep them until you remove them"}
        if days < 0 or days > MOST_DAYS:
            return {"ok": False, "error": "between 0 and %d days" % MOST_DAYS}
        if not self.desk.can_configure:
            return {"ok": False, "error": "This cannot be saved from here. Change it in Stoa's settings: files_keep_days."}
        self.desk._set_config("files_keep_days", days)
        self.sweep()
        return dict(self.listing(), ok=True)

    # -- what is the same file ------------------------------------------------------------------
    def _inside(self, path: Path) -> Optional[Path]:
        """The folder a path is in, when it is directly reachable under one of the assistant's two (after following the
        folder's own links, so a home that is itself a link still counts)."""
        try:
            resolved = path.resolve()
        except OSError:
            return None
        for root in self.roots():
            try:
                base = root.resolve()
            except OSError:
                continue
            if base in resolved.parents:
                return root
        return None

    @staticmethod
    def _mark(path: Path) -> Optional[tuple]:
        """``(size, modified)`` of a plain file. None for a link, a folder, or nothing there."""
        try:
            info = os.lstat(path)
        except OSError:
            return None
        return (info.st_size, int(info.st_mtime_ns)) if stat.S_ISREG(info.st_mode) else None

    # -- the list -------------------------------------------------------------------------------
    def note(self, path: Any, name: Any = "", lane: Any = "") -> dict:
        """The assistant has just kept a copy of a file the talk box handed it: put it on the list."""
        raw = str(path or "").strip()
        if not raw:
            return {"ok": False, "error": "no path"}
        target = Path(raw)
        with self._lock:
            data = self._read()
            if not target.is_absolute() or self._inside(target) is None or self._mark(target) is None:
                data["elsewhere"] += 1          # kept somewhere Stoa does not reach (a workspace, another machine)
                self._write(data)
                return {"ok": False, "listed": False, "error": "that file is not in this profile's attachments or images folder"}
            size, modified = self._mark(target)
            ref = hashlib.sha1(str(target).encode("utf-8")).hexdigest()[:12]
            entry = {"id": ref, "path": str(target), "name": str(name or target.name)[:200], "kind": self._inside(target).name,
                     "bytes": size, "modified": modified, "at": st.now(), "lane": str(lane or "")[:20]}
            data["files"] = [item for item in data["files"] if item["id"] != ref] + [entry]
            self._write(data)
        self.sweep()
        return {"ok": True, "listed": True, "id": ref}

    def _state(self, entry: dict) -> str:
        """there: the same file is still in its folder. gone: nothing is. changed: something else is."""
        target = Path(entry["path"])
        mark = self._mark(target)
        if mark is None:
            return "gone" if not os.path.lexists(target) else "changed"
        if self._inside(target) is None:
            return "changed"
        return "there" if mark == (entry.get("bytes"), entry.get("modified")) else "changed"

    def listing(self) -> dict:
        """For Setup: the files on the list that are still there, when each goes, and how much the assistant holds in
        the two folders altogether (counts and sizes only: the other files there are not Stoa's to name)."""
        with self._lock:
            data = self._read()
        days = self.keep_days()
        shown = []
        for entry in data["files"]:
            state = self._state(entry)
            if state == "gone":
                continue
            age = st.days_since(entry.get("at") or "") or 0.0
            shown.append({"id": entry["id"], "name": entry["name"], "kind": entry["kind"], "bytes": entry.get("bytes") or 0, "at": entry.get("at") or "",
                          "changed": state == "changed", "days_left": None if not days or state == "changed" else max(0, int(days - age + 0.999))})
        folders = []
        for root in self.roots():
            count = size = 0
            try:
                for item in os.scandir(root):
                    if item.is_file(follow_symlinks=False):
                        count, size = count + 1, size + item.stat(follow_symlinks=False).st_size
            except OSError:
                pass
            mine = len([entry for entry in shown if entry["kind"] == root.name])
            folders.append({"name": root.name, "path": str(root), "files": count, "bytes": size, "from_here": mine})
        return {"files": sorted(shown, key=lambda entry: entry["at"], reverse=True), "bytes": sum(entry["bytes"] for entry in shown),
                "keep_days": days, "can_save": bool(self.desk.can_configure), "folders": folders, "elsewhere": data["elsewhere"],
                "removed": data["removed"]}

    def remove(self, ids: Any = None) -> dict:
        """Remove the listed files (all of them, or the ones named), each only while it is still the same file."""
        wanted = None if ids in (None, "", "all") else {str(ref) for ref in (ids if isinstance(ids, (list, tuple, set)) else [ids])}
        return self._clear(lambda entry: wanted is None or entry["id"] in wanted)

    def sweep(self) -> dict:
        """Remove the listed files that have been kept longer than the days set. Never raises: tidying up must not
        get in the way of the desk."""
        days = self.keep_days()
        if not days:
            return {"ok": True, "removed": 0, "left": []}
        try:
            return self._clear(lambda entry: (st.days_since(entry.get("at") or "") or 0.0) >= days)
        except Exception as exc:
            log.warning("stoa: could not tidy the files handed to the assistant: %s", exc)
            return {"ok": False, "removed": 0, "left": []}

    def _clear(self, due) -> dict:
        removed, left = 0, []
        with self._lock:
            data = self._read()
            kept = []
            for entry in data["files"]:
                state = self._state(entry)
                if state == "gone":
                    continue                                    # already removed by someone: forget it
                if not due(entry):
                    kept.append(entry)
                    continue
                if state == "changed":
                    kept.append(entry)
                    left.append({"name": entry["name"], "why": "it is not the file that was handed over any more"})
                    continue
                try:
                    os.unlink(entry["path"])
                    removed += 1
                except OSError as exc:
                    kept.append(entry)
                    left.append({"name": entry["name"], "why": str(exc.strerror or exc)})
            if removed or len(kept) != len(data["files"]):
                data["files"], data["removed"] = kept[-MOST_LISTED:], data["removed"] + removed
                self._write(data)
        if removed:
            log.info("stoa: removed %d file(s) the talk box had handed to the assistant", removed)
        return {"ok": True, "removed": removed, "left": left}
