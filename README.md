## 🎯 La stratégie du desk (playbook)

**Tous les agents appliquent la même méthode**, dans cet ordre strict — aucune étape ne peut être
sautée (implémentation : [`backend/app/domain/playbook.py`](backend/app/domain/playbook.py)) :

| # | Unité de temps | Ce qu'on y cherche |
|---|----------------|--------------------|
| 1 | **Mensuel + Journalier** | Tendance de fond et **supports / résistances majeurs**. Sans tendance claire : pas de trade. |
| 2 | **Journalier** | RSI 14 · MA 20 · MA 50 · volume · **tendance VWAP** · **divergences RSI et MACD** · **Fibonacci en cas de correction**. Doit confirmer le biais. |
| 3 | **4 heures** | Exactement les mêmes facteurs, seconde confirmation. |
| 4 | **1 heure** | **Dernière confirmation** avant de chercher l'entrée : filtre les cas où le 4 h est encore orienté dans notre sens alors que le mouvement s'est déjà retourné en dessous. |
| 5 | **15 minutes** | **La seule unité de temps d'entrée.** Déclencheur : repli sur MA/zone d'or Fibonacci + bougie de reprise, cassure confirmée par le volume, ou divergence. |

### Le risque, l'objectif, et la sécurisation du profit

- **Objectif ≥ 200 pips** et **R/R entre 1:2 et 1:3**. Conséquence arithmétique : le **stop** vaut
  entre 200/3 ≈ 67 et 200/2 = 100 pips. Un stop de cette taille ne peut pas venir de la structure
  15 min (quelques pips) : il est placé derrière la **structure 4 h**. Le 15 min donne le MOMENT de
  l'entrée, il ne porte pas le risque.
- **Sécurisation à +2R.** Dès qu'un trade a parcouru deux fois son risque, le stop est
  automatiquement remonté **sur +2R** : la position ne peut plus redevenir perdante, et on la laisse
  courir vers le R/R maximum. Le stop ne recule jamais.
- l'objectif doit rester **atteignable avant le niveau majeur opposé** (au moins 60 % de marge
  libre) et sous 4 × l'ATR journalier — l'**horizon estimé en jours** est affiché : à 200 pips,
  c'est un swing tenu plusieurs séances ;
- **fenêtres de session** : ouverture de Londres (07:00–10:00 UTC), ouverture de New York
  (12:30–15:30 UTC) et surtout leur **chevauchement (12:00–16:00 UTC)**.

### Marchés, et heures d'ouverture

Le desk travaille le **forex et l'or** : la stratégie est calibrée pour eux (objectifs en pips,
niveaux majeurs mensuels, horaires de Londres et New York). L'**analyse tourne en permanence**, y
compris marchés fermés — c'est ainsi qu'on arrive préparé à l'ouverture. Mais **aucune position
n'est ouverte** quand Londres ET New York sont fermées, ni le week-end : le carnet d'ordres est
vide, les écarts s'élargissent et les mouvements ne sont pas représentatifs.

### Unités de temps : des durées, pas des styles

Le projet ne parle plus de « scalp », « intraday », « swing » ou « position » — ces mots décrivent
une façon de trader, pas une échelle de temps, et deux personnes n'y mettent pas la même durée. Les
seules unités employées sont **15min · 1h · 4h · 1d · 1week · 1month**.

### Filtres issus du backtest (mesurés, pas supposés)

Deux réglages viennent directement des chiffres du backtest, et non d'une intuition :

- **Le déclencheur « divergence » n'ouvre plus de position.** Mesuré à 37,5 % de réussite et
  +0,19 R sur 16 trades, contre 69 % / +1,15 R pour la cassure et 58 % / +0,77 R pour le repli. Il
  reste **calculé et affiché** — une divergence contraire garde toute sa valeur d'avertissement — mais
  il ne sert plus d'entrée (`playbook_allow_divergence_entry`).
- **Filtre de volatilité.** Les trades stoppés ont un ATR journalier supérieur de 21 % à celui des
  gagnants (1,39 % contre 1,15 %) ; tous les autres facteurs (ADX, alignement, confiance, largeur
  relative du stop) sont identiques. La stratégie ne se trompe donc pas de direction, elle se fait
  sortir par le bruit. Au-delà de **1,3 % d'ATR journalier**, le stop est **élargi
  proportionnellement** (mode `adapt`, par défaut) plutôt que de refuser le trade : on garde le
  mouvement et on paie le vrai prix du risque, l'objectif suit (R/R × risque) et la taille de
  position se réduit d'elle-même. Le mode `refuse` est disponible pour ne simplement pas entrer.

### Un résultat n'est JAMAIS inventé

Le module de rejeu (`data/replay.py`) décide si de l'argent a été gagné ou perdu : c'est le plus
prudent du projet. Il a été corrigé après avoir produit des positions « gagnantes » à des prix que
le marché n'avait jamais atteints. Quatre règles le gouvernent désormais :

1. **Aucune conclusion sans données réelles.** Le repli synthétique horodatait ses bougies jusqu'à
   « maintenant » : elles passaient toutes pour postérieures à l'entrée et l'une d'elles finissait
   par franchir l'objectif.
2. **Seules les bougies postérieures à l'entrée comptent**, et celle qui *contient* l'entrée est
   écartée (son extrême a pu être atteint avant qu'on entre).
3. **Le stop l'emporte** quand stop et objectif tombent dans la même bougie.
4. **Une clôture ne peut pas précéder l'ouverture.** Les positions déjà enregistrées avec ce défaut
   sont mises en **quarantaine** : marquées `invalid`, P&L remis à zéro, exclues du taux de réussite
   et de l'apprentissage des agents. Ce qui n'a jamais eu lieu ne compte ni en gain ni en perte.

Quand le verdict est indéterminé (marché fermé, données en retard), la position **reste ouverte**.

### Zéro donnée fictive

Quand une source de marché est indisponible, les connecteurs **ne fabriquent rien** : ils renvoient
une série vide et la page affiche « données indisponibles » (réglage `data_allow_synthetic`, à
`false` par défaut). Chaque réponse de bougies porte sa **source** (`live` / `real` / `unavailable`).
Une donnée inventée affichée comme réelle est pire qu'une page vide : elle conduit à décider sur un
marché qui n'existe pas.

L'agent **playbook** dispose d'un **droit de veto** : si les conditions ne sont pas réunies, aucun
autre agent ne peut déclencher un trade, et jamais dans le sens opposé au playbook.

### 🤖 Entrée automatique — aucun clic

Un setup **🟡 ARMÉ** a validé les étapes 1 à 3 ; il ne lui manque que le déclencheur 15 min, qui
peut apparaître à n'importe quelle bougie. Attendre qu'un humain rafraîchisse une page et clique,
c'est rater l'entrée. Une veille tourne donc en continu (60 s par défaut) : elle **recalcule** la
stratégie pour chaque setup armé et ouvre la position **dès que le déclencheur se forme**, avec son
stop 15 min et son objectif borné 1 h, dimensionnée au profil de risque.

> **Compte DÉMO uniquement.** L'auto-entrée n'utilise que des connexions `paper` — une connexion
> réelle est ignorée, quel que soit son statut KYC. Aucun argent réel ne peut être engagé par cette
> boucle. Les garde-fous de portefeuille (positions max, plafond de risque) s'appliquent, et un même
> symbole/sens déjà ouvert n'est jamais doublé.

### ⚡ Pages instantanées

Appliquer la stratégie à 40 symboles × 5 unités de temps prend des dizaines de secondes. Tant que
c'est la page qui déclenche ce calcul, elle attend. Une **boucle de fond** recalcule donc la
sélection toutes les 45 s et publie un instantané ; les endpoints ne calculent plus rien et
répondent en **~10 ms**, ce qui permet aux pages de se rafraîchir toutes les 10 secondes. Chaque
réponse porte son âge (`age_seconds`, `stale`) : une donnée vieille n'est jamais présentée comme
fraîche. Le même instantané fournit un **verdict par symbole balayé**, que le scanner et les pages
d'analyse réutilisent — un symbole ne peut donc plus être noté différemment selon la page.

### 🎓 Entraînement quotidien des agents sur la stratégie

Chaque nuit, la stratégie est **rejouée sur l'historique réel** (walk-forward strict : à chaque
bougie 15 min, seules les données déjà disponibles sont utilisées). Trois sorties :

1. la **fiabilité mesurée** par symbole, par type de déclencheur et par fenêtre de session — c'est
   elle qui classe les 5 trades du jour ;
2. la **justesse mesurée de chaque facteur** (MA, RSI, MACD, VWAP, structure, volume, divergences),
   agrégée par agent en multiplicateur de poids : c'est ainsi que les agents deviennent experts
   **de cette** stratégie ;
3. une **fiche d'expertise** par agent, réinjectée dans ses prompts.

L'entraînement **refuse les données synthétiques** : une statistique tirée de bougies inventées est
pire qu'une absence de statistique, parce qu'elle a l'apparence d'une mesure. Un symbole non mesuré
reçoit un score neutre — il n'est ni favorisé ni pénalisé.

### Une explication rédigée, et une seule note lisible

Les cartes de prédiction n'affichent **aucun score technique brut**. À la place :

- une **explication rédigée en français** en 7 sections (tendance de fond → niveaux majeurs →
  confirmation journalière → confirmation 4 h → déclencheur 15 min → risque et objectif → moment de
  la journée → conclusion) qui donne tous les arguments du choix **ACHAT / VENTE / attente** ;
- chaque indicateur porte un **score de fiabilité de −5 à +5** : `+1 à +5` = argument d'achat,
  `−1 à −5` = argument de vente, `0` = ne tranche pas ; avec sa valeur mesurée, sa lecture, et au
  clic ce que l'indicateur mesure ;
- l'explication se termine par le **score de fiabilité du trade** : `+1..+5` pour un achat,
  `−1..−5` pour une vente, `0` quand aucun trade n'est justifié.

Chaque étape de la checklist se déplie pour expliquer ce qu'elle vérifie, et tout refus est motivé
avec le même soin qu'une prise de position.

### Les pages d'analyse se rafraîchissent toutes seules

Aucun bouton à cliquer pour voir des données à jour :

| Page | Cadence | Ce qui est recalculé |
|------|---------|----------------------|
| Trades du jour | 2 min | la stratégie complète est **recalculée**, pas relue depuis le cache |
| Paper Trading | 15 s | prix courant, **P&L latent**, progression vers l'objectif, multiple de R |
| Scanner | 3 min | rescan du marché (dès le premier scan lancé) |
| Dashboard | 30 s | portefeuille, risque, variations 24 h |

La boucle se met en pause quand l'onglet est masqué et rafraîchit immédiatement au retour.

---

Chaque agent est spécialisé sur une classe d'actifs ou un domaine d'analyse :

| Agent | Domaine | Rôle |
|-------|---------|------|
| 🎯 **Playbook Agent** | Stratégie | Applique la cascade ci-dessus, produit entrée/SL/TP et **oppose son veto** |
| ₿ **Crypto Agent** | Cryptomonnaies | Analyse crypto (funding, BTC lead) dans le cadre du playbook |
| 💱 **Forex Agent** | Devises | Analyse des paires de devises (filtre DXY) dans le cadre du playbook |
| 📊 **Stocks Agent** | Actions | Analyse actions (régime SPX, gaps) dans le cadre du playbook |
| 🥇 **Gold Agent** | Or & métaux | Dollar, taux réels, VIX, niveaux ronds |
| 🌍 **Macro Agent** | Contexte économique | Filtrage via calendrier économique et régime de marché |

L'**orchestrateur** (Master) collecte les signaux, applique un scoring de consensus pondéré et le
timing de session — mais reste **soumis au veto du playbook**.

---

## ✨ Fonctionnalités

| Fonctionnalité | Description |
|----------------|-------------|
| 🎯 **5 trades par jour** | `GET /api/signals/top-trades` — les meilleurs setups de TOUT l'univers balayé, **classés par fiabilité mesurée** (walk-forward), étiquetés `ready` (exécutable) ou `armed`. Sert l'instantané pré-calculé : ~10 ms, avec son âge |
| 🤖 **Entrée automatique** | `GET /api/signals/auto-entry` — ce que le robot surveille et ce qu'il a ouvert seul. Les setups armés sont pris **sans aucun clic** dès le déclencheur 15 min, en **compte démo exclusivement** |
| 🧪 **Ouverture en compte démo** | `POST /api/execution/playbook/execute` — ouvre les setups prêts en **papier** avec leur SL/TP, dimensionnés au % de risque du profil. Aucune clé broker requise, aucun argent réel. Surveillance et clôture automatiques au SL/TP |
| 🎓 **Entraînement des agents** | `GET /api/agents/training` — walk-forward de la nuit : réussite par symbole / déclencheur / session, justesse de chaque facteur, multiplicateurs de poids et fiches d'expertise |
| 🔍 **Détail des 4 étapes** | `GET /api/signals/playbook/{symbole}` — checklist expliquée, facteurs de chaque unité de temps avec leur contribution au score, niveaux majeurs |
| 🔥 **Veille des sessions** | Recalcul + alerte à l'ouverture de Londres, de New York, et pendant leur chevauchement |
| 🎯 **Signaux de consensus** | Agrégation pondérée des signaux de plusieurs agents |
| 📉 **Détection de régime de marché** | Identification des phases (tendance, range, volatilité) |
| ⏮️ **Backtesting** | Test des stratégies sur données historiques |
| 📅 **Filtrage économique** | Prise en compte du calendrier économique |
| ⚡ **Temps réel** | Traitement continu via infrastructure scalable |

---

## 🛠️ Stack technique

**Backend / IA** — Python · FastAPI · LangGraph (orchestration multi-agents)
**Frontend** — Next.js · React
**Data & Infra** — TimescaleDB (séries temporelles) · Redis (cache temps réel)
**Architecture** — Système multi-agents (agents spécialisés, consensus, orchestration)

---

## 📸 Aperçu

<!-- Ajoute tes captures ici :
![Dashboard](./screenshots/dashboard.png)
-->
*Captures d'écran à venir.*

---

## ⚠️ Avertissement

Ce projet est développé à des fins éducatives et de recherche. Il ne constitue pas un conseil en investissement. Le trading comporte des risques de perte en capital.

---

<p align="center"><i>Quantum Trade AI — Plusieurs agents, une décision éclairée.</i></p>


=== PASSE PORTEE 1h ===
2358 trades | 38.1% | +0.139 R | PF 1.22 | DD 57.79 R | total 326.77 R
annees: 0.96 | paires testees: 48

VERDICT: marginal
Sur 0.96 ans et 48 paires, la stratégie a produit 2358 trades : 899 gagnants et 1459 perdants, soit 38.1 % de réussite, pour une espérance de +0.14 R par trade et un profit factor de 1.22. Verdict global : marginal.

Indices: 6/6 rentables -- JPN225(+0.59R), NAS100(+0.53R), SPX500(+0.50R), US30(+0.40R), GER40(+0.37R)
Actions: 1/1 rentables -- AAPL(+0.51R)
Forex: 10/10 rentables -- USD/CAD(+0.40R), USD/JPY(+0.25R), AUD/JPY(+0.23R), EUR/USD(+0.22R), USD/CHF(+0.20R)
Métaux précieux: 3/4 rentables -- XPT/USD(+0.11R), XPD/USD(+0.09R), XAG/USD(+0.01R), XAU/USD(-0.30R)
Crypto: 0/0 rentables --