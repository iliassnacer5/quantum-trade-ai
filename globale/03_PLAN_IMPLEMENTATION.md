# 03 — Plan d'implémentation complet (6 phases)

> Chaque phase a un livrable, des tâches concrètes ancrées dans le code existant, et un **critère
> de sortie chiffré**. On ne passe pas à la phase suivante sans le critère. Les phases 4 et 5
> tournent en parallèle (le forward test, c'est du temps calendaire, pas du travail).

---

## PHASE 1 — Fondations production (2-3 jours) 🔴 prérequis à tout le reste

Objectif : plus aucune donnée perdue, plus aucune régression silencieuse.

| # | Tâche | Détail |
|---|---|---|
| 1.1 | **Postgres par défaut** | `use_in_memory_db=False` en prod (compose), vérifier `repositories/sql.py` couvre records/journal/positions/edge_map/training ; migration + test `test_postgres_switch.py` au vert en Docker |
| 1.2 | **Backups** | `pg_dump` quotidien (cron conteneur) + rétention 14 j + test de restauration documenté |
| 1.3 | **Secrets** | `secret_key` généré obligatoire (refus de boot en prod avec "change-me"), `.env` hors git, rotation documentée |
| 1.4 | **CI GitHub Actions** | build image + `pytest tests/ -q` dans le conteneur à chaque push ; branche protégée |
| 1.5 | **Observabilité** | Sentry branché (`sentry_dsn`), alerte uptime externe (UptimeRobot gratuit) sur `/api/health`, dashboard `/metrics` existant relié à Grafana (`infra/monitoring`) |
| 1.6 | **État des tests** | exécuter la suite complète, corriger tout rouge : `docker run --rm -v .../backend:/src -w /src quantum-trade-ai-backend python -m pytest tests/ -q` |

**Critère de sortie** : redémarrage du serveur sans perte d'état (journal + positions + edge map
intacts) ; CI verte ; backup restauré une fois avec succès.

---

## PHASE 2 — Stratégie mesurée (3-5 jours) — cf. 02_STRATEGIE_PLAYBOOK.md

Objectif : des agents qui travaillent la stratégie **là où elle est prouvée** et un backtest qui
répond aux questions encore ouvertes.

| # | Tâche | Fichiers principaux |
|---|---|---|
| 2.1 | **Verdict par paire** dans le backtest hebdo (🟢 espérance ≥ +0,4 R et n ≥ 20 sur 2 passages consécutifs ; 🟡 ; 🔴) | `backtest/playbook_backtest.py`, record `playbook_pair_verdicts` |
| 2.2 | **Gating auto-entrée** sur le verdict (`playbook_pair_gating=True`) : 🔴 exclu, 🟡 analysé mais non auto-tradé, 🟢 auto-tradé | `services/auto_entry_service.py`, `services/signal_service.py` (daily_top_trades), `core/config.py` |
| 2.3 | Matrice **paire × déclencheur** ; désactivation du repli où mesuré < +0,4 R (n ≥ 15) | `playbook_backtest.py`, `domain/playbook.py` (entry_trigger paramétrable) |
| 2.4 | **A/B volatilité** (1 run, 3 variantes : adapt / refuse / stop k×ATR4h borné) → on fige la gagnante | `playbook_backtest.py`, `domain/playbook.py` (volatility_adjustment) |
| 2.5 | **Ventilation par session** dans le rapport hebdo (win/espérance par fenêtre) | `playbook_backtest.py` |
| 2.6 | Run comparatif **sécurisation +2R on/off** (informatif, la règle reste) | `playbook_backtest.py` (replay_trade) |
| 2.7 | **UI** : page /today et /edge affichent le verdict par paire + les chiffres qui le motivent | `frontend/app/today`, `frontend/app/edge` |
| 2.8 | **OANDA branché** (compte practice) : connecteur 15 min/1 h profond, vrais spreads ; relance de la passe fidélité 15 min sur 2+ ans | `data/` (nouveau `oanda.py`), `data/markets.py`, config déjà prête |

**Critère de sortie** : le sous-ensemble 🟢 mesure ≥ +0,9 R / PF ≥ 3 au backtest ; passe 15 min
OANDA exécutée et comparée à la passe 1 h ; toutes les décisions A/B figées et documentées dans le
rapport.

---

## PHASE 3 — Sizing & risque portefeuille (2 jours)

| # | Tâche | Détail |
|---|---|---|
| 3.1 | Sizing par conviction : 1 % base, ×1,25 paires 🟢 (n≥30), ×0,5 paires 🟡, plafond 1,5 % | `services/execution_service.py`, `domain/risk.py` |
| 3.2 | Garde de corrélation : max 2 positions partageant une devise | `execution_service` (portfolio guard) |
| 3.3 | Stop quotidien −3 % et hebdo −6 % (gel des entrées + notification) | `services/risk_service.py`, scheduler |
| 3.4 | Journal enrichi : chaque trade enregistre verdict de paire, déclencheur, session, ATR % — le futur méta-filtre (Phase G du 02) en dépend | `services/journal_service.py` |

**Critère de sortie** : simulation du sizing sur les 209 trades du backtest → drawdown max réduit
sans perte d'espérance ; gels testés (test unitaire qui simule une journée à −3 %).

---

## PHASE 4 — Forward test discipliné (4-8 semaines, incompressible) ⏳

Le juge de paix. Zéro code nouveau, du rituel.

- Auto-entrée papier active uniquement sur paires 🟢, sizing Phase 3.
- **Rituel hebdomadaire (30 min, dimanche après le backtest de 3 h UTC)** :
  1. PF / win / espérance réels de la semaine vs prédiction du backtest ;
  2. verdicts de paires : changements 🟢/🟡/🔴 ;
  3. trades refusés par les gates (le « trades évités » existe) — les gates ont-ils eu raison ?
  4. incidents données (symboles en échec, snapshots périmés).
- **Go/No-Go argent réel** (au plus tôt après 4 semaines ET ≥ 30 trades) :
  - GO si PF forward ≥ 1,5 et espérance ≥ +0,4 R sur ≥ 30 trades, sans journée > −3 % non gelée ;
  - NO-GO sinon → retour Phase 2 avec les enseignements. Pas de « on prolonge en espérant ».
  - Même avec GO : réel = compte MICRO, re-validation explicite (règle mémorisée : l'auto-entrée
    réelle exige l'accord de l'utilisateur), et l'avis juridique de la Phase 5 fait.

**Critère de sortie** : décision Go/No-Go documentée avec les chiffres, page track record à jour.

---

## PHASE 5 — SaaS production (1-2 semaines, EN PARALLÈLE de la Phase 4)

Objectif : hébergé, encaissable, légalement présentable.

| # | Tâche | Détail |
|---|---|---|
| 5.1 | **VPS** (Oracle Always Free pour commencer, guide `docs/DEPLOIEMENT.md` existant) + docker compose prod + domaine + **HTTPS Caddy** | débloque aussi fapi.binance (open interest) |
| 5.2 | **Stripe complet** : produits Starter/Pro/Elite, webhooks (checkout, invoice.paid, subscription.deleted), portail client, factures ; tests webhook | `api/billing.py` |
| 5.3 | **Onboarding resserré** : inscription → email vérifié → choix du profil de risque → dashboard avec le playbook du jour. Les modules non essentiels (marketplace, copytrading, team, white-label) passés derrière un feature flag OFF | `frontend/app/onboarding`, `core/plans.py` |
| 5.4 | **Page publique de track record** : chaque trade papier horodaté, verdict, R réalisé, équité — en lecture seule, sans compte. C'est l'argument de vente | nouvelle page frontend + endpoint public |
| 5.5 | **Légal** (obligatoire avant le premier client payant) : avis d'avocat statut « éditeur d'outil d'aide à la décision » vs conseil en investissement (AMF/MiFID), CGU/CGV, disclaimers déjà présents à auditer, RGPD (registre, DPA hébergeur), mentions légales | `docs/legal/` existe, à compléter |
| 5.6 | **Emails transactionnels** (Resend déjà configurable) : vérification, reçus, digest ; Telegram bot (token 2 min) | `alerts/notifier.py` |
| 5.7 | Politique de **budget LLM** vérifiée en charge (garde `llm_daily_budget_usd` déjà en place) | `agents/llm.py` |

**Critère de sortie** : un inconnu peut s'inscrire, payer, recevoir le digest et voir le playbook
du jour, sur un domaine HTTPS, avec CGU acceptées — sans intervention manuelle.

---

## PHASE 6 — Exploitation quotidienne & croissance (continu)

**Le rituel quotidien de l'opérateur (toi), 15 min :**
1. Matin (après le digest 7 h UTC) : lire les trades du jour + setups armés ;
2. Vérifier la page /execution : positions ouvertes, sécurisations +2R passées ;
3. Un coup d'œil aux incidents (Sentry / symboles en échec de données) ;
4. Dimanche : le rituel hebdo de la Phase 4.

Tout le reste est automatique — c'est déjà le cas dans le code (snapshot, auto-entrée, veille,
entraînement, backtest, digest).

**Croissance** : voir [04_SAAS_1M.md](04_SAAS_1M.md). Résumé : le produit ne se vend pas sur une
promesse de gains (interdit et faux) mais sur la **transparence outillée** — track record public,
explication de chaque décision, discipline automatisée. Jalons : 10 payants → 100 → 1 000, chaque
jalon avec son critère avant d'investir le suivant.

---

## Récapitulatif des dépendances

```
Phase 1 (fondations)
   └─→ Phase 2 (stratégie mesurée)
          └─→ Phase 3 (sizing)
                 └─→ Phase 4 (forward test 4-8 sem) ──→ Go/No-Go réel
Phase 5 (SaaS prod) — démarre en parallèle dès la fin de la Phase 1
Phase 6 — démarre dès la Phase 5 livrée
```
