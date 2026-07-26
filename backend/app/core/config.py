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
    # --- PLAYBOOK (stratégie du desk) : Mensuel+Journalier -> 4h -> entrée 15 min -----------------
    # C'est la stratégie de référence appliquée par TOUS les agents. Voir domain/playbook.py.
    playbook_enabled: bool = True
    playbook_veto: bool = True              # un refus du playbook force le HOLD (aucun contournement)
    # Bande de risque/rendement imposée : ni moins de 1,2 (le gain doit dépasser le risque), ni plus
    # de 1,3 (un R/R élevé vient d'un stop serré, que le marché fait sauter avant l'objectif).
    playbook_min_rr: float = 1.2
    playbook_max_rr: float = 1.3
    playbook_min_target_pips: float = 200.0  # objectif minimum de 200 pips
    # Conséquence : 200 pips avec un R/R plafonné à 1,3 impose un stop d'au moins ~154 pips.
    playbook_max_stop_pips: float = 200.0
    # Objectif <= N × ATR journalier. Un objectif de 200 pips vaut ~3 journées moyennes sur EUR/USD :
    # c'est un SWING tenu plusieurs jours, et l'horizon estimé est affiché sur chaque setup.
    playbook_max_atr_multiple: float = 4.0
    playbook_require_real_data: bool = True  # jamais de trade affirmé sur données synthétiques
    playbook_entry_timeframe: str = "15m"   # la seule unité de temps d'entrée
    # Exécution automatique en compte DÉMO (papier) des setups prêts, avec leur SL/TP.
    # N'agit que pour les utilisateurs ayant déjà connecté un broker papier (= opt-in explicite).
    playbook_auto_paper_execute: bool = True
    # Veille des sessions : top trades recalculés à l'ouverture de Londres, de New York et pendant
    # le chevauchement (la fenêtre la plus liquide de la journée).
    session_watch_enabled: bool = True
    session_watch_interval: int = 900       # secondes entre deux vérifications de fenêtre
    daily_top_trades_count: int = 5         # nombre de trades proposés chaque jour

    # Filtre de qualité d'entrée (principiel) : ne trader qu'en régime de tendance et setup solide.
    entry_min_confidence: int = 62      # confiance minimale du signal
    entry_min_adx: float = 22.0         # ADX minimal = tendance réelle (évite les ranges/whipsaw)
    entry_min_rr: float = 1.2           # ratio risque/rendement minimal (aligné sur la bande du playbook)
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


@lru_cache
def get_settings() -> Settings:
    """Singleton mis en cache des settings."""
    return Settings()
