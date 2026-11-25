"""Outside data: People Data Labs enrichment and meeting notes (notes folders, Granola, Fireflies).

Standard library only. Nothing here reads a credential: callers pass the key they got from
The assistant's secret scope. Every network call in the plugin goes through ``http_json`` below, to
exactly four hosts: api.peopledatalabs.com, public-api.granola.ai, api.fireflies.ai and api.exa.ai.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import time
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

PDL = "https://api.peopledatalabs.com/v5"
EXA = "https://api.exa.ai"
GRANOLA = "https://public-api.granola.ai/v1"
FIREFLIES = "https://api.fireflies.ai/graphql"
# What a notes folder may hold. Stoa reads the first group itself. A PDF it cannot read with the standard library, so
# it is listed and handed to the assistant, which reads it with its own tools. A recording is only counted, so the person
# can be told the folder holds audio and no words yet.
NOTE_SUFFIXES = (".md", ".markdown", ".txt", ".docx", ".srt", ".vtt", ".html", ".htm", ".rtf")
NOTE_HANDED = (".pdf",)
NOTE_AUDIO = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus", ".flac", ".mp4", ".mov", ".webm")
NOTE_CHARS = 24000
NOTE_DEPTH = 3            # the folder and three levels below: a recorder's export is often year/month/meeting
NOTE_FILES = 6000         # files looked at in one folder before stopping, so a mistaken path (a home folder) stays cheap
NOTE_SKIP_DIRS = ("node_modules", "templates", "attachments", "assets")


def http_json(url: str, *, headers: Optional[dict] = None, params: Optional[dict] = None, timeout: int = 30,
              data: Optional[dict] = None) -> tuple:
    """GET a JSON document, or POST ``data`` as JSON. Returns ``(status, body)``; never raises for an
    HTTP error status."""
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    base = {"Accept": "application/json", "User-Agent": "hermes-stoa"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        base["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=dict(base, **(headers or {})))
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                body = json.loads(exc.read() or b"{}")
            except ValueError:
                body = {}
            return exc.code, body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return 0, {"error": str(exc)}
    return 0, {"error": "request failed"}


# -- People Data Labs ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    """PDL's free tier returns ``true`` in place of fields it hides; keep real strings only."""
    return value.strip() if isinstance(value, str) else ""


def summarize_person(data: dict) -> dict:
    jobs = []
    for exp in (data.get("experience") or [])[:5]:
        company = (exp.get("company") or {}).get("name") or ""
        title = (exp.get("title") or {}).get("name") or ""
        if company or title:
            jobs.append({"title": title, "company": company, "start": _text(exp.get("start_date")),
                         "end": _text(exp.get("end_date")) or ("present" if exp.get("is_primary") else "")})
    schools = []
    for edu in (data.get("education") or [])[:2]:
        school = (edu.get("school") or {}).get("name") or ""
        if school:
            schools.append({"school": school, "degrees": (edu.get("degrees") or [])[:2]})
    out = {
        "name": _text(data.get("full_name")).title(),
        "title": _text(data.get("job_title")),
        "company": _text(data.get("job_company_name")),
        "company_industry": _text(data.get("job_company_industry")),
        "company_size": _text(data.get("job_company_size")),
        "location": _text(data.get("location_name")) or _text(data.get("location_country")),
        "linkedin": _text(data.get("linkedin_url")),
        "experience": jobs,
        "education": schools,
        "skills": [s for s in (data.get("skills") or []) if isinstance(s, str)][:12],
    }
    return {k: v for k, v in out.items() if v}


def summarize_company(data: dict) -> dict:
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    out = {
        "name": _text(data.get("display_name")) or _text(data.get("name")),
        "website": _text(data.get("website")),
        "industry": _text(data.get("industry")),
        "size": _text(data.get("size")),
        "employees": data.get("employee_count") if isinstance(data.get("employee_count"), int) else None,
        "founded": data.get("founded") if isinstance(data.get("founded"), int) else None,
        "location": _text(location.get("name")),
        "linkedin": _text(data.get("linkedin_url")),
        "summary": _text(data.get("summary"))[:500],
        "tags": [t for t in (data.get("tags") or []) if isinstance(t, str)][:8],
    }
    return {k: v for k, v in out.items() if v}


def pdl_person(key: str, *, email: str = "", linkedin: str = "", name: str = "", company: str = "",
               http: Callable = http_json) -> dict:
    params = {"email": email, "profile": linkedin, "min_likelihood": 6}
    if not (email or linkedin):
        if not (name and company):
            return {"ok": False, "error": "need an email, a LinkedIn URL, or a name plus company"}
        params.update({"name": name, "company": company})
    status, body = http(PDL + "/person/enrich", headers={"X-Api-Key": key}, params=params)
    if status == 200 and isinstance(body.get("data"), dict):
        return {"ok": True, "match": True, "likelihood": body.get("likelihood"), "profile": summarize_person(body["data"])}
    if status == 404:
        return {"ok": True, "match": False}
    return {"ok": False, "error": _pdl_error(status, body)}


def pdl_company(key: str, *, website: str = "", name: str = "", http: Callable = http_json) -> dict:
    if not (website or name):
        return {"ok": False, "error": "need a company website or name"}
    status, body = http(PDL + "/company/enrich", headers={"X-Api-Key": key}, params={"website": website, "name": name})
    if status == 200:
        return {"ok": True, "match": True, "profile": summarize_company(body)}
    if status == 404:
        return {"ok": True, "match": False}
    return {"ok": False, "error": _pdl_error(status, body)}


def _pdl_error(status: int, body: dict) -> str:
    if status in (401, 403):
        return "People Data Labs rejected the API key"
    if status == 402:
        return "People Data Labs account is out of credits"
    if status == 429:
        return "People Data Labs rate limit reached; try again in a minute"
    message = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
    return "People Data Labs request failed (%s)%s" % (status or "network", ": %s" % message if message else "")


def _sql_text(value: str) -> str:
    """A value safe to place inside single quotes in a PDL SQL query."""
    return re.sub(r"[^a-z0-9 .&+/-]", "", (value or "").lower()).strip()[:80]


# The ranks People Data Labs gives a title (`job_title_levels`) that can say yes to a purchase. Its other ranks are
# senior (a senior analyst, a senior engineer), entry, training and unpaid.
PDL_DECIDERS = ("cxo", "owner", "partner", "vp", "director", "manager")


def pdl_people(key: str, *, company: str = "", domain: str = "", titles: Optional[list] = None, limit: int = 3,
               deciders: bool = False, http: Callable = http_json) -> dict:
    """People at one company whose title matches. Each person returned costs one PDL credit. With `deciders`, only
    people People Data Labs ranks manager or above, so a buyer lookup is not answered with an analyst."""
    company, domain = _sql_text(company), _sql_text(domain)
    if not (company or domain):
        return {"ok": False, "error": "need the company name or its website domain"}
    where = ["job_company_website='%s'" % domain if domain else "job_company_name='%s'" % company]
    wanted = [t for t in (_sql_text(t) for t in titles or []) if t][:6]
    if wanted:
        where.append("(" + " OR ".join("job_title LIKE '%%%s%%'" % t for t in wanted) + ")")
    if deciders:
        where.append("(" + " OR ".join("job_title_levels='%s'" % level for level in PDL_DECIDERS) + ")")
    sql = "SELECT * FROM person WHERE " + " AND ".join(where)
    status, body = http(PDL + "/person/search", headers={"X-Api-Key": key},
                        data={"sql": sql, "size": max(1, min(int(limit), 5)), "titlecase": True})
    if status == 200:
        people = [summarize_person(p) for p in body.get("data") or []]
        return {"ok": True, "people": people, "total_matching": body.get("total"), "credits_used": len(people)}
    if status == 404:
        return {"ok": True, "people": [], "total_matching": 0, "credits_used": 0}
    return {"ok": False, "error": _pdl_error(status, body)}


def exa_companies(key: str, query: str, limit: int = 8, http: Callable = http_json) -> dict:
    """Company pages matching a plain-language description, via Exa search."""
    status, body = http(EXA + "/search", headers={"x-api-key": key},
                        data={"query": query[:400], "numResults": max(1, min(int(limit), 15)), "category": "company",
                              "contents": {"text": {"maxCharacters": 500}}})
    if status != 200:
        problem = "Exa rejected the API key" if status in (401, 403) else "Exa search failed (%s)" % (status or "network")
        return {"ok": False, "error": problem}
    results = []
    for item in body.get("results") or []:
        url = item.get("url") or ""
        results.append({"title": (item.get("title") or "")[:120], "url": url, "domain": domain_of(url),
                        "text": " ".join((item.get("text") or "").split())[:500]})
    return {"ok": True, "results": results}


def domain_of(url: str) -> str:
    host = urllib.parse.urlparse(url if "//" in url else "//" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# -- Is a key any good? -------------------------------------------------------------------------
KEY_CHECK_EMAIL = "stoa-key-check@example.com"      # an address nobody has: the lookup cannot match


def check_key(service: str, key: str, http: Callable = http_json) -> dict:
    """One small request that shows whether a service accepts a key. Returns ``{"state", "status"}``.

    ``state`` is ``works``, ``rejected`` (the service refused the key), ``spent`` (accepted, but the
    account is out of credit) or ``unreachable`` (no answer, or one that says nothing about the key).

    - People Data Labs: a person lookup for an address nobody has. It bills a match only, so this is free.
    - Exa: one search for one result with no page text. Exa bills it like any other search.
    - Granola: the first page of the note list, one note long. Free.
    - Fireflies: asks for the id of one meeting. Free.
    """
    if service == "pdl":
        status, _ = http(PDL + "/person/enrich", headers={"X-Api-Key": key}, params={"email": KEY_CHECK_EMAIL, "min_likelihood": 10})
        accepted = status in (200, 404)             # 404 is "no such person", which only an accepted key is told
    elif service == "exa":
        status, _ = http(EXA + "/search", headers={"x-api-key": key}, data={"query": "Exa", "numResults": 1})
        accepted = status == 200
    elif service == "granola":
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        status, _ = http(GRANOLA + "/notes", headers={"Authorization": "Bearer " + key}, params={"updated_after": since, "page_size": 1})
        accepted = status == 200
    elif service == "fireflies":
        data, error = _fireflies(key, "query Check { transcripts(limit: 1) { id } }", {}, http)
        return {"state": "works" if not error else "rejected" if "rejected" in error else "unreachable", "status": 200 if not error else 0}
    else:
        return {"state": "unknown", "status": 0}
    state = "works" if accepted else "rejected" if status in (401, 403) else "spent" if status == 402 else "unreachable"
    return {"state": state, "status": status}


# -- Meeting notes ------------------------------------------------------------------------------
# A notes folder is anything that writes notes to disk: an Obsidian vault, a recorder's export (Plaud, Otter, Fathom),
# files dropped in by hand. A recorder often writes two files for one meeting, a transcript and a summary; they are
# read as one note, the summary first, so one meeting is filed once.
_PART = re.compile(r"[\s_\-·:|()\[\]]*\b(transcript|transcription|summary|summaries|notes?|minutes|mind\s?map|action items)\b[\s_\-·:|()\[\]]*$", re.I)
_DATE_IN_NAME = (re.compile(r"(?<!\d)(20\d\d)[-_. ]?(0[1-9]|1[0-2])[-_. ]?(0[1-9]|[12]\d|3[01])(?!\d)"),
                 re.compile(r"(?<!\d)(0?[1-9]|1[0-2])[-_.](0?[1-9]|[12]\d|3[01])[-_.](20\d\d)(?!\d)"))


def _walk_notes(folder: Path):
    """Every file in a notes folder that could be a note, a handed-over file or a recording: ``(path, stat)``.
    Hidden files and folders, and a few folders that never hold notes, are passed over."""
    seen = 0
    base = len(folder.parts)
    try:
        for root, dirs, files in os.walk(folder):
            depth = len(Path(root).parts) - base
            dirs[:] = [] if depth >= NOTE_DEPTH else sorted(d for d in dirs if not d.startswith(".") and d.lower() not in NOTE_SKIP_DIRS)
            for name in sorted(files):
                if name.startswith(".") or name.startswith("~$"):
                    continue
                seen += 1
                if seen > NOTE_FILES:
                    return
                path = Path(root) / name
                if path.suffix.lower() in NOTE_SUFFIXES + NOTE_HANDED + NOTE_AUDIO:
                    try:
                        yield path, path.stat()
                    except OSError:
                        continue
    except OSError:
        return


def count_notes(folder: Path) -> tuple:
    """How many notes ``folder_notes`` would find in a folder, and when the newest was written (a date, or "").
    Names and dates only: no note is opened."""
    census = folder_census(folder)
    return census["notes"], census["newest"]


def folder_census(folder: Path) -> dict:
    """What a folder holds, without opening anything: notes Stoa reads, files handed to the assistant (PDFs), recordings
    with no words, the kinds of note found, and the newest note's date."""
    kinds: dict = {}
    notes = handed = audio = 0
    newest = 0.0
    for path, stat in _walk_notes(folder):
        suffix = path.suffix.lower()
        if suffix in NOTE_AUDIO:
            audio += 1
            continue
        if suffix in NOTE_HANDED:
            handed += 1
        else:
            notes += 1
        kinds[suffix] = kinds.get(suffix, 0) + 1
        newest = max(newest, stat.st_mtime)
    return {"notes": notes + handed, "read": notes, "handed": handed, "audio": audio,
            "kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
            "newest": dt.date.fromtimestamp(newest).isoformat() if newest else ""}


def _meeting_key(path: Path) -> tuple:
    """Files that are parts of one meeting share this: the folder, and the name without "transcript", "summary" and the like."""
    stem = path.stem
    for _ in range(2):
        stem = _PART.sub("", stem)
    return str(path.parent), " ".join(re.split(r"[\s_\-]+", stem.lower())).strip() or path.stem.lower()


def _part_rank(path: Path) -> int:
    """Which of a meeting's files leads: a summary, then notes, then anything else, then the transcript."""
    name = path.stem.lower()
    if "summary" in name or "summaries" in name:
        return 0
    if "transcript" in name or path.suffix.lower() in (".srt", ".vtt"):
        return 3
    return 1 if "note" in name or "minute" in name else 2


def _name_date(name: str) -> str:
    for pattern in _DATE_IN_NAME:
        match = pattern.search(name)
        if match:
            parts = match.groups()
            year, month, day = (parts[0], parts[1], parts[2]) if len(parts[0]) == 4 else (parts[2], parts[0], parts[1])
            try:
                return dt.date(int(year), int(month), int(day)).isoformat()
            except ValueError:
                continue
    return ""


def folder_notes(folder: str, days: int = 7) -> list:
    """The notes in a folder (and up to three levels below) changed in the last ``days``, newest first, one per meeting.

    Works with anything that writes notes to disk: an Obsidian vault folder, a recorder's export, or files you drop
    in by hand. A meeting's transcript and summary, written as two files, come back as one note.
    """
    root = Path(folder).expanduser() if folder else None
    if root is None or not root.is_dir():
        return []
    cutoff = time.time() - days * 86400
    meetings: dict = {}
    for path, stat in _walk_notes(root):
        if path.suffix.lower() in NOTE_AUDIO:
            continue
        meetings.setdefault(_meeting_key(path), []).append((path, stat))
    out = []
    for parts in meetings.values():
        if max(stat.st_mtime for _, stat in parts) < cutoff:
            continue
        parts.sort(key=lambda item: (_part_rank(item[0]), item[0].name))
        path, stat = parts[0]
        # A note is its meeting: the folder it is in and its name, without "transcript" or "summary". So it is the same
        # note when a file is edited, saved again by a sync tool, or joined by its other half, and it is filed once.
        # ``was`` holds the ids earlier versions gave its files (the file's path, size and time), so a note filed before
        # this version is still known as filed.
        ident = hashlib.sha1(("%s|%s" % _meeting_key(path)).encode()).hexdigest()[:12]
        was = ["file:" + hashlib.sha1(("%s|%d|%d" % (p, st.st_size, int(st.st_mtime))).encode()).hexdigest()[:12] for p, st in parts]
        head = _front_matter(path)
        when = head.get("date") or _name_date(path.stem)
        stamp = dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"id": "file:" + ident, "source": "folder", "path": str(path), "paths": [str(p) for p, _ in parts],
                    "title": head.get("title") or _file_title(path, len(parts) > 1),
                    "when": (when + "T12:00:00Z") if when else stamp, "changed": stamp,
                    "attendees": head.get("attendees") or [], "was": was,
                    "handed": all(p.suffix.lower() in NOTE_HANDED for p, _ in parts)})
    return sorted(out, key=lambda n: n["changed"], reverse=True)


def _front_matter(path: Path) -> dict:
    """What a Markdown note says about itself at its head (``title:``, ``date:``, ``attendees:``), where it has such a head."""
    if path.suffix.lower() not in (".md", ".markdown", ".txt"):
        return {}
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            if handle.readline().strip() != "---":
                return {}
            lines = []
            for _ in range(40):
                line = handle.readline()
                if not line or line.strip() == "---":
                    break
                lines.append(line.rstrip("\n"))
    except OSError:
        return {}
    out: dict = {}
    key, items = "", []
    def keep():
        if key in ("attendees", "participants", "people", "with") and items:
            out["attendees"] = [_person(item) for item in items if item.strip()][:20]
    for line in lines:
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and key:
            items.append(item.group(1).strip().strip("\"'"))
            continue
        pair = re.match(r"^([A-Za-z_ ]+):\s*(.*)$", line)
        if not pair:
            continue
        keep()
        key, value, items = pair.group(1).strip().lower(), pair.group(2).strip().strip("\"'"), []
        if key == "title" and value:
            out["title"] = value[:120]
        elif key in ("date", "created", "meeting date") and value:
            found = _name_date(value)
            if found:
                out["date"] = found
        elif key in ("attendees", "participants", "people", "with") and value:
            items = [part for part in re.split(r"[,;]", value.strip("[]")) if part.strip()]
    keep()
    return out


def _person(text: str) -> dict:
    """``Dana Whitfield <dana@northwind.com>``, a bare address, or a bare name, as ``{name, email}``."""
    text = text.strip().strip("\"'[]")
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    name = re.sub(r"<[^>]*>|\([^)]*\)|[\w.+-]+@[\w-]+\.[\w.-]+", "", text).strip(" ,-")
    return {"name": name[:80], "email": email.group(0).lower() if email else ""}


def _file_title(path: Path, grouped: bool = False) -> str:
    if path.suffix.lower() in (".md", ".markdown", ".txt"):
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for _ in range(15):
                    line = handle.readline()
                    if not line:
                        break
                    if line.startswith("#"):
                        return line.lstrip("#").strip()[:120]
                    match = re.match(r"^title:\s*(.+)$", line, re.I)
                    if match:
                        return match.group(1).strip().strip("\"'")[:120]
        except OSError:
            pass
    stem = path.stem
    if grouped:
        stem = _PART.sub("", stem) or path.stem
    for pattern in _DATE_IN_NAME:      # the date is shown beside the title, so it is not said twice
        dated = pattern.sub(" ", stem, count=1)
        if dated != stem and re.search(r"[A-Za-z]{2}", dated):
            stem = dated
            break
    return " ".join(stem.replace("_", " ").replace("-", " ").split()).strip(" .,:")[:120] or path.stem[:120]


def _plain(path: Path, limit: int = NOTE_CHARS) -> str:
    """One file's words. A Word file's text is taken from its XML; a subtitle file loses its numbers and times; a web
    page and a rich-text file lose their markup. Anything else is read as it is."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("word/document.xml").decode("utf-8", "replace")
        raw = re.sub(r"</w:p>", "\n", raw)
        raw = re.sub(r"<w:tab[^>]*/>", "\t", re.sub(r"<w:br[^>]*/>", "\n", raw))
        return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()[:limit]
    text = path.read_text(encoding="utf-8", errors="replace")
    if suffix in (".srt", ".vtt"):
        kept = [line.strip() for line in text.splitlines()
                if line.strip() and not line.strip().isdigit() and "-->" not in line and not line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:"))]
        said = []
        for line in kept:      # a cue repeated across overlapping captions is said once
            line = re.sub(r"<[^>]+>", "", line)
            if not said or said[-1] != line:
                said.append(line)
        return "\n".join(said)[:limit]
    if suffix in (".html", ".htm"):
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", text)
        return re.sub(r"\n\s*\n\s*\n+", "\n\n", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()[:limit]
    if suffix == ".rtf":
        text = re.sub(r"\\par[d]?\b", "\n", text)
        text = re.sub(r"\{\\\*[^{}]*\}|\\'[0-9a-fA-F]{2}|\\[a-zA-Z]+-?\d* ?|[{}]", "", text)
        return text.strip()[:limit]
    return text[:limit]


def read_folder_note(note: dict, limit: int = NOTE_CHARS) -> str:
    """A note's words: every file of the meeting, the summary first, each under its file name when there are several."""
    paths = [Path(p) for p in note.get("paths") or [note.get("path")] if p]
    out, room = [], limit
    for path in paths:
        if room <= 200:
            break
        if path.suffix.lower() in NOTE_HANDED:
            body = "(%s is a PDF, which Stoa does not read itself. Read it with your own file tool at %s.)" % (path.name, path)
        else:
            try:
                body = _plain(path, room)
            except (OSError, KeyError, zipfile.BadZipFile) as exc:
                body = "(could not read %s: %s)" % (path.name, exc)
        out.append(("## %s\n" % path.name if len(paths) > 1 else "") + body)
        room -= len(body)
    return "\n\n".join(out)[:limit]


def note_print(note: dict) -> str:
    """A mark of what a folder note says (its leading file's bytes), as ``same:<hash>``. A note that was filed and is
    then renamed or moved to another folder has the same mark, so it is known as the note already filed. "" when the
    file cannot be read."""
    try:
        with open(note.get("path") or "", "rb") as handle:
            return "same:" + hashlib.sha1(handle.read(2_000_000)).hexdigest()[:16]
    except OSError:
        return ""


def note_head(note: dict, limit: int = 8000) -> str:
    """The opening of a folder note, for working out who it is about. "" for a file Stoa does not read."""
    if note.get("handed"):
        return ""
    try:
        return read_folder_note(note, limit)
    except Exception:      # a note that cannot be opened is still listed; it simply matches on its title alone
        return ""


def granola_notes(key: str, days: int = 7, http: Callable = http_json, limit: int = 60) -> tuple:
    """Recent Granola notes as ``(notes, error)``. Summaries only; transcripts are never fetched."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, cursor = [], None
    while len(out) < limit:
        status, page = http(GRANOLA + "/notes", headers={"Authorization": "Bearer " + key},
                            params={"updated_after": since, "page_size": 30, "cursor": cursor})
        if status != 200:
            return out, ("Granola rejected the API key" if status in (401, 403) else "Granola request failed (%s)" % (status or "network"))
        for note in page.get("notes") or []:
            out.append(_granola_brief(note))
        cursor = page.get("cursor")
        if not page.get("hasMore") or not cursor:
            break
    return sorted(out, key=lambda n: n["when"], reverse=True)[:limit], ""


def _granola_people(note: dict) -> list:
    event = note.get("calendar_event") or {}
    people = {}
    for person in (note.get("attendees") or []) + (event.get("invitees") or []):
        email = (person.get("email") or "").lower()
        if email:
            people[email] = person.get("name") or people.get(email) or ""
    return [{"email": email, "name": name} for email, name in sorted(people.items())]


def _granola_brief(note: dict) -> dict:
    event = note.get("calendar_event") or {}
    return {"id": "granola:" + str(note.get("id")), "source": "granola", "title": note.get("title") or "(untitled)",
            "when": event.get("scheduled_start_time") or note.get("created_at") or "",
            "attendees": _granola_people(note), "url": note.get("web_url") or ""}


def read_granola_note(key: str, note_id: str, http: Callable = http_json) -> dict:
    raw_id = note_id.split(":", 1)[-1]
    status, note = http(GRANOLA + "/notes/" + urllib.parse.quote(raw_id, safe=""), headers={"Authorization": "Bearer " + key})
    if status != 200:
        return {"ok": False, "error": "Granola note not available (%s)" % (status or "network")}
    brief = _granola_brief(note)
    parts = [note.get("summary_markdown") or note.get("summary_text") or "(no summary yet)"]
    private = note.get("private_notes_markdown") or note.get("private_notes_text")
    if private:
        parts.append("## My notes\n" + private)
    return dict(brief, ok=True, text="\n\n".join(parts)[:NOTE_CHARS])


# -- Fireflies ------------------------------------------------------------------------------------
# One GraphQL endpoint (docs.fireflies.ai). Stoa asks for a meeting's title, date, attendees and Fireflies's own
# summary and action items. It never asks for the sentences (the transcript), the audio or the video.
_FIREFLIES_BRIEF = "id title date dateString organizer_email participants transcript_url meeting_attendees { displayName name email }"
_FIREFLIES_SUMMARY = "summary { overview short_summary action_items shorthand_bullet keywords }"


def _fireflies(key: str, query: str, variables: dict, http: Callable) -> tuple:
    """``(data, error)`` for one GraphQL request."""
    status, body = http(FIREFLIES, headers={"Authorization": "Bearer " + key}, data={"query": query, "variables": variables})
    if status in (401, 403):
        return {}, "Fireflies rejected the API key"
    errors = body.get("errors") if isinstance(body, dict) else None
    if status != 200 or errors:
        first = (errors or [{}])[0] if isinstance(errors, list) else {}
        code = str((first.get("extensions") or {}).get("code") or first.get("code") or "")
        if code in ("auth_failed", "invalid_arguments_api_key", "forbidden") or "api key" in str(first.get("message") or "").lower():
            return {}, "Fireflies rejected the API key"
        return {}, "Fireflies request failed (%s)" % (str(first.get("message") or "")[:120] or status or "network")
    return (body.get("data") or {}), ""


def _fireflies_brief(row: dict) -> dict:
    people = {}
    for person in row.get("meeting_attendees") or []:
        email = str(person.get("email") or "").lower()
        if email:
            people[email] = person.get("displayName") or person.get("name") or people.get(email) or ""
    for email in row.get("participants") or []:
        if email and "@" in str(email):
            people.setdefault(str(email).lower(), "")
    when = str(row.get("dateString") or "")
    if not when and row.get("date"):
        try:
            when = dt.datetime.fromtimestamp(float(row["date"]) / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError, OverflowError, OSError):
            when = ""
    return {"id": "fireflies:" + str(row.get("id")), "source": "fireflies", "title": row.get("title") or "(untitled)", "when": when,
            "attendees": [{"email": email, "name": name} for email, name in sorted(people.items())], "url": row.get("transcript_url") or ""}


def fireflies_notes(key: str, days: int = 7, http: Callable = http_json, limit: int = 50) -> tuple:
    """Recent Fireflies meetings as ``(notes, error)``: titles, dates and attendees only."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    data, error = _fireflies(key, "query Recent($fromDate: DateTime, $limit: Int) { transcripts(fromDate: $fromDate, limit: $limit) { %s } }" % _FIREFLIES_BRIEF,
                             {"fromDate": since, "limit": max(1, min(int(limit), 50))}, http)
    if error:
        return [], error
    return sorted((_fireflies_brief(row) for row in data.get("transcripts") or []), key=lambda n: n["when"], reverse=True), ""


def read_fireflies_note(key: str, note_id: str, http: Callable = http_json) -> dict:
    raw_id = note_id.split(":", 1)[-1]
    data, error = _fireflies(key, "query One($id: String!) { transcript(id: $id) { %s %s } }" % (_FIREFLIES_BRIEF, _FIREFLIES_SUMMARY), {"id": raw_id}, http)
    row = data.get("transcript") if not error else None
    if not row:
        return {"ok": False, "error": error or "Fireflies note not available"}
    summary = row.get("summary") or {}
    parts = [str(summary.get("overview") or summary.get("short_summary") or "").strip() or "(no summary yet)"]
    for title, field in (("Action items", "action_items"), ("Notes", "shorthand_bullet")):
        value = summary.get(field)
        value = "\n".join(str(v) for v in value) if isinstance(value, list) else str(value or "")
        if value.strip():
            parts.append("## %s\n%s" % (title, value.strip()))
    return dict(_fireflies_brief(row), ok=True, text="\n\n".join(parts)[:NOTE_CHARS])
