'use client';

import { TIMEFRAMES } from '@/lib/markets';
import { Segmented } from '@/components/ui';

/**
 * Sélecteur de timeframe partagé. `by` choisit la clé de valeur :
 * - 'tf' → 15min/1h/4h/1d/1week/1month (libellés exposés par l'API)
 * - 'interval' → 15m/1h/4h/1d/1w/1M (intervalles du connecteur de données)
 */
export function TimeframePicker({
  value,
  onChange,
  by = 'tf',
  size = 'sm',
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  by?: 'tf' | 'interval';
  size?: 'sm' | 'md';
  className?: string;
}) {
  return (
    <Segmented
      size={size}
      aria-label="Timeframe"
      className={className}
      value={value}
      onChange={onChange}
      options={TIMEFRAMES.map((t) => ({ value: t[by], label: t.label }))}
    />
  );
}
