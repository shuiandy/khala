"""Write rules. Enforced on the server, not left to clients; mirror clients that sync over Git should apply them too."""
import datetime
import re

import yaml

from . import clock

# Records are root-level .md files starting lowercase; PROTOCOL.md, AGENTS.md, docs/ are instructions, not writable here
RECORD = re.compile(r"[a-z0-9][A-Za-z0-9_.-]*\.md")
SCOPE = re.compile(r"[a-z0-9][a-z0-9_-]*")
STATUSES = {"active", "outdated", "proposed"}
MAX_BYTES = 64 * 1024
SECRET = re.compile(
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|"
    r"sk-[A-Za-z0-9_-]{25,}|xox[bpas]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})\b|"
    r"https?://[^\s/:@]+:[^\s/@]+@")
FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", re.S)


class RuleError(Exception):
    pass


TOP_KEYS = ("name", "description")
META_KEYS = ("scope", "status", "pinned", "proposed_at", "verified", "review_after", "type", "expires_at",
             "sensitivity")
ENFORCED = ("scope", "status", "pinned", "proposed_at")      # the keys access and review decisions read


def _scalar(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value) if isinstance(value, (str, int, float)) else ""


def line_meta(text):
    """The line reader mirror tools use: name and description at the top, the rest indented two spaces under
    metadata, one plain value per line."""
    m = FRONT.match(text or "")
    front = m.group(1) if m else ""
    out = {"_frontmatter": bool(m)}
    for key in TOP_KEYS:
        f = re.search(r"^%s:\s*(.*?)\s*$" % key, front, re.M)
        out[key] = f.group(1).strip("\"'") if f else ""
    for key in META_KEYS:
        f = re.search(r"^  %s:\s*(.*?)\s*$" % key, front, re.M)
        out[key] = f.group(1).strip("\"'") if f else ""
    return out


def yaml_meta(text):
    """The same fields read as YAML, or None when the frontmatter is missing or is not a YAML mapping."""
    m = FRONT.match(text or "")
    if not m:
        return None
    try:
        doc = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    md = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    out = {"_frontmatter": True}
    out.update({k: _scalar(doc.get(k)) for k in TOP_KEYS})
    out.update({k: _scalar(md.get(k)) for k in META_KEYS})
    return out


def meta(text):
    """Frontmatter fields as strings. Read as YAML (docs/record-format.md); frontmatter that is not valid YAML falls
    back to the line reader, so older hand-written records keep their meaning."""
    return yaml_meta(text) or line_meta(text)


def ambiguities(text):
    """Keys that YAML and the line reader read differently. A record written through the service must have none, so
    the server and mirror tools that read lines never disagree about it."""
    y = yaml_meta(text)
    if y is None:
        return []
    lines = line_meta(text)
    return [k for k in TOP_KEYS + META_KEYS if y[k] != lines[k]]


def _ambiguity_hint(text, keys):
    hint = ("write metadata as a block with one key per line, indented two spaces, and quote values that "
            "contain ' #' or ': '")
    return "frontmatter reads differently as YAML and line by line (%s); %s" % (", ".join(keys), hint)


def review_due(m, today=None):
    """Due for review when verified + review_after (e.g. 90d) is before today. Malformed values do not count."""
    today = today or clock.today()
    try:
        verified = datetime.date.fromisoformat(m.get("verified", ""))
        days = int(re.fullmatch(r"(\d+)d", m.get("review_after", "")).group(1))
    except (ValueError, AttributeError):
        return False
    return verified + datetime.timedelta(days=days) < today


def _set_status_proposed(text, today):
    text = re.sub(r"^(  status:)[ \t]*\S*[ \t]*$", r"\1 proposed", text, count=1, flags=re.M)
    if not re.search(r"^  proposed_at:", text, re.M):
        text = re.sub(r"^(  status: proposed)$", r"\1\n  proposed_at: " + today, text, count=1, flags=re.M)
    # the rewrite works on lines; if the layout hid the status from it, refuse rather than let the record through
    after = meta(text)
    if after["status"] != "proposed" or not after["proposed_at"]:
        raise RuleError("could not set status: proposed; " + _ambiguity_hint(text, ["status"]).split("; ", 1)[1])
    return text


def check_write(text, existing_text, can_write, is_auto_load, today=None):
    """Returns (final text, list of hints). can_write(scope) says whether the caller can write a scope,
    is_auto_load(scope) whether it is an auto-loaded scope (called global in old databases)."""
    today = today or clock.today().isoformat()
    notes = []
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise RuleError("record is larger than 64 KB")
    if SECRET.search(text):
        raise RuleError("content looks like it contains a credential; remove it")
    new = meta(text)
    if not new["_frontmatter"]:
        raise RuleError("record must start with YAML frontmatter (--- ... ---)")
    if yaml_meta(text) is None:
        raise RuleError("frontmatter is not valid YAML; quote values that contain ': ', ' #' or backslashes")
    unclear = ambiguities(text)
    if unclear:
        raise RuleError(_ambiguity_hint(text, unclear))
    if not SCOPE.fullmatch(new["scope"]):
        raise RuleError("metadata.scope is missing or invalid")
    if new["status"] not in STATUSES:
        raise RuleError("metadata.status must be one of active, outdated, proposed")
    if not can_write(new["scope"]):
        raise RuleError("you cannot write to scope '%s'" % new["scope"])
    if new["pinned"] == "true":
        raise RuleError("pinned records are set by a person, not through this service")
    auto = is_auto_load(new["scope"])
    if existing_text is not None:
        old = meta(existing_text)
        if old["pinned"] == "true":
            raise RuleError("this record is pinned; only the owner can edit it by hand")
        if old["scope"] != new["scope"] and not can_write(old["scope"]):
            raise RuleError("moving a record needs write access to both scopes")
        if auto and old["scope"] != new["scope"] and new["status"] == "active":
            # moving into an auto-loaded scope counts as creating: proposed until the owner approves, and
            # "create it active elsewhere, then move it in" cannot get around that
            text = _set_status_proposed(text, today)
            notes.append("moved into an auto-loaded scope; status set to proposed until its owner approves it")
        elif auto and old["status"] == "proposed" and new["status"] == "active":
            raise RuleError("approving a proposed record in an auto-loaded scope is done by its owner, "
                            "not through this service")
        elif auto and old["status"] != "active" and new["status"] == "active":
            raise RuleError("records in an auto-loaded scope cannot be activated through this service")
    elif auto and new["status"] != "proposed":
        text = _set_status_proposed(text, today)
        notes.append("new records in an auto-loaded scope start as proposed; status set to proposed")
    if auto and meta(text)["status"] == "proposed" and not meta(text)["proposed_at"]:
        text = _set_status_proposed(text, today)
    return text, notes


def activate(text):
    """A person approved a proposed record: status becomes active. proposed_at stays, as approving on a mirror
    leaves it. Only for decisions a person confirmed; agents never activate records in an auto-loaded scope."""
    text = re.sub(r"^(  status:)[ \t]*\S*[ \t]*$", r"\1 active", text, count=1, flags=re.M)
    if meta(text)["status"] != "active" or ambiguities(text):
        raise RuleError("could not set status: active; " + _ambiguity_hint(text, ["status"]).split("; ", 1)[1])
    return text


def deprecate(text, reason, today=None):
    """Mark a record outdated with a reason (expired_reason, the same field mirror clients use for auto-expiry)."""
    today = today or clock.today().isoformat()
    reason = re.sub(r"\s+", " ", reason or "").strip()[:300] or "marked outdated"
    if not re.search(r"^  status:", text, re.M):
        raise RuleError("record has no metadata.status line")
    text = re.sub(r"^(  status:)[ \t]*\S*[ \t]*$", r"\1 outdated", text, count=1, flags=re.M)
    # single-quoted YAML: the reason may contain ': ' or ' #', and the only escape is a doubled quote
    line = "  expired_reason: '%s (%s)'" % (reason.replace("\\", "/").replace("'", "''"), today)
    if re.search(r"^  expired_reason:", text, re.M):
        text = re.sub(r"^  expired_reason:.*$", lambda m: line, text, count=1, flags=re.M)
    else:
        text = re.sub(r"^(  status: outdated)$", lambda m: m.group(1) + "\n" + line, text, count=1, flags=re.M)
    if meta(text)["status"] != "outdated":
        raise RuleError("could not mark the record outdated; " + _ambiguity_hint(text, ["status"]).split("; ", 1)[1])
    return text

