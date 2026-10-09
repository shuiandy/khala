#!/bin/sh
# Render the systemd units and the pre-receive hook for one server layout into OUTDIR.
#   ROOT=/srv/khala ETC=/etc/khala SERVICE=khala SVC_USER=khala PORT=8100 sh deploy/render.sh OUTDIR
# Files: SERVICE.service, SERVICE-backup.{service,timer}, SERVICE-state-backup.{service,timer}, pre-receive.
set -eu
out=$1
here=$(dirname "$0")/systemd
: "${ROOT:=/srv/khala}" "${ETC:=/etc/khala}" "${SERVICE:=khala}" "${SVC_USER:=khala}" "${PORT:=8100}"
mkdir -p "$out"
render() {
  sed -e "s|@ROOT@|$ROOT|g" -e "s|@ETC@|$ETC|g" -e "s|@USER@|$SVC_USER|g" -e "s|@PORT@|$PORT|g" "$here/$1" > "$out/$2"
}
render server.service.in "$SERVICE.service"
render backup.service.in "$SERVICE-backup.service"
render backup.timer.in "$SERVICE-backup.timer"
render state-backup.service.in "$SERVICE-state-backup.service"
render state-backup.timer.in "$SERVICE-state-backup.timer"
render pre-receive.in pre-receive
chmod 755 "$out/pre-receive"
