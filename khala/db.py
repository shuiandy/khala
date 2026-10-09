"""Accounts, scope ownership, grants, agents, OAuth state, web sessions and audit all live in one SQLite file.
Tokens, codes and session IDs are stored only as hashes.

The schema version is in PRAGMA user_version: 0 = empty or the old users/grants layout, 2 = accounts and agents,
3 = adds passkeys, TOTP and recovery codes, 4 = inbox notes, consolidation proposals, conflicts, 5 = the OAuth
consent page bound to a browser (pending.browser). Old databases migrate in place on open: new tables via
CREATE IF NOT EXISTS, new columns on existing tables via COLUMNS (checks table_info first and skips columns that
exist, so repeated deploys are safe).
"""
import hashlib
import ipaddress
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import legacy

VERSION = 6
ROLES = ("viewer", "editor", "maintainer")
REVIEW_MODES = ("off", "bots", "all")
SCOPE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, handle TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')), created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS account_emails (
  email TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('device', 'bot')), oauth_client_id TEXT,
  ceiling TEXT, created_at REAL NOT NULL, last_used_at REAL, last_ip_prefix TEXT, revoked_at REAL);
CREATE TABLE IF NOT EXISTS scopes (
  id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES accounts(id), title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '', auto_load INTEGER NOT NULL DEFAULT 0, shareable INTEGER NOT NULL DEFAULT 1,
  review_mode TEXT NOT NULL DEFAULT 'off' CHECK (review_mode IN ('off', 'bots', 'all')),
  paths TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, archived_at REAL,
  consolidation TEXT NOT NULL DEFAULT 'review' CHECK (consolidation IN ('review', 'auto')));
CREATE TABLE IF NOT EXISTS grants (
  scope_id TEXT NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('viewer', 'editor', 'maintainer')), granted_by INTEGER,
  created_at REAL NOT NULL, PRIMARY KEY (scope_id, account_id));
CREATE TABLE IF NOT EXISTS invites (
  token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, scope_id TEXT, role TEXT, created_by INTEGER NOT NULL,
  created_at REAL NOT NULL, expires_at REAL NOT NULL, accepted_at REAL);
CREATE TABLE IF NOT EXISTS web_sessions (
  hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  csrf TEXT NOT NULL, created_at REAL NOT NULL, last_seen_at REAL NOT NULL, expires_at REAL NOT NULL,
  reauth_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY, at REAL NOT NULL, account_id INTEGER, agent_id INTEGER, action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', ip_prefix TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS audit_at ON audit_events(at);
CREATE TABLE IF NOT EXISTS passkeys (
  id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  credential_id TEXT NOT NULL UNIQUE, public_key BLOB NOT NULL, sign_count INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL, transports TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, last_used_at REAL);
CREATE TABLE IF NOT EXISTS totp (
  account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE, secret TEXT NOT NULL,
  confirmed_at REAL, last_step INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_codes (
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, code_hash TEXT NOT NULL, used_at REAL,
  PRIMARY KEY (account_id, code_hash));
CREATE TABLE IF NOT EXISTS challenges (
  key TEXT PRIMARY KEY, challenge TEXT NOT NULL, purpose TEXT NOT NULL, account_id INTEGER, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS half_logins (
  key TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  attempts INTEGER NOT NULL DEFAULT 0, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notes (
  id TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), agent_id INTEGER, scope TEXT NOT NULL,
  path TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
  state TEXT NOT NULL DEFAULT 'new' CHECK (state IN ('new', 'done')), outcome TEXT, detail TEXT NOT NULL DEFAULT '',
  guidance TEXT NOT NULL DEFAULT '', done_by INTEGER, done_at REAL, claim_agent INTEGER, claim_until REAL);
CREATE INDEX IF NOT EXISTS notes_open ON notes(state, scope);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, record TEXT NOT NULL, scope TEXT NOT NULL, old_scope TEXT, base_blob TEXT, new_blob TEXT NOT NULL,
  account_id INTEGER NOT NULL, agent_id INTEGER, note_ids TEXT NOT NULL DEFAULT '[]', reason TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL CHECK (state IN ('open', 'applied', 'approved', 'rejected', 'stale', 'undone')),
  commit_sha TEXT, created_at REAL NOT NULL, decided_by INTEGER, decided_at REAL, decision TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS conflicts (
  id TEXT PRIMARY KEY, record TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL, note_ids TEXT NOT NULL,
  detail TEXT NOT NULL, account_id INTEGER NOT NULL, agent_id INTEGER, created_at REAL NOT NULL,
  state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'resolved')), resolution TEXT NOT NULL DEFAULT '',
  decided_by INTEGER, decided_at REAL);
CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pending (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL, expires REAL NOT NULL, account_id INTEGER,
  browser TEXT);
CREATE TABLE IF NOT EXISTS login_codes (
  req TEXT NOT NULL, email TEXT NOT NULL, code_hash TEXT NOT NULL, expires REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (req, email));
CREATE TABLE IF NOT EXISTS sends (email TEXT NOT NULL, ip TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS auth_codes (
  code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, subject TEXT NOT NULL, data TEXT NOT NULL,
  expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS device_codes (
  device_hash TEXT PRIMARY KEY, user_hash TEXT NOT NULL UNIQUE, client_name TEXT NOT NULL, ip TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL, expires REAL NOT NULL, interval INTEGER NOT NULL DEFAULT 5, last_poll REAL,
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'approved', 'denied')),
  account_id INTEGER, agent_id INTEGER, days INTEGER);
CREATE TABLE IF NOT EXISTS tokens (
  token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL, subject TEXT NOT NULL,
  scopes TEXT NOT NULL, resource TEXT, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, agent_id INTEGER);
"""


# Columns added to existing tables: (table, column, definition). New databases get them from SCHEMA, old ones via ALTER
COLUMNS = [
    ("scopes", "consolidation", "TEXT NOT NULL DEFAULT 'review' CHECK (consolidation IN ('review', 'auto'))"),
    ("pending", "browser", "TEXT"),        # cookie hash of the browser that verified; the consent page accepts only it
    ("tokens", "rotated_at", "REAL"),      # when a refresh token was exchanged; reuse after the grace period is theft
    ("tokens", "grace_used", "INTEGER NOT NULL DEFAULT 0"),   # the one reuse allowed within the grace period
]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret(nbytes=32) -> str:
    return secrets.token_urlsafe(nbytes)


def ip_prefix(ip: str) -> str:
    """Keep only the rough source: /24 for IPv4, /48 for IPv6."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return ""
    net = ipaddress.ip_network("%s/%d" % (addr, 24 if addr.version == 4 else 48), strict=False)
    return str(net)


def handle_base(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    h = re.sub(r"[^a-z0-9]+", "-", local).strip("-")[:32]
    return h or "user"


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self._wal()
        with self._lock:
            if self.conn.execute("PRAGMA user_version").fetchone()[0] < VERSION:
                self._migrate()
            if self._missing_columns():     # checked on every open, so new columns land even if VERSION was not bumped
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    self._add_columns()     # checks again under the write lock: another worker may have added them
                    self.conn.execute("COMMIT")
                except BaseException:
                    self.conn.execute("ROLLBACK")
                    raise

    def _wal(self):
        """Switch to WAL once. The mode is stored in the file, so later opens find it set. Switching needs the
        database to itself and does not wait on locks, so workers opening a new file together retry briefly."""
        for attempt in range(50):
            try:
                if self.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                    return
                self.conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                if attempt == 49:
                    raise
            time.sleep(0.05)

    def q(self, sql, *args):
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def changed(self, sql, *args):
        """Run one write and return how many rows it changed. A single statement is atomic in SQLite, also across
        processes, so a conditional UPDATE checked this way is a safe check-and-set."""
        with self._lock:
            return self.conn.execute(sql, args).rowcount

    def one(self, sql, *args):
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def tx(self):
        """Statements inside with db.tx(): ... run in one transaction and roll back together on error."""
        db = self

        class _Tx:
            def __enter__(self):
                db._lock.acquire()
                db.conn.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc_type, exc, tb):
                try:
                    db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    db._lock.release()
        return _Tx()

    def purge(self):
        now = time.time()
        for table in ("pending", "login_codes", "auth_codes", "tokens"):
            self.q("DELETE FROM %s WHERE expires < ?" % table, now)
        self.q("DELETE FROM sends WHERE ts < ?", now - 86400)
        self.q("DELETE FROM web_sessions WHERE expires_at < ?", now)
        self.q("DELETE FROM challenges WHERE expires < ?", now)
        self.q("DELETE FROM half_logins WHERE expires < ?", now)
        self.q("DELETE FROM device_codes WHERE expires < ?", now)
        self.q("DELETE FROM invites WHERE expires_at < ? AND accepted_at IS NULL", now - 30 * 86400)
        self.q("DELETE FROM audit_events WHERE at < ?", now - 400 * 86400)

    # ---- migrations ----
    def _migrate(self):
        c = self.conn
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("BEGIN IMMEDIATE")
        try:
            # read again under the write lock: another worker may have migrated while this one waited
            if c.execute("PRAGMA user_version").fetchone()[0] >= VERSION:
                c.execute("COMMIT")
                return
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            old = legacy.prepare_v1(c, tables)
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    c.execute(stmt)
            self._add_columns()
            if old:
                legacy.migrate_v1(self)
            c.execute("PRAGMA user_version=%d" % VERSION)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.execute("PRAGMA foreign_keys=ON")

    def _missing_columns(self):
        return [(t, c, d) for t, c, d in COLUMNS
                if c not in {r[1] for r in self.conn.execute("PRAGMA table_info(%s)" % t)}]

    def _add_columns(self):
        for table, column, definition in self._missing_columns():
            self.conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))

    def _insert_account(self, email, name, is_admin, created_at=None):
        c = self.conn
        email = email.lower()
        base = handle_base(email)
        handle, n = base, 1
        while c.execute("SELECT 1 FROM accounts WHERE handle=?", (handle,)).fetchone():
            n += 1
            handle = "%s-%d" % (base, n)
        cur = c.execute("INSERT INTO accounts(email, handle, name, is_admin, created_at) VALUES (?,?,?,?,?)",
                        (email, handle, name, int(is_admin), created_at or time.time()))
        c.execute("INSERT INTO account_emails(email, account_id) VALUES (?,?)", (email, cur.lastrowid))
        return cur.lastrowid

    def _ensure_scope(self, scope_id, owner_id, now, auto_load=False):
        self.conn.execute("INSERT OR IGNORE INTO scopes(id, owner_id, auto_load, shareable, created_at) VALUES (?,?,?,?,?)",
                          (scope_id, owner_id, int(auto_load), int(not auto_load), now))

    def _free_scope_id(self, base):
        sid, n = base, 1
        while self.conn.execute("SELECT 1 FROM scopes WHERE id=?", (sid,)).fetchone():
            n += 1
            sid = "%s-%d" % (base, n)
        return sid

    # ---- accounts ----
    def account(self, account_id):
        return self.one("SELECT * FROM accounts WHERE id=?", account_id)

    def account_by_email(self, email):
        return self.one("SELECT a.* FROM accounts a JOIN account_emails e ON e.account_id=a.id WHERE e.email=?",
                        (email or "").strip().lower())

    def active_account_by_email(self, email):
        a = self.account_by_email(email)
        return a if a and a["status"] == "active" else None

    def accounts(self):
        return self.q("SELECT * FROM accounts ORDER BY is_admin DESC, email")

    def emails(self, account_id):
        return [r["email"] for r in self.q("SELECT email FROM account_emails WHERE account_id=? ORDER BY email",
                                           account_id)]

    def create_account(self, email, name, is_admin=False):
        """New account: gets a personal scope <handle> and an auto-loaded scope <handle>-global."""
        with self.tx():
            aid = self._insert_account(email, name, is_admin)
            handle = self.conn.execute("SELECT handle FROM accounts WHERE id=?", (aid,)).fetchone()[0]
            now = time.time()
            personal = self._free_scope_id(handle)
            self.conn.execute("INSERT INTO scopes(id, owner_id, title, created_at) VALUES (?,?,?,?)",
                              (personal, aid, "Personal", now))
            auto = self._free_scope_id(handle + "-global")
            self.conn.execute("INSERT INTO scopes(id, owner_id, title, auto_load, shareable, created_at) "
                              "VALUES (?,?,?,1,0,?)", (auto, aid, "Loaded every session", now))
        return aid

    def add_alias(self, account_id, email):
        self.q("INSERT INTO account_emails(email, account_id) VALUES (?,?)", email.lower(), account_id)

    def ensure_admin(self, email, name):
        """An email from KHALA_OWNERS: the account it belongs to (aliases included) becomes admin; an unknown email
        gets an admin account of its own. Two emails are only the same person when an alias says so."""
        email = email.lower()
        try:
            return self._ensure_admin(email, name)
        except sqlite3.IntegrityError:          # another worker created it at the same moment
            return self._ensure_admin(email, name)

    def _ensure_admin(self, email, name):
        a = self.account_by_email(email)
        if a:
            if not a["is_admin"]:
                self.q("UPDATE accounts SET is_admin=1 WHERE id=?", a["id"])
            return a["id"]
        return self.create_account(email, name, is_admin=True)

    def primary_admin(self):
        return self.one("SELECT * FROM accounts WHERE is_admin=1 AND status='active' ORDER BY id LIMIT 1")

    def set_account_status(self, account_id, status):
        self.q("UPDATE accounts SET status=? WHERE id=?", status, account_id)
        if status == "disabled":
            self.q("DELETE FROM web_sessions WHERE account_id=?", account_id)
            self.q("UPDATE tokens SET revoked=1 WHERE agent_id IN (SELECT id FROM agents WHERE account_id=?)",
                   account_id)

    def set_admin(self, account_id, is_admin):
        self.q("UPDATE accounts SET is_admin=? WHERE id=?", int(is_admin), account_id)

    # ---- scopes and grants ----
    def scope(self, scope_id):
        return self.one("SELECT * FROM scopes WHERE id=?", scope_id)

    def all_scopes(self):
        return self.q("SELECT * FROM scopes ORDER BY id")

    def create_scope(self, scope_id, owner_id, title="", description=""):
        self.q("INSERT INTO scopes(id, owner_id, title, description, created_at) VALUES (?,?,?,?,?)",
               scope_id, owner_id, title, description, time.time())

    def adopt_scopes(self, scope_ids):
        """A scope in the repository but not the database (pushed directly from a mirror) goes to the first admin."""
        known = {r["id"] for r in self.q("SELECT id FROM scopes")}
        missing = [s for s in scope_ids if s not in known and SCOPE_ID.fullmatch(s)]
        admin = self.primary_admin()
        if not missing or not admin:
            return []
        now = time.time()
        with self.tx():
            for s in missing:
                self._ensure_scope(s, admin["id"], now)    # auto-loading is the owner's call, never the name's
        return missing

    def set_auto_load(self, scope_id, on):
        """Turn auto-loading on or off; returns whether the scope changed. An auto-loaded scope is never shared, so
        turning it on is refused while the scope has members or open invitations, or is archived. The check and
        the change are one statement."""
        if on:
            return bool(self.changed(
                "UPDATE scopes SET auto_load=1, shareable=0 WHERE id=? AND auto_load=0 AND archived_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM grants WHERE scope_id=?) AND NOT EXISTS "
                "(SELECT 1 FROM invites WHERE scope_id=? AND accepted_at IS NULL AND expires_at>?)",
                scope_id, scope_id, scope_id, time.time()))
        return bool(self.changed("UPDATE scopes SET auto_load=0, shareable=1 WHERE id=? AND auto_load=1", scope_id))

    def update_scope(self, scope_id, **fields):
        allowed = {"title", "description", "paths", "review_mode", "archived_at", "owner_id", "consolidation"}
        sets = [(k, v) for k, v in fields.items() if k in allowed]
        if sets:
            self.q("UPDATE scopes SET %s WHERE id=?" % ", ".join("%s=?" % k for k, _ in sets),
                   *[v for _, v in sets], scope_id)

    def grants_for_account(self, account_id) -> dict:
        return {r["scope_id"]: r["role"] for r in self.q("SELECT scope_id, role FROM grants WHERE account_id=?",
                                                          account_id)}

    def members(self, scope_id):
        return self.q("SELECT g.*, a.email, a.name, a.status FROM grants g JOIN accounts a ON a.id=g.account_id "
                      "WHERE g.scope_id=? ORDER BY a.email", scope_id)

    def set_grant(self, scope_id, account_id, role, granted_by):
        assert role in ROLES
        self.q("INSERT INTO grants(scope_id, account_id, role, granted_by, created_at) VALUES (?,?,?,?,?) "
               "ON CONFLICT(scope_id, account_id) DO UPDATE SET role=excluded.role, granted_by=excluded.granted_by",
               scope_id, account_id, role, granted_by, time.time())

    def remove_grant(self, scope_id, account_id):
        self.q("DELETE FROM grants WHERE scope_id=? AND account_id=?", scope_id, account_id)

    # ---- agent ----
    def agent(self, agent_id):
        return self.one("SELECT * FROM agents WHERE id=?", agent_id)

    def agents_for(self, account_id):
        return self.q("SELECT * FROM agents WHERE account_id=? ORDER BY revoked_at IS NOT NULL, "
                      "COALESCE(last_used_at, created_at) DESC", account_id)

    def agent_for_client(self, account_id, client_id):
        return self.one("SELECT * FROM agents WHERE account_id=? AND oauth_client_id=? AND revoked_at IS NULL "
                        "ORDER BY id DESC LIMIT 1", account_id, client_id)

    def create_agent(self, account_id, name, kind, client_id=None, ceiling=None):
        cur = None
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO agents(account_id, name, kind, oauth_client_id, ceiling, created_at) VALUES (?,?,?,?,?,?)",
                (account_id, name, kind, client_id, json.dumps(ceiling) if ceiling is not None else None, time.time()))
        return cur.lastrowid

    def update_agent(self, agent_id, name=None, kind=None, ceiling=..., ):
        if name is not None:
            self.q("UPDATE agents SET name=? WHERE id=?", name, agent_id)
        if kind is not None:
            self.q("UPDATE agents SET kind=? WHERE id=?", kind, agent_id)
        if ceiling is not ...:
            self.q("UPDATE agents SET ceiling=? WHERE id=?", json.dumps(ceiling) if ceiling is not None else None,
                   agent_id)

    def revoke_agent(self, agent_id):
        now = time.time()
        self.q("UPDATE agents SET revoked_at=? WHERE id=? AND revoked_at IS NULL", now, agent_id)
        self.q("UPDATE tokens SET revoked=1 WHERE agent_id=?", agent_id)

    def touch_agent(self, agent_id, ip=""):
        """Write to disk at most once a minute so not every request writes to the database."""
        now = time.time()
        self.q("UPDATE agents SET last_used_at=?, last_ip_prefix=COALESCE(NULLIF(?, ''), last_ip_prefix) "
               "WHERE id=? AND (last_used_at IS NULL OR last_used_at < ?)", now, ip_prefix(ip), agent_id, now - 60)

    def token_row(self, token_hash):
        return self.one("SELECT * FROM tokens WHERE token_hash=?", token_hash)

    # ---- invites ----
    def create_invite(self, email, scope_id, role, created_by, ttl=7 * 86400):
        token = new_secret(24)
        now = time.time()
        self.q("INSERT INTO invites(token_hash, email, scope_id, role, created_by, created_at, expires_at) "
               "VALUES (?,?,?,?,?,?,?)", digest(token), email.lower(), scope_id, role, created_by, now, now + ttl)
        return token

    def invite(self, token):
        return self.one("SELECT * FROM invites WHERE token_hash=? AND expires_at>? AND accepted_at IS NULL",
                        digest(token or ""), time.time())

    def open_invite_for(self, email):
        return self.one("SELECT * FROM invites WHERE email=? AND expires_at>? AND accepted_at IS NULL LIMIT 1",
                        (email or "").lower(), time.time())

    def pending_invites(self, scope_id=None):
        if scope_id is None:
            return self.q("SELECT * FROM invites WHERE accepted_at IS NULL AND expires_at>? ORDER BY created_at DESC",
                          time.time())
        return self.q("SELECT * FROM invites WHERE scope_id=? AND accepted_at IS NULL AND expires_at>? "
                      "ORDER BY created_at DESC", scope_id, time.time())

    def cancel_invite(self, token_hash):
        self.q("DELETE FROM invites WHERE token_hash=? AND accepted_at IS NULL", token_hash)

    # ---- audit ----
    def audit(self, action, account_id=None, agent_id=None, target="", detail="", ip=""):
        self.q("INSERT INTO audit_events(at, account_id, agent_id, action, target, detail, ip_prefix) "
               "VALUES (?,?,?,?,?,?,?)", time.time(), account_id, agent_id, action, target or "",
               detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False), ip_prefix(ip))
