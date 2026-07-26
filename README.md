## 🎯 La stratégie du desk (playbook)

**Tous les agents appliquent la même méthode**, dans cet ordre strict — aucune étape ne peut être
sautée (implémentation : [`backend/app/domain/playbook.py`](backend/app/domain/playbook.py)) :

| # | Unité de temps | Ce qu'on y cherche |
|---|----------------|--------------------|
| 1 | **Mensuel + Journalier** | Tendance de fond et **supports / résistances majeurs**. Sans tendance claire : pas de trade. |
| 2 | **Journalier** | RSI 14 · MA 20 · MA 50 · volume · **tendance VWAP** · **divergences RSI et MACD** · **Fibonacci en cas de correction**. Doit confirmer le biais. |
| 3 | **4 heures** | Exactement les mêmes facteurs, seconde confirmation. |
| 4 | **15 minutes** | **La seule unité de temps d'entrée.** Déclencheur : repli sur MA/zone d'or Fibonacci + bougie de reprise, cassure confirmée par le volume, ou divergence. |

Contraintes non négociables appliquées ensuite :

- **Risque / rendement encadré entre 1 : 1,2 et 1 : 1,3** (dans tous les modes de sévérité). La
  bande est resserrée volontairement : un objectif proche du risque est atteint bien plus souvent,
  et le plafond interdit un stop trop serré (qui gonflerait le R/R affiché tout en faisant sauter
  la position au premier soubresaut) ;
- **objectif minimum 200 pips** (équivalence documentée hors forex, cf. `domain/pips.py`) ;
- **stop recalé sur la structure 4 h** si le stop 15 min est trop serré pour l'objectif : viser
  200 pips avec un R/R plafonné à 1,3 impose un stop d'au moins ~154 pips, placé derrière la
  structure 4 h et non sur le bruit 15 min ;
- l'objectif doit être **atteignable avant le niveau majeur opposé** et rester sous 4 × l'ATR
  journalier — l'**horizon estimé** du trade (en jours) est calculé et affiché : à 200 pips, c'est
  un swing tenu plusieurs jours ;
- **fenêtres de session** : ouverture de Londres (07:00–10:00 UTC), ouverture de New York
  (12:30–15:30 UTC) et surtout leur **chevauchement (12:00–16:00 UTC)** — la veille automatique
  recalcule les trades du jour à chaque entrée dans une de ces fenêtres.

L'agent **playbook** dispose d'un **droit de veto** : si les conditions ne sont pas réunies, aucun
autre agent ne peut déclencher un trade, et jamais dans le sens opposé au playbook.

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
| 🎯 **5 trades par jour** | `GET /api/signals/top-trades` — les meilleurs setups conformes à la stratégie, étiquetés `ready` (exécutable) ou `armed` (en attente du déclencheur 15 min) |
| 🧪 **Ouverture en compte démo** | `POST /api/execution/playbook/execute` — ouvre les setups prêts en **papier** avec leur SL/TP, dimensionnés au % de risque du profil. Aucune clé broker requise, aucun argent réel. Surveillance et clôture automatiques au SL/TP |
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
