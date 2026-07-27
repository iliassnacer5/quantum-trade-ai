# Backups Postgres — sauvegarde quotidienne et restauration (plan Phase 1.2)

## Ce qui tourne

Le service `db-backup` du [docker-compose](../infra/docker-compose.yml) exécute
[infra/db/backup.sh](../infra/db/backup.sh) en boucle :

- `pg_dump` compressé (`quantum_YYYYMMDD_HHMMSS.sql.gz`) toutes les **24 h** ;
- rétention **14 jours** (les archives plus vieilles sont supprimées) ;
- les archives vivent dans le volume Docker **`pgbackups`**, séparé de `pgdata` : une corruption
  de la base n'emporte pas ses sauvegardes ;
- un dump raté est renommé `.FAILED` (visible dans les logs), jamais laissé passer pour un backup.

Vérifier que ça tourne :

```powershell
docker compose -f infra/docker-compose.yml logs db-backup --tail 20
docker run --rm -v quantum-trade-ai_pgbackups:/backups alpine ls -lh /backups
```

## Restauration (procédure à TESTER, pas seulement à lire)

1. Repérer l'archive à restaurer :
   ```powershell
   docker run --rm -v quantum-trade-ai_pgbackups:/backups alpine ls -1 /backups
   ```
2. Restaurer dans une base de VÉRIFICATION d'abord (jamais directement sur `quantum`) :
   ```powershell
   docker compose -f infra/docker-compose.yml exec postgres createdb -U quantum quantum_restore
   docker run --rm -v quantum-trade-ai_pgbackups:/backups --network quantum-trade-ai_default `
     -e PGPASSWORD=quantum_dev_pwd timescale/timescaledb:latest-pg16 `
     bash -c "gunzip -c /backups/<ARCHIVE>.sql.gz | psql -h postgres -U quantum quantum_restore"
   ```
3. Contrôler le contenu (journal, positions, edge map) :
   ```powershell
   docker compose -f infra/docker-compose.yml exec postgres psql -U quantum quantum_restore `
     -c "SELECT kind, count(*) FROM records GROUP BY kind ORDER BY 2 DESC LIMIT 10;"
   ```
4. Si le contrôle est bon, basculer : arrêter le backend, renommer `quantum` → `quantum_old`,
   `quantum_restore` → `quantum`, redémarrer le backend.
   ```powershell
   docker compose -f infra/docker-compose.yml stop backend
   docker compose -f infra/docker-compose.yml exec postgres psql -U quantum postgres `
     -c "ALTER DATABASE quantum RENAME TO quantum_old;" `
     -c "ALTER DATABASE quantum_restore RENAME TO quantum;"
   docker compose -f infra/docker-compose.yml start backend
   ```
5. Garder `quantum_old` quelques jours avant de la supprimer.

## Hors-site (recommandé dès le VPS — Phase 5)

Le volume `pgbackups` reste sur la même machine que la base : un disque mort emporte les deux.
Sur le VPS, ajouter une copie hors-site (rclone vers un stockage objet, ou simple `scp` cron
depuis une autre machine). À faire au moment du déploiement 5.1.

## Critère de sortie du plan

« Backup restauré une fois avec succès » : dérouler la procédure ci-dessus une fois en local et
noter la date ici → **restauration testée le : _(à remplir au premier test)_**.
