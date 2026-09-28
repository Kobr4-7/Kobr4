#!/bin/sh
# Mise à jour du site. Lancé chaque minute par cron (voir deploy/README.md) :
# - toutes les 5 minutes, ou tout de suite si le site en a fait la demande
#   (bouton « Mettre à jour » des réglages), récupère les nouveaux commits de la
#   branche et reconstruit l'application ;
# - publie dans deploy/control/ la version installée, les nouveautés disponibles et
#   l'état de la dernière mise à jour, que le site affiche.
set -u
cd "$(dirname "$0")/.."
ctl=deploy/control
mkdir -p "$ctl" && chmod 777 "$ctl" 2>/dev/null || true
exec 9>/tmp/kobr4-auto-update.lock
flock -n 9 || exit 0  # une mise à jour est déjà en cours

requested=0
[ -f "$ctl/request" ] && requested=1
minute=$(date +%M)
if [ "$requested" -eq 0 ] && [ "$(expr "$minute" % 5)" -ne 0 ]; then
  exit 0
fi

branch=$(git rev-parse --abbrev-ref HEAD)
publish() {
  git rev-parse --short HEAD > "$ctl/version"
  git log --format='%h %s' "HEAD..origin/$branch" > "$ctl/pending" 2>/dev/null || : > "$ctl/pending"
  chmod 666 "$ctl/version" "$ctl/pending" 2>/dev/null || true
}
if ! git fetch -q origin "$branch"; then
  echo "$(date '+%F %T') récupération impossible (réseau ou accès GitHub)"
  [ "$requested" -eq 1 ] && echo failed > "$ctl/state" && date -u +%FT%TZ > "$ctl/state-at" \
    && echo "Récupération des modifications impossible (réseau ou accès GitHub)." > "$ctl/log"
  rm -f "$ctl/request"
  exit 1
fi
publish
if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$branch")" ] && [ "$requested" -eq 0 ]; then
  exit 0
fi

rm -f "$ctl/request"
echo running > "$ctl/state"
date -u +%FT%TZ > "$ctl/state-at"
echo "$(date '+%F %T') mise à jour $(git rev-parse --short HEAD) → $(git rev-parse --short "origin/$branch")"
{
  git merge -q --ff-only "origin/$branch" &&
    docker compose --env-file deploy/.env up -d --build &&
    docker image prune -f
} > "$ctl/log" 2>&1
rc=$?
if [ "$rc" -eq 0 ]; then echo done > "$ctl/state"; else echo failed > "$ctl/state"; fi
date -u +%FT%TZ > "$ctl/state-at"
chmod 666 "$ctl"/* 2>/dev/null || true
publish
echo "$(date '+%F %T') terminé (code $rc)"
exit "$rc"
