#!/bin/sh
# Deploy the current commit to a server: back up the state database, install into the existing venv (with
# dependencies), run the tests there, install the systemd units and the pre-receive hook, restart, then check
# the public endpoints.
#   cp deploy/local.env.example deploy/local.env   # once, then fill it in
#   sh deploy/deploy.sh
# The server half is deploy/remote.sh; the layout (directories, service and user names) comes from local.env.
# The first start of a new version migrates the state database in place; the copy from before stays in the
# backups directory next to the database.
set -eu
here=$(dirname "$0")
[ -f "$here/local.env" ] || { echo "missing deploy/local.env; copy deploy/local.env.example and fill it in"; exit 1; }
. "$here/local.env"
: "${DEPLOY_HOST:?set DEPLOY_HOST in deploy/local.env}" "${DEPLOY_SSH_KEY:?}" "${DEPLOY_URL:?}"
: "${DEPLOY_ROOT:=/srv/khala}" "${DEPLOY_ETC:=/etc/khala}" "${DEPLOY_SERVICE:=khala}" "${DEPLOY_USER:=khala}"
: "${DEPLOY_PORT:=8100}" "${DEPLOY_GIT_USER:=}"
for v in "$DEPLOY_ROOT" "$DEPLOY_ETC" "$DEPLOY_SERVICE" "$DEPLOY_USER" "$DEPLOY_PORT" "$DEPLOY_GIT_USER"; do
  case $v in *[!A-Za-z0-9/_.-]*) echo "deploy/local.env: '$v' may only use letters, digits and / _ . -"; exit 1;; esac
done
SSH_OPTS="-i $DEPLOY_SSH_KEY -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes"
git diff --quiet && git diff --cached --quiet || { echo "commit first"; exit 1; }
tmpdir=$(mktemp -d)
tarball=$tmpdir/khala.tgz
git archive --format=tar.gz -o "$tarball" HEAD
scp -q $SSH_OPTS "$tarball" "$DEPLOY_HOST:/tmp/khala.tgz"
rm -r "$tmpdir"
ssh $SSH_OPTS "$DEPLOY_HOST" "set -e; sudo rm -rf /tmp/khala-src; mkdir /tmp/khala-src
tar xzf /tmp/khala.tgz -C /tmp/khala-src; rm /tmp/khala.tgz
ROOT=$DEPLOY_ROOT ETC=$DEPLOY_ETC SERVICE=$DEPLOY_SERVICE SVC_USER=$DEPLOY_USER GIT_USER=$DEPLOY_GIT_USER \
PORT=$DEPLOY_PORT sh /tmp/khala-src/deploy/remote.sh"
curl -fsS -o /dev/null -w "health %{http_code}\n" "$DEPLOY_URL/health"
curl -fsS -o /dev/null -w "web login %{http_code}\n" "$DEPLOY_URL/app/login"
curl -fsS -o /dev/null -w "oauth metadata %{http_code}\n" "$DEPLOY_URL/.well-known/oauth-authorization-server"
