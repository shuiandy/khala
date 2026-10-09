"""Strong factors: passkeys (WebAuthn), TOTP and single-use recovery codes.

Once an account registers a passkey or TOTP, an email code alone can no longer sign in: either use a passkey
directly, or follow the email code with a TOTP code, recovery code or passkey. TOTP secrets are stored encrypted
with a server key, and that key never goes into the database or backups.
"""
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from urllib.parse import quote, urlparse

import segno
import webauthn
from cryptography.fernet import Fernet, InvalidToken
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

from .db import digest

TOTP_STEP = 30
TOTP_DIGITS = 6
CHALLENGE_TTL = 5 * 60
RECOVERY_COUNT = 10
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"      # drops the easily confused 0/o and 1/l/i


class AuthError(Exception):
    pass


# ---------- keys ----------
class SecretBox:
    def __init__(self, key: str):
        if not key:
            raise RuntimeError("KHALA_SECRET_KEY is not set")
        self.fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))

    def seal(self, text: str) -> str:
        return self.fernet.encrypt(text.encode()).decode()

    def open(self, token: str) -> str:
        try:
            return self.fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            raise AuthError("stored secret cannot be decrypted; was KHALA_SECRET_KEY changed?")


# ---------- TOTP (RFC 6238: SHA-1, 30 seconds, 6 digits) ----------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 10 ** TOTP_DIGITS
    return "%0*d" % (TOTP_DIGITS, n)


def totp_match(secret: str, code: str, last_step: int, now=None):
    """Allows one time step either side. Returns the matched step; a step already used, or an earlier one, is
    refused (replay protection)."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return None
    current = int((now or time.time()) // TOTP_STEP)
    for step in (current - 1, current, current + 1):
        if step > last_step and hmac.compare_digest(totp_at(secret, step), code):
            return step
    return None


def otpauth_uri(secret, account_email, issuer_name):
    label = quote("%s:%s" % (issuer_name, account_email))
    return "otpauth://totp/%s?secret=%s&issuer=%s&algorithm=SHA1&digits=6&period=30" % (
        label, secret, quote(issuer_name))


def qr_svg(uri) -> str:
    """Render the QR code as SVG on the server and inline it in the page, with no scripts or external resources."""
    return segno.make(uri, error="m").svg_inline(scale=5, dark="#000", light="#fff", border=2)


# ---------- recovery codes ----------
def new_recovery_codes():
    out = []
    for _ in range(RECOVERY_COUNT):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10))
        out.append("%s-%s" % (raw[:5], raw[5:]))
    return out


def normalize_recovery(code):
    return (code or "").strip().lower().replace(" ", "").replace("-", "")


# ---------- data access ----------
class Factors:
    def __init__(self, db, box: SecretBox, issuer: str, name="Khala"):
        self.db, self.box, self.name = db, box, name
        u = urlparse(issuer)
        self.rp_id = u.hostname
        self.origin = "%s://%s" % (u.scheme, u.netloc)

    # status
    def passkeys(self, account_id):
        return self.db.q("SELECT * FROM passkeys WHERE account_id=? ORDER BY created_at", account_id)

    def totp_row(self, account_id):
        return self.db.one("SELECT * FROM totp WHERE account_id=?", account_id)

    def has_totp(self, account_id):
        row = self.totp_row(account_id)
        return bool(row and row["confirmed_at"])

    def has_strong(self, account_id):
        return bool(self.passkeys(account_id)) or self.has_totp(account_id)

    def recovery_left(self, account_id):
        return self.db.one("SELECT COUNT(*) n FROM recovery_codes WHERE account_id=? AND used_at IS NULL",
                           account_id)["n"]

    # TOTP
    def start_totp(self, account_id):
        secret = new_totp_secret()
        self.db.q("INSERT OR REPLACE INTO totp(account_id, secret, confirmed_at, last_step, created_at) "
                  "VALUES (?,?,NULL,0,?)", account_id, self.box.seal(secret), time.time())
        return secret

    def pending_totp_secret(self, account_id):
        row = self.totp_row(account_id)
        if not row or row["confirmed_at"]:
            return None
        return self.box.open(row["secret"])

    def confirm_totp(self, account_id, code):
        row = self.totp_row(account_id)
        if not row or row["confirmed_at"]:
            return False
        step = totp_match(self.box.open(row["secret"]), code, row["last_step"])
        if step is None:
            return False
        self.db.q("UPDATE totp SET confirmed_at=?, last_step=? WHERE account_id=?", time.time(), step, account_id)
        return True

    def check_totp(self, account_id, code):
        row = self.totp_row(account_id)
        if not row or not row["confirmed_at"]:
            return False
        step = totp_match(self.box.open(row["secret"]), code, row["last_step"])
        if step is None:
            return False
        # conditional update: if two requests use the same code at once, only one can advance last_step
        cur = self.db.q("UPDATE totp SET last_step=? WHERE account_id=? AND last_step<? RETURNING 1",
                        step, account_id, step)
        return bool(cur)

    def remove_totp(self, account_id):
        self.db.q("DELETE FROM totp WHERE account_id=?", account_id)

    # recovery codes
    def new_recovery(self, account_id):
        codes = new_recovery_codes()
        with self.db.tx():
            self.db.conn.execute("DELETE FROM recovery_codes WHERE account_id=?", (account_id,))
            for c in codes:
                self.db.conn.execute("INSERT INTO recovery_codes(account_id, code_hash) VALUES (?,?)",
                                     (account_id, digest(normalize_recovery(c))))
        return codes

    def use_recovery(self, account_id, code):
        h = digest(normalize_recovery(code))
        cur = self.db.q("UPDATE recovery_codes SET used_at=? WHERE account_id=? AND code_hash=? AND used_at IS NULL "
                        "RETURNING 1", time.time(), account_id, h)
        return bool(cur)

    def reset(self, account_id):
        """Server CLI fallback: clears all strong factors and recovery codes."""
        with self.db.tx():
            for t in ("passkeys", "totp", "recovery_codes"):
                self.db.conn.execute("DELETE FROM %s WHERE account_id=?" % t, (account_id,))

    # ---------- WebAuthn ----------
    def _save_challenge(self, key, challenge, purpose, account_id=None):
        self.db.q("INSERT OR REPLACE INTO challenges(key, challenge, purpose, account_id, expires) VALUES (?,?,?,?,?)",
                  key, bytes_to_base64url(challenge), purpose, account_id, time.time() + CHALLENGE_TTL)

    def _take_challenge(self, key, purpose):
        # single use, void after success or failure: taking and deleting is one statement
        rows = self.db.q("DELETE FROM challenges WHERE key=? RETURNING *", key)
        row = rows[0] if rows and rows[0]["purpose"] == purpose and rows[0]["expires"] > time.time() else None
        if not row:
            raise AuthError("the passkey request expired; try again")
        return webauthn.base64url_to_bytes(row["challenge"]), row["account_id"]

    def registration_options(self, key, account):
        existing = [PublicKeyCredentialDescriptor(id=webauthn.base64url_to_bytes(p["credential_id"]))
                    for p in self.passkeys(account["id"])]
        opts = webauthn.generate_registration_options(
            rp_id=self.rp_id, rp_name=self.name, user_id=str(account["id"]).encode(), user_name=account["email"],
            user_display_name=account["name"], exclude_credentials=existing,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED))
        self._save_challenge(key, opts.challenge, "register", account["id"])
        return json.loads(webauthn.options_to_json(opts))

    def register(self, key, account, credential, name):
        challenge, account_id = self._take_challenge(key, "register")
        if account_id != account["id"]:
            raise AuthError("this passkey request belongs to another account")
        try:
            v = webauthn.verify_registration_response(credential=credential, expected_challenge=challenge,
                                                      expected_rp_id=self.rp_id, expected_origin=self.origin,
                                                      require_user_verification=True)
        except Exception as exc:
            raise AuthError("the passkey could not be verified (%s)" % type(exc).__name__)
        cred_id = bytes_to_base64url(v.credential_id)
        if self.db.one("SELECT 1 FROM passkeys WHERE credential_id=?", cred_id):
            raise AuthError("this passkey is already registered")
        transports = ",".join((credential.get("response") or {}).get("transports") or []) if isinstance(credential, dict) else ""
        cur = None
        with self.db._lock:
            cur = self.db.conn.execute(
                "INSERT INTO passkeys(account_id, credential_id, public_key, sign_count, name, transports, created_at) "
                "VALUES (?,?,?,?,?,?,?)", (account["id"], cred_id, v.credential_public_key, v.sign_count,
                                           (name or "Passkey").strip()[:60] or "Passkey", transports[:100], time.time()))
        return cur.lastrowid

    def login_options(self, key, account_id=None):
        """account_id=None: discoverable credentials; the browser lists usable passkeys, no need to know who first."""
        allow = None
        if account_id is not None:
            allow = [PublicKeyCredentialDescriptor(id=webauthn.base64url_to_bytes(p["credential_id"]))
                     for p in self.passkeys(account_id)]
        opts = webauthn.generate_authentication_options(rp_id=self.rp_id, allow_credentials=allow,
                                                        user_verification=UserVerificationRequirement.REQUIRED)
        self._save_challenge(key, opts.challenge, "login", account_id)
        return json.loads(webauthn.options_to_json(opts))

    def authenticate(self, key, credential):
        """Verify one passkey assertion, return (account id, passkey row)."""
        challenge, expected_account = self._take_challenge(key, "login")
        cred_id = credential.get("id") if isinstance(credential, dict) else None
        row = self.db.one("SELECT * FROM passkeys WHERE credential_id=?", cred_id or "")
        if not row or (expected_account is not None and row["account_id"] != expected_account):
            raise AuthError("that passkey is not registered here")
        try:
            v = webauthn.verify_authentication_response(
                credential=credential, expected_challenge=challenge, expected_rp_id=self.rp_id,
                expected_origin=self.origin, credential_public_key=row["public_key"],
                credential_current_sign_count=row["sign_count"], require_user_verification=True)
        except Exception as exc:
            raise AuthError("the passkey could not be verified (%s)" % type(exc).__name__)
        self.db.q("UPDATE passkeys SET sign_count=?, last_used_at=? WHERE id=?", v.new_sign_count, time.time(), row["id"])
        return row["account_id"], row
