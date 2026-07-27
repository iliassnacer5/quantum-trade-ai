# 04 — Le chemin SaaS vers 1 M$ : honnête, chiffré, par jalons

> Franchise d'abord : **personne ne peut promettre 1 M$**, et un plan qui le promettrait serait le
> premier signe qu'il ne faut pas lui faire confiance. Ce document fait ce qu'un plan sérieux peut
> faire : poser l'arithmétique, choisir un positionnement défendable, et découper le chemin en
> jalons dont chacun est atteignable, mesurable, et conditionne l'investissement du suivant.

---

## 1. L'arithmétique de 1 M$ ARR

1 M$ de revenu annuel récurrent, selon le prix moyen par abonné :

| Prix moyen/mois | Abonnés payants requis | Ordre de grandeur d'audience (conversion visiteur→payant ~1 %) |
|---|---|---|
| 29 $ | ~2 900 | ~290 000 visiteurs qualifiés |
| 49 $ | ~1 700 | ~170 000 |
| 79 $ | ~1 060 | ~106 000 |
| 149 $ (pro/prop) | ~560 | ~56 000 |

Enseignement : **1 M$ est un problème de distribution bien plus que de produit**. Le produit
(phases 1-5) doit être bon ; mais c'est l'acquisition et la rétention qui feront le chiffre.

## 2. Positionnement — ce qu'on vend (et ce qu'on ne vend PAS)

**On ne vend jamais** : « des signaux gagnants », « X % de win rate », « devenez rentable ». C'est
illégal en substance (conseil en investissement non agréé + promesse de performance) et c'est le
positionnement de tous les vendeurs de rêve — zone concurrentielle saturée et toxique.

**On vend** : *le desk de trading discipliné que 95 % des traders n'arriveront jamais à être
eux-mêmes*.
1. **Transparence radicale** : track record public horodaté, chaque décision décomposée au chiffre
   près (le narrateur 7 sections existe déjà) — personne d'autre ne montre ses trades perdants ;
2. **Discipline automatisée** : la cascade 5 étapes appliquée à 100 %, jamais de FOMO, veto
   documenté, « trades évités » ;
3. **Mesure permanente** : backtest hebdo, verdicts par paire, forward test — l'utilisateur SAIT
   ce qui marche et où.

Cible initiale : traders forex particuliers sérieux (déjà perdants par indiscipline, pas par
ignorance) + candidats aux prop firms (FTMO etc. — un marché énorme qui a précisément besoin de
discipline de risque : le stop quotidien −3 % du plan est exactement leur contrainte).

## 3. Pricing proposé

| Plan | Prix | Contenu (mapping `core/plans.py` existant) |
|---|---|---|
| Free | 0 | 1 analyse playbook/jour, track record public, pédagogie — c'est le funnel |
| Starter | 29 $/mois | analyses illimitées, alertes multi-canaux, digest quotidien |
| Pro | 79 $/mois | auto-entrée papier, journal + apprentissage, backtest, copilot |
| Elite | 149 $/mois | API, exécution auto (démo→réel selon légal), priorité |

Annuel −20 %. Le palier Pro est le cœur de gamme : 1 060 abonnés Pro ≈ 1 M$.

## 4. Les jalons — chaque euro d'effort conditionné par le précédent

### Jalon 0 → 10 clients payants (objectif : 3 mois après la Phase 5)
- Canal : contenu gratuit à forte preuve — publier CHAQUE SEMAINE la revue du forward test
  (les vrais chiffres, gains ET pertes) sur X/Twitter, un subreddit forex, YouTube court.
  Le track record public est l'aimant ; personne d'autre ne fait ça honnêtement.
- Critère de passage : 10 payants ET churn mensuel < 15 % ET au moins 3 retours utilisateurs
  utilisés. Sinon : le problème est le produit ou la cible, pas le volume — corriger avant de payer
  de l'acquisition.

### Jalon 10 → 100 (MRR ~5-8 k$, objectif : +6 mois)
- Doubler le contenu (SEO : « stratégie multi-timeframe forex », « journal de trading automatisé »),
  programme d'affiliation 20 % récurrent (les communautés trading vivent d'affiliation),
  partenariats avec 2-3 éducateurs trading honnêtes.
- Produit : onboarding mesuré (activation = a vu son premier playbook expliqué < 5 min),
  version mobile stabilisée (le dossier `mobile/` existe).
- Critère : coût d'acquisition < 3 mois de revenu par client ; churn < 8 %.

### Jalon 100 → 1 000 (MRR ~50-80 k$ → **1 M$ ARR au voisinage de 1 000-1 200 Pro**)
- Acquisition payante (uniquement maintenant : les unit economics sont prouvées), localisation
  EN/FR (l'i18n existe), segment prop-firm packagé (règles de risque FTMO préconfigurées),
  éventuellement B2B white-label (le module branding existe — c'est ICI qu'il ressort du gel).
- Équipe : à ~30 k$ MRR, un support/community à temps partiel ; à ~60 k$, un dev.
- Critère permanent : churn < 6 %, NPS mesuré, marge brute > 80 % (surveiller le budget LLM —
  garde `llm_daily_budget_usd` déjà codée).

**Horizon réaliste** : 24-36 mois si chaque jalon passe. Un SaaS à 1 M$ ARR en moins de 2 ans est
l'exception, pas la règle — planifier plus court, c'est planifier une déception.

## 5. Risques majeurs et parades

| Risque | Parade |
|---|---|
| **Légal** : requalification en conseil en investissement (AMF/MiFID) | Avis d'avocat AVANT le 1er payant (Phase 5.5) ; formulation « outil d'aide à la décision » partout ; jamais de promesse de gain ; disclaimers déjà dans le produit à auditer |
| Le forward test échoue (No-Go) | Le SaaS reste vendable : la valeur vendue est la discipline + transparence + journal, pas la performance. Pivot possible : « journal + copilote de discipline » (marché réel) |
| Coupure des données gratuites | OANDA branché en Phase 2.8 ; budget données payantes dès 1 k$ MRR |
| Solo founder / bus factor | Tout est documenté + CI + backups (Phase 1) ; le produit tourne seul par design |
| Churn élevé (générique du trading : les clients perdants partent) | Le produit doit rendre l'utilisateur MEILLEUR (journal, trades évités, pédagogie) — la rétention vient de la progression ressentie, pas des gains promis |

## 6. Ce qu'il faut retenir

1. Le produit est presque prêt ; **la stratégie doit finir son forward test avant tout marketing
   de performance** — et n'en fera jamais, de toute façon (légal).
2. Le différenciateur est déjà codé : explicabilité + honnêteté de mesure. Le travail commercial
   consiste à le **montrer** (track record public, revue hebdo publiée).
3. 1 M$ = ~1 000 clients Pro fidèles = un problème de distribution patiente par jalons, chacun
   validé avant de financer le suivant. C'est le même principe que la stratégie de trading :
   **on n'augmente la mise que là où l'edge est prouvé.**
