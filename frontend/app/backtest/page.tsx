'use client';

/**
 * MOTEUR DE BACKTEST — tester une configuration précise sur l'historique d'une paire.
 *
 * Mêmes sélecteurs que le Scanner (marché → paire → unité de temps) : on ne devrait pas avoir à
 * taper un symbole à la main ici et le choisir dans une liste ailleurs.
 *
 * À ne pas confondre avec le RAPPORT (`/backtest/rapport`), qui présente le backtest de la
 * stratégie du desk sur toutes les paires : ici on teste UNE configuration, là-bas on mesure LA
 * méthode.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, type BacktestConfig, type BacktestReport } from '@/lib/api';
import { MarketSelector, PlaybookSymbolBacktest } from '@/components/domain';
import {
  Button, Card, EmptyState, PageHeader, PROVE_TABS, RouteTabs,
  Table, TBody, TD, TH, THead, TR,
} from '@/components/ui';
import { TIMEFRAMES } from '@/lib/markets';

const PERIODS = [
  { days: 30, label: '30 jours' },
  { days: 90, label: '3 mois' },
  { days: 180, label: '6 mois' },
  { days: 365, label: '1 an' },
];

function iso(daysAgo: number): string {
  return new Date(Date.now() - daysAgo * 86_400_000).toISOString();
}

function Kpi({ label, value, tone, help }: { label: string; value: string; tone?: 'buy' | 'sell'; help?: string }) {
  return (
    <Card className="p-4" title={help}>
      <div className="text-xs text-muted">{label}</div>
      <div className={`text-2xl font-bold ${tone === 'buy' ? 'text-buy' : tone === 'sell' ? 'text-sell' : 'text-white'}`}>
        {value}
      </div>
    </Card>
  );
}

export default function BacktestPage() {
  const [report, setReport] = useState<BacktestReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Sélecteurs — identiques au Scanner.
  const [cls, setCls] = useState('forex');
  const [symbols, setSymbols] = useState<{ symbol: string; asset_class: string }[]>([]);
  const [symbol, setSymbol] = useState('EUR/USD');
  const [tf, setTf] = useState('1h');
  const [days, setDays] = useState(90);
  const [capital, setCapital] = useState(10_000);
  const [riskPct, setRiskPct] = useState(1.0);
  const [useLlm, setUseLlm] = useState(false);

  // Catalogue filtré par marché : on choisit une paire dans une liste, jamais à la main.
  useEffect(() => {
    api.symbols(undefined, cls || undefined)
      .then((d) => {
        setSymbols(d.results);
        if (d.results.length && !d.results.some((s) => s.symbol === symbol)) {
          setSymbol(d.results[0].symbol);
        }
      })
      .catch(() => setSymbols([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cls]);

  const config: BacktestConfig = useMemo(() => ({
    symbol,
    timeframe: TIMEFRAMES.find((t) => t.tf === tf)?.interval ?? tf,
    start_time: iso(days),
    end_time: new Date().toISOString(),
    initial_capital: capital,
    risk_per_trade_pct: riskPct,
    use_llm: useLlm,
  }), [symbol, tf, days, capital, riskPct, useLlm]);

  const run = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setReport(await api.runBacktest(config));
    } catch (e: any) {
      setError(e.message ?? 'Le backtest a échoué.');
      setReport(null);
    } finally {
      setLoading(false);
    }
  }, [config]);

  const m = report?.metrics;

  return (
    <div className="space-y-6 p-8">
      <PageHeader
        title="Moteur de backtest"
        subtitle="Teste UNE configuration sur l'historique d'une paire — frais et slippage inclus."
      />
      <RouteTabs items={PROVE_TABS} />

      {/* LA STRATÉGIE DU DESK, sur un instrument choisi. C'est ce qu'on veut le plus souvent :
          « et sur celui-ci, ça donne quoi ? » — sans attendre les dix minutes du passage complet. */}
      <PlaybookSymbolBacktest />

      <Card className="border-accent/40 bg-accent/5 p-4">
        <p className="text-sm text-white">
          Tu cherches le backtest de <strong>la stratégie du desk sur TOUS les marchés</strong> ?
        </p>
        <p className="mt-0.5 text-[11px] text-muted">
          Il est sur la page <a href="/backtest/rapport" className="text-accent underline">Rapport de backtest</a> :
          les 10 meilleurs instruments de chaque marché, le classement complet par fiabilité, le
          profil des trades perdants, la méthode et ses limites.
        </p>
      </Card>

      {/* ---- Moteur générique : une CONFIGURATION, pas la stratégie du desk ---- */}
      <Card className="space-y-4 p-6">
        <div>
          <h2 className="text-lg font-semibold text-white">
            Moteur générique — tester une configuration
          </h2>
          <p className="mt-0.5 text-xs text-muted">
            Ce bloc-ci ne rejoue PAS la stratégie du desk : il teste une configuration ponctuelle
            (capital, risque par trade, agents LLM) sur une paire. Les deux résultats ne sont pas
            comparables entre eux.
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <label className="mb-1 block text-xs text-muted">Marché</label>
            <MarketSelector value={cls} onChange={setCls} label={null} />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">Paire / Symbole</label>
            <select
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              className="rounded-lg border border-border bg-background px-3 py-2 font-mono text-sm outline-none focus:border-accent"
            >
              {symbols.map((s) => <option key={s.symbol} value={s.symbol}>{s.symbol}</option>)}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">Unité de temps</label>
            <select
              value={tf}
              onChange={(e) => setTf(e.target.value)}
              className="rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-accent"
            >
              {TIMEFRAMES.map((t) => <option key={t.tf} value={t.tf}>{t.label}</option>)}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">Période</label>
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              className="rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-accent"
            >
              {PERIODS.map((p) => <option key={p.days} value={p.days}>{p.label}</option>)}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">Capital initial</label>
            <input
              type="number"
              value={capital}
              onChange={(e) => setCapital(Number(e.target.value))}
              className="w-32 rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">Risque / trade (%)</label>
            <input
              type="number"
              step="0.1"
              value={riskPct}
              onChange={(e) => setRiskPct(Number(e.target.value))}
              className="w-24 rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
          <Button onClick={run} loading={loading}>
            {loading ? 'Simulation…' : 'Lancer le backtest'}
          </Button>
        </div>
        <label className="flex items-center gap-2 text-sm text-muted">
          <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />
          Activer les agents LLM (plus lent, et coûte des appels — le résultat déterministe suffit
          dans la plupart des cas)
        </label>
      </Card>

      {error && <p className="text-sell">{error}</p>}

      {!report && !loading && (
        <EmptyState
          title="Aucun backtest lancé"
          description="Choisis un marché, une paire et une période, puis lance la simulation."
        />
      )}

      {report && m && (
        <div className="space-y-4">
          <p className="text-xs text-muted">
            <span className="font-mono text-white/80">{report.config.symbol}</span> ·{' '}
            {report.config.timeframe} · capital {report.config.initial_capital} ·
            risque {report.config.risk_per_trade_pct} % par trade
          </p>

          <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-6">
            <Kpi
              label="P&L total"
              value={`${m.total_pnl.toFixed(2)} (${m.total_pnl_pct.toFixed(2)} %)`}
              tone={m.total_pnl >= 0 ? 'buy' : 'sell'}
            />
            <Kpi label="Trades" value={`${m.total_trades}`} />
            <Kpi
              label="Taux de réussite"
              value={`${(m.win_rate * 100).toFixed(1)} %`}
              help="Seul, ce chiffre ne dit rien : il doit se lire avec le profit factor."
            />
            <Kpi
              label="Profit factor"
              value={m.profit_factor.toFixed(2)}
              tone={m.profit_factor >= 1.5 ? 'buy' : m.profit_factor < 1 ? 'sell' : undefined}
              help="Somme des gains ÷ somme des pertes. Sous 1, la stratégie perd de l'argent."
            />
            <Kpi
              label="Drawdown max"
              value={`${m.max_drawdown_pct.toFixed(2)} %`}
              tone="sell"
              help="Pire perte cumulée depuis un sommet — ce qu'il faut pouvoir encaisser."
            />
            <Kpi label="Sharpe" value={m.sharpe_ratio.toFixed(2)} />
          </div>

          <Card className="p-6">
            <h2 className="mb-3 text-lg font-semibold text-white">
              Historique des trades ({m.total_trades})
            </h2>
            {report.trades.length === 0 ? (
              <p className="text-sm text-muted">
                Aucun trade sur cette période : les filtres de qualité n&apos;ont jamais été
                entièrement satisfaits. C&apos;est un résultat, pas une panne.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <Table>
                  <THead>
                    <TR>
                      <TH>Entrée</TH><TH>Sens</TH>
                      <TH className="text-right">Prix entrée</TH>
                      <TH className="text-right">Prix sortie</TH>
                      <TH className="text-right">P&L</TH>
                    </TR>
                  </THead>
                  <TBody>
                    {report.trades.map((t: any, i: number) => (
                      <TR key={i}>
                        <TD className="text-muted">{new Date(t.entry_time).toLocaleString('fr-FR')}</TD>
                        <TD className={t.direction === 'BUY' ? 'text-buy' : 'text-sell'}>{t.direction}</TD>
                        <TD className="text-right font-mono">{t.entry_price?.toFixed(5)}</TD>
                        <TD className="text-right font-mono">{t.exit_price?.toFixed(5)}</TD>
                        <TD className={`text-right font-medium ${t.pnl >= 0 ? 'text-buy' : 'text-sell'}`}>
                          {t.pnl?.toFixed(2)} ({t.pnl_pct?.toFixed(2)} %)
                        </TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
              </div>
            )}
          </Card>

          <p className="text-[11px] text-muted">
            Frais et slippage sont inclus dans la simulation. Un résultat passé ne prédit pas le
            futur — il décrit ce qu&apos;aurait donné cette configuration sur la période choisie.
          </p>
        </div>
      )}
    </div>
  );
}
