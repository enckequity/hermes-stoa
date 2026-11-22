"""Assets: what a team reaches for when it sells.

The GTM tab is the one place the person keeps how they sell. Market maps say to whom (and, in their brief, what
the business does), motions say how, and this is what gets sent and shown along the way: the key assets (the
pitch deck, a one-pager, a case study), each either a file kept with the desk or a link to where it lives, and
the links worth having to hand (the website, the booking page).

They belong to the desk as a whole, not to one pipeline. Everything is kept beside the desk: ``hub.json``, and
the files themselves in ``assets/``. The agent can read the list (so a follow-up it drafts points at the real
booking page) and can add a link, but it never reads or sends the files.

``hub.json`` also keeps each market map's internal notes (gtm.py), under ``map_notes``.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Optional



class JsonFile:
    """A small JSON object kept in one file: read afresh when it changes on disk, written whole and moved into place,
    so a reader in another process never sees half of it."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._seen: tuple = (None, {})

    def all(self) -> dict:
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            return {}
        if self._seen[0] != stamp:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            self._seen = (stamp, data if isinstance(data, dict) else {})
        return self._seen[1]

    def get(self, key: str, default: Any = None) -> Any:
        value = self.all().get(key, default)
        return default if value is None else value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            data = dict(self.all())
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            partial = self.path.with_name(".%s.%d.part" % (self.path.name, os.getpid()))
            partial.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(partial, self.path)
            self._seen = (None, {})

ASSET_BYTES = 250_000_000
KINDS = {"deck": "Pitch deck", "one-pager": "One-pager", "case-study": "Case study", "pricing": "Pricing", "demo": "Demo", "other": "Other"}

class Refused(Exception):
    """A request that cannot be done, in words for whoever asked."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_url(url: Any) -> str:
    text = str(url or "").strip()
    if text and not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        text = "https://" + text            # "acme.com" is what people type
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", text, re.I) or len(text) > 2000:
        raise Refused("That is not a web address. It should look like https://example.com/page.")
    return text


def safe_name(name: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ ()-]+", "_", Path(str(name or "file")).name).strip(" .") or "file"
    return cleaned[:120]


class Assets:
    def __init__(self, desk):
        self.desk = desk
        self.file = JsonFile(desk.data_dir / "hub.json")
        self.folder = desk.data_dir / "assets"
        self._lock = threading.RLock()

    # -- reading ---------------------------------------------------------------------------------------------
    def _kept(self, key: str) -> list:
        value = self.file.get(key, [])
        return [dict(item) for item in value] if isinstance(value, list) else []

    def view(self) -> dict:
        assets = []
        for asset in self._kept("assets"):
            path = self.folder / asset["file"] if asset.get("file") else None
            if path is not None and not path.is_file():
                continue                    # the file was taken out of the folder by hand
            assets.append({"id": asset["id"], "label": asset["label"], "kind": asset.get("kind", "other"), "kind_label": KINDS.get(asset.get("kind", "other"), "Other"),
                           "url": asset.get("url", ""), "file": asset.get("file", ""), "name": asset.get("name", ""), "size": path.stat().st_size if path else 0,
                           "added": asset.get("added", "")})
        return {"links": self._kept("links"), "assets": assets, "kinds": KINDS}

    # -- writing ---------------------------------------------------------------------------------------------
    def add_link(self, label: Any, url: Any) -> dict:
        address = clean_url(url)
        name = " ".join(str(label or "").split())[:80] or re.sub(r"^https?://(www\.)?", "", address).split("/")[0]
        with self._lock:
            links = [link for link in self._kept("links") if link["label"].casefold() != name.casefold()]
            links.append({"id": "l" + uuid.uuid4().hex[:8], "label": name, "url": address})
            self.file.set("links", links[:40])
        return self.view()

    def _asset(self, entry: dict) -> dict:
        with self._lock:
            assets = self._kept("assets")
            assets.append(dict(entry, id="a" + uuid.uuid4().hex[:8], added=now()))
            self.file.set("assets", assets[:200])
        return self.view()

    def add_asset_link(self, label: Any, url: Any, kind: Any = "other") -> dict:
        address = clean_url(url)
        name = " ".join(str(label or "").split())[:120]
        if not name:
            raise Refused("An asset needs a name, such as 'Pitch deck'.")
        return self._asset({"label": name, "kind": str(kind or "other") if str(kind or "other") in KINDS else "other", "url": address})

    def add_asset_file(self, name: Any, stream: BinaryIO, label: Any = "", kind: Any = "other") -> dict:
        """Keep a file with the desk. It is written to a scratch name and only then moved into place, and one over
        the size limit is not kept at all."""
        original = safe_name(name)
        self.folder.mkdir(parents=True, exist_ok=True)
        stored = "%s-%s" % (uuid.uuid4().hex[:8], original)
        scratch, written = self.folder / (stored + ".part"), 0
        try:
            with open(scratch, "wb") as out:
                while True:
                    piece = stream.read(1 << 20)
                    if not piece:
                        break
                    written += len(piece)
                    if written > ASSET_BYTES:
                        raise Refused("That file is larger than 250 MB. Keep it where it is and add a link to it instead.")
                    out.write(piece)
            if not written:
                raise Refused("That file is empty.")
            scratch.replace(self.folder / stored)
        finally:
            scratch.unlink(missing_ok=True)
        title = " ".join(str(label or "").split())[:120] or Path(original).stem.replace("_", " ").replace("-", " ").strip() or original
        guessed = str(kind or "") if str(kind or "") in KINDS else ("deck" if re.search(r"deck|pitch|\.(key|pptx?)$", original, re.I) else
                                                                    "one-pager" if re.search(r"one.?pager|overview", original, re.I) else
                                                                    "case-study" if re.search(r"case.?stud", original, re.I) else
                                                                    "pricing" if re.search(r"pric", original, re.I) else "other")
        return self._asset({"label": title, "kind": guessed, "file": stored, "name": original})

    # The page's requests carry JSON, so a file comes in pieces: each base64, in order, under an id the page made up.
    # The pieces gather in a scratch file; the last one hands it to ``add_asset_file``. Saving a copy goes the other way.
    PIECE = 2 * 1024 * 1024

    def receive(self, upload: Any, name: Any, data: Any, part: int, last: bool, label: Any = "", kind: Any = "") -> dict:
        import base64
        key = str(upload or "")
        if not re.fullmatch(r"[a-f0-9]{8,32}", key):
            raise Refused("That upload has no proper id.")
        try:
            chunk = base64.b64decode(str(data or ""), validate=True)
        except ValueError:
            raise Refused("A piece of the file did not arrive whole. Try again.")
        self.folder.mkdir(parents=True, exist_ok=True)
        scratch = self.folder / (".upload-%s.part" % key)
        if part == 0:
            scratch.unlink(missing_ok=True)
        elif not scratch.exists():
            raise Refused("The start of that file did not arrive. Try again.")
        if scratch.exists() and scratch.stat().st_size + len(chunk) > ASSET_BYTES or len(chunk) > ASSET_BYTES:
            scratch.unlink(missing_ok=True)
            raise Refused("That file is larger than 250 MB. Keep it where it is and add a link to it instead.")
        with open(scratch, "ab") as out:
            out.write(chunk)
        if not last:
            return {"ok": True, "part": part}
        try:
            with open(scratch, "rb") as stream:
                return self.add_asset_file(name, stream, label, kind)
        finally:
            scratch.unlink(missing_ok=True)

    def piece(self, asset_id: str, part: int = 0) -> dict:
        import base64
        path, name = self.path_of(asset_id)
        size = path.stat().st_size
        parts = max(1, -(-size // self.PIECE))
        if not 0 <= int(part) < parts:
            raise Refused("There is no such piece of that file.")
        with open(path, "rb") as stream:
            stream.seek(int(part) * self.PIECE)
            data = stream.read(self.PIECE)
        return {"name": name, "size": size, "part": int(part), "parts": parts, "data": base64.b64encode(data).decode("ascii")}

    def change(self, item_id: str, label: Any = None, kind: Any = None) -> dict:
        with self._lock:
            for key in ("links", "assets"):
                items = self._kept(key)
                for item in items:
                    if item["id"] == item_id:
                        if label is not None and " ".join(str(label).split()):
                            item["label"] = " ".join(str(label).split())[:120]
                        if kind is not None and key == "assets" and str(kind) in KINDS:
                            item["kind"] = str(kind)
                        self.file.set(key, items)
                        return self.view()
        raise Refused("That is no longer there.")

    def remove(self, ref: Any) -> dict:
        """Take a link or an asset off the list, by id or by its label. A file that was kept with the desk goes with it."""
        wanted = str(ref or "").strip()
        with self._lock:
            for key in ("links", "assets"):
                items = self._kept(key)
                hit = next((item for item in items if item["id"] == wanted or item["label"].casefold() == wanted.casefold()), None)
                if hit is None:
                    continue
                self.file.set(key, [item for item in items if item["id"] != hit["id"]])
                if hit.get("file"):
                    (self.folder / hit["file"]).unlink(missing_ok=True)
                return self.view()
        raise Refused("There is no link or asset called '%s'." % wanted)

    def path_of(self, asset_id: str) -> tuple:
        """(the file, the name to give it) for an asset kept as a file. Only a file in the desk's own assets folder."""
        asset = next((item for item in self._kept("assets") if item["id"] == asset_id and item.get("file")), None)
        if asset is None:
            raise Refused("That asset is a link, or is no longer there.")
        path = (self.folder / asset["file"]).resolve()
        if path.parent != self.folder.resolve() or not path.is_file():
            raise Refused("That file is no longer in the desk's folder.")
        return path, asset.get("name") or asset["file"]

    # -- the tool an agent uses -------------------------------------------------------------------------------
    def tool(self, args: dict, session: Optional[dict] = None) -> dict:
        """The links and assets actions of ``gtm_context``, as the agent calls them."""
        args = dict(args or {})
        action = str(args.get("action") or "").strip().lower()
        try:
            if (session or {}).get("platform") == "cron" and action != "get":
                raise Refused("A scheduled job may read the links and assets but not change them.")
            if action == "get":
                kept = self.view()
                return {"ok": True, "links": [{"label": link["label"], "url": link["url"]} for link in kept["links"]],
                        "assets": [{"label": asset["label"], "kind": asset["kind_label"], "url": asset["url"] or "(a file kept on the Stoa page)"} for asset in kept["assets"]],
                        "note": "What the business sells, and to whom, is on its market maps."}
            if action == "add_link":
                self.add_link(args.get("label"), args.get("url"))
            elif action == "add_asset":
                self.add_asset_link(args.get("label"), args.get("url"), args.get("kind"))
            elif action == "remove":
                self.remove(args.get("label"))
            else:
                raise Refused("gtm_context does not have an action called %s." % (action or "(none)"))
        except Refused as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "note": "It is under Assets on the GTM tab of the Stoa page."}
