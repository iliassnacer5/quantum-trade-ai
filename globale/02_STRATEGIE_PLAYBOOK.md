# 02 — Stratégie playbook : analyse et améliorations pour la rendre plus profitable

> Principe directeur : **on n'améliore pas une stratégie en l'imaginant, on l'améliore en la
> mesurant**. Chaque proposition ci-dessous part d'un chiffre déjà mesuré par la plateforme, décrit
> l'implémentation, et fixe le critère chiffré qui décidera si elle entre en production. Une idée
> qui échoue à son critère est abandonnée — pas « ajustée jusqu'à ce qu'elle passe » (overfitting).

---

## 1. La stratégie telle qu'elle est (rappel)

Cascade stricte à 5 étapes (`domain/playbook.py`) :
1. **Mensuel + Journalier** → tendance de fond + niveaux MAJEURS (sans tendance : pas de trade)
2. **Journalier** → RSI14, MA20/50, volume, VWAP, divergences, Fibonacci si correction : confirme
3. **4 h** → mêmes facteurs : deuxième confirmation (et structure qui porte le stop)
4. **1 h** → dernière confirmation
5. **15 min** → SEULE unité d'entrée : repli qualifié, cassure confirmée, (divergence désactivée)

Niveaux : objectif ≥ 200 pips, R/R 1:2–1:3 ⇒ stop 67–100 pips sur structure 4 h. Sécurisation :
à +2R le stop monte SUR +2R. Marchés : forex + or. Ouverture : Londres ou New York ouvertes.

## 2. Ce que le backtest a mesuré (26/07/2026, passe 1 h, 1,9 an, 12 paires)

**Global : 209 trades · 57,9 % · +0,78 R/trade · PF 2,86 → exploitable.**

| Paire | Espérance (R) | Trades | Lecture |
|---|---|---|---|
| USD/CHF | **+1,50** | faible n | excellent mais échantillon à confirmer |
| AUD/JPY | +1,10 | moyen | fort |
| GBP/JPY | +0,97 | **46** | fort ET solide statistiquement |
| EUR/JPY | +0,83 | 34 | fort et solide |
| XAG/USD | +0,51 | 40 | correct et solide |
| GBP/USD | +0,43 | moyen | correct |
| XAU/USD | +0,29 | moyen | faible |
| USD/CAD | +0,20 | faible | marginal |

Autres mesures : forex +0,93 R vs métaux +0,43 R ; **paires en yen dominantes** ; déclencheur
cassure 69 % / +1,15 R vs repli 58 % / +0,77 R vs divergence 37,5 % / +0,19 R ; les trades stoppés
ont un ATR journalier +21 % (1,39 % vs 1,15 %).

Déjà appliqué dans le code : divergence désactivée comme déclencheur (`playbook_allow_divergence_entry=False`),
filtre de volatilité en mode « adapt » (élargissement du stop, `MAX_ATR_PCT=1.3`).

**Limites de la mesure (à garder en tête)** : la passe exploitable évalue le déclencheur en 1 h
(proxy du vrai 15 min, faute d'historique) ; 209 trades sur 1,9 an c'est correct globalement mais
mince par paire ; aucune donnée USD/JPY ni EUR/CHF.

---

## 3. Les améliorations, classées par solidité de la preuve

### A. Sélectivité par paire — « la carte de l'edge du playbook » ⭐ priorité n° 1

**Preuve** : l'écart +1,50 R ↔ +0,20 R entre paires est LE plus gros signal du backtest. Couper les
paires sous +0,4 R (XAU/USD, USD/CAD) augmente mécaniquement l'espérance moyenne du portefeuille
sans toucher à la stratégie — c'est le même principe que `auto_trade_green_only` de la carte de
l'edge, jamais appliqué au playbook.

**Implémentation**
- Le backtest hebdomadaire (`playbook_backtest.py`, dimanche 3 h) écrit déjà ses résultats par
  paire. Ajouter un verdict par paire : 🟢 (espérance ≥ +0,4 R ET n ≥ 20), 🟡 (positif mais
  échantillon insuffisant → analysable, pas auto-tradable), 🔴 (< +0,2 R → exclu de l'auto-entrée).
- `auto_entry_service` et `daily_top_trades` filtrent sur ce verdict (nouveau réglage
  `playbook_pair_gating: bool = True`, seuils configurables).
- Stabilité façon `green_streak` : une paire ne devient 🟢 qu'après **2 backtests hebdomadaires
  consécutifs** au-dessus du seuil — un edge qui clignote n'est pas un edge.
- L'interface (page /today, /edge) affiche le verdict par paire avec les chiffres.

**Critère de validation** : sur le backtest, le sous-ensemble 🟢 doit montrer une espérance
≥ +0,9 R et un PF ≥ 3 (vs +0,78/2,86 toutes paires). En forward test : PF du portefeuille filtré
supérieur au non-filtré sur 4 semaines.

### B. Politique de déclencheur par paire

**Preuve** : cassure 69 % / +1,15 R contre repli 58 % / +0,77 R. La divergence est déjà coupée ;
la question restante est : le repli mérite-t-il d'exister partout ?

**Implémentation** : le backtest hebdo ventile déjà par type de déclencheur. Produire la matrice
paire × déclencheur ; désactiver le repli sur les paires où il est mesuré < +0,4 R (réglage
`playbook_trigger_policy` par paire, défaut « tous sauf divergence »).

**Critère** : n ≥ 15 par cellule avant toute désactivation. Espérance du portefeuille en hausse
sur le backtest suivant, PAS de baisse du nombre de trades > 40 % (sinon on affame le forward test).

### C. A/B du filtre de volatilité : « adapt » vs « refuse » vs stop proportionnel à l'ATR

**Preuve** : le +21 % d'ATR des perdants est mesuré, mais le mode « adapt » (élargir le stop) a été
choisi **sans avoir mesuré son effet**. Élargir le stop garde le trade mais dégrade le R réalisé
quand il perd ; refuser sacrifie des gagnants. Troisième option jamais testée : stop = k × ATR(4h)
borné par la bande 67–100 pips (le stop respire avec le marché au lieu d'être élargi après coup).

**Implémentation** : `playbook_backtest.py` accepte déjà les surcharges de config — exécuter les 3
variantes sur les mêmes données (1 run chacune, pas d'itération), comparer espérance/PF/drawdown.

**Critère** : on garde la variante gagnante en espérance à drawdown non aggravé. **Un seul A/B**,
puis on fige (anti-overfitting).

### D. Mesurer la fenêtre de session (avant de la sur-pondérer)

**Preuve manquante** : le gate session module la confiance et le texte, mais son effet sur le
résultat n'a jamais été mesuré. Le backtest enregistre la session de chaque trade simulé.

**Implémentation** : ajouter au rapport hebdo la ventilation win/espérance par fenêtre (Londres,
NY, chevauchement, hors fenêtre). Si « hors fenêtre prime » est mesuré négatif avec n ≥ 20 →
passer `prime` de « réduit la conviction » à « bloque l'auto-entrée ».

**Critère** : décision uniquement sur n ≥ 20 par fenêtre.

### E. Sécurisation +2R : la mesurer, elle aussi

**Preuve indirecte** : l'A/B des sorties côté crypto (12/12 pour `tp_only`) a montré que breakeven
et trailing **tronquaient les gagnants**. La règle +2R du playbook est une exigence utilisateur et
elle est défendable (verrouiller 2R sur un objectif à 2–3R coûte peu) — mais elle n'a jamais été
chiffrée sur le forex. Le rejeu (`replay_trade`) applique déjà la règle : il suffit d'un run avec
`secure_at_r` désactivé pour connaître son coût/bénéfice réel.

**Implémentation** : un run comparatif dans le backtest hebdo, affiché à titre informatif.
**La règle reste en place** (décision utilisateur) — mais on saura ce qu'elle coûte ou rapporte,
et l'utilisateur tranchera sur des chiffres.

### F. Données : passer du gratuit au fiable (OANDA), et enfin valider le vrai 15 min

**Preuve** : la passe fidélité 15 min ne conclut rien (1 trade / 81 jours d'historique Yahoo).
Tant qu'elle n'est pas concluante, la « vérité opérationnelle » de la stratégie est la passe 1 h —
c'est honnête, mais l'entrée réelle se fait en 15 min : l'écart entre les deux n'est pas mesuré.

**Implémentation** : brancher OANDA (clés déjà prévues dans la config, compte practice gratuit,
historique 15 min de plusieurs années, vrais spreads forex). Puis relancer la passe fidélité sur
2+ ans de 15 min réel. C'est aussi la réponse au risque « Yahoo coupe l'accès » pour un SaaS payant.

**Critère** : la passe 15 min sur données OANDA doit confirmer l'ordre de grandeur de la passe 1 h
(espérance > +0,4 R). Si elle l'infirme → c'est une information capitale à avoir AVANT tout argent
réel, et le forward test papier tranche.

### G. Méta-filtrage (plus tard, ≥ 100 trades forward)

Quand le journal contiendra ≥ 100 trades playbook réels (papier), entraîner un filtre logistique
simple (features : fiabilité contexte, alignement, ATR %, session, paire, déclencheur) qui prédit
la probabilité de gain. Gate optionnel, validé OOS. **Ne pas le faire avant** : 100 trades à ~10-15
trades/mois = c'est un chantier pour dans 6+ mois, le mettre en route trop tôt = apprendre du bruit.

---

## 4. Sizing et risque portefeuille (multiplie le résultat, ne change pas la stratégie)

1. **Sizing par conviction mesurée** : risque de base 1 % ; × 1,25 sur les paires 🟢 à n ≥ 30 ;
   × 0,5 sur les 🟡. Jamais plus de 1,5 % (fraction de Kelly ÷ 4, plafonnée — avec 57,9 % / RR 2,
   Kelly plein ≈ 37 %, donc ÷4 plafonné reste très conservateur, c'est voulu : Kelly plein ruine
   sur une série noire).
2. **Corrélation devises** : 3 positions JPY simultanées = UN pari sur le yen, pas trois trades.
   Limite : 2 positions maximum partageant une même devise (`paper_portfolio_guard` étendu).
3. **Stop de perte quotidien** : −3 % du capital papier / jour → gel des entrées jusqu'au
   lendemain. Protège des journées où le régime change plus vite que le filtre de volatilité.
4. **Limite hebdomadaire** : −6 % / semaine → gel + revue manuelle. Un système qui perd 6 % en une
   semaine ne doit pas continuer sans qu'un humain regarde.

---

## 5. Ce qu'on ne fera PAS (et pourquoi)

- **Ajouter des indicateurs** (Ichimoku, order blocks, etc.) : la stratégie a une espérance
  positive AVEC ses facteurs actuels ; chaque ajout est un degré de liberté d'overfitting en plus.
- **Optimiser les poids des facteurs** (0,30/0,20/0,18/0,16/0,16) par recherche exhaustive : c'est
  la recette classique du backtest magnifique qui meurt en forward.
- **Élargir aux cryptos/actions** pour le playbook : la stratégie est calibrée pips/sessions forex.
  Les autres marchés ont leur propre pipeline (carte de l'edge).
- **Baisser les seuils pour trader plus** : 209 trades / 1,9 an ≈ 9/mois, c'est peu ET c'est
  cohérent avec une stratégie de swing sélective. La rentabilité vient de l'espérance × sizing,
  pas de la fréquence.
