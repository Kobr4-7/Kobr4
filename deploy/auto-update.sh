#!/bin/sh
# Mise à jour automatique : si la branche a de nouveaux commits, les récupère et
# reconstruit l'application. À lancer régulièrement (cron), voir deploy/README.md.
set -eu
cd "$(dirname "$0")/.."
exec 9>/tmp/kobr4-auto-update.lock
flock -n 9 || exit 0  # une mise à jour est déjà en cours

branch=$(git rev-parse --abbrev-ref HEAD)
git fetch -q origin "$branch"
[ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$branch")" ] && exit 0

echo "$(date '+%F %T') mise à jour $(git rev-parse --short HEAD) → $(git rev-parse --short "origin/$branch")"
git merge -q --ff-only "origin/$branch"
docker compose --env-file deploy/.env up -d --build
docker image prune -f >/dev/null
echo "$(date '+%F %T') terminé"
