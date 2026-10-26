#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""DbMailBackup: email a readable copy of SQLite databases, every day.

Each database becomes one JSON file (plain text: its table definitions and
every row), checked by rebuilding a database from it and comparing every
table, then zipped with a password and emailed through Brevo.  See README.md.

    uv run dbmailbackup.py send SETTINGS.json            # make, check and email the backup
    uv run dbmailbackup.py send SETTINGS.json --to DIR   # the same, but save the zip in DIR instead
    uv run dbmailbackup.py restore BACKUP.zip|BACKUP.json OUT_DIR [--password P]

Or from a long-running server, which is how projects use it: call
`start(settings_file, brevo_key=...)` once at startup.  A background thread
then runs `daily` in a separate process every day at the settings'
"send-at-utc" time (and at startup, when today's was missed because the
server was down): the databases, then any new or changed files in the
settings' "folders" (photos, say), in as many emails as their size needs.

Only the standard library: it runs on any machine that has uv.  The Brevo
key is read with the machine's credential store tool (~/creds/creds.py, or
$CREDS_DIR/creds.py).

Canonical copy: github.com/freeideas/DbMailBackup.  Projects carry a copy in
their scripts/ folder; change it there, then copy it back out.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fnmatch
import hashlib
import json
import math
import os
import pathlib
import secrets
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib

FORMAT = "sqlite-json-backup/1"
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


# --- the database as JSON ---------------------------------------------------------

def _value(v):
    """A cell as JSON: bytes and the floats JSON cannot hold become small objects."""
    if isinstance(v, bytes):
        return {"$base64": base64.b64encode(v).decode("ascii")}
    if isinstance(v, float) and not math.isfinite(v):
        return {"$float": repr(v)}
    return v


def _cell(v):
    """The reverse of _value."""
    if isinstance(v, dict):
        if "$base64" in v:
            return base64.b64decode(v["$base64"])
        if "$float" in v:
            return float(v["$float"])
    return v


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _plan(con, skip: list[str]) -> tuple[list[dict], list[dict]]:
    """Every schema object in creation order, and the tables whose rows to copy.

    Virtual tables keep their definition; their rows are copied through the
    table itself when SQLite can read it, and their internal helper tables
    ("shadow" tables, named after them) are never copied, since inserting the
    rows rebuilds them.  sqlite_sequence (AUTOINCREMENT's counters) is copied
    so numbering carries on where it left off.
    """
    objects = [dict(type=t, name=n, table=tb, sql=s) for t, n, tb, s in con.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid")]
    virtual = [o["name"] for o in objects if o["type"] == "table"
               and o["sql"].lstrip().upper().startswith("CREATE VIRTUAL TABLE")]
    tables = []
    for o in objects:
        if o["type"] != "table":
            continue
        name = o["name"]
        o["virtual"] = name in virtual
        if any(name != v and name.startswith(v + "_") for v in virtual):
            o["note"] = "internal part of a virtual table; rebuilt from it"
        elif any(fnmatch.fnmatch(name, pattern) for pattern in skip):
            o["note"] = "left out by the settings"
        elif not name.startswith("sqlite_"):
            tables.append(o)
    gone = {o["name"] for o in objects if o.get("note")}
    for o in objects:
        if o["type"] != "table" and o["table"] in gone:
            o["note"] = f"belongs to {o['table']}, which is not copied"
    seq = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone()
    if seq:
        tables.append(dict(type="table", name="sqlite_sequence", table="sqlite_sequence", sql=None, virtual=False))
    return objects, tables


def _columns(con, table: str) -> list[str]:
    """The columns a row can be written to, with `rowid` first when the table keeps one of its own.

    Generated columns are computed, not stored, and a virtual table's hidden
    columns (full-text search's `rank` and the one named after the table) are
    not data, so neither is copied.  The rowid is copied whenever no column
    stands for it (an INTEGER PRIMARY KEY does), because other tables may
    point at it: a full-text index is usually joined to its rows by rowid.
    """
    info = list(con.execute(f"PRAGMA table_xinfo({_q(table)})"))
    cols = [r[1] for r in info if r[6] == 0] or [r[1] for r in con.execute(
        f"PRAGMA table_info({_q(table)})")]
    if {c.lower() for c in cols} & {"rowid", "oid", "_rowid_"}:
        return cols                              # a real column has the name; rowid cannot be reached
    pk = [r for r in info if r[5]]
    if len(pk) == 1 and (pk[0][2] or "").upper() == "INTEGER":
        return cols                              # that column is the rowid
    try:
        con.execute(f"SELECT rowid FROM {_q(table)} LIMIT 0")
    except sqlite3.Error:
        return cols                              # WITHOUT ROWID
    return ["rowid"] + cols


def _rows(con, table: str, cols: list[str]):
    return con.execute(f"SELECT {', '.join(_q(c) for c in cols)} FROM {_q(table)}")


def _fingerprint(rows) -> tuple[int, str]:
    """Row count and a hash of the rows that does not depend on their order."""
    digests = sorted(hashlib.sha256(json.dumps([_value(v) for v in r], ensure_ascii=False,
                                               default=str).encode()).digest() for r in rows)
    return len(digests), hashlib.sha256(b"".join(digests)).hexdigest()


def export_json(db: pathlib.Path, skip: list[str], out: pathlib.Path) -> dict:
    """Write the database as JSON, one row per line; return what went in."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    objects, tables = _plan(con, skip)
    summary = {"tables": {}, "left_out": {}, "unreadable": {}}
    for o in objects:
        if o.get("note") and o["type"] == "table":
            summary["left_out"][o["name"]] = o["note"]
    with out.open("w", encoding="utf-8") as f:
        head = {"format": FORMAT, "database": db.name,
                "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "sqlite_version": sqlite3.sqlite_version,
                "how_to_restore": "uv run dbmailbackup.py restore THIS_FILE OUT_DIR "
                                  "(github.com/freeideas/DbMailBackup), or read it: it is plain JSON.",
                "schema": [{k: o[k] for k in ("type", "name", "table", "sql")} for o in objects
                           if not o.get("note") and not o["name"].startswith("sqlite_")],
                "left_out": summary["left_out"]}
        f.write(json.dumps(head, ensure_ascii=False, indent=1)[:-2] + ',\n "tables": {')
        first = True
        for t in tables:
            try:
                cols = _columns(con, t["name"])
                rows = _rows(con, t["name"], cols)
            except sqlite3.Error as e:   # a virtual table whose extension this SQLite lacks
                summary["unreadable"][t["name"]] = str(e)
                continue
            f.write(("" if first else ",") + f"\n  {json.dumps(t['name'])}: {{\"columns\": "
                    f"{json.dumps(cols, ensure_ascii=False)}, \"rows\": [")
            first = False
            n = 0
            for r in rows:
                f.write(("\n   " if n == 0 else ",\n   ")
                        + json.dumps([_value(v) for v in r], ensure_ascii=False, default=str))
                n += 1
            f.write("\n  ]}" if n else "]}")
            summary["tables"][t["name"]] = n
        f.write("\n }\n}\n")
    con.close()
    return summary


def restore_json(src: pathlib.Path, db: pathlib.Path, expected: set[str] = frozenset()) -> list[str]:
    """Build a database from a backup JSON file.  Returns notes on anything it could not do.

    `expected` names tables already reported as unreadable when the file was
    made (a virtual table whose extension this SQLite lacks): that they cannot
    be made here either is no news, so it is not a note.
    """
    data = json.loads(src.read_text(encoding="utf-8"))
    if "sqlite_sequence" in data.get("tables", {}):            # last, after the rows that refill it
        data["tables"]["sqlite_sequence"] = data["tables"].pop("sqlite_sequence")
    if data.get("format") != FORMAT:
        raise SystemExit(f"{src} is not a {FORMAT} file")
    if db.exists():
        raise SystemExit(f"{db} already exists; restore into a new name")
    notes: list[str] = []
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys = OFF")
    schema = data["schema"]
    made = set()
    # tables first, then their rows, then indexes, views and triggers, so no
    # trigger fires while the rows go back in
    for o in schema:
        if o["type"] == "table":
            try:
                con.execute(o["sql"])
                made.add(o["name"])
            except sqlite3.Error as e:
                if o["name"] not in expected:
                    held = ("its rows are in the file" if o["name"] in data["tables"]
                            else "the file holds no rows for it")
                    notes.append(f"table {o['name']} could not be made here ({e}); {held}")
    for name, t in data["tables"].items():
        if name == "sqlite_sequence":
            con.execute("DELETE FROM sqlite_sequence")   # refilled by the inserts above; the originals win
        elif name not in made:
            continue
        cols = t["columns"]
        sql = f"INSERT INTO {_q(name)} ({', '.join(_q(c) for c in cols)}) VALUES ({', '.join('?' * len(cols))})"
        con.executemany(sql, ([_cell(v) for v in r] for r in t["rows"]))
    for o in schema:
        if o["type"] != "table":
            try:
                con.execute(o["sql"])
            except sqlite3.Error as e:
                if o["table"] not in expected:
                    notes.append(f"{o['type']} {o['name']} could not be made here ({e})")
    con.commit()
    con.close()
    return notes


def check(original: pathlib.Path, rebuilt: pathlib.Path, tables: dict) -> list[str]:
    """Every copied table: same rows in both.  Returns the differences (none is good)."""
    a = sqlite3.connect(f"file:{original}?mode=ro", uri=True)
    b = sqlite3.connect(f"file:{rebuilt}?mode=ro", uri=True)
    bad = []
    for name in tables:
        cols = _columns(a, name)
        try:
            if _fingerprint(_rows(a, name, cols)) != _fingerprint(_rows(b, name, cols)):
                bad.append(name)
        except sqlite3.Error as e:
            bad.append(f"{name} ({e})")
    a.close()
    b.close()
    return bad


# --- a password zip (the classic kind every computer opens) ------------------------

_CRC = [0] * 256
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if _c & 1 else _c >> 1
    _CRC[_i] = _c


class _ZipCrypto:
    """PKWARE's original zip encryption (APPNOTE 6.1): weak, but built into
    Windows, macOS and Linux unzip tools."""

    def __init__(self, password: bytes):
        self.k0, self.k1, self.k2 = 0x12345678, 0x23456789, 0x34567890
        for c in password:
            self._update(c)

    def _update(self, c: int) -> None:
        self.k0 = _CRC[(self.k0 ^ c) & 0xFF] ^ (self.k0 >> 8)
        self.k1 = ((self.k1 + (self.k0 & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
        self.k2 = _CRC[(self.k2 ^ (self.k1 >> 24)) & 0xFF] ^ (self.k2 >> 8)

    def encrypt(self, data: bytes) -> bytes:
        out = bytearray(len(data))
        k0, k1, k2, crc = self.k0, self.k1, self.k2, _CRC
        for i, b in enumerate(data):
            t = (k2 | 2) & 0xFFFF
            out[i] = b ^ (((t * (t ^ 1)) >> 8) & 0xFF)
            k0 = crc[(k0 ^ b) & 0xFF] ^ (k0 >> 8)
            k1 = ((k1 + (k0 & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
            k2 = crc[(k2 ^ (k1 >> 24)) & 0xFF] ^ (k2 >> 8)
        self.k0, self.k1, self.k2 = k0, k1, k2
        return bytes(out)


def write_zip(out: pathlib.Path, files: list, password: str) -> None:
    """A zip of `files` (paths, or (path, name in the zip) pairs), each deflated,
    then encrypted with `password`."""
    now = time.localtime()
    dos_time = (now.tm_hour << 11) | (now.tm_min << 5) | (now.tm_sec // 2)
    dos_date = ((now.tm_year - 1980) << 9) | (now.tm_mon << 5) | now.tm_mday
    central, offset = [], 0
    with out.open("wb") as z:
        for item in files:
            path, arcname = item if isinstance(item, tuple) else (item, item.name)
            raw = path.read_bytes()
            crc = zlib.crc32(raw) & 0xFFFFFFFF
            comp = zlib.compressobj(9, zlib.DEFLATED, -15)
            packed = comp.compress(raw) + comp.flush()
            cipher = _ZipCrypto(password.encode("utf-8"))
            header = bytearray(secrets.token_bytes(11)) + bytes([crc >> 24])
            body = cipher.encrypt(bytes(header)) + cipher.encrypt(packed)
            name = arcname.encode("utf-8")
            flags = 0x0001 | 0x0800                     # encrypted; name in UTF-8
            local = struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, 8, dos_time, dos_date,
                                crc, len(body), len(raw), len(name), 0)
            z.write(local + name + body)
            central.append(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flags, 8, dos_time,
                                       dos_date, crc, len(body), len(raw), len(name), 0, 0, 0, 0,
                                       0o100644 << 16, offset) + name)
            offset += len(local) + len(name) + len(body)
        cd = b"".join(central)
        z.write(cd + struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(files), len(files),
                                 len(cd), offset, 0))


# --- sending ------------------------------------------------------------------------

def creds_get(path: str) -> str:
    """A secret from the machine's credential store, through its own tool."""
    folder = pathlib.Path(os.environ.get("CREDS_DIR") or pathlib.Path.home() / "creds").expanduser()
    tool = folder / "creds.py"
    if not tool.is_file():
        raise SystemExit(f"no credential store tool at {tool}")
    r = subprocess.run(["uv", "run", "--quiet", str(tool), "get", path], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise SystemExit(f"could not read {path} from the credential store")
    return r.stdout.strip()


def send_mail(key: str, settings: dict, subject: str, text: str, attachment: pathlib.Path | None) -> None:
    payload = {"sender": {"email": settings["from-email"], "name": settings.get("from-name", "Backups")},
               "to": [{"email": e} for e in settings["recipients"]],
               "subject": subject, "textContent": text}
    if attachment:
        payload["attachment"] = [{"name": attachment.name,
                                  "content": base64.b64encode(attachment.read_bytes()).decode("ascii")}]
    req = urllib.request.Request(BREVO_URL, json.dumps(payload).encode(), {
        "api-key": key, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Brevo refused the email: {e.code} {e.read()[:300]!r}") from None


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 100_000 else f"{max(1, round(n / 1000))} KB"


def send(settings_file: pathlib.Path, save_to: pathlib.Path | None = None, brevo_key: str | None = None,
         say=print) -> int:
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    base = settings_file.resolve().parent
    project = settings["project"]
    limit = float(settings.get("max-email-mb", 9)) * 1e6
    today = dt.date.today().isoformat()
    lines, problems, files = [], [], []
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = pathlib.Path(tmp_name)
        found = []
        for entry in settings["databases"]:
            if any(c in entry["file"] for c in "*?["):   # a pattern: every database it matches, new ones too
                matches = sorted(p for p in base.glob(entry["file"]) if p.is_file())
                if not matches:
                    lines.append(f"{entry['file']}: no databases match yet.")
                found.extend((p.resolve(), {**entry, "name": None}) for p in matches)
                continue
            db = (base / entry["file"]).resolve()
            if not db.is_file():
                problems.append(f"{entry['file']}: not found")
                continue
            found.append((db, entry))
        labels = set()
        for db, entry in found:
            # a name for its JSON file: two databases called data.sqlite3 are told
            # apart by their folders
            label, up = entry.get("name") or db.stem, db.parent
            while label in labels and up != up.parent:
                label, up = f"{up.name}-{label}", up.parent
            labels.add(label)
            snap = tmp / f"{label}.sqlite3"           # a consistent copy, even while the app writes
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as src, sqlite3.connect(snap) as dst:
                src.backup(dst)
            out = tmp / f"{label}-{today}.json"
            summary = export_json(snap, entry.get("skip-tables", []), out)
            rebuilt = tmp / f"rebuilt-{label}.sqlite3"
            notes = restore_json(out, rebuilt, set(summary["unreadable"]))
            bad = check(snap, rebuilt, summary["tables"])
            rows = sum(summary["tables"].values())
            verdict = ("restore test passed: every row matches" if not bad and not notes
                       else f"RESTORE TEST FAILED for {', '.join(bad) or 'part of it'}")
            lines.append(f"{db.name}: {len(summary['tables'])} tables, {rows:,} rows, "
                         f"{_mb(db.stat().st_size)} on disk; {verdict}.")
            for name, n in summary["tables"].items():
                lines.append(f"    {name}: {n:,} row{'' if n == 1 else 's'}")
            for name, why in summary["left_out"].items():
                lines.append(f"    {name}: not copied ({why})")
            for name, why in summary["unreadable"].items():
                lines.append(f"    {name}: not copied (this machine's SQLite cannot read it: {why})")
            lines.extend(f"    note: {n}" for n in notes)
            if bad or notes:
                problems.append(f"{db.name}: restore test failed")
            files.append(out)
        name = f"{''.join(c for c in project if c.isalnum()) or 'backup'}-{today}.zip"
        zipped = tmp / name
        # the databases' content, apart from the time each JSON file was made
        content = hashlib.sha256()
        for f in files:
            content.update(f.name.rsplit("-", 3)[0].encode())
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.startswith(' "made": '):
                    content.update(line.encode())
        fingerprint = content.hexdigest()
        last_file = _last_file(settings_file)
        try:
            last = json.loads(last_file.read_text(encoding="utf-8")) if last_file.is_file() else {}
        except ValueError:
            last = {}
        unchanged = bool(files) and not problems and not save_to and last.get("fingerprint") == fingerprint
        if files:
            write_zip(zipped, files, settings["zip-password"])
        size = zipped.stat().st_size if files else 0
        too_big = size > limit
        if too_big:
            problems.append(f"the backup is {_mb(size)}, more than the {_mb(int(limit))} an email can carry")
        elif size > limit * 2 / 3:
            lines.append(f"Warning: the backup is {_mb(size)}, two thirds of the way to the "
                         f"{_mb(int(limit))} an email can carry. Time to send only the changes.")
        subject = (f"{project} database backup {today}"
                   + (": PROBLEM, please read" if problems else ": no change" if unchanged else ""))
        text = "\n".join([
            f"No change since the backup sent {last.get('sent')}: the zip attached to that email "
            f"({last.get('zip')}) is still the latest. Today's restore test passed as well.",
            "",
            *lines,
        ]) if unchanged else "\n".join([
            f"The {project} databases as of {today}.",
            "",
            *(["PROBLEMS:", *(f"  - {p}" for p in problems), ""] if problems else []),
            *lines,
            "",
            (f"Attached: {name} ({_mb(size)}). Open it with a double-click and the backup password. "
             "Inside is one .json file per database: plain text, every table and every row, readable in "
             "a web browser or text editor." if files and not too_big else "Nothing is attached."),
            "",
            "Each day the file is checked by building a fresh database from it and comparing every "
            "table, row by row, with the real one. To rebuild a database from it: "
            "uv run dbmailbackup.py restore FILE.json OUT_FOLDER (github.com/freeideas/DbMailBackup).",
        ])
        if save_to:
            save_to.mkdir(parents=True, exist_ok=True)
            if files:
                (save_to / name).write_bytes(zipped.read_bytes())
            (save_to / f"{name}.txt").write_text(f"{subject}\n\n{text}\n", encoding="utf-8")
            say(f"saved {save_to / name} and its email text; nothing sent")
        else:
            key = brevo_key or creds_get(settings.get("brevo-key", "passwords/brevo-api-key"))
            attach = files and not too_big and not unchanged
            send_mail(key, settings, subject, text, zipped if attach else None)
            if attach:
                last_file.write_text(json.dumps({
                    "fingerprint": fingerprint, "zip": name,
                    "sent": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}), encoding="utf-8")
            say((f"sent {name} ({_mb(size)})" if attach else "sent a no-change note" if unchanged
                 else "sent a note with nothing attached") + f" to {', '.join(settings['recipients'])}"
                + (f"; problems: {'; '.join(problems)}" if problems else ""))
        if save_to:
            say(text)
    return 1 if problems else 0


# --- files beside the database (photos, uploads) -----------------------------------

def _files_state_file(settings_file: pathlib.Path) -> pathlib.Path:
    return _state_file(settings_file).with_name(_state_file(settings_file).name.replace(
        ".backup-sent", ".backup-files.json"))


def _load_files_state(settings_file: pathlib.Path) -> dict:
    f = _files_state_file(settings_file)
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {"day": "", "sent": {}}
    except ValueError:
        return {"day": "", "sent": {}}


def send_files(settings_file: pathlib.Path, save_to: pathlib.Path | None = None, brevo_key: str | None = None,
               say=print) -> int:
    """Email the files that are new or changed since they were last sent, in as many
    emails as their size needs.  Each file counts as sent once its email went."""
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    folders = settings.get("folders") or []
    if not folders:
        return 0
    base = settings_file.resolve().parent
    project = settings["project"]
    limit = float(settings.get("max-email-mb", 9)) * 1e6
    room = limit * 0.75                      # an email's attachment is sent as base64, a third bigger
    today = dt.date.today().isoformat()
    state = _load_files_state(settings_file)
    pending, too_big = [], []
    for folder in folders:
        root = (base / folder["path"]).resolve()
        label = folder.get("name") or root.name
        patterns = folder.get("include") or ["*"]
        if not root.is_dir():
            too_big.append(f"{folder['path']}: not found")
            continue
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root).as_posix()
            if not p.is_file() or not any(fnmatch.fnmatch(rel, pat) for pat in patterns):
                continue
            st = p.stat()
            sig = [st.st_size, int(st.st_mtime)]
            key = f"{label}/{rel}"
            if state["sent"].get(key) == sig:
                continue
            if st.st_size > room:
                too_big.append(f"{key} ({_mb(st.st_size)})")
                continue
            pending.append((p, key, sig, st.st_size))
    batches, batch, size = [], [], 0
    for item in pending:
        if batch and size + item[3] > room:
            batches.append(batch)
            batch, size = [], 0
        batch.append(item)
        size += item[3]
    if batch:
        batches.append(batch)
    key = None
    failures = 0
    with tempfile.TemporaryDirectory() as tmp_name:
        for n, batch in enumerate(batches, 1):
            name = f"{''.join(c for c in project if c.isalnum()) or 'backup'}-files-{today}-{n}.zip"
            zipped = pathlib.Path(tmp_name) / name
            write_zip(zipped, [(p, k) for p, k, _, _ in batch], settings["zip-password"])
            part = f" (part {n} of {len(batches)})" if len(batches) > 1 else ""
            subject = f"{project} files backup {today}{part}"
            text = "\n".join([
                f"{len(batch)} file{'' if len(batch) == 1 else 's'} of {project} that are new or changed "
                f"since they were last sent{part}:", "",
                *(f"  {k} ({_mb(s)})" for _, k, _, s in batch), "",
                f"Attached: {name} ({_mb(zipped.stat().st_size)}). Open it with a double-click and the "
                "backup password; the files keep their folders.",
                *(["", "Too big to email, so not sent:", *(f"  {t}" for t in too_big)] if too_big and n == 1 else []),
            ])
            try:
                if save_to:
                    save_to.mkdir(parents=True, exist_ok=True)
                    (save_to / name).write_bytes(zipped.read_bytes())
                    (save_to / f"{name}.txt").write_text(f"{subject}\n\n{text}\n", encoding="utf-8")
                else:
                    key = key or brevo_key or creds_get(settings.get("brevo-key", "passwords/brevo-api-key"))
                    send_mail(key, settings, subject, text, zipped)
            except SystemExit as e:              # a refused email: these files wait for the next day
                say(f"files backup part {n} not sent: {e}")
                failures += 1
                continue
            if not save_to:                      # a trial run into a folder sends nothing, so records nothing
                for _, k, sig, _ in batch:
                    state["sent"][k] = sig
                _files_state_file(settings_file).write_text(json.dumps(state, indent=0), encoding="utf-8")
            say(f"{'saved' if save_to else 'sent'} {name}: {len(batch)} files, {_mb(zipped.stat().st_size)}")
    if too_big:
        say(f"files too big to email: {', '.join(too_big)}")
    return 1 if failures or too_big else 0


def files_due(settings_file: pathlib.Path, now: dt.datetime | None = None) -> bool:
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    if not settings.get("folders"):
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    hour, minute = (int(x) for x in str(settings.get("send-at-utc", "03:00")).split(":"))
    return now.time() >= dt.time(hour, minute) and _load_files_state(settings_file).get("day") != now.date().isoformat()


# --- every day, inside a server ---------------------------------------------------------

def _last_file(settings_file: pathlib.Path) -> pathlib.Path:
    """What the last backup that carried a zip contained, and when it went."""
    return _state_file(settings_file).with_name(_state_file(settings_file).name.replace(
        ".backup-sent", ".backup-last.json"))


def _state_file(settings_file: pathlib.Path) -> pathlib.Path:
    """Where the date of the last send is kept: beside the first database (not in the
    project's committed files)."""
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    first = (settings_file.resolve().parent / settings["databases"][0]["file"]).resolve()
    return first.with_name(first.name + ".backup-sent")


def due(settings_file: pathlib.Path, now: dt.datetime | None = None) -> bool:
    """Today's send time (UTC) has passed and today's backup has not gone yet."""
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    now = now or dt.datetime.now(dt.timezone.utc)
    hour, minute = (int(x) for x in str(settings.get("send-at-utc", "03:00")).split(":"))
    state = _state_file(settings_file)
    last = state.read_text().strip() if state.is_file() else ""
    return now.time() >= dt.time(hour, minute) and last != now.date().isoformat()


def daily(settings_file: pathlib.Path, brevo_key: str | None = None, say=print) -> int:
    """Whatever is due today: the databases, then the files.  Each counts as done for
    the day once tried, so a failure is not retried every few minutes."""
    code = 0
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    if due(settings_file):
        _state_file(settings_file).write_text(today)
        try:
            code = max(code, send(settings_file, brevo_key=brevo_key, say=say))
        except SystemExit as e:                  # a refused email
            say(f"database backup failed: {e}")
            code = 1
    if files_due(settings_file):
        state = _load_files_state(settings_file)
        state["day"] = today
        _files_state_file(settings_file).write_text(json.dumps(state, indent=0), encoding="utf-8")
        code = max(code, send_files(settings_file, brevo_key=brevo_key, say=say))
    return code


def start(settings_file, brevo_key: str | None = None, say=print) -> threading.Thread:
    """Send the backup daily, from the calling server.  A thread checks every few
    minutes; the work itself runs in a separate process, so the server stays quick
    while photos are zipped."""
    settings_file = pathlib.Path(settings_file).resolve()
    env = {**os.environ, **({"DBMAILBACKUP_BREVO_KEY": brevo_key} if brevo_key else {})}

    def run() -> None:
        while True:
            try:
                if due(settings_file) or files_due(settings_file):
                    r = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()), "daily",
                                        str(settings_file)], env=env, capture_output=True, text=True,
                                       timeout=3600)
                    for line in (r.stdout + r.stderr).splitlines():
                        if line.strip():
                            say(line.strip())
            except Exception as e:   # noqa: BLE001 - never take the server down
                say(f"backup failed: {e}")
            time.sleep(300)

    thread = threading.Thread(target=run, name="dbmailbackup", daemon=True)
    thread.start()
    return thread


def restore(src: pathlib.Path, out_dir: pathlib.Path, password: str | None) -> int:
    import zipfile
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = []
    if src.suffix == ".zip":
        with zipfile.ZipFile(src) as z:
            for info in z.infolist():
                parts = [p for p in pathlib.PurePosixPath(info.filename).parts if p not in ("..", "/")]
                target = out_dir.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info, pwd=password.encode() if password else None))
                if len(parts) == 1 and target.suffix == ".json":
                    sources.append(target)
                else:
                    print(f"restored {target}")
    else:
        sources.append(src)
    for s in sources:
        db = out_dir / (s.stem.rsplit("-", 3)[0] + ".sqlite3")
        notes = restore_json(s, db)
        print(f"rebuilt {db}" + "".join(f"\n  note: {n}" for n in notes))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("send", help="make, check and email today's backup")
    s.add_argument("settings", type=pathlib.Path)
    s.add_argument("--to", type=pathlib.Path, help="save the zip and email text in this folder; send nothing")
    d = sub.add_parser("daily", help="send whatever is due today (what a server's start() runs)")
    d.add_argument("settings", type=pathlib.Path)
    r = sub.add_parser("restore", help="rebuild databases from a backup zip or JSON file")
    r.add_argument("backup", type=pathlib.Path)
    r.add_argument("out_dir", type=pathlib.Path)
    r.add_argument("--password")
    a = p.parse_args()
    key = os.environ.get("DBMAILBACKUP_BREVO_KEY") or None
    if a.command == "send":
        return max(send(a.settings, a.to, brevo_key=key), send_files(a.settings, a.to, brevo_key=key))
    if a.command == "daily":
        return daily(a.settings.resolve(), brevo_key=key)
    return restore(a.backup, a.out_dir, a.password)


if __name__ == "__main__":
    sys.exit(main())
