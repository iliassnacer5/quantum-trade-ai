'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { cn } from '@/lib/cn';

/** Onglets de navigation entre pages liées (surligne la route active). */
export function RouteTabs({ items, className }: { items: { href: string; label: string }[]; className?: string }) {
  const pathname = usePathname();
  return (
    <div className={cn('inline-flex flex-wrap gap-0.5 rounded-lg border border-border bg-background p-0.5', className)}>
      {items.map((it) => {
        const active = pathname === it.href;
        return (
          <Link
            key={it.href}
            href={it.href}
            aria-current={active ? 'page' : undefined}
            className={cn(
              'rounded-md px-3 py-1.5 text-sm font-medium transition',
              active ? 'bg-accent text-background shadow-card' : 'text-muted hover:text-white',
            )}
          >
            {it.label}
          </Link>
        );
      })}
    </div>
  );
}

/** Onglets « Prouver » : preuve de l'edge, stratégies, backtest, track record. */
export const PROVE_TABS = [
  { href: '/edge', label: '🗺️ Edge' },
  { href: '/strategies', label: 'Stratégies' },
  { href: '/backtest', label: 'Backtest' },
  { href: '/backtest/rapport', label: 'Rapport' },
  { href: '/track-record', label: 'Track record' },
];
