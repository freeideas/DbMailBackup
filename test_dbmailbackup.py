# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///
"""Run with: uv run --with pytest pytest -q"""
import datetime as dt
import json
import pathlib
import sqlite3
import zipfile

import dbmailbackup as m

SCHEMA = """
CREATE TABLE person (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, photo BLOB,
                     score REAL, shout TEXT GENERATED ALWAYS AS (upper(name)) VIRTUAL);
CREATE TABLE note (person_id INTEGER REFERENCES person(id), body TEXT) ;
CREATE TABLE secret (token TEXT);
CREATE INDEX note_person ON note(person_id);
CREATE INDEX secret_token ON secret(token);
CREATE VIEW loud AS SELECT shout FROM person;
CREATE TABLE audit (what TEXT);
CREATE TRIGGER person_added AFTER INSERT ON person BEGIN INSERT INTO audit VALUES ('added ' || new.name); END;
CREATE VIRTUAL TABLE place USING rtree(id, minx, maxx, miny, maxy);
CREATE TABLE plain (k TEXT PRIMARY KEY, v) WITHOUT ROWID;
CREATE VIRTUAL TABLE words USING fts5(header, body);
"""


def make_db(path: pathlib.Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO person (name, photo, score) VALUES ('Áine', ?, 1.5)", (bytes(range(256)),))
    con.execute("INSERT INTO person (name, photo, score) VALUES ('Bríd', NULL, ?)", (float("inf"),))
    con.execute("DELETE FROM person WHERE name = 'Bríd'")            # the counter remembers 2
    con.execute("INSERT INTO note VALUES (1, 'line one\nline \"two\"')")
    con.execute("INSERT INTO secret VALUES ('do-not-send')")
    con.execute("INSERT INTO place VALUES (7, 1.5, 2.5, -8.5, -8.25)")
    con.executemany("INSERT INTO plain VALUES (?, ?)", [("a", 1), ("b", 2.25), ("c", None)])
    con.execute("INSERT INTO note (rowid, person_id, body) VALUES (10, 1, 'joined by rowid')")
    con.execute("INSERT INTO words (rowid, header, body) VALUES (42, 'Torts', 'negligence and duty')")
    con.commit()
    con.close()


def settings(tmp: pathlib.Path) -> pathlib.Path:
    (tmp / "data").mkdir()
    make_db(tmp / "data" / "app.sqlite3")
    f = tmp / "backup.json"
    f.write_text(json.dumps({"project": "Test Project", "recipients": ["a@example.com"], "zip-password": "pw 1",
                             "from-email": "x@example.com", "send-at-utc": "04:30",
                             "databases": [{"file": "data/app.sqlite3", "skip-tables": ["secret"]}]}))
    return f


def test_backup_restores_every_row(tmp_path):
    f = settings(tmp_path)
    assert m.send(f, save_to=tmp_path / "out", say=lambda s: None) == 0
    zipped = next((tmp_path / "out").glob("*.zip"))
    text = (tmp_path / "out" / (zipped.name + ".txt")).read_text()
    assert "restore test passed" in text and "secret: not copied" in text and "PROBLEM" not in text
    with zipfile.ZipFile(zipped) as z:                        # the standard library opens it, with the password
        doc = json.loads(z.read(z.namelist()[0], pwd=b"pw 1"))
    assert "do-not-send" not in json.dumps(doc) and "secret_token" not in json.dumps(doc)
    assert m.restore(zipped, tmp_path / "back", "pw 1") == 0
    con = sqlite3.connect(tmp_path / "back" / "app.sqlite3")
    assert con.execute("SELECT name, photo, score, shout FROM person").fetchall() == [
        ("Áine", bytes(range(256)), 1.5, "ÁINE")]
    assert con.execute("SELECT * FROM audit").fetchall() == [("added Áine",), ("added Bríd",)]   # no trigger re-ran
    assert con.execute("SELECT seq FROM sqlite_sequence WHERE name = 'person'").fetchone() == (2,)
    con.execute("INSERT INTO person (name) VALUES ('Cáit')")                    # numbering carries on
    assert con.execute("SELECT id FROM person WHERE name = 'Cáit'").fetchone() == (3,)
    assert con.execute("SELECT * FROM place").fetchall() == [(7, 1.5, 2.5, -8.5, -8.25)]
    assert con.execute("SELECT * FROM loud").fetchall()
    assert sorted(con.execute("SELECT * FROM plain").fetchall()) == [("a", 1), ("b", 2.25), ("c", None)]
    assert con.execute("SELECT rowid FROM note WHERE body = 'joined by rowid'").fetchone() == (10,)
    assert con.execute("SELECT rowid, header FROM words WHERE words MATCH 'duty'").fetchall() == [(42, "Torts")]


def test_a_table_this_sqlite_cannot_read_is_reported_not_failed(tmp_path):
    """A virtual table whose extension is missing here (sqlite-vec's vec0, say) is
    listed as not copied, and the restore test of everything else still passes."""
    f = settings(tmp_path)
    con = sqlite3.connect(tmp_path / "data" / "app.sqlite3")
    con.execute("PRAGMA writable_schema = ON")
    con.execute("INSERT INTO sqlite_master VALUES ('table', 'vecs', 'vecs', 0,"
                " 'CREATE VIRTUAL TABLE vecs USING nosuchmodule(embedding)')")
    con.commit()
    con.close()
    assert m.send(f, save_to=tmp_path / "out", say=lambda s: None) == 0
    text = next((tmp_path / "out").glob("*.txt")).read_text()
    assert "vecs: not copied (this machine's SQLite cannot read it" in text
    assert "restore test passed" in text and "PROBLEM" not in text


def test_wrong_password_is_refused(tmp_path):
    f = settings(tmp_path)
    m.send(f, save_to=tmp_path / "out", say=lambda s: None)
    zipped = next((tmp_path / "out").glob("*.zip"))
    with zipfile.ZipFile(zipped) as z:
        try:
            z.read(z.namelist()[0], pwd=b"wrong")
            assert False, "opened with the wrong password"
        except RuntimeError:
            pass


def test_too_big_for_email_is_not_attached(tmp_path):
    f = settings(tmp_path)
    s = json.loads(f.read_text())
    s["max-email-mb"] = 0.0001
    f.write_text(json.dumps(s))
    assert m.send(f, save_to=tmp_path / "out", say=lambda s: None) == 1
    text = next((tmp_path / "out").glob("*.txt")).read_text()
    assert "PROBLEM" in text and "Nothing is attached" in text


def test_due_once_a_day_after_the_time(tmp_path):
    f = settings(tmp_path)
    at = lambda h, mi: dt.datetime(2026, 10, 26, h, mi, tzinfo=dt.timezone.utc)
    assert not m.due(f, at(4, 29)) and m.due(f, at(4, 30))
    m._state_file(f).write_text("2026-10-26")
    assert not m.due(f, at(23, 0))
    assert m.due(f, dt.datetime(2026, 10, 27, 5, 0, tzinfo=dt.timezone.utc))


def test_files_go_once_in_parts_that_fit(tmp_path, monkeypatch):
    f = settings(tmp_path)
    photos = tmp_path / "data" / "photos"
    for i in range(5):
        (photos / str(i)).mkdir(parents=True)
        (photos / str(i) / "display.jpg").write_bytes(bytes([i]) * 300_000)
        (photos / str(i) / "original.jpg").write_bytes(b"x" * 5_000_000)
    s = json.loads(f.read_text())
    s.update({"max-email-mb": 1, "folders": [{"path": "data/photos", "include": ["*/display.jpg"]}]})
    f.write_text(json.dumps(s))
    sent = []
    monkeypatch.setattr(m, "send_mail", lambda key, st, subject, text, att: sent.append((subject, att.read_bytes())))
    assert m.send_files(f, brevo_key="k", say=lambda s: None) == 0
    assert len(sent) == 3 and "part 1 of 3" in sent[0][0]           # 750 KB of room each: two photos per email
    assert m.send_files(f, brevo_key="k", say=lambda s: None) == 0 and len(sent) == 3   # nothing new
    (photos / "4" / "display.jpg").write_bytes(b"changed" * 50_000)
    m.send_files(f, brevo_key="k", say=lambda s: None)
    assert len(sent) == 4                                            # only the changed one
    (tmp_path / "last.zip").write_bytes(sent[-1][1])
    m.restore(tmp_path / "last.zip", tmp_path / "back", "pw 1")
    assert (tmp_path / "back" / "photos" / "4" / "display.jpg").read_bytes() == b"changed" * 50_000


def test_daily_sends_once_a_day(tmp_path, monkeypatch):
    f = settings(tmp_path)
    s = json.loads(f.read_text())
    s.update({"send-at-utc": "00:00", "folders": [{"path": "data", "include": ["*.txt"]}]})
    f.write_text(json.dumps(s))
    (tmp_path / "data" / "a.txt").write_text("hello")
    sent = []
    monkeypatch.setattr(m, "send_mail", lambda key, st, subject, text, att: sent.append(subject))
    assert m.daily(f, brevo_key="k", say=lambda s: None) == 0
    assert len(sent) == 2 and "database backup" in sent[0] and "files backup" in sent[1]
    assert m.daily(f, brevo_key="k", say=lambda s: None) == 0 and len(sent) == 2
