#!/bin/sh
# The server half of deploy.sh, run from the unpacked source in /tmp/khala-src. The layout comes from the
# environment (see local.env.example): ROOT, ETC, SERVICE, SVC_USER, PORT, and GIT_USER for Git pushes.
# Paths the server itself uses (repository, state database, TOTP key) are read from $ETC/server.env, with the
# layout's defaults when it does not set them, so this script and the service always agree.
set -eu
cd /                                    # every path below is absolute; the service user may not read our home
: "${ROOT:?}" "${ETC:?}" "${SERVICE:?}" "${SVC_USER:?}" "${PORT:?}"
GIT_USER=${GIT_USER:-$SVC_USER}
SRC=/tmp/khala-src
APP=$ROOT/app
PY=$APP/.venv/bin/python
setting() { sudo sed -n "s/^$1=//p" "$ETC/server.env" 2>/dev/null | tail -n 1; }
if ! sudo test -s "$ETC/server.env"; then
  echo "write $ETC/server.env first: KHALA_ISSUER, KHALA_OWNERS and the SMTP settings (see the README)"
  exit 1
fi
REPO=$(setting KHALA_REPO); REPO=${REPO:-$ROOT/vault.git}
DB=$(setting KHALA_DB); DB=${DB:-$ROOT/state/khala.db}
KEY=$(setting KHALA_SECRET_KEY_FILE); KEY=${KEY:-$ETC/secret.key}
STATE=$(dirname "$DB")

# A new server: the service user, the state directory, the virtualenv and an empty record repository. Each step only
# runs when its piece is missing, so it does nothing on a server that is already set up.
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  sudo useradd --system --home-dir "$ROOT" --shell /usr/sbin/nologin "$SVC_USER"
  echo "created user $SVC_USER"
fi
# install -d also resets the mode of a directory that exists, so only create what is missing
sudo test -d "$ROOT" || sudo install -d -m 755 "$ROOT"
sudo test -d "$STATE" || sudo install -d -o "$SVC_USER" -g "$SVC_USER" -m 700 "$STATE"
sudo test -d "$STATE/backups" || sudo install -d -o "$SVC_USER" -g "$SVC_USER" -m 700 "$STATE/backups"
if ! sudo test -x "$PY"; then                          # with sudo: the deploy user may not read $ROOT
  sudo install -d -o root -g "$SVC_USER" -m 750 "$APP"
  sudo python3 -m venv "$APP/.venv"
  echo "created $APP/.venv"
fi
if ! sudo test -e "$REPO/HEAD"; then
  sudo git init -q --bare -b main "$REPO"
  sudo chown -R "$SVC_USER:$SVC_USER" "$REPO"
  echo "created $REPO"
fi
# The key that encrypts TOTP secrets: created once for a new instance, never stored in the database or the record
# backups. An instance with a database but no key stops here: a new key would orphan every authenticator app.
if ! sudo test -s "$KEY"; then
  if sudo test -s "$DB"; then
    echo "the key at $KEY is missing but $DB exists; restore the key first (deploy/RESTORE.md)"
    exit 1
  fi
  sudo sh -c "umask 027; openssl rand -base64 48 > '$KEY'"
  sudo chown "root:$SVC_USER" "$KEY" && sudo chmod 640 "$KEY"
  echo "created $KEY"
fi
stamp=$(date +%Y%m%d-%H%M%S)
backup=""
if sudo test -s "$DB"; then
  backup="$STATE/backups/$(basename "$DB" .db)-$stamp.db"
  sudo -u "$SVC_USER" "$PY" -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" \
    "$DB" "$backup"
  sudo sh -c "ls -1t '$STATE'/backups/*.db | tail -n +11 | xargs -r rm --"
fi
# Distributions from before the rename would leave a second, stale copy of the code importable
sudo "$PY" -m pip -q uninstall -y agent-memory-server kalashtar >/dev/null 2>&1 || true
sudo "$PY" -m pip -q install "$SRC" >/dev/null                      # adds new dependencies, keeps the rest
sudo "$PY" -m pip -q install --no-deps --force-reinstall "$SRC" >/dev/null
sudo rm -rf "$APP/memory_server" "$APP/agent_memory_server.egg-info" "$APP/build"
for d in khala tests deploy docs; do sudo rm -rf "$APP/$d"; sudo cp -r "$SRC/$d" "$APP/"; done
sudo cp "$SRC/pyproject.toml" "$APP/"
sudo chown -R "root:$SVC_USER" "$APP" && sudo chmod -R g+rX,o-rwx "$APP"
# Stop here when the tests fail, before the restart that would migrate the state database
sudo -u "$SVC_USER" env HOME="$ROOT" sh -c "cd '$APP' && '$PY' -m unittest discover -s tests" >/tmp/khala-test.log 2>&1 \
  || { tail -30 /tmp/khala-test.log; echo "tests failed; service not restarted"; exit 1; }
tail -1 /tmp/khala-test.log

units=$(mktemp -d)
ROOT=$ROOT ETC=$ETC SERVICE=$SERVICE SVC_USER=$SVC_USER PORT=$PORT sh "$SRC/deploy/render.sh" "$units"
# Dry-run the hook as the pushing user first: a broken hook would refuse every push, so only install it if it runs
if printf "" | sudo -u "$GIT_USER" env GIT_DIR="$REPO" "$PY" -I -m khala.hooks pre-receive; then
  sudo install -o root -g "$SVC_USER" -m 755 "$units/pre-receive" "$REPO/hooks/pre-receive"
  echo "pre-receive hook installed"
else
  echo "pre-receive hook NOT installed: dry run as $GIT_USER failed"
fi
names="$SERVICE.service"
if sudo test -s "$ETC/backup.env"; then                # record backups, once a remote is configured
  names="$names $SERVICE-backup.service $SERVICE-backup.timer"
fi
if sudo test -s "$ETC/state-backup.env"; then          # encrypted state snapshots, once configured
  names="$names $SERVICE-state-backup.service $SERVICE-state-backup.timer"
fi
for u in $names; do sudo install -m 644 "$units/$u" /etc/systemd/system/; done
rm -rf "$units"
sudo systemctl daemon-reload
for u in $names; do case $u in *.timer) sudo systemctl enable -q --now "$u";; esac; done
sudo systemctl enable -q "$SERVICE.service"
# Stop, move notes and proposals to their current ref names, start. Moving them while the old version runs could
# let it write a note under the old name a moment later.
# Whatever happens in between, the service is started again; the server also moves the refs at startup.
sudo systemctl stop "$SERVICE"
sudo -u "$SVC_USER" env KHALA_REPO="$REPO" KHALA_BRANCH="$(setting KHALA_BRANCH)" "$APP/.venv/bin/khala" migrate-refs \
  || echo "migrate-refs failed; the server will try again when it starts"
sudo systemctl start "$SERVICE"; sleep 3; systemctl is-active "$SERVICE"
[ -n "$backup" ] && echo "backup: $backup"
sudo rm -rf "$SRC"
