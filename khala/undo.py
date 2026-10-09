"""Undo: a single commit, or all of an agent's changes over a period (to clean up after a runaway or injected bot).

There is one rule: the record's current version must be exactly the result of the change(s) being undone, with no
one else's change in between. Otherwise it is skipped and listed, never overwriting later work by others. An undo is
a new commit; history is not rewritten.
"""
import json
import time

from . import rules
from .store import Conflict

WINDOWS = {"1": 86400, "7": 7 * 86400, "30": 30 * 86400, "all": 3650 * 86400}


class Plan:
    def __init__(self):
        self.items = []          # {"name", "action": restore|remove|skip, "why", "expected", "before", "scope"}

    @property
    def doable(self):
        return [i for i in self.items if i["action"] != "skip"]


def agent_plan(db, store, memory, me, agent_id, since):
    """The agent's own commits, plus commits consolidated from its notes."""
    notes = {r["id"] for r in db.q("SELECT id FROM notes WHERE agent_id=?", agent_id)}

    def mine(c):
        return c["agent_id"] == str(agent_id) or bool(notes.intersection(c["notes"]))

    touched = {}
    for c in store.recent(since, limit=5000):              # newest first
        if mine(c):
            for name in c["files"]:
                touched.setdefault(name, []).append(c)
    plan = Plan()
    records = memory.records()
    for name in sorted(touched):
        commits = touched[name]
        newest, oldest = commits[0], commits[-1]
        own = {c["sha"] for c in commits}
        item = {"name": name, "expected": store.blob_at(newest["sha"], name),
                "before": store.blob_at(oldest["sha"] + "^", name), "changes": len(commits)}
        cur = records.get(name)
        scope = cur.meta["scope"] if cur else None
        item["scope"] = scope
        between = []
        for c in store.history(name, limit=500):
            between.append(c["sha"])
            if c["sha"] == oldest["sha"]:
                break
        if (cur.sha if cur else None) != item["expected"]:
            item.update(action="skip", why="changed after this agent's last edit")
        elif any(sha not in own for sha in between):
            item.update(action="skip", why="someone else edited it in between")
        elif cur is not None and cur.meta.get("pinned") == "true":
            item.update(action="skip", why="pinned; edit it by hand in a clone of the repository")
        elif not writable(me, store, cur, item["before"]):
            item.update(action="skip", why="you cannot write to its scope")
        else:
            item["action"] = "restore" if item["before"] else "remove"
        plan.items.append(item)
    return plan, notes


def writable(me, store, cur, before_blob):
    from_scope = cur.meta["scope"] if cur else None
    to_scope = None
    if before_blob:
        to_scope = rules.meta(store.blob_text(before_blob) or "")["scope"]
    return all(me.can_write(s) for s in {from_scope, to_scope} - {None, ""})


def apply(store, me, item, message, trailers):
    """Put a record back to its before version (or delete it). Give up on it if it changed again in the meantime."""
    text = store.blob_text(item["before"]) if item["before"] else None

    def prepare(existing):
        if (existing.sha if existing else None) != item["expected"]:
            raise Conflict("changed")
        return text

    try:
        store.write(item["name"], prepare, me.name, me.email, message, agent=("web", None), trailers=trailers)
        return True
    except Conflict:
        return False


def commit_item(store, memory, me, sha, name):
    """Undo one commit's change to a record: the record must still be at that commit's result."""
    cur = memory.records().get(name)
    item = {"name": name, "expected": store.blob_at(sha, name), "before": store.blob_at(sha + "^", name)}
    if (cur.sha if cur else None) != item["expected"]:
        return item, "the record changed after this commit; undo the later change first"
    if cur is not None and cur.meta.get("pinned") == "true":
        return item, "pinned records are edited by hand in a clone of the repository"
    if item["expected"] == item["before"]:
        return item, "this commit did not change the record"
    if not writable(me, store, cur, item["before"]):
        return item, "you cannot write to this record's scope"
    return item, None


def reject_open(db, inbox, me, agent_id, notes):
    """Also reject its open proposals and discard its unprocessed notes."""
    rejected = 0
    for prop in db.q("SELECT * FROM proposals WHERE state='open'"):
        if prop["agent_id"] == agent_id or notes.intersection(json.loads(prop["note_ids"])):
            inbox.reject(me, prop, "undone together with the agent's changes")
            rejected += 1
    dropped = db.q("UPDATE notes SET state='done', outcome='discarded', detail='removed with the agent''s changes', "
                   "done_at=?, claim_agent=NULL, claim_until=NULL WHERE agent_id=? AND state='new' RETURNING id",
                   time.time(), agent_id)
    return rejected, len(dropped)
