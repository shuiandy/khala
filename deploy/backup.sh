#!/bin/sh
# Push the records (the main branch) and the inbox notes (refs/khala/inbox, pushed as the branch inbox) to a
# private Git remote as a backup. Never force-pushes:
# if the remote has diverged this fails and waits for a person. Each ref remembers the last commit it pushed
# and is skipped when unchanged. Proposal refs stay on the server (approved changes are already on main).
# Settings: $KHALA_ETC/backup.env, see backup.env.example. The unit passes the layout (KHALA_ROOT, KHALA_ETC) and
# server.env (KHALA_REPO, KHALA_DB).
set -eu
ROOT=${KHALA_ROOT:-/srv/khala}
ETC=${KHALA_ETC:-/etc/khala}
. "$ETC/backup.env"
: "${BACKUP_REMOTE:?set BACKUP_REMOTE in $ETC/backup.env}"
STATE=$(dirname "${KHALA_DB:-$ROOT/state/khala.db}")
cd "${KHALA_REPO:-$ROOT/vault.git}"
export GIT_SSH_COMMAND="ssh -i $STATE/github-deploy-key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$STATE/github_known_hosts"
push() {
  ref=$1 dest=$2 mark=$3
  head=$(git rev-parse --verify -q "$ref") || return 0
  last=$(cat "$mark" 2>/dev/null || true)
  [ "$head" = "$last" ] && return 0
  git push -q "$BACKUP_REMOTE" "$ref:$dest"
  echo "$head" > "$mark"
}
branch=refs/heads/${KHALA_BRANCH:-main}
push "$branch" "$branch" "$STATE/backup-last"
push refs/khala/inbox refs/heads/inbox "$STATE/backup-inbox-last"
