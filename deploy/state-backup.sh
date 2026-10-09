#!/bin/sh
# Once a day, snapshot what a full restore needs but Git does not hold: the state database (accounts, grants,
# agents, token hashes, note and proposal state), the key that encrypts TOTP secrets, and the pending proposal
# refs. The snapshot is encrypted to an age public key and pushed to a private Git remote.
# The server only has the public key: it can encrypt, not decrypt. Restoring is described in RESTORE.md.
# Settings: $KHALA_ETC/state-backup.env, see state-backup.env.example. The unit passes the layout (KHALA_ROOT,
# KHALA_ETC) and server.env (KHALA_REPO, KHALA_DB, KHALA_SECRET_KEY_FILE).
set -eu
ROOT=${KHALA_ROOT:-/srv/khala}
ETC=${KHALA_ETC:-/etc/khala}
. "$ETC/state-backup.env"
: "${STATE_BACKUP_RECIPIENT:?}" "${STATE_BACKUP_REMOTE:?set STATE_BACKUP_REMOTE in $ETC/state-backup.env}"
DB=${KHALA_DB:-$ROOT/state/khala.db}
KEY=${KHALA_SECRET_KEY_FILE:-$ETC/secret.key}
STATE=$(dirname "$DB")
VAULT=${KHALA_REPO:-$ROOT/vault.git}
CLONE=$STATE/state-backup
export GIT_SSH_COMMAND="ssh -i $STATE/github-state-key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$STATE/github_known_hosts"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# SQLite's backup API gives a consistent snapshot (including what is still in the WAL) while the server runs
"$ROOT/app/.venv/bin/python" -c 'import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()' "$DB" "$WORK/memory.db"
cp "$KEY" "$WORK/secret.key"
git --git-dir="$VAULT" for-each-ref --format='%(objectname) %(refname)' > "$WORK/refs.txt"
files="memory.db secret.key refs.txt"
if git --git-dir="$VAULT" for-each-ref --count=1 refs/khala/proposals | grep -q .; then
  # Only objects that main lacks; on restore, main comes from the record backup
  git --git-dir="$VAULT" bundle create -q "$WORK/proposals.bundle" --glob=refs/khala/proposals \
    --not "refs/heads/${KHALA_BRANCH:-main}"
  files="$files proposals.bundle"
fi
date -u +%Y-%m-%dT%H:%M:%SZ > "$WORK/created"
tar -C "$WORK" -cf - $files created | age -r "$STATE_BACKUP_RECIPIENT" > "$WORK/state.tar.age"

if [ ! -d "$CLONE/.git" ]; then
  git clone -q "$STATE_BACKUP_REMOTE" "$CLONE" 2>/dev/null
fi
cp "$WORK/state.tar.age" "$CLONE/state.tar.age"
cd "$CLONE"
git add state.tar.age
git -c user.name="Khala server" -c user.email="khala@localhost" -c commit.gpgsign=false \
  commit -q -m "State snapshot $(cat "$WORK/created")"
git push -q origin HEAD:main                   # never forced: a diverged remote fails and waits for a person
echo "$(date +%s) $(git rev-parse HEAD)" > "$STATE/state-backup-last"
