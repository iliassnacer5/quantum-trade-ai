# PLAN GLOBAL — Quantum Trade AI : plateforme complète, stratégie renforcée, SaaS rentable

> Rédigé le 26 juillet 2026, après analyse complète du code (backend, agents, pipelines, frontend,
> infra) et des résultats **mesurés** de la plateforme. Ce plan complète le `docs/PLAN_MAITRE.md`
> (orienté carte de l'edge multi-stratégies) en se concentrant sur la **stratégie playbook**, la
> **mise en production** et le **chemin commercial**.

## Les 4 documents

| Fichier | Contenu |
|---|---|
| [01_ANALYSE_PROJET.md](01_ANALYSE_PROJET.md) | Diagnostic complet : architecture, agents, pipelines, fonctionnalités — forces et faiblesses |
| [02_STRATEGIE_PLAYBOOK.md](02_STRATEGIE_PLAYBOOK.md) | Analyse de la stratégie + **7 améliorations classées par preuve mesurée**, avec critère de validation pour chacune |
| [03_PLAN_IMPLEMENTATION.md](03_PLAN_IMPLEMENTATION.md) | Feuille de route en 6 phases : fondations → stratégie → risque → forward test → production → exploitation quotidienne |
| [04_SAAS_1M.md](04_SAAS_1M.md) | Chemin commercial vers 1 M$ ARR : pricing, économie unitaire, jalons, légal, go-to-market |

## Résumé exécutif

**Ce qui est vrai aujourd'hui (mesuré, pas supposé) :**
- La stratégie playbook backtestée (passe 1 h, 1,9 an, 12 paires) donne **209 trades, 57,9 % de
  réussite, +0,78 R d'espérance, profit factor 2,86**. C'est un résultat sérieux — les meilleurs
  fonds quantitatifs vivent avec des PF de 1,1–1,3.
- Mais l'espérance est **très inégale selon les paires** : USD/CHF +1,50 R contre USD/CAD +0,20 R.
  Le levier le plus puissant et le moins risqué n'est PAS de complexifier la stratégie : c'est de
  **ne trader que là où elle est prouvée** (cf. 02, amélioration A).
- La machine est excellente (discipline, transparence, zéro donnée fictive, walk-forward honnête).
  Ce qui manque pour une plateforme « prête à héberger et à utiliser au quotidien » est surtout de
  l'**industrialisation** : persistance Postgres par défaut, CI, backups, HTTPS, Stripe complet,
  monitoring — pas de nouvelles features.

**Ce qu'aucun plan ne peut promettre :** un win rate, un revenu de trading, ou 1 M$ de chiffre
d'affaires. Ce plan définit des critères de passage mesurables à chaque étape (Go/No-Go), de sorte
qu'à tout moment on **sait** si l'on est sur la trajectoire — et qu'on s'arrête ou pivote quand les
chiffres le disent, avant de brûler du capital ou du temps.

## Ordre d'exécution

```
Phase 1  Fondations production        (2-3 j)   Postgres, CI, backups, secrets, Sentry
Phase 2  Stratégie mesurée           (3-5 j)   carte de l'edge DU PLAYBOOK + A/B volatilité/sécurisation
Phase 3  Sizing & risque portefeuille (2 j)    Kelly/4 par paire, stop quotidien, corrélation devises
Phase 4  Forward test discipliné     (4-8 sem) papier only, revue hebdo, Go/No-Go chiffré
Phase 5  SaaS production             (1-2 sem) VPS+HTTPS, Stripe, onboarding, légal — EN PARALLÈLE de 4
Phase 6  Exploitation & croissance   (continu) rituel quotidien, track record public, acquisition
```

La règle d'or reste celle du PLAN_MAITRE : **une idée qui n'améliore pas les chiffres out-of-sample
n'entre pas en production**, quelle que soit sa beauté théorique.
