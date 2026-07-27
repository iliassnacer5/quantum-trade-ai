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
    # Bande de risque/rendement imposée : le gain visé vaut 2 à 3 fois le risque pris.
    playbook_min_rr: float = 2.0
    playbook_max_rr: float = 3.0
    # --- Niveaux du trade : objectif ≥ 200 pips, stop STRUCTUREL --------------------------------
    # Conséquence arithmétique de « 200 pips minimum » + « R/R au plus 1:3 » : le stop vaut au moins
    # 200 / 3 ≈ 67 pips, et au plus 200 / 2 = 100 pips. Un stop de cette taille ne peut pas venir de
    # la structure 15 min (quelques pips) : il est placé derrière la structure 4 h / journalière.
    # Le 15 min ne sert donc qu'à DÉCLENCHER l'entrée (le timing) ; il ne porte pas le risque.
    playbook_min_target_pips: float = 200.0   # objectif minimum, en pips
    playbook_stop_timeframe: str = "4h"       # UT qui porte le stop (structure)
    playbook_target_timeframe: str = "1d"     # UT qui borne l'objectif (niveaux majeurs)
    playbook_max_stop_pips: float = 150.0     # garde-fou absolu
    # Marge laissée avant le niveau majeur opposé : on sort AVANT le niveau, pas dessus.
    playbook_target_level_buffer: float = 0.15   # × ATR de l'UT qui borne
    # Objectif <= N × ATR journalier. 200 pips ≈ 2 à 3 journées moyennes sur une major : ce sont
    # des trades de SWING, tenus plusieurs séances.
    playbook_max_atr_multiple: float = 4.0
    playbook_require_real_data: bool = True  # jamais de trade affirmé sur données synthétiques
    playbook_entry_timeframe: str = "15m"   # la seule unité de temps d'entrée
    playbook_confirm_timeframe: str = "1h"  # dernière confirmation avant le déclencheur 15 min

    # --- SÉCURISATION DU PROFIT ------------------------------------------------------------------
    # Dès qu'un trade a parcouru `playbook_secure_at_r` fois son risque ET que l'objectif supérieur
    # reste atteignable, le stop est remonté SUR ce niveau : le trade ne peut plus redevenir
    # perdant, et il continue de courir vers le R/R maximum.
    playbook_secure_profit_enabled: bool = True
    playbook_secure_at_r: float = 2.0        # on sécurise quand +2R est touché
    playbook_secure_stop_at_r: float = 2.0   # ...en plaçant le stop exactement sur +2R

    # --- HEURES DE TRADING ------------------------------------------------------------------------
    # L'ANALYSE tourne en permanence (y compris marchés fermés) ; l'OUVERTURE de position, elle,
    # exige qu'une des deux grandes places soit ouverte : Londres ou New York. Hors de ces heures
    # le forex est illiquide et les mouvements ne sont pas exploitables.
    playbook_trade_only_when_open: bool = True

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
    playbook_auto_entry_interval: int = 60      # secondes entre deux vérifications des armés
    # Provisionne tout seul le compte démo du tenant (sinon il faut connecter un broker papier à la
    # main). Sûr : un compte papier n'engage aucun argent.
    playbook_auto_entry_autoprovision: bool = True
    # --- VERDICT PAR PAIRE (backtest hebdo) + GATING DE L'AUTO-ENTRÉE ----------------------------
    # Chaque passage du backtest hebdomadaire note chaque paire : 🟢 espérance ≥ +0,4 R et n ≥ 20
    # sur `playbook_verdict_green_streak` passages CONSÉCUTIFS ; 🔴 espérance ≤ 0 avec un
    # échantillon suffisant ; 🟡 tout le reste (positif mais fragile, échantillon court, premier
    # passage vert). L'auto-entrée ne trade QUE les paires 🟢 ; les 🟡 restent analysées et
    # affichées ; les 🔴 sont exclues de l'auto-trade. Les refus sont journalisés (« trades
    # évités ») pour que le rituel hebdo puisse vérifier si les gates ont eu raison.
    playbook_pair_gating: bool = True
    playbook_verdict_min_expectancy: float = 0.4   # seuil d'espérance (en R) pour le vert
    playbook_verdict_min_trades: int = 20          # échantillon minimal pour le vert
    playbook_verdict_green_streak: int = 2         # nb de passages verts consécutifs requis
    playbook_verdict_red_min_trades: int = 8       # échantillon minimal pour oser un rouge
    # Matrice paire × déclencheur : un déclencheur mesuré < +0,4 R sur n ≥ 15 pour UNE paire y est
    # désactivé comme déclencheur d'auto-entrée (il reste affiché comme information).
    playbook_trigger_matrix_gating: bool = True
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
    daily_top_trades_count: int = 5         # nombre de trades proposés chaque jour
    # --- MARCHÉS DU DESK -------------------------------------------------------------------------
    # La stratégie est calibrée sur le FOREX et l'OR : objectifs en pips, horaires de Londres et de
    # New York, niveaux majeurs mensuels. Les autres classes d'actifs restent analysables, mais
    # elles passent après — l'univers est balayé dans cet ordre.
    playbook_focus_classes: str = "forex,commodity"
    playbook_focus_only: bool = True     # true = on ne balaie QUE le forex et les métaux
    # Univers balayé par la stratégie pour élire les 5 meilleurs trades. Avec `playbook_focus_only`
    # le catalogue utile (majeures + croisées liquides + métaux) tient largement dans 16 symboles :
    # monter plus haut ne fait qu'ajouter des paires illiquides ET alourdir chaque cycle de fond.
    playbook_universe_limit: int = 16
    # Symboles analysés en parallèle. Chacun déclenche 5 requêtes réseau : au-delà de 4, on se fait
    # limiter en débit par les fournisseurs (Yahoo répond alors 422), ce qui rallonge tout le cycle.
    playbook_max_parallel: int = 4

    # --- SNAPSHOT TEMPS RÉEL ---------------------------------------------------------------------
    # La stratégie tourne en BOUCLE DE FOND et publie un instantané prêt à servir : les pages lisent
    # ce snapshot (réponse immédiate) au lieu de déclencher un recalcul de 150 s.
    playbook_snapshot_enabled: bool = True
    # Intervalle entre deux recalculs COMPLETS. Il doit rester nettement supérieur à la durée d'un
    # cycle, sinon la boucle repart aussitôt terminée et sature la machine en continu. Les pages,
    # elles, relisent l'instantané toutes les 10 s : leur fraîcheur ne dépend pas de ce réglage.
    playbook_snapshot_interval: int = 180   # secondes entre deux recalculs complets
    playbook_snapshot_max_age: int = 600    # au-delà, le snapshot est signalé comme périmé

    # --- ENTRAÎNEMENT QUOTIDIEN DES AGENTS SUR LA STRATÉGIE --------------------------------------
    # Walk-forward nocturne : la stratégie est rejouée sur l'historique de chaque symbole (le
    # déclencheur 15 min aurait-il atteint le TP avant le SL ?). Les statistiques obtenues (par
    # symbole, par type de déclencheur, par fenêtre de session) classent les trades du jour et
    # pondèrent les agents. Une fiche d'expertise LLM est ensuite rédigée pour chaque agent.
    playbook_training_enabled: bool = True
    playbook_training_hour: int = 2          # heure UTC du passage nocturne
    playbook_training_symbols: int = 18      # nb de symboles entraînés par passage
    # Profondeur d'historique 15 min. 1000 = le plafond réel d'une requête Binance/klines
    # (demander davantage ne renvoie pas plus) ≈ 10 jours de marché continu.
    playbook_training_bars: int = 1000
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
    playbook_backtest_step_1h: int = 3       # pas d'évaluation, en bougies 1 h
    playbook_backtest_step_15m: int = 4      # idem pour la passe 15 min

    # Filtre de qualité d'entrée (principiel) : ne trader qu'en régime de tendance et setup solide.
    entry_min_confidence: int = 62      # confiance minimale du signal
    entry_min_adx: float = 22.0         # ADX minimal = tendance réelle (évite les ranges/whipsaw)
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
    paper_max_positions: int = 5           # nb max de positions ouvertes simultanées
    paper_max_exposure_pct: float = 60.0   # exposition totale max (% du capital)

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
