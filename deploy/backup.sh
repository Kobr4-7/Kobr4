#!/bin/sh
# Sauvegarde quotidienne de la base (utilisateurs, bots, journal), compressée.
# Garde BACKUP_KEEP_DAYS jours. À copier ailleurs (autre serveur, stockage objet) :
# une sauvegarde sur le même disque ne protège pas d'une panne du serveur.
set -eu
while true; do
  stamp=$(date -u +%Y%m%d-%H%M%S)
  if pg_dump -h db -U kobr4 -d kobr4 -Fc -f "/backups/kobr4-$stamp.dump"; then
    echo "sauvegarde /backups/kobr4-$stamp.dump"
  else
    echo "ÉCHEC de la sauvegarde $stamp" >&2
  fi
  find /backups -name 'kobr4-*.dump' -mtime +"${BACKUP_KEEP_DAYS:-30}" -delete
  sleep 86400
done
