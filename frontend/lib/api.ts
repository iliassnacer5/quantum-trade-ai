'use client';

const API = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
const WS = process.env.NEXT_PUBLIC_WS_URL ?? 'ws://localhost:8000/ws';

export type Signal = {
  id?: string;
  asset: string;
  direction: 'BUY' | 'SELL' | 'HOLD';
  entry: number;
  stop_loss: number;
  take_profit_1: number;
  take_profit_2?: number;
  take_profit_3?: number;
  risk_reward: number;
  confidence: number;
  timeframe: string;
  rationale: string;
  risk_warning?: string | null;
  metrics?: Record<string, any>;
  consensus_pct?: number;
  mtf?: { aligned: number; total: number; details: Record<string, string> };
  high_conviction?: boolean;
  agents?: { name: string; score: number; confidence: number; rationale: string; details?: Record<string, any> }[];
  news?: { headline: string; sentiment?: number | null }[];
  created_at?: string;
  trade_outcome?: { outcome: string; pnl: number | null } | null;
};

export type SignalsTrackRecord = {
  observed: { total_entries: number; closed: number; open: number; wins: number; losses: number; win_rate: number; total_pnl: number };
  avoided: { blocked: number; would_have_lost: number; would_have_won: number; undecided: number };
};

/** Métriques de LA STRATÉGIE sur un symbole (backtest), à ne pas confondre avec le walk-forward :
 *  celui-ci répond « bat-on le buy & hold ? », celles-ci « que gagne la méthode par trade ? ». */
export type PlaybookSymbolStats = {
  trades: number; win_rate: number; expectancy_r: number; profit_factor: number | null;
  max_drawdown_r: number; trades_per_day?: number | null; days_between_trades?: number | null;
  r_per_month?: number | null; rank?: number | null; verdict?: string;
};
export type EdgeRow = {
  strategy: string; strategy_name: string; symbol: string; market: string; timeframe: string;
  alpha: number; pf: number; win: number; trades: number; verdict?: string;
  data_real: boolean; status: 'green' | 'yellow' | 'red'; green_streak?: number;
  playbook?: PlaybookSymbolStats | null;
};
export type EdgeMap = {
  generated_at?: string; rows: EdgeRow[]; greens: number; yellows: number; reds: number; note: string;
  strategy?: string; symbols?: number; timeframes?: string[];
  playbook_summary?: {
    measured: boolean; note: string;
    symbols?: number; trades?: number; expectancy_r?: number;
    trades_per_day?: number; trades_per_week?: number;
  };
};

/** Verdict 🟢/🟡/🔴 d'une paire, issu du backtest hebdomadaire de la stratégie du desk. */
export type PairVerdict = {
  symbol: string; status: 'green' | 'yellow' | 'red'; emoji: string;
  expectancy_r: number | null; trades: number | null; win_rate: number | null;
  profit_factor: number | null; green_streak: number; reason: string; measured_at?: string;
};
export type PairVerdicts = {
  available: boolean; date?: string; updated_at?: string; note?: string;
  criteria?: Record<string, number>;
  pairs: Record<string, PairVerdict>;
  disabled_triggers?: Record<string, string[]>;
  refusals?: { symbol: string; reason: string; at: string; direction?: string }[];
};

export type MarketRegime = {
  utc_time: string; sessions: { id: string; label: string; window_utc: string; open: boolean }[];
  open_sessions: string[]; vix: number | null; regime: 'on' | 'off' | 'neutral'; regime_label: string;
  rate_trend?: string | null; inflation?: number | null;
};

export type Candle = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
};

/** Bougies + provenance. `real: false` signifie qu'AUCUNE donnée de marché n'a pu être obtenue —
 *  la page doit le dire, pas afficher une courbe inventée. */
export type OhlcvResponse = {
  candles: Candle[];
  source: 'live' | 'real' | 'synthetic' | 'unavailable' | string;
  real: boolean;
  note?: string;
};

export type Me = {
  id: string;
  email: string;
  full_name?: string;
  risk_profile: string;
  capital: number;
  watchlist: string[];
  onboarded: boolean;
  plan: string;
};

export type Settings = {
  watchlist: string[];
  max_exposure_pct: number;
  max_daily_signals: number;
  daily_loss_limit_pct: number;
  alert_email: boolean;
  alert_telegram: boolean;
  telegram_chat_id: string | null;
  push_enabled?: boolean;
  locale?: string;
  daily_digest?: boolean;
  mfa_enabled: boolean;
};

export type Branding = { brand_name: string; primary_color: string; logo_url: string; custom_domain?: string; tenant_id?: string };

export type Wallet = {
  starting_balance: number; balance: number; equity: number; realized_pnl: number; unrealized_pnl: number; return_pct: number;
  stats: { trades: number; wins: number; losses: number; win_rate: number; profit_factor: number; open_positions: number; best_trade: number; worst_trade: number };
  positions: { id: string; symbol: string; side: string; entry: number; qty: number; current_price: number | null; stop_loss?: number | null; take_profit?: number | null; unrealized_pnl: number }[];
  equity_curve: { t: string | null; equity: number; symbol?: string; outcome?: string; pnl?: number }[];
};

export type RiskStatus = {
  capital: number;
  exposure_value: number;
  exposure_pct: number;
  max_exposure_pct: number;
  daily_signals: number;
  max_daily_signals: number;
  breaches: string[];
  ok: boolean;
};

export type Position = {
  id?: string;
  asset: string;
  direction: string;
  entry: number;
  current_price: number | null;
  size: number;
  value: number;
  pnl: number;
};

export type Portfolio = {
  total_pnl: number;
  total_value: number;
  pnl_pct: number;
  positions: Position[];
};

export type HeatmapItem = { symbol: string; price: number; change_pct: number; asset_class?: string };

export type BacktestConfig = {
  symbol: string;
  timeframe: string;
  start_time: string;
  end_time: string;
  initial_capital: number;
  risk_per_trade_pct: number;
  use_llm?: boolean;
};

export type BacktestMetrics = {
  total_trades: number;
  win_rate: number;
  profit_factor: number;
  total_pnl: number;
  total_pnl_pct: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
};

export type BacktestReport = {
  id: string;
  config: BacktestConfig;
  metrics: BacktestMetrics;
  trades: any[];
  equity_curve: any[];
  created_at: string;
};

export type PlanInfo = {
  plan: string;
  features: Record<string, boolean>;
  feature_requirements: Record<string, string>;
};

export type JournalEntry = {
  id: string;
  signal_id?: string | null;
  symbol: string;
  direction: string;
  outcome: string; // open | win | loss | breakeven
  pnl?: number | null;
  agent_scores?: Record<string, number>;
  created_at?: string | null;
  /** 'playbook' = position démo (auto-entrée, « Ouvrir en démo »...) ; absent = signal classique
   *  (bouton « Générer un signal »). Un trade playbook se gère depuis la page Paper Trading — ce
   *  n'est pas une entrée de journal « pure », donc ni close ni explain n'y sont proposés ici. */
  source?: 'playbook' | 'signal';
  /** Pips réalisés (entrée -> sortie), signés dans le sens du trade. Renseigné pour un trade
   *  playbook clôturé ; absent pour un signal classique, qui n'a pas de prix de sortie. */
  pips?: number | null;
  pips_label?: string;
  closed_at?: string | null;
};

export type JournalInsights = {
  stats: {
    total_entries: number;
    closed: number;
    open: number;
    wins: number;
    losses: number;
    win_rate: number;
    total_pnl: number;
  };
  weight_multipliers: Record<string, number>;
  reliability?: { agent: string; samples: number; hit_rate: number; multiplier: number; low_sample?: boolean }[];
  trades_learned?: number;
  /** 'signals' = mesuré sur le flux « Générer un signal » ; 'training' = repli sur le walk-forward
   *  nocturne de la stratégie quand ce flux est vide mais que des trades playbook existent. */
  reliability_source?: 'signals' | 'training';
};

export type TeamMember = { id: string; email: string; full_name?: string | null; role: string; onboarded: boolean };

export type BrokerConn = { id: string; broker: string; mode: string; key_hint: string; created_at?: string };
export type Order = {
  id: string; broker: string; mode: string; symbol: string; side: string; qty: number;
  status: string; filled_price: number | null; copied_from?: string;
  entry?: number | null; stop_loss?: number | null; take_profit?: number | null;
  risk_reward?: number | null; risk_amount?: number | null; potential_profit?: number | null;
  // Vérification d'issue (gagné/perdu/ouvert)
  outcome?: 'won' | 'lost' | 'open' | null; exit_price?: number | null; realized_pnl?: number | null;
  closed_at?: string | null; current_price?: number | null; unrealized_pnl?: number | null; note?: string;
};
export type Trader = { tenant_id: string; display_name: string; win_rate: number; total_pnl: number; closed_trades: number };
export type CopyFollow = { id: string; leader_tenant: string; allocation_pct: number; max_per_trade: number; min_confidence: number; active: boolean };
export type Listing = { id: string; title: string; kind: string; price: number; description: string; seller_tenant: string; created_at?: string };
export type ApiKey = { id: string; label: string; prefix: string; active: boolean; created_at?: string };

export type WalkForward = {
  symbol: string; timeframe: string; strategy_id?: string | null; total_trades: number; folds_evaluated: number;
  profitable_folds: number; beats_hold_folds?: number; consistency: number; avg_win_rate: number; avg_profit_factor: number;
  avg_pnl_pct: number; avg_alpha_pct?: number; data_real: boolean; verdict: string; label: string;
  folds: { fold: number; from: string; to: string; trades: number; win_rate: number; profit_factor: number; pnl_pct: number; alpha_pct?: number; max_drawdown_pct: number; profitable: boolean; beats_hold?: boolean }[];
};

export type TrackRecord = {
  date: string;
  validation: WalkForward[];
  summary: { symbols: number; robust: number };
  observed: { total_entries: number; closed: number; open: number; wins: number; losses: number; win_rate: number; total_pnl: number };
  disclaimer: string;
};

export type AgentInfo = {
  name: string;
  role: string;
  desc: string;
  model: string;
  weight?: number | null;
  /** Compétence MESURÉE de l'agent sur la stratégie (issue du walk-forward nocturne). */
  competence?: {
    accuracy: number; observations: number; multiplier: number; trained_on: string;
    factors: Record<string, { observations: number; accuracy: number }>;
  } | null;
  /** Fiche d'expertise du jour : les règles que l'agent a tirées de ses résultats mesurés. */
  expertise?: string | null;
};

export type AgentStatus = {
  status: string;
  llm_enabled: boolean;
  providers: { anthropic: boolean; google: boolean };
  agents: AgentInfo[];
  strategy?: {
    name: string; enabled: boolean; veto: boolean; steps: string[];
    min_risk_reward: number; max_risk_reward?: number; min_target_pips: number;
    entry_timeframe: string; daily_trades: number;
    confirm_timeframe?: string;
    /** Unités de temps sur lesquelles la tendance est mesurée, et le seuil de netteté exigé. */
    trend_timeframes?: string[]; trend_min_score?: number;
    /** Comment l'entrée est autorisée : "hybrid" | "legacy" | "confluence", et son seuil. */
    entry_mode?: string; confluence_min_score?: number;
    /** Niveau (en multiples du risque) où le stop est remonté pour verrouiller le gain. */
    secure_at_r?: number; secure_profit?: boolean;
    /** Fraction du chemin TP1 verrouillée quand TP1 est touché et le momentum confirmé. */
    tp1_lock_fraction?: number;
    /** Vrai si aucune position n'est ouverte quand Londres ET New York sont fermées. */
    trade_only_when_open?: boolean;
    auto_entry?: boolean; auto_entry_mode?: string;
  };
  /** Résumé du dernier entraînement quotidien des agents sur la stratégie. */
  training?: {
    trained: boolean; note?: string; date?: string;
    trades_replayed?: number; symbols?: number; duration_s?: number;
    overall?: TrainingMetrics; agent_multipliers?: Record<string, number>;
  };
  session?: SessionContext;
};

/** Un facteur d'une étape (indicateur de tendance ou outil de confirmation) avec son poids. */
export type StrategyInput = {
  key: string; label: string; role: string;
  weight?: number | null; weight_pct?: number; strong?: boolean;
};

/** Une étape de la stratégie, décrite comme le moteur l'exécute. */
export type StrategyStep = {
  n: number; title: string; summary: string;
  timeframes?: string[];
  /** Unités de temps dont l'ACCORD est exigé pour valider la tendance (étape 1). */
  required_timeframes?: string[];
  inputs?: StrategyInput[];
  timeframe_weights?: Record<string, number>;
  blocking?: string[];
  /** Cases CALCULÉES et affichées mais qui ne refusent plus le trade (décision du 28/07/2026) —
   *  à ne pas confondre avec `blocking` : un ❌ ici informe, il ne bloque rien. */
  informative?: string[];
  rules?: string[];
  stop_candidates?: string[];
  stop_rule?: string;
  scale_explained?: string;
  not_used?: string;
  measured?: string;
  mode?: string;
  mode_explained?: string;
};

/**
 * LA STRATÉGIE DU DESK décrite de A à Z, lue dans la configuration RÉELLE du moteur.
 * La page n'invente ni ne recopie aucun seuil : une documentation recopiée à la main finit
 * toujours par décrire une version de la stratégie qui n'existe plus.
 */
export type StrategySpec = {
  name: string;
  one_liner: string;
  enabled: boolean;
  veto: boolean;
  principles: { title: string; body: string }[];
  steps: StrategyStep[];
  risk: { sizing: string; correlation: string; freeze: string; gating: string; volatility: string };
  scope: {
    markets: string[]; universe_size: number; universe_capped: boolean; universe_note?: string; hours: string;
    entry_timeframe: string; confirm_timeframe: string; trend_timeframes: string[];
    proposals_per_day: number | string; min_reliability: number;
    auto_entry: boolean; auto_entry_mode: string; why_all_markets: string;
  };
  data_honesty: string[];
  settings: Record<string, number | string | boolean>;
};

/** Contexte de session : ouvertures Londres / New York et leur chevauchement. */
export type SessionContext = {
  utc_time: string;
  active: string[];
  active_labels: string[];
  kill_zones: string[];
  overlap: boolean;
  quality: number;
  prime: boolean;
  label: string;
  next_window?: { id: string; label: string; starts_in_minutes: number; window_utc: string } | null;
};

/** Un facteur d'analyse EXPLIQUÉ : sa valeur, sa lecture, son poids et sa contribution au score. */
export type PlaybookFactor = {
  key: string;
  label: string;
  value: string;
  reading: string;
  signal: number;          // vote interne du facteur, -1 à +1 (non affiché)
  /** Score AFFICHÉ : +1..+5 = argument haussier, -1..-5 = argument baissier, 0 = ne tranche pas. */
  score: number;
  reliability: string;     // « fiable », « fragile »…
  weight: number;
  weight_pct: number;
  contribution: number;
  verdict: string;         // haussier | baissier | neutre | multiplicateur | contexte
  explain: string;         // ce que MESURE l'indicateur (pédagogie)
  multiplier?: number;
};

/** Une unité de temps analysée, avec la décomposition arithmétique de son score. */
export type PlaybookLayer = {
  label: string;
  score: number;
  bias: number;
  score_5: number;         // fiabilité de l'unité de temps, -5 à +5
  reliability: string;
  strength: string;        // « biais net », « neutre »…
  notes: string[];
  metrics: Record<string, any>;
  factors: PlaybookFactor[];
  explanation: string;     // phrase qui explique d'où vient le score
  breakdown: {
    votes: { label: string; signal: number; weight_pct: number; contribution: number }[];
    sum_of_votes: number;
    divergence_adjustment: number;
    volume_multiplier: number;
    final: number;
    bias_threshold: number;
  };
};

/** Un setup produit par la stratégie du desk (playbook multi-unités de temps). */
export type PlaybookTrade = {
  symbol: string;
  asset_class: string;
  tier: 'ready' | 'armed';
  direction: 'BUY' | 'SELL' | 'NO_TRADE';
  entry: number | null;
  stop_loss: number | null;
  take_profit_1: number | null;
  take_profit_2: number | null;
  take_profit_3: number | null;
  risk_pips: number;
  reward_pips: number;
  risk_reward: number;
  pips_label: string;
  trigger: string | null;
  /** D'où vient le stop — toujours la structure 15 min (l'unité d'entrée). */
  stop_basis: string;
  /** D'où vient l'objectif : bande de R/R, ou bornage par le prochain niveau 1 h. */
  target_basis: string;
  /** Le niveau 1 h qui borne l'objectif (on sort DEVANT lui). */
  target_level: number | null;
  horizon_days: number | null;
  /** Horizon en heures : l'échelle naturelle d'un trade dont l'entrée est en 15 min. */
  horizon_hours: number | null;
  /** Horizon prêt à afficher (« ~3.5 h », « ~1.2 j »). */
  horizon_label: string;
  /** Rang dans le classement des trades du jour (1 = le plus fiable). */
  rank?: number;
  confidence: number;
  strength: string;
  ready: boolean;
  context_ok: boolean;
  summary: string;
  reasons: string[];
  trend_explanation: string;
  /** Explication complète en français : pourquoi BUY / SELL / attente, argument par argument. */
  narrative: string;
  /** Score de fiabilité du trade : +1..+5 pour un achat, -1..-5 pour une vente, 0 = pas de trade. */
  reliability_score: number;
  reliability: string;
  /** Fiabilité du CONTEXTE (étapes 1-3) — c'est elle qui qualifie un setup ARMÉ. */
  context_reliability: number;
  context_reliability_label: string;
  /** Fiabilité MESURÉE par le walk-forward nocturne sur ce symbole / ce déclencheur. */
  edge: { score: number; win_rate?: number; trades: number; status: string } | null;
  edge_score: number;
  checklist: { step: number; label: string; pass: boolean; value: string; explain?: string }[];
  levels: Record<string, any>;
  layers: Record<string, PlaybookLayer>;
};

/** Une position papier telle qu'affichée : niveaux CHOISIS à l'ouverture + P&L live.
 *  (Distinct de `Position`, qui décrit une ligne du portefeuille agrégé.) */
export type PaperPosition = Order & {
  closed: boolean;
  current_price?: number;
  unrealized_pnl?: number;
  pnl_pct?: number;
  progress_pct?: number;   // avancement vers l'objectif (100 % = TP touché)
  r_multiple?: number;     // gain/perte exprimé en multiples du risque initial
  opened_at?: string | null;    // heure d'ENTRÉE dans la position
  held_seconds?: number | null; // durée de détention (entrée -> sortie, ou -> maintenant)
  // Pips SIGNÉS dans le sens du trade : positif = en notre faveur. `pips` est le résultat réalisé
  // pour une position clôturée, le latent pour une position ouverte.
  pips?: number | null;
  pips_label?: string;          // « pips » ou « pips éq. » selon la classe d'actif
  target_pips?: number | null;
  stop_pips?: number | null;
  close_reason?: string | null; // ce qui a fermé la position (quel stop, quel objectif)
};

export type PositionsSnapshot = {
  positions: PaperPosition[];
  open_count: number;
  closed_count: number;
  unrealized_pnl: number;
  realized_pnl: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  as_of: string;
};

/** Rapport d'ouverture des trades en compte démo (papier). */
export type PaperExecutionReport = {
  mode: string;
  connection_id: string;
  requested: number;
  opened: {
    order_id: string; symbol: string; side: string; qty: number; entry: number | null;
    stop_loss: number; take_profit: number; risk_reward: number; target_pips: number;
    stop_pips: number; pips_label: string; risk_amount: number | null;
    potential_profit: number | null; trigger: string | null; horizon_days: number | null;
  }[];
  skipped: { symbol: string; reason: string }[];
  armed_waiting: { symbol: string; direction: string; reason: string }[];
  summary: string;
  symbol?: string;        // renseigné quand l'ouverture ne concerne qu'un symbole
};

export type TopTrades = {
  date?: string;
  generated_at: string;
  /** Instant du calcul de fond (l'instantané est servi tel quel, sans recalcul). */
  computed_at?: string;
  /** Âge de l'instantané en secondes — la fraîcheur fait partie de la donnée. */
  age_seconds?: number | null;
  /** Vrai si l'instantané est trop vieux pour être présenté comme à jour. */
  stale?: boolean;
  /** Cadence de recalcul côté serveur (secondes). */
  refresh_interval?: number;
  strategy: string;
  session: SessionContext;
  scanned: number;
  ready: number;
  armed?: number;
  /** Plafond demandé, ou la chaîne « tous » quand la sélection n'en a aucun (le cas par défaut). */
  requested: number | string;
  /** Setups conformes à la stratégie, avant le plancher de fiabilité. */
  conform?: number;
  /** Combien de conformes ont été écartés par ce plancher — un filtre qu'on compte est un filtre
   *  qu'on peut contester. */
  below_reliability?: number;
  min_reliability?: number;
  picks: PlaybookTrade[];
  /** Verdict de la stratégie pour CHAQUE symbole balayé (pas seulement les 5 retenus). */
  verdicts?: Record<string, PlaybookVerdict>;
  /** Vrai si l'auto-entrée en compte démo est active. */
  auto_entry?: boolean;
  note: string;
};

/** Verdict léger de la stratégie pour un symbole — partagé par toutes les pages d'analyse. */
export type PlaybookVerdict = {
  symbol: string;
  asset_class: string;
  tier: 'ready' | 'armed' | 'none' | 'insufficient';
  direction: 'BUY' | 'SELL' | 'NO_TRADE';
  context_ok: boolean;
  veto: boolean;
  reliability_score: number;
  context_reliability: number;
  confidence: number;
  risk_reward: number;
  edge_score: number;
  reason: string;
};

/** État de l'AUTO-ENTRÉE : ce que le robot surveille et ce qu'il a ouvert seul (compte démo). */
export type AutoEntryStatus = {
  enabled: boolean;
  mode: 'paper';
  interval_seconds: number;
  /** Délai minimum entre deux entrées sur le même symbole/sens (anti-doublon). */
  cooldown_min?: number;
  /** Un verdict de paire peut-il encore refuser une entrée dont le déclencheur s'est formé ? */
  pair_gating?: boolean;
  /** Symboles dont le déclencheur 15 min est actif MAINTENANT. */
  ready_now?: string[];
  /** Exécutables à l'écran mais refusés au dernier passage (garde-fou de portefeuille, corrélation,
   *  anti-doublon…) — sans ce champ, un refus légitime ressemble à une panne. */
  blocked?: { symbol: string; reason: string }[];
  watching: { symbol: string; direction: string; tier: string; reason: string }[];
  recent: {
    id: string; symbol: string; side: string; entry: number | null;
    stop_loss: number | null; take_profit: number | null; trigger: string | null;
    message: string; created_at?: string;
  }[];
  note: string;
};

/** Entraînement quotidien des agents sur la stratégie (walk-forward + fiches d'expertise). */
export type TrainingReport = {
  trained: boolean;
  note?: string;
  date?: string;
  generated_at?: string;
  duration_s?: number;
  symbols_trained?: number;
  trades?: number;
  min_trades?: number;
  strategy?: string;
  overall?: TrainingMetrics;
  by_symbol?: Record<string, TrainingMetrics>;
  by_trigger?: Record<string, TrainingMetrics>;
  by_session?: Record<string, TrainingMetrics>;
  factor_competence?: Record<string, { observations: number; accuracy: number; aligned_win_rate: number | null }>;
  agent_multipliers?: Record<string, number>;
  expertise?: Record<string, string>;
  failures?: { symbol: string; error: string }[];
};

/** Métriques complètes d'un lot de trades rejoués par le backtest de la stratégie. */
export type PlaybookBacktestMetrics = {
  trades: number; wins: number; losses: number; expired: number;
  win_rate: number; expectancy_r: number; profit_factor: number | null; total_r: number;
  avg_win_r: number; avg_loss_r: number; avg_planned_rr: number;
  avg_reward_pips?: number; avg_risk_pips?: number;
  avg_bars_held: number; secured_rate: number; max_drawdown_r: number;
};

/** Une paire dans le classement de fiabilité. `rank: null` = échantillon insuffisant, non classée. */
export type PlaybookPairRank = PlaybookBacktestMetrics & {
  symbol: string; rank: number | null; verdict: string;
};

/** Les N meilleurs instruments D'UN marché. Classer dans chaque marché plutôt que globalement :
 *  sinon le marché le plus volatil occupe tout le haut du tableau et les autres disparaissent. */
export type PlaybookMarketTop = {
  market: string;
  label: string;
  rated: number;
  unrated: number;
  profitable: number;
  note: string;
  top: (PlaybookPairRank & { market_rank: number })[];
};

/** Une passe de backtest (portée 1 h, ou fidélité 15 min). */
export type PlaybookBacktestPass = {
  entry_timeframe: string;
  years_covered: number;
  pairs_tested: number;
  trades: number;
  duration_s: number;
  overall: PlaybookBacktestMetrics;
  ranking: PlaybookPairRank[];
  /** Les 10 meilleurs instruments de chaque marché, classés à l'intérieur du marché. */
  market_tops?: Record<string, PlaybookMarketTop>;
  by_trigger: Record<string, PlaybookBacktestMetrics>;
  by_session: Record<string, PlaybookBacktestMetrics>;
  by_direction: Record<string, PlaybookBacktestMetrics>;
  losers_profile: {
    sample?: number; note?: string; findings?: string[];
    avg_bars_to_stop?: number | null;
    comparisons?: Record<string, { label: string; perdants: number; gagnants: number; ecart_pct: number }>;
  };
  failures: { symbol: string; error: string }[];
  coverage: Record<string, string>;
  min_trades: number;
};

/** État d'exécution du backtest — il tourne en arrière-plan, pas dans la requête HTTP. */
export type BacktestRunState = {
  running: boolean;
  started_at: string | null;
  phase: string | null;
  done: number;
  total: number;
  elapsed_s?: number;
};

/** Backtest COMPLET de la stratégie du desk sur le forex et l'or. */
export type PlaybookBacktest = {
  available: boolean;
  run_state?: BacktestRunState;
  note?: string;
  date?: string;
  strategy?: string;
  universe?: string[];
  scope?: PlaybookBacktestPass;
  fidelity?: PlaybookBacktestPass;
  /** Passe LONGUE (5 ans, échelle swing). Peut ne rien produire — voir `conclusion.long.measurable`,
   *  qui dit alors POURQUOI plutôt que d'afficher une section vide. */
  long?: PlaybookBacktestPass | null;
  data_limits?: {
    note: string; m15_days: number; h1_years: number; daily_years: number;
    h4_years?: number; weekly_years?: number;
  };
  conclusion?: {
    headline: string; verdict: string; lines: string[];
    markets: Record<string, PlaybookMarketBreakdown>;
    /** Les 10 meilleurs instruments par marché, tels que mesurés par la passe portée. */
    market_tops?: Record<string, PlaybookMarketTop>;
    long?: {
      measurable: boolean; note?: string; years?: number; pairs_tested?: number;
      overall?: TrainingMetrics; markets?: Record<string, PlaybookMarketBreakdown>;
    };
    /** Fréquence d'opportunité mesurée : la réponse chiffrée à « combien de trades par jour ». */
    frequency?: OpportunityRate;
    volume_levers?: VolumeLevers;
  };
};

export type PlaybookMarketBreakdown = {
  pairs: number; trades: number; win_rate: number; expectancy_r: number;
  best: string | null; best_expectancy_r?: number; trades_per_day?: number;
};

/** Combien de trades l'univers balayé produit, en moyenne des TAUX par symbole (jamais total ÷
 *  période la plus longue : les historiques n'ont pas la même profondeur). */
export type OpportunityRate = {
  symbols: number; note: string;
  avg_trades_per_day_per_symbol?: number;
  median_trades_per_day_per_symbol?: number;
  days_between_trades_per_symbol?: number | null;
  universe_trades_per_day?: number;
  universe_trades_per_week?: number;
  universe_trades_per_month?: number;
  projection?: Record<string, number>;
};

/** Comment obtenir plus de trades : le levier gratuit (élargir l'univers) et les leviers payants
 *  (desserrer un seuil), chacun avec ce qu'il coûte. */
export type VolumeLevers = {
  note: string;
  free: {
    lever: string; why: string;
    measured_rate_per_symbol_per_day: number;
    projection_trades_per_day: Record<string, number>;
    projection_trades_per_week: Record<string, number>;
    best_producers: {
      symbol: string; trades_per_day: number; days_between_trades: number | null;
      expectancy_r: number; r_per_month: number | null;
    }[];
  };
  free_secondary: { lever: string; why: string };
  costly: { setting: string; current: number | boolean; direction: string; effect: string; risk: string }[];
};

/** Backtest de la STRATÉGIE DU DESK sur UN seul instrument — même code, même walk-forward strict
 *  que le backtest complet, restreint à un symbole. */
export type PlaybookSymbolBacktest = {
  available: boolean;
  run_state?: { running: boolean; started_at: string | null; elapsed_s?: number };
  note?: string;
  symbol: string;
  market?: string;
  market_label?: string;
  entry_timeframe: string;
  step?: number;
  error?: string | null;
  verdict?: string;
  duration_s?: number;
  bars?: number;
  bars_evaluated?: number;
  coverage?: string;
  years_covered?: number;
  days_evaluated?: number;
  trades_per_day?: number;
  days_between_trades?: number | null;
  overall?: PlaybookBacktestMetrics;
  by_trigger?: Record<string, PlaybookBacktestMetrics>;
  by_session?: Record<string, PlaybookBacktestMetrics>;
  by_direction?: Record<string, PlaybookBacktestMetrics>;
  by_outcome?: Record<string, PlaybookBacktestMetrics>;
  losers_profile?: PlaybookBacktestPass['losers_profile'];
  /** Nombre de trades en dessous duquel une ventilation ne conclut rien. */
  min_trades?: number;
  secure_ab?: { note?: string; delta_r?: number; trades?: number };
  tp_management_ab?: { note?: string; delta_r?: number; trades?: number };
  trades?: PlaybookBacktestTrade[];
};

/** Un trade rejoué par le backtest — c'est ce qui rend le résultat vérifiable. */
export type PlaybookBacktestTrade = {
  symbol: string; at: string; direction: 'BUY' | 'SELL'; trigger: string;
  entry: number; stop_loss: number; target: number;
  risk_pips: number; reward_pips: number; planned_rr: number;
  outcome: string; r: number; bars_held: number; secured: boolean;
  exit_reason?: string; session?: string; confidence?: number;
};

/** Les marchés backtestables et leurs instruments (menus de la page backtest). */
export type PlaybookBacktestMarkets = {
  markets: { market: string; label: string; symbols: string[]; count: number }[];
  entry_timeframes: { tf: string; label: string; note: string }[];
};

/** L'avis du modèle sur UN instrument, produit HORS stratégie du desk. */
export type MarketOpinion = {
  symbol: string;
  error?: string;
  asset_class?: string;
  direction?: 'BUY' | 'SELL' | 'HOLD';
  stance?: string;              // haussier | baissier | neutre
  confidence?: number;          // 0-100
  conviction?: string;          // lecture en français de la confiance
  consensus_pct?: number;
  headline?: string;
  rationale?: string;
  /** Le détail de CHAQUE agent : c'est ce qui rend l'avis vérifiable plutôt que déclaratif. */
  agents?: { name: string; score: number; confidence: number; rationale: string; details?: any }[];
  master?: {
    score?: number; threshold?: number; consensus?: number; conflict?: boolean;
    weights_used?: Record<string, number>;
  };
  metrics?: Record<string, any>;
  price?: number;
  timeframe?: string;
  /** Niveaux INDICATIFS (ATR) : hors stratégie, ce ne sont pas des ordres à passer. */
  levels?: {
    entry?: number; stop_loss?: number; take_profit_1?: number;
    risk_reward?: number; source?: string;
  };
};

/** L'analyse quotidienne complète — forex + or, hors stratégie. */
export type DailyAnalysis = {
  available: boolean;
  /** Vrai si l'analyse servie ne porte pas la date du jour (annoncé plutôt que caché). */
  stale?: boolean;
  note?: string;
  universe?: string[];
  date?: string;
  generated_at?: string;
  duration_s?: number;
  opinions?: MarketOpinion[];
  summary?: {
    analysed: number; failed?: number;
    bullish?: number; bearish?: number; neutral?: number;
    strongest?: { symbol: string; direction: string; confidence: number; headline: string };
    note: string;
  };
  method?: string;
  disclaimer?: string;
};

export type TrainingMetrics = {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  expectancy_r: number;
  profit_factor: number | null;
  total_r: number;
};

function token(): string | null {
  if (typeof window === 'undefined') return null;
  return localStorage.getItem('qta_token');
}

export function setToken(t: string) {
  localStorage.setItem('qta_token', t);
}

export function clearToken() {
  localStorage.removeItem('qta_token');
}

/** Erreur API enrichie du status HTTP, pour le routage des toasts globaux. */
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/**
 * Transforme un corps d'erreur d'API en TEXTE lisible — toujours une chaîne, jamais un objet.
 *
 * FastAPI renvoie sur une 422 un `detail` qui est un TABLEAU d'objets
 * `[{type, loc, msg, input, ctx}]`. Le passer tel quel à React déclenche l'erreur #31
 * (« Objects are not valid as a React child ») et casse la page au lieu d'afficher le problème.
 * On le met donc à plat ici, une bonne fois pour toutes, pour tous les appels.
 */
export function errorMessage(body: any, status: number): string {
  const detail = body?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => {
        if (typeof d === 'string') return d;
        // `loc` = chemin du champ fautif, ex. ["query", "timeframe"] -> "timeframe".
        const field = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== 'body' && x !== 'query').join('.') : '';
        const msg = d?.msg ?? d?.type ?? 'valeur invalide';
        return field ? `${field} : ${msg}` : String(msg);
      })
      .filter(Boolean);
    if (parts.length) return `Requête invalide — ${parts.join(' · ')}`;
  }
  if (detail && typeof detail === 'object') {
    const msg = (detail as any).msg ?? (detail as any).message;
    if (typeof msg === 'string') return msg;
  }
  if (typeof body?.message === 'string' && body.message.trim()) return body.message;
  return `Erreur ${status}`;
}

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json', ...(opts.headers as object) };
  const t = token();
  if (t) headers.Authorization = `Bearer ${t}`;
  const res = await fetch(`${API}${path}`, { ...opts, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const message = errorMessage(body, res.status);
    // Signal global : le Toaster décide s'il l'affiche (il ignore 401/402/404, gérés inline).
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('qta:api-error', { detail: { message, status: res.status } }));
      // SESSION EXPIRÉE : un token était envoyé et le serveur le rejette (expire après 60 min).
      // Sans ce redirect, les pages qui se rafraîchissent seules (positions, scanner, trades du
      // jour…) avalent l'erreur en silence dans leur boucle d'auto-refresh et affichent des
      // zéros qui ressemblent à un compte vide — alors que les données existent bel et bien côté
      // serveur. On ne redirige QUE si un token était présent : un 401 sur une tentative de
      // connexion (mauvais mot de passe) ne doit jamais renvoyer vers /login en boucle.
      if (res.status === 401 && t && !window.location.pathname.startsWith('/login')) {
        clearToken();
        window.location.href = '/login?expired=1';
      }
    }
    throw new ApiError(message, res.status);
  }
  return res.json() as Promise<T>;
}

export const api = {
  register: (email: string, password: string) =>
    req<{ access_token: string }>('/api/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),
  login: (email: string, password: string) =>
    req<{ access_token: string }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),
  me: () => req<Me>('/api/auth/me'),
  onboard: (risk_profile: string, capital: number, watchlist: string[]) =>
    req<Me>('/api/onboarding', { method: 'POST', body: JSON.stringify({ risk_profile, capital, watchlist }) }),
  listSignals: () => req<Signal[]>('/api/signals'),
  getSignal: (id: string) => req<Signal>(`/api/signals/${id}`),
  signalsTrackRecord: () => req<SignalsTrackRecord>('/api/signals/track-record'),
  signalMode: () => req<{ mode: string }>('/api/signals/mode'),
  setSignalMode: (mode: string) => req<{ mode: string }>(`/api/signals/mode?mode=${mode}`, { method: 'POST' }),
  marketRegime: () => req<MarketRegime>('/api/market/regime'),
  edgeMap: () => req<EdgeMap>('/api/backtest/edge-map'),
  /** Verdicts 🟢/🟡/🔴 par paire du backtest hebdo — seules les 🟢 sont auto-tradées. */
  pairVerdicts: () => req<PairVerdicts>('/api/backtest/playbook/verdicts'),
  runEdgeSweep: (timeframe?: string, market?: string) => {
    const p = new URLSearchParams();
    if (timeframe) p.set('timeframe', timeframe);
    if (market) p.set('market', market);
    return req<EdgeMap>(`/api/backtest/edge-map/run?${p}`, { method: 'POST' });
  },
  clearJournal: () => req<{ cleared: number }>('/api/journal', { method: 'DELETE' }),
  clearSignals: () => req<{ deleted: number }>('/api/signals', { method: 'DELETE' }),
  /** Bougies d'un actif AVEC leur source : une page ne doit jamais afficher du fictif comme du réel. */
  ohlcv: (asset: string, timeframe: string) =>
    req<OhlcvResponse>(`/api/market/ohlcv?asset=${encodeURIComponent(asset)}&timeframe=${timeframe}`),
  symbols: (q?: string, asset_class?: string, session?: string) => {
    const p = new URLSearchParams();
    if (q) p.set('q', q);
    if (asset_class) p.set('asset_class', asset_class);
    if (session) p.set('session', session);
    return req<{ results: { symbol: string; asset_class: string; label?: string }[]; classes: string[] }>(`/api/market/symbols?${p}`);
  },
  dailyPicks: (refresh = false, timeframe = '1h') =>
    req<{ date: string; timeframe?: string; picks: any[]; generated_at: string }>(
      `/api/signals/daily-picks?timeframe=${timeframe}${refresh ? '&refresh=true' : ''}`,
    ),
  /** Les trades du jour issus de la STRATÉGIE (mensuel+journalier → 4 h → entrée 15 min).
   *  Sans `refresh`, la réponse est l'instantané déjà calculé par la boucle de fond : elle arrive
   *  en quelques millisecondes, ce qui autorise un rafraîchissement toutes les 10 secondes. */
  topTrades: (refresh = false, count = 0) =>
    req<TopTrades>(`/api/signals/top-trades?count=${count}${refresh ? '&refresh=true' : ''}`),
  /** État de l'auto-entrée en compte démo (ce que le robot surveille et ce qu'il a ouvert seul). */
  autoEntry: () => req<AutoEntryStatus>('/api/signals/auto-entry'),
  /** Force un passage de veille de l'auto-entrée. */
  runAutoEntry: () => req<{ opened: any[]; note: string }>('/api/signals/auto-entry/run', { method: 'POST' }),
  /** REMET L'AUTO-ENTRÉE À ZÉRO : positions démo en cours neutralisées (issue « reset », hors
   *  statistiques) + traces effacées, puis un passage de veille immédiat. Ne touche rien de réel. */
  resetAutoEntry: () =>
    req<{ closed: { symbol: string }[]; events_cleared: number; note: string; run?: any }>(
      '/api/signals/auto-entry/reset', { method: 'POST' },
    ),
  /** Dernier backtest de LA STRATÉGIE sur le forex et l'or : classement des paires + conclusion. */
  playbookBacktest: () => req<PlaybookBacktest>('/api/backtest/playbook'),
  /** LANCE le backtest en arrière-plan et rend la main aussitôt (il dure une dizaine de minutes).
   *  L'avancement se suit via `playbookBacktest().run_state`. */
  runPlaybookBacktest: () =>
    req<{ started: boolean; run_state: BacktestRunState; note: string }>(
      '/api/backtest/playbook/run', { method: 'POST' },
    ),
  /** Marchés backtestables + instruments de chacun (les mêmes que le backtest complet). */
  playbookBacktestMarkets: () => req<PlaybookBacktestMarkets>('/api/backtest/playbook/markets'),
  /** Dernier backtest de la stratégie du desk sur UN symbole. */
  playbookSymbolBacktest: (symbol: string, entryTf = '1h') =>
    req<PlaybookSymbolBacktest>(
      `/api/backtest/playbook/symbol?symbol=${encodeURIComponent(symbol)}&entry_tf=${entryTf}`,
    ),
  /** LANCE ce backtest en arrière-plan (quelques secondes à une minute selon la profondeur). */
  runPlaybookSymbolBacktest: (symbol: string, entryTf = '1h', step = 4) =>
    req<{ started: boolean; symbol: string; note: string }>(
      `/api/backtest/playbook/symbol/run?symbol=${encodeURIComponent(symbol)}` +
      `&entry_tf=${entryTf}&step=${step}`,
      { method: 'POST' },
    ),
  /** ANALYSE QUOTIDIENNE des marchés (forex + or), produite HORS stratégie du desk. */
  dailyAnalysis: () => req<DailyAnalysis>('/api/analysis/daily'),
  /** Force une analyse immédiate (synchrone : l'univers est court). */
  runDailyAnalysis: () => req<DailyAnalysis>('/api/analysis/daily/run', { method: 'POST' }),
  /** Entraînement quotidien des agents sur la stratégie (walk-forward mesuré + fiches). */
  training: () => req<TrainingReport>('/api/agents/training'),
  /** Relance un entraînement complet (opération longue). */
  runTraining: () => req<TrainingReport>('/api/agents/training/run', { method: 'POST' }),
  /** Détail des 4 étapes de la stratégie pour un symbole. */
  playbook: (symbol: string) =>
    req<PlaybookTrade & { summary: string }>(`/api/signals/playbook/${symbol}`),
  /** Ouvre les trades prêts du playbook en COMPTE DÉMO, avec leur SL/TP. */
  executePlaybook: (count = 5) =>
    req<PaperExecutionReport>(`/api/execution/playbook/execute?count=${count}`, { method: 'POST' }),
  /** Ouvre en démo le trade d'UN symbole (bouton d'une carte). La stratégie est recalculée. */
  executePlaybookSymbol: (symbol: string) =>
    req<PaperExecutionReport>(`/api/execution/playbook/execute-symbol/${symbol}`, { method: 'POST' }),
  /** Positions + P&L latent calculé côté serveur (pour un rafraîchissement automatique). */
  positions: () => req<PositionsSnapshot>('/api/execution/positions'),
  verifySignal: (s: Signal) =>
    req<{ verdict: string; passed: number; total: number; checks: { label: string; pass: boolean; value: any }[]; backtest: any }>(
      '/api/signals/verify',
      {
        method: 'POST',
        body: JSON.stringify({
          symbol: s.asset,
          timeframe: s.timeframe,
          direction: s.direction,
          confidence: s.confidence,
          consensus_pct: s.consensus_pct ?? 0,
          risk_reward: s.risk_reward,
          mtf_aligned: s.mtf?.aligned ?? 0,
          mtf_total: s.mtf?.total ?? 0,
          adx: s.metrics?.adx ?? null,
        }),
      },
    ),
  /** Scan par LA STRATÉGIE DU DESK : chaque ligne porte son verdict, ses métriques et son `why`
   *  (comment la tendance a été établie, quelles confirmations, ce qui a bloqué). */
  scan: (asset_class?: string, timeframe = '1h', limit = 100, high_conviction_only = false, session?: string) =>
    req<{
      count: number; high_conviction: number; results: any[];
      ready?: number; armed?: number; refused?: number; not_scanned?: number; strategy?: string;
    }>(
      `/api/signals/scan?${new URLSearchParams({
        ...(asset_class ? { asset_class } : {}),
        ...(session ? { session } : {}),
        timeframe,
        limit: String(limit),
        high_conviction_only: String(high_conviction_only),
      })}`,
    ),
  dataSource: (asset: string, timeframe = '1h') =>
    req<{ asset: string; source: string; real: boolean; label: string }>(
      `/api/market/data-source?asset=${encodeURIComponent(asset)}&timeframe=${timeframe}`,
    ),
  sessions: () =>
    req<{ utc_time: string; active: string[]; sessions: { id: string; label: string; window_utc: string; open: boolean; symbol_count: number }[] }>('/api/market/sessions'),
  generate: (asset: string, timeframe: string, notify = false) =>
    req<Signal>('/api/signals/generate', { method: 'POST', body: JSON.stringify({ asset, timeframe, notify }) }),
  plans: () => req<{ id: string; price: number; features: string[] }[]>('/api/billing/plans'),
  checkout: (plan: string) =>
    req<{ mode: string; checkout_url?: string; user?: Me }>(`/api/billing/checkout/${plan}`, {
      method: 'POST',
    }),
  getSettings: () => req<Settings>('/api/settings'),
  updateSettings: (patch: Partial<Settings>) =>
    req<Settings>('/api/settings', { method: 'PATCH', body: JSON.stringify(patch) }),
  riskStatus: () => req<RiskStatus>('/api/risk/status'),
  portfolio: () => req<Portfolio>('/api/portfolio'),
  heatmap: (mix = false) => req<HeatmapItem[]>(`/api/market/heatmap${mix ? '?mix=true' : ''}`),
  mfaSetup: () => req<{ secret: string; otpauth_uri: string }>('/api/auth/mfa/setup', { method: 'POST' }),
  mfaEnable: (code: string) => req<Me>('/api/auth/mfa/enable', { method: 'POST', body: JSON.stringify({ code }) }),
  mfaDisable: () => req<Me>('/api/auth/mfa/disable', { method: 'POST' }),
  runBacktest: (config: BacktestConfig) => req<BacktestReport>('/api/backtest/run', { method: 'POST', body: JSON.stringify(config) }),
  listBacktests: () => req<BacktestReport[]>('/api/backtest/reports'),
  walkForward: (symbol: string, timeframe = '1h', folds = 4) =>
    req<WalkForward>(`/api/backtest/walk-forward?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&folds=${folds}`, { method: 'POST' }),
  trackRecord: (refresh = false) =>
    req<TrackRecord>(`/api/backtest/track-record${refresh ? '?refresh=true' : ''}`),
  // La stratégie du desk. Il n'y a plus de bibliothèque ni de sélection : le projet entier
  // applique cette méthode et elle seule, sur tous les marchés.
  autoTrade: () => req<{ auto_trade: boolean }>('/api/agents/auto-trade'),
  setAutoTrade: (enabled: boolean) =>
    req<{ auto_trade: boolean }>(`/api/agents/auto-trade?enabled=${enabled}`, { method: 'POST' }),
  agentsStatus: () => req<AgentStatus>('/api/agents/status'),
  /** LA stratégie décrite de A à Z, avec les seuils réellement appliqués par le moteur. */
  strategySpec: () => req<StrategySpec>('/api/agents/strategy'),
  // Phase 3
  myPlan: () => req<PlanInfo>('/api/plan'),
  upgrade: (plan: string) => req<{ mode: string; checkout_url?: string }>(`/api/billing/checkout/${plan}`, { method: 'POST' }),
  copilotAsk: (message: string, asset?: string) =>
    req<{ asset: string; answer: string }>('/api/copilot/ask', { method: 'POST', body: JSON.stringify({ message, ...(asset ? { asset } : {}) }) }),
  journalList: () => req<JournalEntry[]>('/api/journal'),
  journalInsights: () => req<JournalInsights>('/api/journal/insights'),
  journalAutoResolve: () => req<{ resolved: number }>('/api/journal/auto-resolve', { method: 'POST' }),
  journalClose: (id: string, outcome: string, pnl: number | null) =>
    req<JournalEntry>(`/api/journal/${id}/close`, { method: 'POST', body: JSON.stringify({ outcome, pnl }) }),
  journalExplain: (id: string) =>
    req<{ id: string; explanation: string }>(`/api/journal/${id}/explain`, { method: 'POST' }),
  team: () => req<{ plan: string; members: TeamMember[] }>('/api/team'),
  teamInvite: (email: string, full_name?: string) =>
    req<{ member: TeamMember; temp_password: string }>('/api/team/invite', { method: 'POST', body: JSON.stringify({ email, full_name }) }),
  // Phase 4 — Exécution broker + KYC
  kycStatus: () => req<{ status: string }>('/api/kyc'),
  kycSubmit: (legal_name: string, country: string, doc_id: string) =>
    req<{ status: string }>('/api/kyc', { method: 'POST', body: JSON.stringify({ legal_name, country, doc_id }) }),
  brokers: () => req<BrokerConn[]>('/api/execution/brokers'),
  connectBroker: (broker: string, mode: string, api_key = '', api_secret = '') =>
    req<BrokerConn>('/api/execution/brokers', { method: 'POST', body: JSON.stringify({ broker, mode, api_key, api_secret }) }),
  revokeBroker: (id: string) => req<{ revoked: boolean }>(`/api/execution/brokers/${id}`, { method: 'DELETE' }),
  placeOrder: (conn_id: string, symbol: string, side: string, qty: number, stop_loss?: number | null, take_profit?: number | null) =>
    req<Order>('/api/execution/orders', {
      method: 'POST',
      body: JSON.stringify({ conn_id, symbol, side, qty, stop_loss: stop_loss ?? null, take_profit: take_profit ?? null }),
    }),
  orders: () => req<Order[]>('/api/execution/orders'),
  wallet: () => req<Wallet>('/api/wallet'),
  resetWallet: (starting_balance: number, clear_orders = true) =>
    req<Wallet>(`/api/wallet/reset?starting_balance=${starting_balance}&clear_orders=${clear_orders}`, { method: 'POST' }),
  checkOrder: (id: string) => req<Order>(`/api/execution/orders/${id}/check`, { method: 'POST' }),
  closeOrder: (id: string) => req<Order>(`/api/execution/orders/${id}/close`, { method: 'POST' }),
  /** Modifie MANUELLEMENT le stop et/ou l'objectif d'une position papier encore ouverte. Au moins
   *  un des deux champs doit être fourni. Refusé si le prix actuel a déjà franchi le niveau
   *  demandé (la position se clôturerait sinon dès le prochain passage de surveillance). */
  updateOrderLevels: (id: string, levels: { stop_loss?: number; take_profit?: number }) =>
    req<PaperPosition>(`/api/execution/orders/${id}/levels`, {
      method: 'POST', body: JSON.stringify(levels),
    }),
  // Phase 4 — Copy-trading
  leaderboard: () => req<Trader[]>('/api/copytrading/leaderboard'),
  publishProfile: (display_name: string) => req<unknown>('/api/copytrading/publish', { method: 'POST', body: JSON.stringify({ display_name }) }),
  following: () => req<CopyFollow[]>('/api/copytrading/following'),
  follow: (leader_tenant: string, allocation_pct: number, max_per_trade: number, min_confidence: number) =>
    req<CopyFollow>('/api/copytrading/follow', { method: 'POST', body: JSON.stringify({ leader_tenant, allocation_pct, max_per_trade, min_confidence }) }),
  unfollow: (id: string) => req<{ unfollowed: boolean }>(`/api/copytrading/follow/${id}`, { method: 'DELETE' }),
  commissions: () => req<{ total: number; count: number; items: any[] }>('/api/copytrading/commissions'),
  // Phase 4 — Marketplace
  listings: () => req<Listing[]>('/api/marketplace/listings'),
  createListing: (l: { title: string; kind: string; price: number; description: string; config: any }) =>
    req<Listing>('/api/marketplace/listings', { method: 'POST', body: JSON.stringify(l) }),
  buyListing: (id: string) => req<{ purchase_id: string; config: any }>(`/api/marketplace/listings/${id}/buy`, { method: 'POST' }),
  purchases: () => req<any[]>('/api/marketplace/purchases'),
  apiKeys: () => req<ApiKey[]>('/api/marketplace/api-keys'),
  createApiKey: (label: string) => req<{ id: string; api_key: string; prefix: string }>('/api/marketplace/api-keys', { method: 'POST', body: JSON.stringify({ label }) }),
  revokeApiKey: (id: string) => req<{ revoked: boolean }>(`/api/marketplace/api-keys/${id}`, { method: 'DELETE' }),
  // Phase 5 — i18n + white-label
  i18n: (locale: string) => req<{ locale: string; supported: string[]; messages: Record<string, string> }>(`/api/i18n/${locale}`),
  branding: () => req<Branding>('/api/branding'),
  setBranding: (b: Partial<Branding>) => req<Branding>('/api/branding', { method: 'PUT', body: JSON.stringify(b) }),
};

/** Copilot en streaming SSE. Appelle onDelta pour chaque fragment, onDone à la fin. */
export async function copilotStream(
  message: string,
  onDelta: (s: string) => void,
  onDone?: () => void,
  asset?: string,
): Promise<void> {
  const t = token();
  const res = await fetch(`${API}/api/copilot/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(t ? { Authorization: `Bearer ${t}` } : {}) },
    body: JSON.stringify({ message, ...(asset ? { asset } : {}) }),
  });
  if (!res.ok || !res.body) {
    throw new Error(res.status === 402 ? 'Copilot réservé au plan Pro' : `Erreur ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n\n');
    buffer = lines.pop() ?? '';
    for (const block of lines) {
      const line = block.trim();
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (payload === '[DONE]') { onDone?.(); return; }
      try {
        const obj = JSON.parse(payload);
        if (obj.delta) onDelta(obj.delta);
      } catch { /* ignore */ }
    }
  }
  onDone?.();
}

export type LiveCandle = { symbol: string; interval: string; open: number; high: number; low: number; close: number; volume: number };

export function openSignalStream(
  onSignal: (s: Signal) => void,
  onCandle?: (c: LiveCandle) => void,
): WebSocket | null {
  const t = token();
  if (!t) return null;
  const ws = new WebSocket(`${WS}/signals?token=${t}`);
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'signal') onSignal(msg.data as Signal);
      else if (msg.type === 'candle') onCandle?.(msg.data as LiveCandle);
    } catch {
      /* ignore */
    }
  };
  return ws;
}
