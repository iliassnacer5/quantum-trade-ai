"""Configuration centralisée, chargée depuis les variables d'environnement."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Paramètres de l'application (12-factor : tout vient de l'environnement)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Environnement
    environment: str = "dev"
    log_level: str = "INFO"
    # Observabilité (Phase 5) — Sentry optionnel (no-op si vide).
    sentry_dsn: str = ""
    # Cache des complétions LLM (s) — réduction des coûts (Phase 5).
    llm_cache_ttl: int = 300
    # Automatisation : sélection quotidienne de trades (heure UTC du pré-calcul + digest).
    daily_digest_hour: int = 7
    daily_digest_enabled: bool = True

    # Ingestion temps réel (WebSocket Binance, crypto, gratuit/sans clé).
    live_ingestion_enabled: bool = True
    live_symbols: str = "BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,AVAX/USDT"
    live_interval: str = "1h"
    # Refuse de passer un ordre si les données du marché sont synthétiques (démo).
    block_synthetic_orders: bool = True
    # --- DONNÉES FICTIVES ------------------------------------------------------------------------
    # À false (défaut), aucun connecteur ne FABRIQUE de bougies quand la source réelle est
    # indisponible : il renvoie une série vide et la page affiche « données indisponibles ».
    # Une donnée inventée affichée comme une vraie est pire qu'une page vide — elle conduit à des
    # décisions prises sur un marché qui n'existe pas. Les tests qui veulent des séries
    # déterministes l'activent explicitement.
    data_allow_synthetic: bool = False
    # --- PLAYBOOK (stratégie du desk) : Mensuel+Journalier -> 4h -> entrée 15 min -----------------
    # C'est la stratégie de référence appliquée par TOUS les agents. Voir domain/playbook.py.
    playbook_enabled: bool = True
    playbook_veto: bool = True              # un refus du playbook force le HOLD (aucun contournement)
    # Bande de risque/rendement imposée : ni moins de 1,2 (le gain doit dépasser le risque), ni plus
    # de 1,3 (un R/R élevé vient d'un stop serré, que le marché fait sauter avant l'objectif).
    # Bande de risque/rendement imposée : le gain visé vaut 2 à 3 fois le risque pris. Hors de
    # cette bande, aucune position n'est ouverte — c'est une condition bloquante.
    # Plafond ramené de 1:4 à 1:3 sur MESURE : espérance +0,814 R contre +0,714 R et profit factor
    # 3,01 contre 2,64, à drawdown identique. Un objectif plus lointain est trop rarement atteint.
    playbook_min_rr: float = 2.0
    playbook_max_rr: float = 3.0
    # --- Niveaux du trade : stop et objectif posés sur des NIVEAUX de marché ---------------------
    # Le stop vient de ce qui invalide le scénario (zone d'offre/demande, creux de structure,
    # support/résistance) et l'objectif du premier obstacle réel devant le prix.
    # OBJECTIF MINIMUM : 50 pips (décision utilisateur du 28/07/2026, remplace les 200 pips).
    # L'unité reste comparable d'un marché à l'autre : hors forex/métaux, 1 pip = 1 point de base du
    # prix (cf. `domain/pips.py`), donc 50 pips = 0,5 % de mouvement aussi bien sur le DAX que sur
    # BTC. C'est ce plancher-là qui commande désormais l'échelle du trade, plus l'ATR.
    playbook_min_target_pips: float = 50.0
    # ÉCHELLE DU TRADE, en multiples de l'ATR journalier.
    # L'ATR NE BORNE PLUS L'OBJECTIF (`playbook_min_target_atr_daily = 0`) : demandé explicitement —
    # « ne prends pas en considération l'ATR dans le profit ». Le plancher d'objectif est désormais
    # le plancher en pips ci-dessus, et lui seul.
    # Le plancher de STOP suit : un stop à 0,55 × ATR journalier (≈ 69 pips sur EUR/USD) imposerait
    # mécaniquement un objectif d'au moins 138 pips au R/R minimum de 1:2, ce qui rendrait les
    # 50 pips demandés inatteignables. Ramené à 0,20 × ATR (≈ 25 pips sur EUR/USD), il laisse
    # exactement passer un objectif de 50 pips à 1:2.
    # Le PLAFOND de risque, lui, ne bouge pas : c'est lui qui contient le drawdown — l'oublier
    # l'avait fait doubler (17,0 R contre 8,9 R) pour une espérance équivalente.
    # CONFIRMÉ PAR CALIBRATION le 28/07/2026 : sur la watchlist, 0,20 donne le MEILLEUR drawdown
    # (21,66 R) des quatre valeurs testées (0,20/0,30/0,40/0,55) — élargir le stop l'a systématiquement
    # dégradé (jusqu'à 35,36 R à 0,55). Ce n'était donc pas qu'une nécessité arithmétique, la mesure
    # le confirme comme le meilleur réglage.
    playbook_min_stop_atr_daily: float = 0.20
    playbook_max_stop_atr_daily: float = 0.85
    playbook_min_target_atr_daily: float = 0.0
    playbook_max_stop_pips: float = 150.0     # garde-fou absolu
    # Objectif <= N × ATR journalier — INFORMATIF depuis le 28/07/2026 : la case reste affichée dans
    # la checklist mais ne refuse plus le trade (cf. `playbook_block_on_atr_reach`).
    playbook_max_atr_multiple: float = 4.0
    playbook_require_real_data: bool = True  # jamais de trade affirmé sur données synthétiques
    playbook_entry_timeframe: str = "15m"   # la seule unité de temps d'entrée
    playbook_confirm_timeframe: str = "1h"  # dernière confirmation avant le déclencheur 15 min

    # --- ÉTAPE 1 : DÉTECTION DE TENDANCE MULTI-INDICATEURS (cf. domain/trend.py) ------------------
    # La direction est mesurée par six indicateurs (EMA50/200, structure HH/HL, SuperTrend, MACD,
    # RSI, volume) sur quatre unités de temps, puis figée pour tout le reste de l'analyse.
    playbook_trend_engine: bool = True
    # L'ADX ne fait PAS partie de la stratégie : essayé comme verrou, il a été mesuré comme
    # coupant surtout de bons trades (espérance +0,12 R avec, +0,33 R sans, sur le même
    # échantillon). Il reste calculé et affiché, il ne décide rien.
    # Seuil de netteté de la tendance : |score agrégé| minimal pour nommer une direction.
    playbook_trend_min_score: float = 0.10
    # Poids par indicateur, au format "ema:0.28,structure:0.20,...". Vide = poids par défaut du
    # domaine. Les indicateurs non calculables (volume sur le forex spot) sont retirés du calcul.
    playbook_trend_weights: str = ""
    # UNITÉS DE TEMPS QUI DOIVENT S'ALIGNER pour valider la tendance. Le journalier en a été RETIRÉ
    # le 28/07/2026 sur décision de l'utilisateur : « le D1 n'est pas obligatoire, il suffit du 1 h
    # et du 4 h ». Le journalier continue de peser 40 % dans le score agrégé (donc un journalier
    # contraire fait toujours baisser la confiance et peut faire passer sous le seuil de netteté) —
    # il ne pose simplement plus de veto à lui seul.
    playbook_trend_required_tfs: str = "4h,1h"

    # --- CE QUI REFUSE UN TRADE, ET CE QUI EST SEULEMENT AFFICHÉ ---------------------------------
    # Règle demandée le 28/07/2026 : « si le déclencheur 15 min est trouvé, il doit entrer, même si
    # le stop structurel et la marge jusqu'au niveau majeur opposé sont en échec ». Ces trois cases
    # restent CALCULÉES et affichées dans la checklist (l'information ne coûte rien) mais ne
    # bloquent plus l'ouverture. Les remettre à True rétablit l'ancien comportement sans toucher au
    # code — c'est la seule façon honnête de pouvoir mesurer ce que la décision coûte ou rapporte.
    playbook_require_daily_confirmation: bool = False   # étape « le journalier confirme »
    playbook_block_on_stop_width: bool = False          # étape « stop structurel cohérent »
    playbook_block_on_major_level: bool = False         # étape « objectif atteignable avant le niveau majeur »
    playbook_block_on_atr_reach: bool = False           # étape « objectif ≤ N × ATR journalier »

    # --- ÉTAPE 2 : CONFLUENCE D'ENTRÉE (cf. domain/entry_confluence.py) ---------------------------
    # Règle du minimum de confirmations : on entre dès que trois confirmations pondérées se
    # rejoignent, sans exiger que tous les outils s'alignent (ce qui ne produirait presque jamais
    # d'opportunité). Ce que le prix FAIT (zone d'offre/demande, cassure de structure, figure de
    # retournement, support/résistance majeur) pèse le double des indicateurs qui le commentent.
    #   "hybrid"     : déclencheur historique OU confluence (défaut). La cassure confirmée reste le
    #                  signal le mieux mesuré du backtest (69 %, +1,15 R) : on ne s'en prive pas.
    #   "legacy"     : uniquement les déclencheurs historiques — sert de référence à l'A/B test.
    #   "confluence" : uniquement la nouvelle règle.
    playbook_entry_mode: str = "hybrid"
    playbook_confluence_min_score: float = 3.0
    # Poids par outil, au format "supply_demand:1.0,rsi:0.5,...". Vide = poids par défaut.
    playbook_confluence_weights: str = ""

    # --- SÉCURISATION DU PROFIT ------------------------------------------------------------------
    # Dès qu'un trade a parcouru `playbook_secure_at_r` fois son risque ET que l'objectif supérieur
    # reste atteignable, le stop est remonté SUR ce niveau : le trade ne peut plus redevenir
    # perdant, et il continue de courir vers le R/R maximum.
    playbook_secure_profit_enabled: bool = True
    playbook_secure_at_r: float = 2.0        # on sécurise quand +2R est touché
    playbook_secure_stop_at_r: float = 2.0   # ...en plaçant le stop exactement sur +2R
    # Seconde règle, COMPLÉMENTAIRE de la précédente : quand TP1 est touché ET que le momentum
    # 15 min / 1 h confirme la continuation, le stop monte à 80 % du chemin parcouru et la position
    # part chercher TP2. Les deux règles coexistent : la plus favorable au trade s'applique, et le
    # stop ne recule jamais.
    playbook_tp1_lock_fraction: float = 0.8
    playbook_tp2_management: bool = True

    # --- HEURES DE TRADING ------------------------------------------------------------------------
    # Le desk trade désormais TOUS les marchés (forex, métaux, indices, actions, crypto), 24 h/24.
    # La restriction « Londres ou New York ouverte » a donc été levée : elle n'avait de sens que
    # pour un univers forex, et elle fermait les séances asiatiques ainsi que la crypto.
    # La fenêtre de session reste MESURÉE et affichée (elle module la conviction du setup) — elle
    # n'interdit simplement plus l'ouverture.
    playbook_trade_only_when_open: bool = False

    # --- FILTRES ISSUS DU BACKTEST (mesurés, pas supposés) ---------------------------------------
    # 1) DÉCLENCHEUR « divergence » : mesuré à 37,5 % de réussite et +0,19 R sur 16 trades, contre
    #    69 % / +1,15 R pour la cassure et 58 % / +0,77 R pour le repli. Il ne détruit pas de
    #    capital, mais il dilue l'espérance : on le désactive comme déclencheur d'ENTRÉE. Il reste
    #    calculé et affiché — une divergence contraire garde sa valeur d'avertissement.
    playbook_allow_divergence_entry: bool = False
    # 2) FILTRE DE VOLATILITÉ : les trades stoppés ont un ATR journalier supérieur de 21 % à celui
    #    des gagnants (1,39 % contre 1,15 %) — c'est le SEUL facteur qui les distingue nettement.
    #    Au-delà du seuil, un stop calibré sur une volatilité normale saute sur du bruit.
    #    Deux modes possibles, cf. `playbook_volatility_mode` :
    #      - "adapt"  (défaut) : le stop est ÉLARGI proportionnellement à l'excès de volatilité.
    #                            On garde le trade, on paie le vrai prix du risque.
    #      - "refuse"          : le setup est refusé au-dessus du seuil.
    playbook_volatility_filter: bool = True
    playbook_volatility_mode: str = "adapt"       # "adapt" | "refuse"
    playbook_max_atr_pct: float = 1.3             # ATR journalier, en % du prix
    playbook_volatility_max_widen: float = 1.6    # élargissement max du stop (× la distance initiale)

    # --- AUTO-ENTRÉE (compte DÉMO uniquement) ----------------------------------------------------
    # Dès qu'un setup ARMÉ (étapes 1-3 validées) voit son déclencheur 15 min se former, la position
    # est ouverte AUTOMATIQUEMENT en compte démo, avec son SL 15 min et son TP borné 1 h.
    # Aucun clic, jamais d'argent réel : l'auto-entrée ne touche QUE les connexions `paper`.
    playbook_auto_paper_execute: bool = True
    playbook_auto_entry_enabled: bool = True
    # 30 s (abaissé de 60 le 01/08/2026, à la demande). Chaque passage RECALCULE la stratégie sur
    # données fraîches pour les seuls setups ARMÉS — jamais tout l'univers : le coût est
    # proportionnel au nombre de contextes déjà validés, en général un ou deux. Doubler la cadence
    # divise donc par deux le retard maximal entre la formation d'un déclencheur 15 min et
    # l'ouverture de la position, pour un surcoût négligeable.
    playbook_auto_entry_interval: int = 30      # secondes entre deux vérifications des armés
    # Provisionne tout seul le compte démo du tenant (sinon il faut connecter un broker papier à la
    # main). Sûr : un compte papier n'engage aucun argent.
    playbook_auto_entry_autoprovision: bool = True
    # ANTI-DOUBLON : délai minimum entre deux entrées automatiques sur le MÊME symbole et le même
    # sens. Sans lui, un déclencheur qui reste actif plusieurs passages de veille rouvre la position
    # dès que la précédente se referme — c'est ce qui a produit quatre entrées ETH/USDT d'affilée en
    # quatre minutes, à quelques dixièmes de pourcent d'écart. Le garde-fou `_already_open` ne
    # couvre que les positions ENCORE ouvertes ; celui-ci couvre le cas où elles viennent de sortir.
    playbook_auto_entry_cooldown_min: int = 45
    # --- VERDICT PAR PAIRE (backtest hebdo) + GATING DE L'AUTO-ENTRÉE ----------------------------
    # Chaque passage du backtest hebdomadaire note chaque paire : 🟢 espérance ≥ +0,4 R et n ≥ 20
    # sur `playbook_verdict_green_streak` passages CONSÉCUTIFS ; 🔴 espérance ≤ 0 avec un
    # échantillon suffisant ; 🟡 tout le reste (positif mais fragile, échantillon court, premier
    # passage vert). L'auto-entrée ne trade QUE les paires 🟢 ; les 🟡 restent analysées et
    # affichées ; les 🔴 sont exclues de l'auto-trade. Les refus sont journalisés (« trades
    # évités ») pour que le rituel hebdo puisse vérifier si les gates ont eu raison.
    #
    # DÉSACTIVÉ le 28/07/2026 : c'est ce gate qui expliquait « j'ai ce trade dans les trades du jour
    # mais il n'a pas été lancé automatiquement ». Un setup PRÊT était bien détecté, puis écarté en
    # silence parce que sa paire n'était pas encore notée 🟢 (il faut deux backtests hebdomadaires
    # consécutifs au-dessus du seuil, donc au minimum deux semaines pendant lesquelles RIEN ne se
    # déclenche). Le verdict continue d'être calculé, affiché et de moduler la TAILLE de position
    # (`conviction_sizing_enabled`) — il ne décide simplement plus s'il y a trade ou non.
    playbook_pair_gating: bool = False
    playbook_verdict_min_expectancy: float = 0.4   # seuil d'espérance (en R) pour le vert
    playbook_verdict_min_trades: int = 20          # échantillon minimal pour le vert
    playbook_verdict_green_streak: int = 2         # nb de passages verts consécutifs requis
    playbook_verdict_red_min_trades: int = 8       # échantillon minimal pour oser un rouge
    # Matrice paire × déclencheur : un déclencheur mesuré < +0,4 R sur n ≥ 15 pour UNE paire y est
    # désactivé comme déclencheur d'auto-entrée (il reste affiché comme information).
    # Désactivée avec le gate de paire, et pour la même raison : elle refusait des entrées sans que
    # rien ne le montre à l'écran.
    playbook_trigger_matrix_gating: bool = False
    playbook_trigger_matrix_min_trades: int = 15
    playbook_trigger_matrix_min_expectancy: float = 0.4

    # --- SIZING PAR CONVICTION (plan Phase 3.1) --------------------------------------------------
    # Le risque de base vient du profil de l'utilisateur (1 % en modéré). Il est ensuite modulé par
    # le verdict MESURÉ de la paire : ×1,25 sur une paire 🟢 à échantillon solide (n ≥ 30), ×0,5 sur
    # une paire 🟡, plafonné en absolu. Une paire non mesurée reste au risque de base.
    conviction_sizing_enabled: bool = True
    conviction_green_mult: float = 1.25
    conviction_green_min_trades: int = 30
    conviction_yellow_mult: float = 0.5
    conviction_risk_cap_pct: float = 1.5           # plafond absolu du risque par trade (% capital)

    # --- GARDE DE CORRÉLATION (plan Phase 3.2) ---------------------------------------------------
    # Deux positions EUR/USD et EUR/JPY ne sont pas deux paris indépendants : c'est deux fois le
    # même pari sur l'euro. Au-delà de N positions ouvertes partageant une même devise, on refuse.
    correlation_guard_enabled: bool = True
    max_positions_per_currency: int = 2

    # --- GEL DES ENTRÉES SUR PERTE (plan Phase 3.3) ----------------------------------------------
    # Journée à −3 % du capital (P&L réalisé) ou semaine à −6 % : plus AUCUNE nouvelle entrée
    # playbook jusqu'à la période suivante, et l'utilisateur est prévenu. Les positions déjà
    # ouvertes continuent d'être gérées (sécurisation, SL/TP) — on arrête d'empiler, pas de gérer.
    loss_freeze_enabled: bool = True
    daily_loss_freeze_pct: float = 3.0
    weekly_loss_freeze_pct: float = 6.0

    # Veille des sessions : top trades recalculés à l'ouverture de Londres, de New York et pendant
    # le chevauchement (la fenêtre la plus liquide de la journée).
    session_watch_enabled: bool = True
    session_watch_interval: int = 900       # secondes entre deux vérifications de fenêtre
    # NOMBRE DE TRADES PROPOSÉS : 0 = AUCUN PLAFOND. On rend TOUS les setups que la stratégie
    # valide, pas les N premiers. Un plafond arbitraire cachait des trades conformes uniquement
    # parce qu'ils arrivaient 6e au classement — or la stratégie ne dit pas « les 5 meilleurs »,
    # elle dit « ceux qui remplissent les trois étapes ». Le classement reste, il n'élimine plus.
    daily_top_trades_count: int = 0
    # Filet contre le bruit : un setup n'est proposé que si sa fiabilité MESURÉE atteint ce seuil
    # (score /5 issu de la confiance de la stratégie, cf. `PlaybookSetup.reliability_score` et
    # `context_reliability`). C'est ce qui distingue « tous les trades fiables » de « tout ».
    daily_min_reliability: int = 2
    # --- PRÉCALCUL DE LA SÉLECTION DU JOUR (performance, 30/07/2026) ------------------------------
    # Le « scanner complémentaire » de la page Trades du jour lançait jusqu'à 32 backtests complets
    # DANS la requête HTTP, et une fois par unité de temps : le premier clic sur chaque bouton
    # (15 min / 1 h / 4 h / 1 j / 1 semaine / 1 mois) faisait attendre l'utilisateur plusieurs
    # dizaines de secondes. Une boucle de fond les calcule désormais à l'avance, exactement comme
    # `live_snapshot` le fait déjà pour les trades du jour du haut de page — la route ne fait plus
    # que LIRE. C'est le même principe appliqué au même problème.
    daily_picks_precompute_enabled: bool = True
    daily_picks_precompute_interval: int = 900   # 15 min entre deux cycles complets
    daily_picks_timeframes: str = "15m,1h,4h,1d,1w,1M"
    # --- MARCHÉS DU DESK -------------------------------------------------------------------------
    # La stratégie s'applique à TOUS les marchés : forex, métaux, INDICES, actions et crypto. Elle
    # ne repose sur rien de spécifique au forex — la tendance se lit de la même façon sur un indice,
    # et le stop comme l'objectif viennent de niveaux du graphique, plus d'un nombre de pips.
    # `playbook_focus_classes` ne sert plus qu'à ORDONNER le balayage (les plus liquides d'abord).
    # (Les métaux en ont été retirés le 29/07/2026 : ils sont exclus du desk, cf.
    #  `playbook_excluded_classes` — les laisser ici n'aurait fait que prioriser un marché interdit.)
    playbook_focus_classes: str = "forex,index,stock"
    playbook_focus_only: bool = False    # false = aucune classe d'actif n'est exclue
    # UNIVERS BALAYÉ : 0 = TOUT LE CATALOGUE (crypto, forex, actions, métaux, indices). La stratégie
    # ne suppose rien de propre à un marché — son échelle de trade s'exprime en ATR journalier et ses
    # niveaux se lisent sur n'importe quel graphique. Limiter le balayage à 16 symboles revenait à
    # décider d'avance où l'edge se trouve ; c'est la mesure qui doit le dire, pas le réglage.
    # `playbook_focus_classes` ne sert plus qu'à ORDONNER le balayage (les plus liquides d'abord).
    playbook_universe_limit: int = 0
    # LISTE RESTREINTE, mesurée le 28/07/2026 : quand activée, le balayage EN LIGNE (page trades du
    # jour, auto-entrée) ne porte QUE sur ces symboles, tous les autres réglages ci-dessus
    # (focus_classes, universe_limit) sont ignorés. Deux effets recherchés à la fois :
    #  - PERFORMANCE : 20 symboles au lieu de 88 -> le cycle de balayage (240 s) a cinq fois moins
    #    d'appels réseau à faire, la CPU du backend cesse de tourner en continu ;
    #  - FIABILITÉ : ce sont les 10 meilleures actions et les 10 meilleures paires forex mesurées
    #    par le backtest avec les réglages courants (objectif 50 pips, sans ATR) — TOUTES rentables
    #    sur l'échantillon mesuré, alors que la moyenne de l'univers entier ne l'était que
    #    faiblement (+0,24 R). Le backtest complet (`playbook_backtest`), lui, continue de balayer
    #    TOUT le catalogue : cette liste ne restreint que ce qui est réellement TRADÉ, jamais ce qui
    #    est mesuré — sinon on ne découvrirait plus jamais un edge ailleurs.
    # ACTIVÉE le 28/07/2026 après calibration mesurée : sur cette même watchlist, faire varier
    # `playbook_min_stop_atr_daily` (0,20 → 0,55) a montré que le stop actuel (0,20) était déjà le
    # MEILLEUR des quatre réglages testés sur le drawdown (21,66 R, contre 25,83 à 35,36 R pour les
    # variantes plus larges) — l'hypothèse « élargir le stop réduit le drawdown » était fausse, la
    # mesure l'a contredite. Ce qui a réellement réduit le drawdown est cette watchlist elle-même :
    # même réglage de stop, univers complet (88 symboles) contre watchlist (20) -> 36,79 R contre
    # 21,66 R (-41 %). Voir [[regles-28-07-2026-gates-informatifs]] côté mémoire.
    # DÉSACTIVÉE le 29/07/2026, sur demande explicite : le desk trade TOUS les marchés et tous les
    # symboles rentables, plus une liste de 20. Ce que la watchlist apportait vraiment (moins de
    # drawdown) tenait à la SÉLECTION, pas au nombre — et cette sélection est déjà faite en aval,
    # symbole par symbole, par trois filtres qui, eux, sont mesurés : le plancher de fiabilité
    # (`daily_min_reliability`), la carte de l'edge (walk-forward nocturne) et le verdict de paire.
    # Garder en plus une liste figée revenait à décider d'avance où se trouve l'edge.
    # La liste reste ici : elle documente la mesure du 28/07/2026 et se réactive d'un seul booléen.
    playbook_watchlist_only: bool = False
    playbook_watchlist_symbols: str = (
        "ADBE,WMT,NVDA,AMD,PLTR,GOOGL,NFLX,CRM,V,JPM,"
        "EUR/USD,GBP/JPY,NZD/USD,USD/CHF,GBP/USD,AUD/JPY,USD/JPY,AUD/USD,EUR/CAD,EUR/CHF"
    )
    # CLASSES D'ACTIFS INTERDITES AU TRADING — exclusion DURE, pas une simple priorité de balayage.
    # `commodity` = les métaux précieux (XAU/USD, XAG/USD, XPT/USD, XPD/USD) : exclus sur demande du
    # desk le 29/07/2026. L'or et l'argent produisent le plus grand nombre de setups du catalogue
    # (~7× EUR/USD), ils occupaient donc une part démesurée des positions ouvertes alors que le desk
    # n'en veut aucune. L'exclusion est appliquée à DEUX endroits pour qu'aucun chemin ne la
    # contourne : le balayage (`playbook_service.daily_universe`) et l'ouverture de position
    # (`execution_service`), y compris pour un ordre lancé à la main depuis l'interface.
    playbook_excluded_classes: str = "commodity"
    # Symboles analysés en parallèle. Chacun déclenche 5 requêtes réseau : au-delà de 4, on se fait
    # limiter en débit par les fournisseurs (Yahoo répond alors 422), ce qui rallonge tout le cycle.
    # Valeur MESURÉE, laissée telle quelle malgré l'élargissement de l'univers : c'est le cache
    # multi-UT qui absorbe le catalogue complet (seul le 15 min est rechargé à chaque tour, les
    # unités longues ont un TTL de 15 min à 6 h), pas un parallélisme plus agressif.
    playbook_max_parallel: int = 4

    # --- SNAPSHOT TEMPS RÉEL ---------------------------------------------------------------------
    # La stratégie tourne en BOUCLE DE FOND et publie un instantané prêt à servir : les pages lisent
    # ce snapshot (réponse immédiate) au lieu de déclencher un recalcul de 150 s.
    playbook_snapshot_enabled: bool = True
    # Intervalle entre deux recalculs COMPLETS. Il doit rester nettement supérieur à la durée d'un
    # cycle, sinon la boucle repart aussitôt terminée et sature la machine en continu. Les pages,
    # elles, relisent l'instantané toutes les 10 s : leur fraîcheur ne dépend pas de ce réglage.
    # Relevé à 240 s depuis que l'univers couvre tout le catalogue : un cycle complet dure plus
    # longtemps, et repartir avant qu'il ne finisse saturerait la machine sans rien rafraîchir.
    # 300 s (relevé de 240 le 01/08/2026, sur mesure). Un cycle complet a été chronométré à
    # 236 s — 88 symboles × 5 unités de temps, dont 290 chargements soumis au régulateur Yahoo.
    # Annoncer 240 s alors que le calcul en demande 236 ne laissait aucune marge : le moindre
    # ralentissement faisait déborder le cycle sur le suivant, et l'âge affiché grimpait sans
    # qu'aucune anomalie ne soit visible. Mieux vaut une cadence honnête qu'une cadence tenue de
    # justesse.
    #
    # Cela ne retarde AUCUNE entrée : l'auto-entrée revérifie les setups armés toutes les 60 s en
    # recalculant la stratégie sur données fraîches. Seule la DÉCOUVERTE d'un contexte nouvellement
    # validé attend le cycle suivant — or ce contexte s'établit sur des unités de 4 h et 1 j.
    playbook_snapshot_interval: int = 300   # secondes entre deux recalculs complets
    playbook_snapshot_max_age: int = 900    # au-delà, le snapshot est signalé comme périmé

    # --- ANALYSE QUOTIDIENNE DES MARCHÉS, HORS STRATÉGIE ----------------------------------------
    # L'avis du modèle sur le forex et l'or, produit par les agents SANS la stratégie du desk
    # (l'agent playbook n'est ni exécuté ni compté, son veto ne s'applique pas). C'est un SECOND
    # REGARD : la stratégie garde seule la décision de trader, mais elle refuse presque tout par
    # construction et ne dit jamais « voilà ce que je pense de l'euro aujourd'hui ».
    # Cf. `services.market_opinion_service`.
    market_opinion_enabled: bool = True
    market_opinion_hour: int = 6      # heure UTC (07h Maroc) — prête avant l'ouverture de Londres
    # Rattrapage au démarrage : si l'analyse du jour manque, elle est produite peu après le
    # lancement. C'est ce qui garantit « chaque jour je lance le projet, je la trouve prête ».
    market_opinion_on_startup: bool = True
    #: Vide = univers par défaut du service (10 paires majeures + or + argent).
    market_opinion_symbols: str = ""

    # --- ENTRAÎNEMENT QUOTIDIEN DES AGENTS SUR LA STRATÉGIE --------------------------------------
    # Walk-forward nocturne : la stratégie est rejouée sur l'historique de chaque symbole (le
    # déclencheur 15 min aurait-il atteint le TP avant le SL ?). Les statistiques obtenues (par
    # symbole, par type de déclencheur, par fenêtre de session) classent les trades du jour et
    # pondèrent les agents. Une fiche d'expertise LLM est ensuite rédigée pour chaque agent.
    playbook_training_enabled: bool = True
    # 20h à Rabat = 19h UTC en heure standard marocaine (UTC+1, en vigueur toute l'année depuis
    # 2018) SAUF pendant le Ramadan, où le Maroc repasse à UTC+0 pendant environ un mois — ce
    # réglage fixe ne peut pas suivre ce changement automatiquement (Ramadan 2026 : ~18/02–19/03,
    # donc sans effet sur ce réglage aujourd'hui). Pendant le Ramadan, le passage tombera à 19h
    # locale au lieu de 20h ; à ajuster manuellement si cela compte pour l'utilisateur.
    playbook_training_hour: int = 19         # heure UTC du passage nocturne (20h Maroc hors Ramadan)
    # Nombre de symboles entraînés par passage. 0 = TOUT l'univers balayé (catalogue complet moins
    # les classes exclues). Le principe n'a pas changé — entraînement et trading réel doivent porter
    # sur le même univers, sinon la fiabilité mesurée ne dit rien des symboles réellement tradés —
    # mais cet univers n'est plus la watchlist de 20 : c'est tout ce que le desk trade désormais.
    # Le coût reste contenu : un seul passage par nuit (19h UTC), à parallélisme réduit (moitié de
    # `playbook_max_parallel`) pour ne pas se faire limiter en débit par les fournisseurs.
    playbook_training_symbols: int = 0
    # Profondeur d'historique 15 min. RELEVÉ de 1000 à 5000 le 28/07/2026 : 1000 (~10 jours) ne
    # servait qu'à ne pas dépasser le plafond RÉEL d'une requête Binance (crypto) — mais la
    # watchlist est aujourd'hui majoritairement forex/actions, servis par Yahoo, dont le plafond
    # réel avoisine 60 jours en 15 min (~5 760 bougies). 5000 s'en approche sans le dépasser ; sur
    # le crypto (Binance), la requête reste de toute façon plafonnée à ~1000 par le fournisseur —
    # ce réglage ne change donc rien pour ces symboles-là, seulement pour les autres.
    playbook_training_bars: int = 5000
    playbook_training_step: int = 4          # pas d'évaluation (en bougies 15 min)
    playbook_training_min_trades: int = 8    # nb de trades requis pour croire une statistique
    playbook_expertise_llm: bool = True      # fiche d'expertise LLM par agent

    # --- BACKTEST DE LA STRATÉGIE (longue portée) ------------------------------------------------
    # Rejoue la stratégie sur TOUTES les paires du desk et sur toute la profondeur d'historique
    # disponible. C'est lui qui produit le classement des paires par fiabilité. Beaucoup plus lourd
    # que le walk-forward quotidien : il tourne une fois par semaine, le week-end (marchés fermés).
    playbook_backtest_enabled: bool = True
    playbook_backtest_weekday: int = 6       # 0 = lundi … 6 = dimanche
    playbook_backtest_hour: int = 3          # heure UTC
    # Pas relevé de 3 à 4 depuis que l'univers couvre tout le catalogue : le coût est linéaire en
    # (symboles × bougies / pas), et un balayage qui dure plus longtemps que la semaine ne sert à rien.
    playbook_backtest_step_1h: int = 4       # pas d'évaluation, en bougies 1 h
    playbook_backtest_step_15m: int = 4      # idem pour la passe 15 min
    # --- PASSE LONGUE (5 ANS) — DÉSACTIVÉE, ET C'EST UNE CONCLUSION MESURÉE ----------------------
    # L'intraday n'existe pas gratuitement au-delà de ~2 ans (Yahoo plafonne le 1 h et le 4 h à
    # 730 jours, le 15 min à 60). Pour atteindre 5 ans il faudrait monter toute l'échelle d'un cran :
    # contexte MENSUEL + HEBDOMADAIRE, entrée JOURNALIÈRE (cf. `playbook_backtest._LADDERS`).
    #
    # MESURÉ le 28/07/2026 : cette transposition ne produit RIEN. Six échelles de trade essayées
    # (objectif 1,8 à 3,6 × ATR, stop 0,55 à 1,80 ×) donnent toutes **1 à 2 trades sur 5 ans et
    # 6 symboles**. Diagnostic par comptage des motifs de refus : le contexte est validé 30 % du
    # temps — le moteur de tendance fonctionne — mais l'ÉTAPE D'ENTRÉE ne se déclenche pas.
    #
    # La cause est structurelle, pas un réglage à trouver : les confirmations d'entrée sont de la
    # micro-structure lue SUR L'UNITÉ D'ENTRÉE (zones d'offre/demande, cassure de structure, figure
    # de retournement, VWAP, volume). Quand cette unité devient le journalier, cette information a
    # déjà été consommée par l'étape de tendance et il ne reste plus de confirmation indépendante à
    # réunir. La stratégie a donc besoin d'une unité d'entrée nettement plus fine que son contexte.
    #
    # Conséquence à assumer : un backtest de 5 ans de CETTE stratégie n'est pas réalisable avec des
    # données gratuites. Le code de la passe est conservé — il redeviendra utile le jour où une
    # source d'intraday profond (payante) sera branchée. Le remettre à True fait tourner une passe
    # d'une heure pour zéro trade.
    playbook_backtest_deep_enabled: bool = False
    playbook_backtest_deep_years: float = 5.0   # profondeur visée de la passe longue
    playbook_backtest_deep_step: int = 1        # pas d'évaluation, en bougies journalières
    playbook_backtest_deep_weekday: int = 2     # relancée aussi en milieu de semaine (mercredi)

    # Filtre de qualité d'entrée (principiel) : ne trader qu'en régime de tendance et setup solide.
    entry_min_confidence: int = 62      # confiance minimale du signal
    entry_min_rr: float = 2.0           # ratio risque/rendement minimal (aligné sur la bande du playbook)
    entry_quality_gate: bool = True     # appliquer le filtre au live (le backtest l'applique toujours)
    entry_trend_filter: bool = True     # anti-couteau-qui-tombe : pas de trade contre l'EMA longue
    # Surveillance des positions papier : clôture auto au SL/TP atteint.
    position_monitor_enabled: bool = True
    position_monitor_interval: int = 60  # secondes
    # Apprentissage continu : résolution auto des signaux -> affine les poids des agents.
    learning_enabled: bool = True
    learning_interval: int = 300  # secondes
    # Réalisme du backtest : coûts de transaction (par côté) — sinon les résultats mentent.
    backtest_fee_pct: float = 0.1        # frais broker (%) par côté (Binance ~0,1%)
    backtest_slippage_pct: float = 0.05  # slippage estimé (%) par côté
    # Stops dynamiques (backtest) — CHOIX MESURÉ par A/B test (juil. 2026, 12/12 comparaisons) :
    # la config "tp_only" (SL/TP fixes, PAS de breakeven ni trailing) domine partout.
    # Meilleure combinaison out-of-sample : MTF EMA × 4h -> PF 1,14, alpha +10,4% (BTC+ETH+SOL).
    backtest_trailing_stop: bool = False
    backtest_trailing_atr_mult: float = 3.0
    backtest_breakeven_at_r: float = 0.0  # 0 = désactivé (le breakeven tronquait les gagnants)
    # Alertes : la stratégie active déclenche une notification quand elle donne un signal.
    strategy_alerts_enabled: bool = True
    strategy_alerts_interval: int = 600   # secondes
    # Carte de l'edge (plan maître, Phase B) : sweep systématique stratégies × marchés × TF.
    edge_sweep_enabled: bool = True
    edge_sweep_interval_hours: int = 24
    edge_min_green_streak: int = 1      # nb de sweeps verts consécutifs requis pour l'auto-trade
    auto_trade_green_only: bool = True  # l'auto-trade papier ne prend que les combos verts
    strategy_alerts_timeframe: str = "4h"  # TF des alertes/auto-trade (4h = meilleur combo mesuré)
    # Agents experts par marché + filtre événementiel (Phase 1).
    expert_agents_enabled: bool = True
    event_blackout_enabled: bool = True
    fomc_dates: str = ""            # CSV de dates ISO (YYYY-MM-DD) des réunions FOMC
    cross_asset_ttl: int = 1800     # cache funding/BTC-lead (s)
    # Risque au niveau portefeuille (paper) : protège le capital simulé.
    paper_portfolio_guard: bool = True
    # Plafond de NOMBRE de positions retiré le 28/07/2026 (décision explicite) : 0 = aucune limite.
    # Ce qui protège réellement le capital reste `paper_max_exposure_pct` juste en dessous — le
    # RISQUE total ouvert, en % du capital. Compter les positions une par une n'a pas de sens :
    # cinq positions à 0,2 % de risque chacune pèsent moins qu'une seule à 10 %.
    paper_max_positions: int = 0           # 0 = aucun plafond de NOMBRE de positions
    paper_max_exposure_pct: float = 60.0   # exposition totale max (% du capital) — le vrai garde-fou

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    secret_key: str = "change-me"
    access_token_expire_minutes: int = 60
    cors_origins: str = "http://localhost:3000"

    # Persistance
    database_url: str = "postgresql+asyncpg://quantum:quantum_dev_pwd@postgres:5432/quantum"
    redis_url: str = "redis://redis:6379/0"
    kafka_bootstrap_servers: str = "redpanda:9092"

    # Persistance MVP : in-memory par défaut -> l'app tourne sans Postgres.
    # Passer à false pour utiliser la base SQL (DATABASE_URL).
    use_in_memory_db: bool = True

    # --- RÉSEAU DES CONNECTEURS DE MARCHÉ (performance, 30/07/2026) -------------------------------
    # MESURÉ en conteneur sur 6 actions : Alpaca 3/6 succès (31,5 s cumulées), Yahoo 3/6 succès
    # (25,4 s cumulées) — et CHAQUE échec était un `ConnectTimeout` à 10-12 s, pas une réponse lente.
    # Une connexion TCP qui n'est pas établie en 3 s ne s'établira pas : le reste du délai est du
    # temps mort pur. Séparer le délai de CONNEXION du délai de LECTURE est ce qui a fait tomber
    # `/api/execution/positions` de 22 s à moins d'une seconde (10 s Alpaca + 12 s Yahoo en cascade).
    market_connect_timeout: float = 3.0
    # Le délai de LECTURE n'est PAS réduit (12 s, la valeur historique de Yahoo) : la mesure a montré
    # des lectures légitimement lentes mais réussies (Yahoo/PEP a répondu en 8,09 s). Raccourcir la
    # lecture aurait transformé des succès en échecs — le temps mort était dans la CONNEXION, pas là.
    market_read_timeout: float = 12.0
    # Requêtes PROFONDES (backtest : plusieurs milliers de bougies) : la lecture est légitimement
    # plus longue, on lui laisse de la marge. La CONNEXION, elle, garde le même délai court.
    market_deep_read_timeout: float = 25.0
    market_deep_limit: int = 1200
    # Cache court des bougies (`data.markets.load_candles`), en secondes. Même valeur que le cache de
    # `data.ohlcv` : un seul chiffre à régler pour toute la fraîcheur des données de marché.
    market_cache_ttl: float = 20.0
    # CACHE PROPORTIONNEL À L'UNITÉ DE TEMPS (`data.markets._ttl_for`).
    #
    # `market_cache_ttl` s'appliquait à TOUTES les unités de temps : une bougie JOURNALIÈRE était
    # redemandée toutes les 20 s alors qu'elle ne se referme qu'une fois par jour. C'est ce
    # gaspillage qui portait le balayage (84 symboles × 5 UT = 420 requêtes / 240 s) au-delà de ce
    # que le point d'accès Yahoo public tolère — d'où les HTTP 429 et les « données indisponibles ».
    #
    # Multiplicateur appliqué à la DURÉE DE LA BOUGIE, borné par `market_cache_ttl_max`. À 1/60e,
    # une bougie 1 j est gardée 24 min et une 1 h 60 s : dans les deux cas très en dessous de la
    # durée de la bougie, donc l'apparition d'une NOUVELLE bougie ne peut pas passer inaperçue.
    # `market_cache_ttl` reste le plancher — aucune unité n'est cachée moins longtemps qu'avant.
    # 1/20e (relevé de 1/60e le 01/08/2026, sur mesure). À 1/60e, la bougie 4 h était cachée
    # exactement 240 s — la durée d'un cycle d'instantané — donc rechargée à CHAQUE passage pour
    # une valeur qui ne change que toutes les quatre heures. Avec 290 des 440 chargements d'un
    # cycle passant par le régulateur Yahoo (1 req/s), cela portait le cycle à ~236 s pour un
    # intervalle de 240 s : le balayage n'arrivait plus à suivre sa propre cadence.
    #
    # À 1/20e : 15 min -> 45 s · 1 h -> 180 s · 4 h -> 720 s · 1 j -> 30 min (plafond).
    # Chaque durée reste très inférieure à la bougie elle-même (5 %), donc aucune nouvelle bougie
    # ne peut être masquée — la propriété vérifiée par
    # `test_cache_duration_never_hides_a_new_candle`. Et aucune DÉCISION ne lit ce cache : les
    # chemins qui engagent de l'argent passent par `fresh=True`.
    market_cache_ttl_ratio: float = 1 / 20
    market_cache_ttl_max: float = 1800.0
    # Débit maximal vers Yahoo, en requêtes par seconde (0 = pas de régulation). Mesuré le
    # 01/08/2026 : en rafale, 0/12 requêtes aboutissent (429 systématique) ; espacées de 1,5 s,
    # 17/17 aboutissent, toutes classes d'actifs confondues. On s'auto-limite donc au lieu de se
    # faire refuser. Ne s'applique JAMAIS aux lectures `fresh=True` (cf. `data.markets`).
    yahoo_max_rps: float = 1.0

    # ---- Sources complémentaires (01/08/2026) ----
    # TWELVE DATA — FILET DE SÉCURITÉ pour les bougies, jamais une source primaire.
    #
    # Mesuré sur le plan gratuit : forex, crypto, actions et XAU/USD sont servis correctement en
    # 15min / 1h / 1day, mais le quota est de ~8 REQUÊTES PAR MINUTE (« You have run out of API
    # credits for the current minute »). Le balayage en demande 420 par cycle : en faire une source
    # primaire l'épuiserait en quelques secondes et priverait de données les chemins qui en ont
    # vraiment besoin. Il n'est donc interrogé que lorsque TOUS les autres fournisseurs ont échoué.
    # XAG/USD et les indices exigent un plan payant — inutile de les lui demander.
    twelve_data_api_key: str = ""
    twelve_data_max_rpm: float = 8.0
    # FINNHUB — la clé vit plus bas avec les autres sources de news (`finnhub_api_key`), le
    # connecteur `data/news.py` l'utilise déjà. Seul le secret manquait.
    # NEWS UNIQUEMENT : `/stock/candle` répond 403 sur le plan gratuit (bougies réservées au plan
    # payant), alors que les actualités sont riches et fiables (247 articles mesurés sur AAPL).
    finnhub_api_secret: str = ""

    # JWT
    jwt_algorithm: str = "HS256"

    # Sécurité
    rate_limit_enabled: bool = True

    # LLM
    anthropic_api_key: str = ""
    google_api_key: str = ""
    litellm_default_model: str = "gemini/gemini-2.5-pro"
    llm_enabled: bool = True
    # Modèles par rôle (overridables par env) — stratégie hybride Claude/Gemini.
    # NB : la série gemini-1.5 a été retirée de l'API v1beta ; on utilise la série 2.5 (GA).
    # Tout sur Gemini 2.5-flash (rapide, fiable, GA). La clé Claude est prête dans .env : pour activer
    # la stratégie hybride (Claude Sonnet sur vision/reasoning, Opus sur master) une fois le compte
    # Anthropic crédité, remettre :
    #   llm_model_vision/reasoning = "anthropic/claude-sonnet-4-6" ; llm_model_master = "anthropic/claude-opus-4-8"
    # (le failover retombe sur Gemini automatiquement si Claude échoue).
    llm_model_master: str = "gemini/gemini-2.5-flash"
    llm_model_reasoning: str = "gemini/gemini-2.5-flash"
    llm_model_fast: str = "gemini/gemini-2.5-flash"
    llm_model_vision: str = "gemini/gemini-2.5-flash"
    llm_model_grounding: str = "gemini/gemini-2.5-flash"
    
    # LLM Budget Guards
    llm_max_requests_per_minute: int = 15
    llm_max_tokens_per_minute: int = 30000
    llm_daily_budget_usd: float = 1.0

    # Données marché / news
    binance_api_key: str = ""
    binance_api_secret: str = ""
    finnhub_api_key: str = ""
    newsapi_key: str = ""
    newsdata_key: str = ""
    massive_news_key: str = ""
    # Multi-marchés (Phase 2)
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    oanda_api_key: str = ""
    oanda_account_id: str = ""
    fred_api_key: str = ""

    # Facturation
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_starter: str = ""

    # Alertes
    telegram_bot_token: str = ""
    resend_api_key: str = ""
    email_from: str = "alerts@quantumtrade.ai"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def database_url_sync(self) -> str:
        """URL SQLAlchemy synchrone (les repositories du MVP sont synchrones).

        Convertit l'éventuel driver async (asyncpg) en driver sync (psycopg2).
        """
        url = self.database_url
        if url.startswith("postgresql+asyncpg://"):
            return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+psycopg2://", 1)
        return url


def enforce_prod_secrets(settings: "Settings") -> None:
    """REFUS DE BOOT en production avec le secret par défaut (plan, tâche 1.3).

    Un JWT signé avec « change-me » se forge en une ligne de Python : n'importe qui devient
    n'importe quel utilisateur. En dev on avertit ; en prod on refuse de démarrer — un service
    down se voit et se corrige, un service falsifiable ne se voit pas.
    """
    if settings.environment.strip().lower() in {"prod", "production"} and (
        "change-me" in (settings.secret_key or "") or len(settings.secret_key or "") < 16
    ):
        raise RuntimeError(
            "SECRET_KEY par défaut (ou trop court) interdit en production. Génère un secret fort "
            "(`openssl rand -hex 32`), mets-le dans le .env (hors git) et redémarre. "
            "Rotation : générer un nouveau secret, le déployer, invalider les sessions."
        )


@lru_cache
def get_settings() -> Settings:
    """Singleton mis en cache des settings."""
    return Settings()
