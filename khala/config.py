"""Server settings from the environment.

Every setting is read as KHALA_<NAME>. The MEMORY_<NAME> spelling from before the rename still works,
and the server logs which old names it found so they can be updated. SMTP_* keeps its own prefix.
"""
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from . import clock

log = logging.getLogger("khala.config")

DEFAULT_REPO = "/var/lib/khala/vault.git"
DEFAULT_DB = "/var/lib/khala/state/khala.db"
DEFAULT_SECRET_KEY_FILE = "/var/lib/khala/secret.key"


class ConfigError(RuntimeError):
    pass


def _positive(settings, name, default):
    raw = settings.get(name, str(default)).strip() or str(default)
    if not raw.isdigit() or int(raw) <= 0:
        raise ConfigError("KHALA_%s must be a whole number above zero, not %r" % (name, raw))
    return int(raw)


class Settings:
    """KHALA_* first, then the legacy MEMORY_* name. Remembers which legacy names were used."""
    def __init__(self, env=None):
        self.env = os.environ if env is None else env
        self.legacy = []

    def get(self, name, default=""):
        if "KHALA_" + name in self.env:
            return self.env["KHALA_" + name]
        if "MEMORY_" + name in self.env:
            self.legacy.append("MEMORY_" + name)
            return self.env["MEMORY_" + name]
        return default


def db_path(env=None):
    return Path(Settings(env).get("DB", DEFAULT_DB))


class Config:
    def __init__(self, env=None):
        s = Settings(env)
        e = s.env
        self.repo = Path(s.get("REPO", DEFAULT_REPO))
        # The branch that holds the records; mirrors push to it, and it is the only ref they may push
        self.branch = s.get("BRANCH", "main").strip() or "main"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9._-]+)*", self.branch) or ".." in self.branch \
                or self.branch.endswith((".lock", ".")):
            raise ConfigError("KHALA_BRANCH %r is not a usable branch name" % self.branch)
        self.db = Path(s.get("DB", DEFAULT_DB))
        self.state_dir = Path(s.get("STATE_DIR", str(self.db.parent)))
        # The public URL people and agents reach the server at. There is no sensible default: OAuth metadata,
        # passkeys and cookies are all bound to it.
        self.issuer = s.get("ISSUER").strip().rstrip("/")
        parsed = urlparse(self.issuer)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ConfigError("set KHALA_ISSUER to the server's public URL, for example https://memory.example.com")
        # Peers whose X-Forwarded-For / X-Forwarded-Proto are believed: the reverse proxy in front. The client address
        # feeds the audit log and sign-in rate limits, so trust only a proxy that sets these headers itself.
        # Comma-separated addresses or networks, * for any peer, empty for none.
        self.trusted_proxies = s.get("TRUSTED_PROXIES", "127.0.0.1,::1").strip()
        hosts = s.get("ALLOWED_HOSTS")
        # Host headers the MCP endpoint accepts; defaults to the issuer's host
        self.allowed_hosts = [h.strip() for h in hosts.split(",") if h.strip()] if hosts else [parsed.netloc]
        self.owners = [o.strip().lower() for o in s.get("OWNERS").split(",") if o.strip()]
        self.owner_name = s.get("OWNER_NAME", "Owner")
        # Shown in page titles, sign-in emails, passkey prompts and authenticator apps
        self.instance_name = s.get("INSTANCE_NAME", "Khala").strip()[:60] or "Khala"
        # Per-agent limits: writes (records, proposals, marking outdated) an hour and a day, notes an hour, and how
        # long a claim on inbox notes lasts
        self.writes_per_hour = _positive(s, "WRITES_PER_HOUR", 60)
        self.writes_per_day = _positive(s, "WRITES_PER_DAY", 300)
        self.notes_per_hour = _positive(s, "NOTES_PER_HOUR", 120)
        self.lease_minutes = _positive(s, "INBOX_LEASE_MINUTES", 30)
        # Time zone for "today" (verified and proposed dates, review due) and for times shown on the web
        self.timezone = s.get("TIMEZONE", "UTC").strip() or "UTC"
        try:
            clock.zone_from(self.timezone)
        except Exception:
            raise ConfigError("KHALA_TIMEZONE %r is not a known time zone; use an IANA name such as "
                              "America/Toronto, or UTC" % self.timezone)
        self.mailer = s.get("MAILER", "smtp")
        self.smtp = {k: e.get("SMTP_" + k.upper(), "") for k in ("host", "port", "username", "password", "from")}
        self.smtp["security"] = (e.get("SMTP_SECURITY", "") or "ssl").strip().lower()
        if self.smtp["security"] not in ("ssl", "starttls", "none"):
            raise ConfigError("SMTP_SECURITY must be ssl (implicit TLS, port 465), starttls (port 587) or none")
        self.origin = "{0.scheme}://{0.netloc}".format(parsed)
        # The account that pushes over Git (a local mirror); defaults to the first admin. With PUSH_ENFORCE=1
        # the pre-receive hook refuses policy violations, otherwise it only warns.
        self.git_pusher = s.get("GIT_PUSHER").strip().lower()
        self.push_enforce = s.get("PUSH_ENFORCE") == "1"
        # Whether an empty state database may take over a repository that already holds records, giving every
        # existing scope to the first admin (a fresh install importing records from elsewhere)
        self.adopt_existing = s.get("ADOPT_EXISTING") == "1"
        # Instance policy: every account must have a passkey or an authenticator app. Until it adds one, an
        # account signed in with an email code can only reach its security settings, and cannot approve agents.
        self.require_strong = s.get("REQUIRE_STRONG_FACTOR") == "1"
        # Key that encrypts TOTP secrets. It stays out of the database and out of the record backups.
        self.secret_key_file = Path(s.get("SECRET_KEY_FILE", DEFAULT_SECRET_KEY_FILE))
        # empty when the file is missing; create_app makes one for a new instance and refuses to start otherwise
        self.secret_key = s.get("SECRET_KEY") or (
            self.secret_key_file.read_text().strip() if self.secret_key_file.exists() else "")
        self.legacy_names = sorted(set(s.legacy))
        if self.legacy_names:
            log.warning("legacy setting names in use, rename them to KHALA_*: %s", ", ".join(self.legacy_names))
