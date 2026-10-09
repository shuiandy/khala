"""Creating an instance (khala init) and reading its settings file.

init makes the data directory (record repository, state database, the key that encrypts TOTP secrets), creates
the first admin and writes a settings file that `khala serve` and systemd's EnvironmentFile both read. It never
overwrites an existing settings file or key.
"""
import os
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from . import clock
from .db import DB
from .store import Store

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class InitError(Exception):
    pass


def read_env(path):
    """KEY=VALUE lines; blank lines and # comments are skipped, and matching outer quotes are removed."""
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def load_env(path, environ=None):
    """Put a settings file into the environment. Values already set win, so a one-off override needs no edit."""
    environ = os.environ if environ is None else environ
    for key, value in read_env(path).items():
        environ.setdefault(key, value)


def missing_key(cfg):
    """Why the server must not start for want of its TOTP key, or None. Checked before the server starts, so the
    reason is one clear line instead of a traceback from inside the app factory."""
    if cfg.secret_key or cfg.secret_key_file.exists() or not cfg.db.exists():
        return None
    try:
        with sqlite3.connect("file:%s?mode=ro" % cfg.db, uri=True) as c:
            accounts = c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    except sqlite3.Error:
        return None
    if not accounts:
        return None
    return ("the key that encrypts TOTP secrets is missing at %s; restore it from the state backup, or point "
            "KHALA_SECRET_KEY_FILE at it" % cfg.secret_key_file)


def ensure_key(path, fresh):
    """The key that encrypts TOTP secrets. A new instance gets one; an instance that already has accounts must
    find its own, since a new key would leave every enrolled authenticator app unreadable."""
    path = Path(path)
    if not path.exists():
        if not fresh:
            raise InitError("the key that encrypts TOTP secrets is missing at %s; restore it from the state backup, "
                            "or point KHALA_SECRET_KEY_FILE at it" % path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _write_new(path, secrets.token_urlsafe(48) + "\n", 0o600)
        except FileExistsError:                 # another worker made it a moment ago
            pass
    return path.read_text().strip()


def _write_new(path, text, mode):
    """Create a file that must not exist yet, with its permissions set from the start."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def init(url, admin, data_dir, env_file, name="", timezone="UTC"):
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise InitError("--url must be the address people and agents use, for example https://memory.example.com")
    admin = (admin or "").strip().lower()
    if "@" not in admin or admin.startswith("@") or admin.endswith("@"):
        raise InitError("--admin must be the email address of the first admin")
    try:
        clock.zone_from(timezone)
    except Exception:
        raise InitError("--timezone %r is not a known time zone; use an IANA name such as America/Toronto, or UTC"
                        % timezone)
    env_file = Path(env_file).resolve()
    if env_file.exists():
        raise InitError("%s already exists; edit it instead of running init again" % env_file)

    data = Path(data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    os.chmod(data, 0o700)
    repo, db_file, key = data / "vault.git", data / "state" / "khala.db", data / "secret.key"
    Store(repo).ensure()
    if not key.exists():
        _write_new(key, secrets.token_urlsafe(48) + "\n", 0o600)
    db = DB(db_file)
    try:
        account = db.account_by_email(admin)
        if account:
            db.set_admin(account["id"], True)
        else:
            db.create_account(admin, name.strip() or admin.split("@", 1)[0], is_admin=True)
        db.audit("instance.initialized", None, target=admin, detail={"url": url})
    finally:
        db.conn.close()

    local = parsed.hostname in LOCAL_HOSTS
    lines = ["# Khala settings, written by khala init. Read by `khala serve --env %s` and usable as a systemd" % env_file.name,
             "# EnvironmentFile. All settings are described in the README.",
             "KHALA_ISSUER=%s" % url,
             "KHALA_REPO=%s" % repo,
             "KHALA_DB=%s" % db_file,
             "KHALA_SECRET_KEY_FILE=%s" % key,
             "KHALA_TIMEZONE=%s" % (timezone.strip() or "UTC"),
             ""]
    if local:
        lines += ["# Sign-in codes are printed in the server log. To send them by email instead, set",
                  "# KHALA_MAILER=smtp and fill in the SMTP settings (port 465, implicit TLS).",
                  "KHALA_MAILER=log",
                  "#SMTP_HOST=", "#SMTP_PORT=465", "#SMTP_USERNAME=", "#SMTP_PASSWORD=", "#SMTP_FROM="]
    else:
        lines += ["# Sign-in codes are sent by email. Fill these in before the first sign-in (port 465, implicit TLS).",
                  "KHALA_MAILER=smtp",
                  "SMTP_HOST=", "SMTP_PORT=465", "SMTP_USERNAME=", "SMTP_PASSWORD=", "SMTP_FROM="]
    _write_new(env_file, "\n".join(lines) + "\n", 0o600)
    return {"env_file": env_file, "data": data, "url": url, "local": local}
