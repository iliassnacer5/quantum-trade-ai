#!/bin/bash
# Backup quotidien de Postgres (plan Phase 1.2) — pg_dump compressé + rétention 14 jours.
#
# Tourne dans un conteneur dédié (service `db-backup` du docker-compose) : une boucle infinie qui
# dumpe, purge les archives de plus de BACKUP_RETENTION_DAYS jours, puis dort 24 h. Pas de cron à
# installer, pas de dépendance à l'hôte — le conteneur EST le cron.
#
# Restauration : voir docs/BACKUPS.md (procédure testée, pas seulement écrite).
set -u

: "${POSTGRES_HOST:=postgres}"
: "${POSTGRES_USER:=quantum}"
: "${POSTGRES_DB:=quantum}"
: "${BACKUP_DIR:=/backups}"
: "${BACKUP_RETENTION_DAYS:=14}"
: "${BACKUP_INTERVAL_SECONDS:=86400}"

mkdir -p "$BACKUP_DIR"

while true; do
  stamp="$(date -u +%Y%m%d_%H%M%S)"
  target="$BACKUP_DIR/quantum_${stamp}.sql.gz"
  echo "[db-backup] $(date -u -Iseconds) dump -> $target"
  if pg_dump -h "$POSTGRES_HOST" -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$target"; then
    size=$(du -h "$target" | cut -f1)
    echo "[db-backup] OK ($size)"
  else
    # On garde le fichier partiel visible avec un suffixe explicite : un backup silencieusement
    # vide est pire qu'un échec bruyant.
    mv "$target" "${target}.FAILED" 2>/dev/null
    echo "[db-backup] ÉCHEC du pg_dump — archive marquée .FAILED" >&2
  fi
  # Rétention : supprime les archives plus vieilles que BACKUP_RETENTION_DAYS jours.
  find "$BACKUP_DIR" -name 'quantum_*.sql.gz*' -mtime "+${BACKUP_RETENTION_DAYS}" -delete
  echo "[db-backup] archives présentes : $(ls -1 "$BACKUP_DIR" | wc -l)"
  sleep "$BACKUP_INTERVAL_SECONDS"
done
