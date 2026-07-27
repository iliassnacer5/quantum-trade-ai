# 05 — État d'implémentation du plan (27 juillet 2026)

> Réalisé en une passe le 27/07/2026 sur la branche `feat/playbook-strategy`.
> Suite de tests : **362 verts** (342 existants + 20 nouveaux, `tests/test_plan_phases.py`).

## PHASE 1 — Fondations production

| # | Tâche | État |
|---|---|---|
| 1.1 | Postgres par défaut | ✅ déjà câblé (`USE_IN_MEMORY_DB=false` dans compose) ; `test_postgres_switch` vert |
| 1.2 | Backups | ✅ service `db-backup` (pg_dump quotidien gzip, rétention 14 j, volume `pgbackups` séparé) + [docs/BACKUPS.md](../docs/BACKUPS.md). ⏳ Reste : dérouler la restauration une fois et noter la date |
| 1.3 | Secrets | ✅ `enforce_prod_secrets` : refus de boot en `ENVIRONMENT=production` avec `change-me` ou secret < 16 caractères (testé) |
| 1.4 | CI | ✅ `.github/workflows/ci.yml` refondu : build image + pytest DANS le conteneur, toutes branches. ⏳ Reste : pousser sur GitHub + protéger la branche (action GitHub, pas code) |
| 1.5 | Observabilité | 🟡 Sentry câblé (`sentry_dsn`, no-op si vide) ; UptimeRobot/Grafana = comptes externes à créer |
| 1.6 | Tests | ✅ 361 verts en Docker |

## PHASE 2 — Stratégie mesurée

| # | Tâche | État |
|---|---|---|
| 2.1 | Verdict par paire | ✅ `services/verdict_service.py` + record `playbook_pair_verdicts`. 🟢 = ≥ +0,4 R, n ≥ 20, **2 passages consécutifs** ; 🔴 = espérance ≤ 0 (n ≥ 8) ; 🟡 = reste. Une paire absente d'un passage perd son vert. Relancer 2× le même jour ne fabrique pas de série |
| 2.2 | Gating auto-entrée | ✅ `playbook_pair_gating=True` : l'auto-entrée ne trade que les 🟢 ; refus journalisés (`gate_refusal`, avec niveaux — rejouables au rituel hebdo) |
| 2.3 | Matrice paire × déclencheur | ✅ `by_pair_trigger` dans le backtest ; déclencheur < +0,4 R (n ≥ 15) désactivé PAR paire (`disabled_triggers`) |
| 2.4 | A/B volatilité | ✅ code + **run exécuté le 26/07 au soir** : adapt 247 trades / 64,8 % / **+0,98 R** · refuse 198 / 63,1 % / +0,94 R · stop k×ATR4h 380 / 51,3 % / +0,55 R. **Gagnante : « adapt » — qui est déjà le réglage de production** (`playbook_volatility_mode="adapt"`) : la décision est FIGÉE, rien à changer. Record `playbook_volatility_ab`, endpoints `GET/POST /api/backtest/playbook/volatility-ab` |
| 2.5 | Ventilation par session | ✅ existait déjà (`by_session` + conclusion rédigée) |
| 2.6 | Comparatif +2R on/off | ✅ chaque trade rejoué 2× (avec/sans sécurisation) → bloc `secure_ab` du rapport |
| 2.7 | UI verdicts | ✅ `PairVerdictsPanel` : tableau complet sur /edge, chips compactes sur /today + refus récents |
| 2.8 | OANDA | ❌ externe : exige un compte practice + clé API (config `oanda_api_key` prête) |

## PHASE 3 — Sizing & risque portefeuille

| # | Tâche | État |
|---|---|---|
| 3.1 | Sizing par conviction | ✅ 1 % base × 1,25 (🟢 n ≥ 30) / × 0,5 (🟡/🔴), plafond absolu 1,5 % (testé, y compris le plafonnement d'un profil agressif) |
| 3.2 | Garde de corrélation | ✅ max 2 positions ouvertes partageant une devise (testé) |
| 3.3 | Gels −3 % / −6 % | ✅ `risk_service.entries_frozen` (P&L réalisé, jour/semaine) + notification unique/jour + application dans `execute_playbook_trades` (testé : journée simulée à −3,1 %) |
| 3.4 | Journal enrichi | ✅ chaque ordre porte `pair_verdict`, `trigger_type`, `session_window`, `atr_pct`, `conviction_mult`, `risk_pct` |

## PHASES 4-6 — non réalisables en code

- **Phase 4 (forward test)** : du temps calendaire (4-8 semaines). Tout l'outillage est prêt :
  gating 🟢, sizing, gels, « trades évités », backtest dominical 3 h UTC.
- **Phase 5 (SaaS)** : exige des ressources externes — VPS, domaine, compte Stripe, avis d'avocat,
  DSN Sentry, compte Resend/Telegram. Le code (billing, onboarding, plans) existe déjà en partie.
- **Phase 6** : rituel d'exploitation, démarre après la Phase 5.

## Réglages ajoutés (core/config.py)

`playbook_pair_gating`, `playbook_verdict_min_expectancy/min_trades/green_streak/red_min_trades`,
`playbook_trigger_matrix_gating/min_trades/min_expectancy`, `conviction_sizing_enabled/green_mult/
green_min_trades/yellow_mult/risk_cap_pct`, `correlation_guard_enabled`, `max_positions_per_currency`,
`loss_freeze_enabled`, `daily_loss_freeze_pct`, `weekly_loss_freeze_pct`.

## Résultats MESURÉS (backtest du 26/07/2026 au soir, nouveau code, 283 trades / 14 paires)

- **Global (passe portée 1 h)** : 283 trades, **61,5 %** de réussite, **+0,89 R** d'espérance,
  **PF 3,34**, pire série 6 R. (Amélioration vs 209 trades / +0,78 R / PF 2,86 de la veille : le
  backtest applique désormais les MÊMES filtres que la production — divergence coupée, volatilité
  « adapt » — ce qui n'était pas le cas avant.)
- **Sous-ensemble candidat 🟢** (esp ≥ 0,4 R et n ≥ 20 au dernier passage : EUR/JPY, AUD/JPY,
  GBP/JPY, USD/JPY, XAG/USD) : **184 trades, 65,2 %, +1,00 R, PF 3,92** →
  **critère de sortie Phase 2 atteint** (exigeait ≥ +0,9 R / PF ≥ 3).
- **Simulation sizing sur les 283 trades** (critère Phase 3) : risque plat 1 % → +250,8 unités,
  drawdown max 6,0 ; sizing par conviction en régime de croisière → +248,7 unités (−0,8 %),
  **drawdown max 5,25 (−12,5 %)** → **critère atteint** (drawdown réduit sans perte d'espérance).
- **Sécurisation +2R on/off (2.6)** : la règle a modifié l'issue de 103/283 trades et coûte
  **−0,04 R par trade** (+0,89 avec, +0,93 sans). Prix modeste pour la réduction de variance ;
  la règle reste (décision utilisateur).
- **Matrice paire × déclencheur (2.3)** : « repli » désactivé sur **XAU/USD** et **GBP/USD**
  (mesuré < +0,4 R avec n ≥ 15). Les autres cellules n'ont pas l'échantillon pour conclure.
- **Verdicts** : 12 paires 🟡, dont 5 à un passage vert. Les deux runs du 26/07 comptent pour UN
  seul passage (garde anti-« série fabriquée », même jour UTC) : les 🟢 se décideront au prochain
  passage hebdomadaire (dimanche 03:00 UTC) — ou à un run manuel un autre jour.

## Conséquence à connaître

Tant que DEUX backtests hebdomadaires consécutifs n'ont pas confirmé une paire, **aucune paire
n'est 🟢 et l'auto-entrée n'ouvre rien** (les refus sont journalisés et visibles sur /today et
/edge). C'est voulu par le plan : on ne trade que là où c'est prouvé deux fois.
