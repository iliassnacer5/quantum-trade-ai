import {
  Sun,
  LayoutDashboard,
  Radar,
  Star,
  Map,
  Layers,
  History,
  FlaskConical,
  ClipboardList,
  PlayCircle,
  Wallet,
  BookOpen,
  MessageSquare,
  Bot,
  Store,
  Users,
  Palette,
  CreditCard,
  Settings,
  type LucideIcon,
} from 'lucide-react';

export type NavItem = { href: string; label: string; icon: LucideIcon; accent?: boolean };
export type NavGroup = { title: string; items: NavItem[] };

/**
 * Sidebar groupée par intention (remplace la navbar 18 liens).
 * Toutes les routes existantes restent accessibles — aucune régression fonctionnelle.
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    title: 'Trader',
    items: [
      { href: '/today', label: 'Ma journée', icon: Sun, accent: true },
      { href: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
      { href: '/scanner', label: 'Scanner', icon: Radar },
      { href: '/daily', label: 'Trades du jour', icon: Star, accent: true },
    ],
  },
  {
    title: 'Prouver',
    items: [
      { href: '/edge', label: 'Edge', icon: Map },
      { href: '/strategies', label: 'Stratégies', icon: Layers },
      { href: '/backtest', label: 'Backtest', icon: FlaskConical },
      { href: '/backtest/rapport', label: 'Rapport de backtest', icon: ClipboardList },
      { href: '/track-record', label: 'Track record', icon: History },
    ],
  },
  {
    title: 'Exécuter',
    items: [
      { href: '/execution', label: 'Paper trading', icon: PlayCircle },
      { href: '/wallet', label: 'Portefeuille', icon: Wallet },
      { href: '/journal', label: 'Journal', icon: BookOpen },
    ],
  },
  {
    title: 'Plus',
    items: [
      { href: '/copilot', label: 'Copilot', icon: MessageSquare },
      { href: '/agents', label: 'Agents', icon: Bot },
      { href: '/marketplace', label: 'Marketplace', icon: Store },
      { href: '/copytrading', label: 'Copy trading', icon: Users },
      { href: '/branding', label: 'White-label', icon: Palette },
      { href: '/plans', label: 'Plans', icon: CreditCard },
      { href: '/settings', label: 'Réglages', icon: Settings },
    ],
  },
];

/** Pages « nues » : rendues sans le shell (landing + parcours d'auth). */
export const BARE_ROUTES = ['/', '/login', '/register', '/onboarding'];

/** Sélection mobile prioritaire pour la bottom-nav (5 entrées). */
export const MOBILE_PRIMARY = ['/today', '/dashboard', '/scanner', '/edge', '/wallet'];
