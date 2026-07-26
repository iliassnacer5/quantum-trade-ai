'use client';

/**
 * Rafraîchissement AUTOMATIQUE des pages d'analyse — l'utilisateur n'a jamais à cliquer.
 *
 * - relance `fn` à intervalle régulier ;
 * - suspend la boucle quand l'onglet est masqué (inutile de charger en arrière-plan) et rafraîchit
 *   immédiatement au retour ;
 * - ignore les exécutions concurrentes (si un appel est lent, on ne l'empile pas) ;
 * - expose l'heure du dernier rafraîchissement et un compte à rebours pour l'afficher.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export type AutoRefresh = {
  /** Horodatage du dernier rafraîchissement réussi. */
  lastRefresh: Date | null;
  /** Secondes restantes avant le prochain rafraîchissement. */
  nextIn: number;
  /** Vrai pendant un rafraîchissement automatique. */
  refreshing: boolean;
  /** Force un rafraîchissement immédiat et réarme le minuteur. */
  refreshNow: () => void;
};

export function useAutoRefresh(
  fn: () => Promise<unknown>,
  intervalMs: number,
  enabled = true,
): AutoRefresh {
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [nextIn, setNextIn] = useState(Math.round(intervalMs / 1000));
  const [refreshing, setRefreshing] = useState(false);
  const running = useRef(false);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const run = useCallback(async () => {
    if (running.current || document.hidden) return;
    running.current = true;
    setRefreshing(true);
    try {
      await fnRef.current();
      setLastRefresh(new Date());
    } catch {
      /* une erreur ponctuelle ne doit pas arrêter la boucle */
    } finally {
      running.current = false;
      setRefreshing(false);
      setNextIn(Math.round(intervalMs / 1000));
    }
  }, [intervalMs]);

  const refreshNow = useCallback(() => {
    setNextIn(Math.round(intervalMs / 1000));
    void run();
  }, [run, intervalMs]);

  useEffect(() => {
    if (!enabled) return;
    const tick = setInterval(() => {
      setNextIn((s) => {
        if (document.hidden) return s;
        if (s <= 1) {
          void run();
          return Math.round(intervalMs / 1000);
        }
        return s - 1;
      });
    }, 1000);
    // Retour sur l'onglet : on remet les données à jour sans attendre le prochain tick.
    const onVisible = () => {
      if (!document.hidden) void run();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(tick);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [enabled, intervalMs, run]);

  return { lastRefresh, nextIn, refreshing, refreshNow };
}

/** Badge « mis à jour il y a … · prochain dans … s » à afficher sur les pages temps réel. */
export function refreshLabel(a: AutoRefresh): string {
  const at = a.lastRefresh
    ? a.lastRefresh.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : '—';
  if (a.refreshing) return `mise à jour en cours… (dernière : ${at})`;
  return `mis à jour à ${at} · prochain dans ${a.nextIn} s`;
}
