'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useState } from 'react';
import { api, setToken } from '@/lib/api';
import { Button } from '@/components/ui';

/**
 * Isolé dans son propre composant + Suspense : `useSearchParams` fait basculer en rendu
 * client tout composant qui l'appelle, ce qui casserait le prérendu statique de `/register`
 * si l'appel vivait directement dans `AuthForm` (partagé par les deux pages).
 */
function ExpiredNotice() {
  const searchParams = useSearchParams();
  // Redirigé ici parce que le jeton a expiré (session de 60 min) pendant qu'une page se
  // rafraîchissait toute seule : sans ce message, l'écran de connexion semble sorti de nulle part.
  if (searchParams.get('expired') !== '1') return null;
  return (
    <p className="rounded-lg border border-warn/30 bg-warn-soft/20 px-3 py-2 text-sm text-white/85">
      Session expirée (elle dure 60 minutes) — reconnecte-toi pour retrouver ton poste, tes trades
      ne sont pas perdus.
    </p>
  );
}

export function AuthForm({ mode }: { mode: 'login' | 'register' }) {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const isRegister = mode === 'register';

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const res = isRegister ? await api.register(email, password) : await api.login(email, password);
      setToken(res.access_token);
      const me = await api.me();
      router.push(me.onboarded ? '/dashboard' : '/onboarding');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Erreur');
    } finally {
      setLoading(false);
    }
  }

  return (
    <form onSubmit={submit} className="glass w-full max-w-sm space-y-4 rounded-2xl p-7 shadow-elevated">
      <div className="mb-1 flex items-center gap-2 font-bold text-white">
        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-gradient text-background">Q</span>
        Quantum<span className="text-gradient">Trade</span>
      </div>
      <div>
        <h1 className="text-h2 text-white">{isRegister ? 'Créer un compte' : 'Bon retour'}</h1>
        <p className="text-sm text-muted">
          {isRegister ? 'Quelques secondes pour démarrer.' : 'Connecte-toi pour retrouver ton poste.'}
        </p>
      </div>
      <div className="space-y-3">
        <input
          type="email"
          placeholder="Email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
          className="w-full rounded-lg border border-border bg-background/60 px-3 py-2.5 text-sm outline-none transition focus:border-accent"
        />
        <input
          type="password"
          placeholder="Mot de passe (8+ caractères)"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          minLength={8}
          className="w-full rounded-lg border border-border bg-background/60 px-3 py-2.5 text-sm outline-none transition focus:border-accent"
        />
      </div>
      {!isRegister && !error && (
        <Suspense fallback={null}>
          <ExpiredNotice />
        </Suspense>
      )}
      {error && <p className="rounded-lg border border-sell/30 bg-sell/10 px-3 py-2 text-sm text-sell">{error}</p>}
      <Button type="submit" size="lg" loading={loading} className="w-full">
        {isRegister ? 'Créer mon compte' : 'Se connecter'}
      </Button>
      <p className="text-center text-xs text-muted">
        {isRegister ? (
          <a href="/login" className="transition hover:text-white">Déjà un compte ? Se connecter</a>
        ) : (
          <a href="/register" className="transition hover:text-white">Pas de compte ? Créer un compte</a>
        )}
      </p>
    </form>
  );
}
