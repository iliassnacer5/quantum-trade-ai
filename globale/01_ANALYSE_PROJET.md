# 01 — Analyse complète du projet

Analyse effectuée sur la branche `feat/playbook-strategy` (26 juillet 2026), en tant qu'expert
IA / trading quantitatif / plateformes SaaS. Chaque constat renvoie au code réel.

---

## 1. Architecture d'ensemble

```
tradingIA/
├── backend/            FastAPI (Python, 100 % Docker — aucun Python local)
│   └── app/
│       ├── agents/     10 agents (playbook, technical→4 experts, volume, sentiment,
│       │               pattern, fundamental, macro, risk, journal, master)
│       ├── api/        26 routeurs (signals, market, backtest, execution, billing, kyc,
│       │               copytrading, marketplace, team, wallet, copilot, …)
│       ├── domain/     playbook.py (1 588 lignes — la stratégie), indicators, pips, risk, ta
│       ├── signal_engine/  engine + mtf + quality gates
│       ├── backtest/   engine, metrics, walkforward, playbook_backtest, report
│       ├── data/       Binance, Yahoo, sessions, news, macro, calendrier éco, replay
│       ├── services/   scheduler, signal, playbook, auto_entry, execution, training,
│       │               edge_map, journal, wallet, copilot, portfolio, …
│       ├── realtime/   bus WebSocket + ingestion live Binance
│       ├── repositories/ store (in-memory OU SQL), entities
│       └── core/       config (12-factor), sécurité, TOTP, ratelimit, plans, i18n, metrics
├── frontend/           Next.js — 24 pages (dashboard, today, edge, backtest, journal,
│                       execution, copilot, marketplace, plans, settings, …)
├── mobile/             app mobile (Expo/React Native)
├── infra/              docker-compose, db, monitoring
└── docs/               PLAN_MAITRE, DEPLOIEMENT, architecture, legal, …
```

### Boucles de fond (scheduler asyncio, `services/scheduler.py`)

| Boucle | Intervalle | Rôle |
|---|---|---|
| Snapshot playbook | 180 s | recalcul complet de la stratégie sur l'univers (16 symboles), pages servies instantanément |
| **Auto-entrée** | 60 s | recalcule les setups ARMÉS ; ouvre en **démo** dès qu'un déclencheur 15 min se forme |
| Veille de sessions | 900 s | top trades recalculés à l'ouverture Londres/NY/chevauchement |
| Moniteur de positions | 60 s | clôture SL/TP + sécurisation +2R (`secure_open_profits`) |
| Apprentissage | 300 s | résolution des signaux → multiplicateurs de poids des agents |
| Entraînement nocturne | 2 h UTC | walk-forward par symbole/déclencheur/session + fiches d'expertise LLM |
| **Backtest playbook** | dimanche 3 h UTC | rejeu complet de la stratégie, 14 paires, toute la profondeur — produit le classement |
| Edge sweep | 24 h | 8 stratégies × 18 symboles × {4h,1d} → carte 🟢/🟡/🔴, auto-trade vert uniquement |
| Digest quotidien | 7 h UTC | trades du jour par email/Telegram/push |

C'est un vrai pipeline de desk : analyse continue → setups armés → entrée automatique →
surveillance → apprentissage → ré-étalonnage nocturne. Peu de projets amateurs ont cette boucle
fermée.

---

## 2. Les agents

| Agent | Rôle | Poids Master |
|---|---|---|
| **playbook** | Chef de file : applique LA stratégie, droit de **veto** absolu (`master.py:98-111`) | 0,35 |
| technical | Routeur vers 4 experts marché (crypto/forex/or/actions, bonus ×1,3) | 0,25 |
| sentiment | News scorées par titre | 0,20 |
| volume | Validation par volume relatif | 0,15 |
| pattern | 14 figures chartistes | 0,15 |
| fundamental | Fondamentaux par classe d'actif | 0,15 |
| macro | Régime macro (accentué ×1,3 en régime extrême) | 0,10 |
| risk | Contrainte transversale (réduit la confiance, plancher 0,4) | — |
| journal | Apprentissage bayésien n/(n+12) par marché | multiplicateurs |
| master | Arbitrage pondéré + anti-dilution + gate ADX + gate session | — |

**Architecture saine** : le playbook n'est pas un votant parmi d'autres — aucun trade ne peut partir
contre lui, ni sans lui. Les autres agents **nuancent la confiance**, ils ne créent pas de trades.
C'est exactement le bon modèle pour une stratégie de desk.

---

## 3. Forces (à préserver absolument)

1. **Zéro donnée fictive** — `data_allow_synthetic=False`, le backtest refuse les replis
   synthétiques (`NoRealDataError`), quarantaine des clôtures impossibles. La leçon de l'incident
   du 26/07 (13 059 € de profit fictif) est codée en dur. C'est LA condition de crédibilité d'un
   SaaS de trading.
2. **Explicabilité totale** — chaque score est décomposé (votes pondérés, ajustement divergences,
   multiplicateur volume), glossaire pédagogique, narration en français en 7 sections. C'est le
   différenciateur produit n° 1 face aux « signaux boîte noire » du marché.
3. **Honnêteté de mesure** — walk-forward strict, frais + slippage, alpha vs buy & hold, couverture
   de données annoncée, passe 15 min qui dit elle-même qu'elle ne conclut rien (1 trade / 80 j).
4. **Filtres issus du backtest, pas de l'intuition** — divergence désactivée comme déclencheur
   (37,5 % vs 69 % pour la cassure), filtre de volatilité (ATR 1,39 % perdants vs 1,15 % gagnants).
5. **Garde-fous d'exécution** — auto-entrée papier uniquement, recalcul systématique avant ordre
   (jamais sur un snapshot), pas de doublon, garde de portefeuille, pas d'ouverture marchés fermés.
6. **Surface SaaS déjà large** — multi-tenant, plans free→enterprise avec gating 402
   (`core/plans.py`), TOTP, KYC, i18n, marketplace, copytrading, branding white-label, audit.

---

## 4. Faiblesses et risques (par gravité)

### Bloquants pour la production
1. **`use_in_memory_db: bool = True` par défaut** (`config.py:214`) — tout l'état (journal, track
   record, positions, edge map, entraînement) **disparaît à chaque redémarrage**. L'apprentissage
   et le forward test sont impossibles à capitaliser. Le passage Postgres est LE prérequis.
2. **`secret_key: str = "change-me"`** + clés API dans `.env` sans coffre — acceptable en dev,
   pas en prod multi-tenant.
3. **Pas de CI visible** — ~200 tests existent mais rien ne les exécute automatiquement à chaque
   push. Un SaaS sans CI casse en silence.
4. **Stripe incomplet** (un seul `stripe_price_starter`, pas de webhooks testés en prod, pas de
   portail client) — on ne peut pas encaisser.

### Structurels (trading)
5. **Dépendance aux données gratuites** — Yahoo (limites : 15 min ≈ 81 j, 1 h ≈ 2 ans, 422 en
   rafale), Binance pour le live. Un SaaS payant ne peut pas reposer sur des sources qui peuvent
   couper du jour au lendemain. OANDA est déjà prévu dans la config mais pas branché.
6. **Échantillons faibles par paire** — seuls GBP/JPY (46), XAG (40), EUR/JPY (34) ont un
   échantillon solide. Les décisions par paire doivent en tenir compte (cf. 02).
7. **Poids du Master fixés à la main** (`DEFAULT_WEIGHTS`) — l'apprentissage les module, mais la
   base n'a jamais été validée par une mesure. Peu grave tant que le playbook a le veto.
8. **La qualité de session n'a jamais été mesurée** — le gate session module la confiance, mais
   personne n'a vérifié que les trades en fenêtre « prime » gagnent plus (le backtest enregistre la
   session : la mesure est à une requête de distance).

### Produit / SaaS
9. **Surface trop large pour un lancement** — marketplace, copytrading, team, white-label, KYC :
   c'est de la maintenance et de la surface d'attaque avant le premier client. À geler (pas
   supprimer) jusqu'au product-market fit.
10. **Pas de page publique de track record** — c'est pourtant l'argument de vente n° 1 d'un produit
    dont le pitch est « transparence mesurée ».
11. **Monitoring partiel** — `/metrics` existe, Sentry optionnel non branché, pas d'alerte uptime.

---

## 5. Verdict

Le projet est **au-dessus du niveau d'un MVP** : la boucle signal → exécution → apprentissage est
fermée, la discipline de mesure est réelle, la stratégie a une espérance positive mesurée. Les deux
chantiers qui séparent l'état actuel d'une plateforme professionnelle rentable sont :

1. **Concentrer la stratégie là où elle est prouvée** (02_STRATEGIE_PLAYBOOK.md) — gain espéré le
   plus élevé pour le risque le plus faible ;
2. **Industrialiser** (03_PLAN_IMPLEMENTATION.md) — Postgres, CI, hébergement, encaissement, légal.

Aucun des deux ne demande d'invention : tout est de l'exécution ordonnée.
