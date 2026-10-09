"""Permission checks, shared by the web UI, MCP and the command line.

What a call may do = account role on the scope ∩ agent ceiling ∩ server hard rules, whichever is strictest.
Every request queries the database again, so changed grants and revoked agents take effect on the next request.
"""
import json

from .db import ROLES

RANK = {"viewer": 1, "editor": 2, "maintainer": 3, "owner": 4}


class Denied(Exception):
    pass


class Principal:
    """An account, optionally acting through an agent. Web sessions have no agent and use the account's own rights."""

    def __init__(self, db, account, agent=None):
        self.db = db
        self.account = account
        self.agent = agent
        self.id = account["id"]
        self.email = account["email"]
        self.name = account["name"]
        self.is_admin = bool(account["is_admin"])
        self._scopes = {r["id"]: r for r in db.all_scopes()}
        self._grants = db.grants_for_account(self.id)
        raw = agent["ceiling"] if agent is not None else None
        self.ceiling = json.loads(raw) if raw else None        # None = inherits all of the account's rights

    @property
    def agent_id(self):
        return self.agent["id"] if self.agent is not None else None

    @property
    def agent_label(self):
        return self.agent["name"] if self.agent is not None else "web"

    def scope(self, scope_id):
        return self._scopes.get(scope_id)

    def role(self, scope_id):
        """The account's own role, ignoring the agent ceiling."""
        s = self._scopes.get(scope_id)
        if s is None:
            return None
        if s["owner_id"] == self.id:
            return "owner"
        if s["auto_load"]:                      # auto-loaded scopes are never shared, whatever grants exist
            return None
        return self._grants.get(scope_id)

    def _ceiling_mode(self, scope_id):
        if self.ceiling is None:
            return "rw"
        return self.ceiling.get(scope_id)

    def can_read(self, scope_id):
        s = self._scopes.get(scope_id)
        if s is None or s["archived_at"]:
            return False
        return self.role(scope_id) is not None and self._ceiling_mode(scope_id) in ("r", "rw")

    def can_write(self, scope_id):
        s = self._scopes.get(scope_id)
        if s is None or s["archived_at"]:
            return False
        return RANK.get(self.role(scope_id), 0) >= RANK["editor"] and self._ceiling_mode(scope_id) == "rw"

    def at_least(self, scope_id, role):
        """Admin actions in the web UI: only the account role counts (web sessions have no agent ceiling)."""
        return RANK.get(self.role(scope_id), 0) >= RANK[role]

    def is_auto_load(self, scope_id):
        s = self._scopes.get(scope_id)
        return bool(s and s["auto_load"])

    def readable_scopes(self):
        return sorted(s for s in self._scopes if self.can_read(s))

    def visible_scopes(self):
        """Scopes listed in the web UI: those I own (including archived) and those others share with me."""
        out = []
        for sid, s in sorted(self._scopes.items()):
            role = self.role(sid)
            if role is not None:
                out.append((s, role))
        return out

    def may_create_scope(self):
        """A restricted agent cannot create new scopes on the side; inheriting agents and the web UI can."""
        return self.ceiling is None


def check_role(role):
    if role not in ROLES:
        raise Denied("unknown role")
