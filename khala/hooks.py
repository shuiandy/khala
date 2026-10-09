"""pre-receive hook of the record repository: polices commits pushed over Git (a local mirror).

The hook runs as the pushing user (gitsync) and cannot read the state database; permissions come from the
push-policy.json the service writes into the bare repository. Mirror pushes are also a human path
(approving candidates, hand-editing pinned records, deleting files on reject), so not all of the MCP hard rules apply here:

- Always blocked: pushing refs other than the main branch (refs/khala/* is written only by the
  service, otherwise "nothing enters main before approval" means nothing), records that look like they hold
  credentials, records over 64 KB. A mirror's sync client should refuse to push
  the last two as well, so the two sides agree.
- Checked by policy: whether the pusher can write the record's scope (both scopes on a move, the old scope on a
  delete), and whether a record newly created in an auto-loaded scope is proposed. With enforce=false in the
  policy these only warn (logged and printed to the pusher), to be switched on once no false positives show up.

    hooks/pre-receive:  exec /srv/khala/app/.venv/bin/python -I -m khala.hooks pre-receive
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import rules

POLICY = "push-policy.json"
LOG = "push-check.log"
ZERO = "0" * 40


def git(*args, input=None):
    r = subprocess.run(["git"] + list(args), capture_output=True, input=input, timeout=60)
    if r.returncode:
        raise RuntimeError("git %s: %s" % (args[0], r.stderr.decode(errors="replace").strip()))
    return r.stdout


def changed(old, new):
    """List of (status, path), only for record files at the root."""
    if old == ZERO:
        names = git("ls-tree", "--name-only", "-z", new).decode(errors="replace").split("\0")
        return [("A", n) for n in names if n and rules.RECORD.fullmatch(n)]
    out = git("diff-tree", "-r", "--no-renames", "--name-status", "-z", old, new).decode(errors="replace").split("\0")
    pairs = list(zip(out[0::2], out[1::2]))
    return [(st[0], path) for st, path in pairs if path and "/" not in path and rules.RECORD.fullmatch(path)]


def blob(commit, path):
    try:
        return git("cat-file", "blob", "%s:%s" % (commit, path)).decode("utf-8", errors="replace")
    except RuntimeError:
        return None


def introduced(old, new):
    """Every commit this push brings in (not yet in the repository), oldest first."""
    # main only fast-forwards, so it is old..new; a new ref (old all zeros) excludes every commit already present
    span = [new, "--not", "--all"] if old == ZERO else ["%s..%s" % (old, new)]
    return git("rev-list", "--reverse", *span).decode().split()


def hard_problems(commit):
    """Whether records a commit adds or changes against each parent are oversized or look like credentials. A credential
    added and removed by intermediate commits still stays in server history (and is pushed to the GitHub backup), so
    every commit is checked, not just the final diff of the push."""
    out = git("diff-tree", "-r", "-m", "--root", "--no-renames", "--name-status", "-z", "--no-commit-id",
              commit).decode(errors="replace").split("\0")
    problems = []
    for status, path in zip(out[0::2], out[1::2]):
        if not path or "/" in path or not rules.RECORD.fullmatch(path) or status.startswith("D"):
            continue
        text = blob(commit, path) or ""
        if len(text.encode("utf-8")) > rules.MAX_BYTES:
            problems.append("%s: larger than 64 KB (commit %s)" % (path, commit[:10]))
        if rules.SECRET.search(text):
            problems.append("%s: looks like it contains a credential (commit %s)" % (path, commit[:10]))
        unclear = [k for k in rules.ambiguities(text) if k in rules.ENFORCED]
        if unclear:
            # the server and the pusher's tools would disagree about who may see or approve this record
            problems.append("%s: %s reads differently as YAML and line by line; write metadata as a block, two "
                            "spaces deep, one key per line (commit %s)" % (path, ", ".join(unclear), commit[:10]))
    return problems


def check(old, new, policy):
    """Returns (problems that must be rejected, policy problems). Hard checks run per commit, policy checks only on
    the final state after the push."""
    hard, soft = [], []
    for commit in introduced(old, new):
        hard += hard_problems(commit)
    scopes = (policy or {}).get("scopes", {})
    pusher = (policy or {}).get("pusher")

    def can_write(scope):
        s = scopes.get(scope)
        return s is None or pusher in s.get("writers", [])     # the service gives new scopes to the admin

    for status, path in changed(old, new):
        before = blob(old, path) if old != ZERO and status != "A" else None
        after = blob(new, path) if status != "D" else None
        if after is not None:
            unclear = [k for k in rules.ambiguities(after) if k not in rules.ENFORCED]
            if unclear:
                soft.append("%s: %s reads differently as YAML and line by line; quote values that contain ' #' or "
                            "': '" % (path, ", ".join(unclear)))
        if policy is None:
            continue
        old_scope = rules.meta(before)["scope"] if before else None
        new_scope = rules.meta(after)["scope"] if after else None
        for scope in {old_scope, new_scope} - {None, ""}:
            if not can_write(scope):
                soft.append("%s: the pushing account cannot write to scope '%s'" % (path, scope))
        entering = after is not None and (before is None or old_scope != new_scope)
        if entering and scopes.get(new_scope, {}).get("auto_load") and rules.meta(after)["status"] == "active":
            soft.append("%s: a record entering auto-loaded scope '%s' (new or moved in) must start as proposed"
                        % (path, new_scope))
    return hard, soft


def pre_receive(lines, git_dir):
    lines = [line for line in lines if line.strip()]
    if not lines:                       # dry run during deploy: nothing pushed, no warning, no log
        return 0
    policy_path = Path(git_dir) / POLICY
    try:
        policy = json.loads(policy_path.read_text())
    except (OSError, ValueError):
        policy = None
    hard, soft = [], []
    for line in lines:
        parts = line.split()
        if len(parts) != 3:
            continue
        old, new, ref = parts
        branch_ref = "refs/heads/" + ((policy or {}).get("branch") or "main")
        if ref != branch_ref:
            hard.append("%s: only %s may be pushed; the inbox and proposals belong to the server" % (ref, branch_ref))
            continue
        if new == ZERO:
            hard.append("deleting main is not allowed")
            continue
        h, s = check(old, new, policy)
        hard += h
        soft += s
    enforce = bool(policy and policy.get("enforce"))
    if policy is None:
        soft.append("%s is missing; scope permissions were not checked" % POLICY)
    rejected = hard + (soft if enforce else [])
    if hard or soft:
        try:
            with open(Path(git_dir) / LOG, "a") as f:
                f.write(json.dumps({"at": time.time(), "rejected": bool(rejected), "hard": hard, "soft": soft,
                                    "enforce": enforce}, ensure_ascii=False) + "\n")
        except OSError:
            pass
    for msg in hard:
        print("memory: rejected: " + msg, file=sys.stderr)
    for msg in soft:
        print("memory: %s: %s" % ("rejected" if enforce else "warning", msg), file=sys.stderr)
    return 1 if rejected else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] != ["pre-receive"]:
        sys.exit("usage: python -m khala.hooks pre-receive")
    git_dir = os.environ.get("GIT_DIR", ".")
    sys.exit(pre_receive(sys.stdin.read().splitlines(), git_dir))


if __name__ == "__main__":
    main()
