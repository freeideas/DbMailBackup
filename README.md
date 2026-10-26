# DbMailBackup

Emails a readable backup of a project's SQLite databases every day.

Each database becomes one JSON file: plain text holding its table definitions and every row, so anyone who receives it can open it in a web browser or text editor and see that their data is there. Before sending, the tool builds a fresh database from that JSON and compares every table, row by row, with the real one; the email says whether that restore test passed. The JSON files go into one password-protected zip (the classic kind of zip password, which Windows, macOS and Linux open with a double-click; it keeps out casual readers, not determined ones), sent through Brevo.

It is one file, `dbmailbackup.py`, using only Python's standard library. Projects carry a copy (in their server's package) and start it from their own server, so no extra service or timer is needed: a thread of the server checks every five minutes, and the work runs in a separate process, so the server stays quick while files are zipped. This repository is the original: change it here, then copy it into the projects.

## Settings

Each project has one JSON settings file, committed with the project (its repository must be private, since the password is in it):

```json
{
  "project": "Well Water Network",
  "recipients": ["you@example.com", "a.friend@example.com"],
  "zip-password": "the password people type to open the zip",
  "send-at-utc": "04:00",
  "from-email": "backups@example.com",
  "from-name": "Well Water Network backups",
  "brevo-key": "passwords/brevo-api-key",
  "max-email-mb": 9,
  "databases": [{"file": "data/wwn.sqlite3", "skip-tables": ["auth_session", "login_code"]}],
  "folders": [{"path": "data/photos", "include": ["*/display.*"], "name": "photos"}]
}
```

- `databases[].file` is relative to the settings file. `skip-tables` takes names or patterns (`cache_*`): leave out tables that are only short-lived or security data (sign-in sessions and codes, rate limits), or that can be rebuilt and are too big for email. Indexes and triggers of a left-out table are left out with it.
- `send-at-utc` is the time of day, in UTC, to send. A server that was down at that time sends when it starts again. The date of the last send is kept beside the first database, in `<database>.backup-sent`.
- `brevo-key` is the path of the Brevo key in the credential store (`~/creds`); a server that already holds the key passes it in instead.
- `folders` (optional): files kept beside the database, such as photos. After the databases, each day's run emails the files that are new or changed since they were last sent (by size and time), as many to an email as fit, in as many emails as needed, each a password zip that keeps their folders. A file counts as sent once its email went; one that failed goes the next day. `include` takes patterns of paths within the folder (`*/display.*`); `name` is the folder's name inside the zips. What was sent is recorded beside the first database, in `<database>.backup-files.json`; deleting it sends everything again. A single file too big for an email is listed, not sent.
- `max-email-mb`: a backup bigger than this is not attached, and the email says so. Past two thirds of it, the email warns that it is time to send only the changes each day.

## Use

In a server, once at startup:

```python
from . import dbmailbackup
dbmailbackup.start("path/to/db-backup.json", brevo_key=key, say=log.info)
```

From a shell:

```sh
uv run dbmailbackup.py send SETTINGS.json --to some/folder   # make and check it, save the zip and email text; send nothing
uv run dbmailbackup.py send SETTINGS.json                    # send it now
uv run dbmailbackup.py restore BACKUP.zip OUT_FOLDER --password PASSWORD   # rebuild each database
uv run --with pytest pytest -q                               # the tests
```

## What is in the JSON

`format`, the database's file name, when it was made, the SQLite version, the `schema` (every table, index, view and trigger, in the order they were made), what was `left_out` and why, and `tables`: for each, its `columns` and its `rows`, one row per line. Bytes are written as `{"$base64": "..."}` and infinities as `{"$float": "inf"}`. Virtual tables (such as an R-tree) keep their definition and rows; their internal helper tables are not copied, since putting the rows back rebuilds them. A virtual table that needs an extension this SQLite does not have is reported in the email and not copied. `sqlite_sequence` (the AUTOINCREMENT counters) is copied, so numbering carries on where it left off.

A restore makes the tables, puts the rows back, and only then makes the indexes, views and triggers, so no trigger runs twice.
