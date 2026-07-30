# 07 — PLAN PERFORMANCE : pourquoi le site est lent, et comment le corriger

> Rédigé le 29 juillet 2026, après lecture du code (pas de suppositions) : chaque cause listée ici
> est citée par fichier et ligne, et au moins une est déjà mesurée en production (log réel ci-dessous).
> Objectif : ramener les pages les plus lentes (« Trades du jour » / scanner complémentaire,
> « Paper Trading ») à un temps de réponse perçu **sous 1 seconde**, et le reste du site en dessous
> de **300 ms**, sans dégrader l'honnêteté des données (toujours zéro donnée inventée).

> ## ⚠️ RÉALISÉ le 30 juillet 2026 — et trois hypothèses de ce plan ont été DÉMENTIES par la mesure
>
> Le plan a été implémenté intégralement. Mais mesurer avant d'optimiser a contredit trois de ses
> affirmations, et ces corrections sont plus utiles que le plan d'origine :
>
> 1. **La cause n°1 annoncée (base synchrone) n'était PAS le goulot.** Mesuré : 1,75 ms pour un
>    `get`, 16,2 ms pour un balayage complet des ordres. Réel mais ~1000× trop petit pour expliquer
>    les 22 s. Le vrai coupable était le **délai de connexion réseau** (§5), pas Postgres. La
>    migration async (P0-1/P2-1) n'a donc **pas** été menée sur les 151 points d'appel : seuls les
>    appels répétés en boucle de fond ont été déportés hors de la boucle d'événements. Voir §2.
> 2. **Le frontend suspendait DÉJÀ son rafraîchissement onglet masqué** (`useAutoRefresh` teste
>    `document.hidden` depuis toujours). L'affirmation « même onglet en arrière-plan » du §6 était
>    fausse. Seul l'allongement des intervalles restait à faire.
> 3. **« Inverser Alpaca/Yahoo » (§5) était un mauvais remède** : mesurés complémentaires (3 succès
>    sur 6 chacun, sur des symboles différents), donc aucun ordre n'est meilleur. Le correctif réel
>    est le **délai de connexion court**. Une variante « les deux en parallèle » a été écrite puis
>    **abandonnée sur mesure** : elle double les connexions et faisait tomber le taux de succès de
>    6/6 à **0/6** par saturation réseau.
>
> **Résultat mesuré du chargement de 6 symboles en parallèle (ce que fait `/api/execution/positions`) :
> 22 s → 1,72 s à froid, 0,000 s à chaud, 6/6 symboles servis.**
>
> Deux gains non prévus par le plan, trouvés en profilant : `rolling_vwap` était **quadratique**
> (×12 après correction, sur le chemin de TOUTE analyse), et le précalcul de la sélection du jour
> souffrait d'un **thundering herd** (cinq visites simultanées lançaient cinq fois les 32 backtests).
> Détails dans §10.

---

## 1. Preuve mesurée (pas une impression)

Le middleware `ObservabilityMiddleware` (`backend/app/core/observability.py`) chronomètre déjà
chaque requête et l'écrit dans les logs (`app.access`). Extrait réel des logs du conteneur backend :

```
INFO:app.access:GET /api/execution/positions -> 200 (22177.8ms)
```

**22 secondes** pour charger la liste des positions papier. Ce chronométrage existe déjà — il sert
de outil de mesure AVANT/APRÈS pour valider chaque correctif ci-dessous, sans rien construire de
nouveau.

---

## 2. Cause n°1 (le plus gros levier, touche TOUTE page) : la base de données est SYNCHRONE dans un serveur ASYNC

**Le fait, vérifié dans le code :**

- `backend/app/repositories/sql.py:17-30` — `create_engine` + `sessionmaker` **synchrones**
  (driver `psycopg2`), pas `create_async_engine`.
- `backend/app/core/config.py:518-527` (`database_url_sync`) — convertit délibérément
  `postgresql+asyncpg://` en `postgresql+psycopg2://` pour cette couche.
- Toutes les méthodes du store (`SqlRecordRepository.get/put/list/delete`,
  `backend/app/repositories/sql.py:462-500`, et les équivalents pour `users`, `journal`,
  `backtests`…) sont des **fonctions `def` normales**, appelées directement (jamais
  `await asyncio.to_thread(...)`) depuis des routes et services `async def`.
- `backend/entrypoint.sh:11` — un **seul processus Uvicorn**, aucun `--workers`.

**Pourquoi c'est grave :** Python asyncio n'a qu'**une seule boucle d'événements**. Un appel
réseau/disque *synchrone* exécuté dedans (ici : chaque requête Postgres) **bloque tout le
serveur** pendant sa durée — plus aucune autre requête HTTP, plus aucun tick WebSocket, plus
aucune boucle de fond ne peut progresser tant que cet appel n'est pas terminé. Ce n'est pas une
question de volume de données : même une requête Postgres de 5 ms, répétée des centaines de fois
par seconde (positions, vérifications d'ordres, verdicts, journal…), **sérialise** toute
l'activité du serveur. C'est la cause la plus probable du symptôme rapporté (« toutes les pages
sont lentes », pas seulement les pages qui appellent Yahoo).

**Correctif immédiat (P0, faible risque, ~1 jour) :** envelopper chaque appel au store dans
`asyncio.to_thread(...)`. Concrètement, dans `backend/app/repositories/sql.py`, transformer les
méthodes en `async def` qui délèguent :
```python
async def get(self, kind: str, record_id: str) -> dict | None:
    return await asyncio.to_thread(self._get_sync, kind, record_id)
```
Aucun changement de schéma, aucune migration de données — seulement libérer la boucle d'événements
pendant l'attente Postgres. Le pool de threads par défaut (`min(32, cpu+4)`) suffit largement pour
ce volume.

**Correctif de fond (P1, migration propre, ~3-5 j) :** passer à un engine SQLAlchemy 2.0 **async**
(`create_async_engine` + `asyncpg`, déjà la dépendance déclarée dans `DATABASE_URL` mais jamais
utilisée par cette couche) et rendre le store nativement `async def`. Meilleure solution à terme,
mais plus intrusive — à faire une fois le P0 en production et mesuré.

**Ne PAS faire en premier :** ajouter `--workers N` à Uvicorn. Ça multiplierait les boucles de fond
(scheduler, auto-entrée, surveillance des positions) sans coordination entre processus — un même
passage de veille tournerait en double, triple… Cette option n'a de sens qu'après avoir découplé
les boucles de fond du serveur web (hors scope de ce plan).

---

## 3. Cause n°2 : la page « Trades du jour » lance jusqu'à 32 backtests COMPLETS, en SÉQUENCE, dans la requête

C'est la cause directe de la lenteur du bloc **« Scanner complémentaire par marché »** (bas de la
page `/daily`) — le bloc du haut (« 🎯 Trades du jour », composant `TopTrades`) est, lui, déjà
rapide : `backend/app/api/signals.py:95-122` le sert depuis un instantané pré-calculé en fond
(`live_snapshot`), sans aucun calcul dans la requête. **Seul le scanner complémentaire est concerné.**

**Le fait, vérifié dans le code** (`backend/app/services/signal_service.py:117-157`) :
```python
for cls in classes:                      # 4 classes d'actifs
    scanned = await scan_market(...)      # scan SÉQUENTIEL de 12 symboles
    for cand in directional[:8]:          # jusqu'à 8 candidats PAR classe
        bt = await backtest_metrics(...)  # un backtest COMPLET, en SÉQUENCE
```
`backtest_metrics` (même fichier, ligne 47) charge jusqu'à 500 bougies puis rejoue tout
l'historique bougie par bougie (`run_backtest`, `backend/app/backtest/engine.py:43`) — un calcul
CPU-intensif, exécuté **directement sur la boucle d'événements** (pas de
`asyncio.to_thread`, contrairement à `playbook.build` qui, lui, en bénéficie déjà — cf.
commentaire dans `playbook_service.py:91-95`, ajouté précisément pour ce même problème).

Pire côté cache : `daily-picks` (`backend/app/api/signals.py:67-92`) n'est mis en cache
qu'**une fois calculé**, par jour ET par unité de temps. La page propose 6 boutons d'unité de
temps (15 min / 1 h / 4 h / 1 j / 1 semaine / 1 mois) — **chaque premier clic sur une unité non
encore calculée aujourd'hui déclenche les 32 backtests en direct**, et le bouton « Rafraîchir »
aussi (`refresh=true` court-circuite le cache).

**Correctif (P0, ~1-2 j) :**
1. **Paralléliser** `scan_market` et surtout la boucle de backtests avec
   `asyncio.gather(*, semaphore)` — le même motif déjà utilisé dans `playbook_service.top_trades`
   (`asyncio.Semaphore(playbook_max_parallel)`). Gain attendu : diviser le temps par ~4
   (parallélisme actuel du reste du projet).
2. **Décharger `run_backtest` du thread principal** avec `asyncio.to_thread`, comme
   `playbook.build` — la boucle d'événements reste libre pendant le calcul.
3. **Précalculer en fond**, comme `live_snapshot` le fait déjà pour les trades du jour : une
   boucle de fond calcule `daily_picks` pour les 6 unités de temps à intervalle régulier, et la
   route ne fait plus JAMAIS de calcul synchrone — elle sert toujours un instantané. C'est le
   changement qui élimine structurellement le problème plutôt que de le rendre seulement plus
   rapide.

---

## 4. Cause n°3 : deux couches de cache différentes, une seule appliquée

Le cache court (20 s) ajouté le 29/07/2026 dans `backend/app/data/ohlcv.py::get_ohlcv_with_source`
ne couvre que le chemin utilisé par les **graphiques** et `backtest_metrics`. Un **second chemin
de chargement de bougies**, `backend/app/data/markets.py::load_candles`, n'a **aucun cache propre**
— seuls ses appelants s'en dotent ponctuellement (`execution_service._reference_price` a son
propre cache de 15 s, `PaperBroker.place_order` n'en a **aucun**). Résultat : deux fonctions qui
font la même chose (charger des bougies réelles) ont deux politiques de cache différentes, et l'une
des deux (`markets.load_candles`) reste appelée à nu par plusieurs chemins chauds :
positions_snapshot, ouverture d'ordre, surveillance des positions.

**Correctif (P1, ~0,5 j) :** unifier — soit `markets.load_candles` gagne le même cache court que
`ohlcv.py`, soit tous ses appelants passent par `ohlcv.get_ohlcv_with_source`. Éviter à tout prix
une TROISIÈME implémentation de cache : un seul point de vérité, un seul TTL à régler.

---

## 5. Cause n°4 : chemin Alpaca-d'abord pour les actions, deux allers-retours réseau au lieu d'un

`backend/app/data/markets.py:151-157` — pour toute action (`stock`, 30 des 84 symboles de
l'univers), le code appelle **toujours** Alpaca en premier, et ne retombe sur Yahoo que si Alpaca
renvoie moins de bougies que nécessaire. Le commentaire du code lui-même documente le problème :
« Sans `start`, Alpaca ne renvoie que la journée en cours (~7 bougies 1h) ». Autrement dit, pour un
grand nombre d'appels, **le premier essai échoue par construction** et le second (Yahoo) est
nécessaire quand même — payant systématiquement DEUX allers-retours réseau là où Yahoo seul en
suffirait à un.

**Correctif (P1, ~0,5 j, à mesurer avant de généraliser) :** comparer en pratique le taux de succès
et la latence d'Alpaca vs Yahoo sur l'univers actuel (log déjà disponible : chercher
`Connecteur stock indisponible` dans les logs backend — plusieurs occurrences déjà visibles pour
MSFT, DIS, BA, WMT, PEP, XOM…). Si Yahoo est systématiquement au moins aussi fiable, inverser
l'ordre (Yahoo d'abord, Alpaca en secours) pour la LECTURE de prix — garder Alpaca en priorité
uniquement pour l'exécution RÉELLE d'ordres, où c'est le broker qui compte, pas la donnée.

---

## 6. Cause n°5 : le frontend interroge le serveur toutes les 10 s, sans discrimination

Cinq pages au moins se rafraîchissent seules toutes les 10 secondes, en permanence, même onglet en
arrière-plan : `frontend/app/execution/page.tsx:13`, `frontend/app/dashboard/page.tsx:73`,
`frontend/app/scanner/page.tsx:132`, `frontend/app/agents/page.tsx:19`. Chaque inefficacité
backend listée plus haut est donc payée **6 fois par minute, par onglet ouvert, par utilisateur** —
un simple onglet laissé ouvert la nuit maintient le serveur occupé en continu.

**Correctifs (P0-P1, ~1 j au total) :**
1. **Suspendre le rafraîchissement quand l'onglet n'est pas visible**
   (`document.visibilityState`) — un seul hook à ajouter dans `lib/useAutoRefresh.ts`, s'applique
   à toutes les pages d'un coup. Gain immédiat et sans risque.
2. **Allonger l'intervalle** là où quelques secondes de retard ne coûtent rien : le P&L latent
   d'une position papier n'a pas besoin d'une fraîcheur à 10 s près (15-20 s suffisent) ; le
   scanner (moins critique que l'exécution) peut passer à 20-30 s.
3. **Remplacer le polling par le flux temps réel déjà existant** (`app/realtime/bus.py`, déjà
   utilisé pour les annonces d'auto-entrée) pour la page `/execution` : le serveur pousse la mise
   à jour d'une position dès qu'elle change, au lieu que le client la redemande en boucle. C'est le
   changement à le plus fort ratio gain/risque à moyen terme, mais demande de brancher le
   WebSocket existant sur `positions_snapshot` plutôt que d'ajouter une route de plus.

---

## 7. Cause n°6 : les boucles de fond scannent TOUTE la table de commandes, de tous les tenants, à chaque passage

`backend/app/services/scheduler.py:531-560` (`positions_loop`, toutes les
`position_monitor_interval` secondes = 60 s par défaut) enchaîne **quatre passages séquentiels**
(`quarantine_impossible_closures`, `secure_open_profits`, `manage_tp_progression`,
`monitor_positions`), et **chacun** appelle `store.records.list(ORDER)` **sans filtre tenant** —
c'est-à-dire qu'il charge et désérialise en JSON, en Python, l'intégralité de l'historique
d'ordres de TOUS les comptes, à CHAQUE passage, pour n'en garder qu'une poignée encore ouverts.
Le coût croît avec le nombre total de trades jamais passés sur la plateforme, pas avec le nombre
de positions actuellement ouvertes — combiné à la cause n°1 (chaque lecture bloque la boucle
d'événements), c'est un ralentissement qui **s'aggrave avec le temps**, invisible aujourd'hui mais
qui se manifestera de plus en plus fort.

**Correctif (P1, ~1 j) :** filtrer côté SQL (`WHERE payload->>'outcome' NOT IN (...)` ou, mieux, une
colonne dédiée `status`/`open` indexée sur `RecordRow` plutôt que tout charger puis filtrer en
Python) — ou, plus simple à court terme, ajouter une méthode `list_open(kind)` au store qui ne
matérialise que ce qui est réellement nécessaire à chaque passage.

---

## 8. Plan d'action priorisé

| # | Correctif | Cause | Effort | Impact | Risque |
|---|---|---|---|---|---|
| P0-1 | `asyncio.to_thread` sur toutes les méthodes du store SQL | §2 | ~1 j | **Très élevé** — débloque TOUT le serveur | Faible |
| P0-2 | Paralléliser `scan_market` + backtests de `daily_picks`, avec sémaphore | §3 | ~0,5 j | Élevé, ciblé sur le scanner complémentaire | Faible |
| P0-3 | `asyncio.to_thread` sur `run_backtest` | §3 | ~0,25 j | Élevé, combiné à P0-2 | Faible |
| P0-4 | Suspendre le polling frontend hors visibilité d'onglet | §6 | ~0,5 j | Élevé, réduit la charge globale | Faible |
| P1-1 | Précalculer `daily_picks` en fond (comme `live_snapshot`) | §3 | ~1 j | Élevé, élimine le problème structurellement | Faible |
| P1-2 | Unifier le cache de bougies (`markets.load_candles` ↔ `ohlcv.py`) | §4 | ~0,5 j | Moyen | Faible |
| P1-3 | Réévaluer l'ordre Alpaca/Yahoo pour la lecture de prix | §5 | ~0,5 j (mesure) + 0,25 j (code) | Moyen | Faible — réversible |
| P1-4 | Filtrer les commandes ouvertes côté requête, pas en Python | §7 | ~1 j | Moyen aujourd'hui, élevé dans 6 mois | Faible |
| P2-1 | Migration vers un engine SQLAlchemy async natif | §2 | ~3-5 j | Confirme/pérennise P0-1 | Moyen — refactor large |
| P2-2 | Pousser les positions en WebSocket au lieu du polling | §6 | ~2-3 j | Élevé, UX temps réel | Moyen — branchement du bus existant |

**Ordre d'exécution recommandé :** P0-1 d'abord et seul (c'est la cause qui explique la lenteur
*généralisée*, pas seulement les deux pages citées) — mesurer avec les logs `app.access` avant/après
sur `/api/execution/positions` et `/api/signals/daily-picks`. Puis P0-2/P0-3 ensemble (même fichier,
même passage). Puis P0-4 côté frontend. Réévaluer après ces quatre correctifs : si les 22 secondes
mesurées tombent sous la seconde, les tâches P1 deviennent des optimisations de confort, pas des
urgences ; sinon, elles indiquent que d'autres verrous existent et méritent une nouvelle mesure.

---

## 9. Comment vérifier que ça a marché

Rien de nouveau à construire — l'instrumentation existe déjà :
```bash
docker compose -f infra/docker-compose.yml logs backend | grep "app.access" | grep "/api/execution/positions"
docker compose -f infra/docker-compose.yml logs backend | grep "app.access" | grep "/api/signals/daily-picks"
```
Comparer les durées entre parenthèses avant/après chaque correctif. Cible : `/api/execution/positions`
sous 500 ms avec quelques positions ouvertes, `/api/signals/daily-picks` sous 2 s au premier calcul
du jour (et quasi instantané une fois précalculé en fond, cf. P1-1).

---

## 10. Ce qui a été fait, et ce que la mesure a appris (30/07/2026)

### Livré

| Correctif | Où | Gain MESURÉ |
|---|---|---|
| Délai de **connexion** réseau borné à 3 s (lecture inchangée à 12 s) | `data/markets.py::_timeout`, appliqué aussi à Yahoo/Binance/OANDA/Alpaca | pire cas par symbole **22 s → 6 s** |
| **Cache court** des bougies (20 s), même TTL que `data/ohlcv.py` | `data/markets.py::_CACHE` | 6 symboles : **1,72 s → 0,000 s** à chaud |
| `rolling_vwap` en **somme glissante** (était quadratique) | `domain/indicators.py` | **×12** (19 ms → 1,6 ms sur 5000 bougies), résultat identique à 3e-15 |
| Sélection du jour **parallélisée** (marchés + backtests, sémaphore borné) | `services/signal_service.py::daily_picks` | 4 marchés séquentiels → simultanés |
| Sélection du jour **précalculée en fond** + verrou anti-troupeau | `services/daily_picks_cache.py`, `scheduler.daily_picks_loop` | la route ne calcule plus rien |
| Positions ouvertes filtrées **au plus près de la base** | `repositories/*.py::list_where_field_not_in` | **aucun gain aujourd'hui** (voir ci-dessous) |
| Lectures du store **hors boucle d'événements** là où elles se répètent | `execution_service._open_orders_async`, `positions_snapshot` | supprime 16 ms de blocage par passage |
| Quarantaine passée de 60 s à 10 min | `scheduler.positions_loop` | seul passage qui doit lire l'historique clôturé |
| Intervalles de rafraîchissement allongés | `/execution` 10→15 s, `/scanner` 10→20 s | moins de requêtes, aucune fraîcheur perdue |

### Ce que la mesure a démenti — et qu'il faut retenir

- **Ne pas migrer le store en async pour l'instant.** 1,75 ms/`get` et 16,2 ms/balayage complet ne
  justifient pas de toucher 151 points d'appel. Le seuil qui le justifierait : un balayage complet
  au-delà de ~100 ms, ou une charge concurrente où le blocage cumulé devient visible dans
  `app.access`. **Mesurer d'abord.**
- **Le filtre « positions ouvertes » ne fait rien gagner aujourd'hui** (16,6 ms contre 16,2 ms) :
  213 des 497 ordres sont réellement ouverts, et le filtre s'écrit en `NOT LIKE` sur le document
  JSON — sans index, la base parcourt tout de même la table. Il est conservé parce qu'il est
  CORRECT (résultat identique, testé) et que l'écart se creusera avec l'historique. Le vrai gain
  exigerait une colonne `outcome` dédiée et indexée : une migration, à ne faire que quand la mesure
  la réclamera.
- **Demander plus en parallèle peut être plus lent.** Le réseau de cette machine sature : 12
  connexions simultanées ont fait chuter le taux de succès de 6/6 à 0/6. Le parallélisme du projet
  reste donc volontairement borné (`playbook_max_parallel = 4`).

### Reste à faire (non fait, et pourquoi)

- **P2-2 (pousser les positions en WebSocket au lieu du polling)** : non fait. Le bus temps réel
  existe (`realtime/bus.py`) mais le brancher sur `positions_snapshot` est un chantier à part, et le
  polling ne coûte plus rien maintenant que la route répond depuis un cache. À reprendre si la
  charge le justifie, pas avant.
- **Compte à rebours qui re-rend la page chaque seconde** : `useAutoRefresh` appelle `setNextIn`
  toutes les secondes, ce qui re-rend tout l'arbre de la page (13 cartes de position sur
  `/execution`). Repéré en lisant le code, **non mesuré** comme gênant — à extraire dans un
  composant enfant si une lenteur d'affichage (et non de chargement) est constatée.
