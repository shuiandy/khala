"""Read and write the bare repository directly, without a working tree.

Reads: each request first resolves main's current commit, then loads the root records from it, cached by commit SHA.
Writes: hash-object → temporary index → write-tree → commit-tree → update-ref (with the old value, CAS).
Two more ref kinds: refs/khala/inbox holds notes (append-only); refs/khala/proposals/<id> holds changes awaiting
approval. Neither is a branch, so a clone of the repository only sees the main branch.
When a mirror push and a server write run concurrently, only one update-ref succeeds; the other rereads the latest
content and validates again, so no lock is needed and nothing is ever half-written.
"""
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import rules

INBOX = "refs/khala/inbox"           # notes; mirrors sync only the main branch and never see them
PROPOSALS = "refs/khala/proposals/"
OLD_INBOX, OLD_PROPOSALS = "refs/heads/inbox", "refs/proposals/"     # the names before 0.3; migrate_refs moves them
ZERO = "0" * 40
RAW = re.compile(r"^:\d+ \d+ ([0-9a-f]{40}) ([0-9a-f]{40}) [A-Z]\t(.+)$")   # :srcmode dstmode srcsha dstsha status\tpath


class StoreError(Exception):
    pass


class Conflict(StoreError):
    """expected_sha does not match the current version in the repository."""


# Commit log format: SHA, time, author, email, subject, plus the trailers the service adds when it writes
LOG = ("%H%x1f%at%x1f%an%x1f%ae%x1f%s%x1f%(trailers:key=Agent,valueonly,separator=)%x1f"
       "%(trailers:key=Agent-Id,valueonly,separator=)%x1f%(trailers:key=Notes,valueonly,separator=)%x1f"
       "%(trailers:key=Reason,valueonly,separator=)%x1f%(trailers:key=Proposal,valueonly,separator=)")


def _commit(chunk):
    parts = (chunk.strip("\n").split("\x1f") + [""] * 10)[:10]
    sha, at, an, ae, subject, agent, agent_id, notes, reason, proposal = parts
    return {"sha": sha.strip(), "at": int(at or 0), "author": an, "email": ae, "subject": subject,
            "agent": agent.strip(), "agent_id": agent_id.strip(), "notes": notes.split(), "reason": reason.strip(),
            "proposal": proposal.strip()}


@dataclass(frozen=True)
class Record:
    name: str
    sha: str
    text: str
    meta: dict


class Store:
    def __init__(self, repo: Path, branch="main"):
        self.repo = Path(repo)
        self.branch, self.ref = branch, "refs/heads/" + branch
        self._cache = (None, {})

    def _git(self, *args, input=None, env=None, check=True):
        r = subprocess.run(["git", "--git-dir", str(self.repo)] + list(args), input=input,
                           capture_output=True, env=env, timeout=30)
        if check and r.returncode:
            raise StoreError("git %s failed: %s" % (args[0], r.stderr.decode(errors="replace").strip()[-300:]))
        return r

    def ensure(self):
        """Create an empty bare repository when there is none yet (a fresh install)."""
        if not self.repo.exists():
            self.repo.parent.mkdir(parents=True, exist_ok=True)
            # built aside and renamed into place, so workers starting together cannot see or make a half repository
            tmp = self.repo.parent / (".init-%s" % secrets.token_hex(6))
            os.mkdir(tmp)                           # unlike mkdtemp this follows the umask, so group sharing works
            subprocess.run(["git", "init", "-q", "--bare", "-b", self.branch, tmp],
                           check=True, capture_output=True, timeout=30)
            try:
                os.rename(tmp, self.repo)
            except OSError:
                shutil.rmtree(tmp, ignore_errors=True)
                if not (self.repo / "HEAD").exists():
                    raise

    def migrate_refs(self):
        """Move notes and proposals from their names before 0.3 to refs/khala/*, all in one update-ref transaction,
        so they move together or not at all. A name that already exists under refs/khala is left alone. Safe to run
        from several workers at once: a transaction that loses the race fails whole. Returns the old names moved."""
        listed = self._git("for-each-ref", "--format=%(objectname) %(refname)", OLD_INBOX, OLD_PROPOSALS)
        ops, moved = [], []
        for line in filter(None, listed.stdout.decode().splitlines()):
            sha, old = line.split(" ", 1)
            if old == OLD_INBOX and self.branch == "inbox":
                continue                                    # that is this instance's main branch, not notes
            new = INBOX if old == OLD_INBOX else PROPOSALS + old[len(OLD_PROPOSALS):]
            if self.ref_head(new):
                continue
            ops += ["create %s %s" % (new, sha), "delete %s %s" % (old, sha)]
            moved.append(old)
        if ops and self._git("update-ref", "--stdin", input=("\n".join(ops) + "\n").encode(), check=False).returncode:
            return []                                       # another worker moved them first
        return moved

    def head(self) -> str:
        """The commit main points at, or "" while the repository has no commits yet."""
        return self.ref_head(self.ref) or ""

    def records(self) -> dict:
        """All records at the root of the current main, {name: Record}, cached per commit."""
        head = self.head()
        if self._cache[0] == head:
            return self._cache[1]
        out = {}
        if not head:
            self._cache = (head, out)
            return out
        entries = []
        for line in self._git("ls-tree", "-z", head).stdout.decode().split("\0"):
            if not line:
                continue
            info, name = line.split("\t", 1)
            mode, kind, sha = info.split()
            if mode == "100644" and kind == "blob" and rules.RECORD.fullmatch(name):
                entries.append((name, sha))
        if entries:
            batch = self._git("cat-file", "--batch", input="".join(s + "\n" for _, s in entries).encode()).stdout
            pos = 0
            for name, sha in entries:
                nl = batch.index(b"\n", pos)
                size = int(batch[pos:nl].split()[2])
                body = batch[nl + 1:nl + 1 + size].decode("utf-8", errors="replace")
                pos = nl + 1 + size + 1
                out[name] = Record(name, sha, body, rules.meta(body))
        self._cache = (head, out)
        return out

    def _log(self, args):
        """git log --raw: each commit with the records it changed and their before/after blobs (None: side absent)."""
        out = self._git("log", "--raw", "--no-abbrev", "--no-renames", "--format=%x1e" + LOG, *args).stdout.decode(
            errors="replace")
        commits = []
        for chunk in out.split("\x1e"):
            if not chunk.strip():
                continue
            head, _, rest = chunk.partition("\n")
            c = _commit(head)
            c["blobs"] = {}
            for line in rest.splitlines():
                m = RAW.match(line)
                if m and rules.RECORD.fullmatch(m.group(3)):
                    old, new = m.group(1), m.group(2)
                    c["blobs"][m.group(3)] = (None if old == ZERO else old, None if new == ZERO else new)
            c["files"] = list(c["blobs"])
            commits.append(c)
        return commits

    def history(self, name, limit=30):
        """Change history of one record, newest first. Each item's blobs[name] holds the two versions before and
        after that commit. Callers must authorize each side by the scope it was in: the same file name may have
        belonged to different scopes, or even different accounts, over time."""
        if not rules.RECORD.fullmatch(name):
            return []
        return self._log(["-n", str(limit), self.ref, "--", name]) if self.head() else []

    def recent(self, since_epoch, limit=200):
        """Commits within a time range and the records they changed (with blobs for both sides)."""
        return self._log(["-n", str(limit), "--since=@%d" % int(since_epoch), self.ref]) if self.head() else []

    def diff(self, commit, name):
        if not re.fullmatch(r"[0-9a-f]{40}", commit or "") or not rules.RECORD.fullmatch(name):
            return ""
        r = self._git("show", "--no-color", "--format=", "--no-ext-diff", commit, "--", name, check=False)
        return r.stdout.decode("utf-8", errors="replace")[:200_000]

    def blob_at(self, commit, name):
        """This record's blob at a commit, or None if it did not exist yet. A trailing ^ on commit means its parent."""
        if not re.fullmatch(r"[0-9a-f]{40}\^?", commit or "") or not rules.RECORD.fullmatch(name):
            return None
        r = self._git("rev-parse", "--verify", "--quiet", "%s:%s" % (commit, name), check=False)
        return r.stdout.decode().strip() or None

    def head_time(self):
        r = self._git("log", "-1", "--format=%ct", self.ref, check=False)
        return int(r.stdout.strip() or 0)

    # ---------- Low level: change the tree, commit, update refs against the old value ----------
    def ref_head(self, ref):
        r = self._git("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
        return r.stdout.decode().strip() or None

    def _blob(self, text):
        return self._git("hash-object", "-w", "--stdin", input=text.encode("utf-8")).stdout.decode().strip()

    def _tree(self, base_commit, changes):
        """Apply {path: blob or None (delete)} to base_commit's tree and return the new tree."""
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
            if base_commit:
                self._git("read-tree", base_commit, env=env)
            for path, blob in changes.items():
                if blob is None:            # bare repo can't use --force-remove; mode 0 in index-info means delete
                    self._git("update-index", "--index-info", input=("0 %s\t%s\n" % (ZERO, path)).encode(), env=env)
                else:
                    self._git("update-index", "--add", "--cacheinfo", "100644,%s,%s" % (blob, path), env=env)
            return self._git("write-tree", env=env).stdout.decode().strip()

    def _commit_tree(self, tree, parent, message, author, committer=None):
        committer = committer or author
        ident = dict(os.environ, GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1],
                     GIT_COMMITTER_NAME=committer[0], GIT_COMMITTER_EMAIL=committer[1])
        args = ["commit-tree", tree] + (["-p", parent] if parent else []) + ["-m", message]
        return self._git(*args, env=ident).stdout.decode().strip()

    def _swap(self, ref, new, old):
        """update-ref with the old value: fails if someone moved it first. old=None means the ref must not exist yet."""
        return self._git("update-ref", ref, new, old or ZERO, check=False).returncode == 0

    def blob_text(self, sha):
        if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            return None
        r = self._git("cat-file", "blob", sha, check=False)
        return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None

    # ---------- Inbox branch: append-only ----------
    def add_note(self, path, text, author, message, attempts=5):
        blob = self._blob(text)
        for _ in range(attempts):
            head = self.ref_head(INBOX)
            commit = self._commit_tree(self._tree(head, {path: blob}), head, message, author)
            if self._swap(INBOX, commit, head):
                return blob, commit
        raise StoreError("inbox kept changing, try again")

    def note_text(self, path):
        r = self._git("cat-file", "blob", "%s:%s" % (INBOX, path), check=False)
        return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None

    # ---------- Proposals: a separate commit based on main, kept out of main until approved ----------
    def make_proposal(self, pid, name, text, author, message):
        if not rules.RECORD.fullmatch(name) or not re.fullmatch(r"[a-z0-9-]{4,40}", pid):
            raise StoreError("invalid proposal")
        blob = self._blob(text)
        head = self.head()
        commit = self._commit_tree(self._tree(head, {name: blob}), head, message, author)
        if not self._swap(PROPOSALS + pid, commit, None):
            raise StoreError("proposal id already used")
        return blob, commit

    def write(self, name, prepare, author_name, author_email, message, attempts=3, agent=None, trailers=None,
              committer=None):
        """prepare(existing: Record | None) -> final text, or None to delete the record; raises if validation fails.
        Retries with the latest content when CAS fails. agent=(name, id) adds Agent:/Agent-Id: commit trailers, so
        the writer stays out of the record body; trailers are extra commit trailers (Notes:, Approved-By: and so on)."""
        if not rules.RECORD.fullmatch(name):
            raise StoreError("invalid record name")
        lines = []
        if agent is not None:
            label = re.sub(r"[\r\n]+", " ", str(agent[0])).strip() or "agent"
            lines += ["Agent: %s" % label, "Agent-Id: %s" % (agent[1] if agent[1] is not None else "-")]
        for k, v in (trailers or {}).items():
            lines.append("%s: %s" % (k, re.sub(r"[\r\n]+", " ", str(v)).strip()))
        if lines:
            message = "%s\n\n%s" % (message, "\n".join(lines))
        for _ in range(attempts):
            head = self.head()
            existing = self.records().get(name)
            text = prepare(existing)
            if text is None:
                if existing is None:
                    return None, head, False
                blob = None
            else:
                blob = self._blob(text)
                if existing is not None and existing.sha == blob:
                    return blob, head, False
            commit = self._commit_tree(self._tree(head, {name: blob}), head, message, (author_name, author_email),
                                       committer)
            if self._swap(self.ref, commit, head):
                return blob, commit, True
        raise StoreError("repository kept changing, try again")
