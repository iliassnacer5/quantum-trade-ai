/**
 * Source unique de vérité pour les marchés et timeframes.
 * Auparavant dupliquée dans 5+ pages (une liste crypto/forex/actions/or par page) — Phase F2.
 */
export type MarketClass = { id: string; label: string };

export const MARKET_CLASSES: MarketClass[] = [
  { id: '', label: 'Tous' },
  { id: 'forex', label: 'Forex' },
  { id: 'index', label: '📊 Indices' },
  { id: 'commodity', label: '🥇 Or & Métaux' },
  { id: 'stock', label: 'Actions' },
  { id: 'crypto', label: 'Crypto' },
];

/** Marchés concrets (sans « Tous ») — pour les pages qui exigent un marché précis. */
export const MARKET_CLASSES_CONCRETE: MarketClass[] = MARKET_CLASSES.filter((c) => c.id);

/** Badge court par marché (listes, tableaux). */
export const MARKET_BADGE: Record<string, string> = {
  crypto: '₿ Crypto',
  forex: '💱 Forex',
  stock: '📈 Actions',
  commodity: '🥇 Or',
  index: '📊 Indice',
};

export type Timeframe = { tf: string; interval: string; label: string };

/**
 * Unités de temps du desk, nommées par leur DURÉE et non par un style de trading.
 * « Scalp », « intraday », « swing » décrivent une façon de trader, pas une échelle de temps.
 * Ce sont exactement les unités de la stratégie : mensuel/journalier → 4 h → 1 h → entrée 15 min.
 */
export const TIMEFRAMES: Timeframe[] = [
  { tf: '15min', interval: '15m', label: '15 min' },
  { tf: '1h', interval: '1h', label: '1 h' },
  { tf: '4h', interval: '4h', label: '4 h' },
  { tf: '1d', interval: '1d', label: '1 jour' },
  { tf: '1week', interval: '1w', label: '1 semaine' },
  { tf: '1month', interval: '1M', label: '1 mois' },
];
