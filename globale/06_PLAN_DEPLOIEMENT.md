# 06 — PLAN DE DÉPLOIEMENT, DE A À Z

> Rédigé le 29 juillet 2026. Objectif : rendre Quantum Trade AI **accessible depuis le web**, en
> HTTPS, 24 h/24, avec un budget de **150 DH**. Le document couvre la recommandation et sa
> justification, la procédure complète pas à pas, la vérification, puis l'exploitation courante.
>
> Fichiers livrés avec ce plan (déjà dans le dépôt) :
> - [`infra/docker-compose.prod.yml`](../infra/docker-compose.prod.yml) — la stack de production
> - [`infra/Caddyfile`](../infra/Caddyfile) — la porte d'entrée HTTPS

---

## 1. Ce qu'il faut héberger (et pourquoi ça oriente tout le reste)

Cette application n'est pas un site : c'est un **robot qui tourne en continu**. Trois boucles de
fond ne s'arrêtent jamais (`app/services/scheduler.py`) :

| Boucle | Cadence | Ce qu'elle fait |
|---|---|---|
| Balayage stratégie | ~240 s | recalcule le playbook sur tout l'univers et publie l'instantané |
| Auto-entrée | 60 s | ouvre les positions démo dès qu'un déclencheur 15 min se forme |
| Suivi des positions | continu | sécurisation +2R, gestion TP1→TP2, clôture au stop/objectif |
| Entraînement | 1×/nuit (19h UTC) | walk-forward, carte de l'edge, verdicts par paire |

**Conséquence directe sur le choix d'hébergement :** tout ce qui « s'endort quand personne ne
visite » est éliminé d'office. Un plan gratuit Render, un dyno qui hiberne, une fonction serverless
(Vercel, Netlify, Lambda) ne peuvent pas héberger ce backend : la boucle d'auto-entrée s'arrêterait,
et un déclencheur 15 min manqué est un trade manqué. Il faut **un serveur qui reste allumé**.

Services à faire tourner : `postgres` (TimescaleDB) · `redis` · `backend` (FastAPI) ·
`frontend` (Next.js) · `caddy` (HTTPS) · `db-backup` (pg_dump quotidien).

**Un service a été retiré de la production : `redpanda`.** Vérification faite dans le code, aucun
consommateur Kafka n'existe — seule la variable `kafka_bootstrap_servers` est déclarée, sans un
seul appel. Le conteneur réservait ~1 Go de RAM pour ne rien faire, soit un quart d'un VPS à 4 Go.

---

## 2. La recommandation — et pourquoi celle-là

### Recommandé : **VPS Hetzner CX22 + domaine + Caddy**, ≈ **60 DH/mois**

| Poste | Détail | Coût |
|---|---|---|
| VPS Hetzner **CX22** | 2 vCPU x86, **4 Go RAM**, 40 Go SSD, 20 To de trafic, Nuremberg/Helsinki | 4,59 €/mois ≈ **50 DH** |
| Nom de domaine `.com` | Cloudflare Registrar ou Namecheap (~11 €/an) | ≈ **10 DH/mois** |
| Certificat HTTPS | Let's Encrypt via Caddy, renouvelé automatiquement | **0 DH** |
| Données de marché | Yahoo (sans clé), FRED et Finnhub en offre gratuite | **0 DH** |
| **Total** | | **≈ 60 DH/mois** |

Il reste donc **~90 DH de marge** sur les 150 DH — assez pour l'option sauvegarde de l'hébergeur
(+20 %, ≈ 10 DH) et un petit budget de clés LLM pour le copilote, sans jamais toucher au plafond.

### Pourquoi ce choix plutôt qu'un autre

| Option | Coût/mois | Pourquoi elle n'est pas retenue |
|---|---|---|
| **Hetzner CX22** ✅ | ≈ 50 DH | **Retenue** : le moins cher qui tienne 4 Go, x86 (aucun risque d'image Docker incompatible), facturation à l'heure — on peut arrêter du jour au lendemain |
| Oracle Cloud « Always Free » | 0 DH | Gratuit à vie **sur le papier** : 4 cœurs ARM / 24 Go. En pratique, « Out of capacity » bloque souvent la création, les instances inactives peuvent être récupérées, et il n'y a aucun SLA. Excellent **plan B**, mauvais socle pour un robot qui doit tourner sans surveillance |
| Hetzner CAX11 (ARM) | ≈ 42 DH | 8 DH moins cher, mais architecture ARM : chaque image (TimescaleDB en tête) doit exister en arm64, et le build Next.js y est plus lent. Économiser 8 DH ne vaut pas ce risque |
| Contabo VPS | ≈ 60 DH | Plus de RAM au même prix, mais performances CPU irrégulières et provisionnement parfois long. **Plan C** si le paiement Hetzner échoue (Contabo accepte PayPal) |
| Railway / Render / Fly.io (managé) | 200-350 DH | Dépasse le budget dès qu'il faut 2 services + une base **qui ne dorment pas**. Le confort managé se paie 3 à 5× le prix d'un VPS |
| Vercel + backend managé | > 200 DH | Vercel héberge très bien Next.js, mais **rien** du backend : pas de processus long, pas de WebSocket persistant, pas de scheduler |
| Hébergement mutualisé marocain | ≈ 30-50 DH | Pas de Docker, pas de processus permanent, pas de WebSocket. Techniquement hors-jeu |

**En une phrase :** le budget de 150 DH est largement suffisant pour un VPS, et un VPS est le seul
format qui accepte un robot permanent ; le seul vrai arbitrage est donc entre *gratuit mais
incertain* (Oracle) et *50 DH mais garanti* (Hetzner) — à 50 DH, on achète la garantie.

### Note de paiement depuis le Maroc

Hetzner demande une carte bancaire internationale (ou PayPal) et vérifie parfois l'identité d'un
nouveau compte sous 24-48 h. Si la carte est refusée : **Contabo** (PayPal accepté) puis **Oracle
Always Free** sont les replis, dans cet ordre. La procédure ci-dessous est identique sur les trois —
seul le fournisseur du VPS change, tout le reste est du Docker.

---

## 3. Prérequis (à préparer avant de commencer — 15 min)

- [ ] Une carte bancaire internationale ou un compte PayPal
- [ ] Un compte GitHub (le dépôt sera **privé** — il contiendra la stratégie)
- [ ] Une clé SSH. Sur Windows, dans PowerShell : `ssh-keygen -t ed25519 -C "quantum-trade"`
      → la clé publique est dans `C:\Users\Hp\.ssh\id_ed25519.pub`
- [ ] Le fichier `.env` local (il contient déjà les clés utiles), **jamais** commité

Durée totale du déploiement : **~1 h**, dont ~15 min d'attente (DNS, build).

---

## 4. Procédure, étape par étape

### Étape 1 — Pousser le code sur un dépôt GitHub **privé** (10 min)

```powershell
cd C:\Users\Hp\Desktop\tradingIA
git status                      # vérifier qu'aucun .env n'est suivi
git add -A
git commit -m "Préparation du déploiement"
git push -u origin feat/playbook-strategy
```

> Le dépôt doit être **privé**. Il contient la stratégie complète, les seuils calibrés et la carte
> de l'edge : c'est la totalité de la valeur du projet.

### Étape 2 — Créer le VPS (10 min)

1. Créer un compte sur **console.hetzner.cloud** → nouveau projet `quantum-trade`.
2. **Add Server** :
   - Location : **Nuremberg** ou **Falkenstein** (Allemagne — ~40 ms depuis le Maroc)
   - Image : **Ubuntu 24.04**
   - Type : **Shared vCPU → x86 (Intel/AMD) → CX22**
   - Volume/Firewall : rien pour l'instant
   - **SSH keys** : coller le contenu de `id_ed25519.pub`
   - Name : `quantum-trade`
3. **Create & Buy now**. Noter l'**IPv4** affichée → on l'appellera `IP_VPS`.

### Étape 3 — Pointer le domaine (5 min + propagation)

Chez le registrar (Cloudflare Registrar recommandé, prix coûtant), créer **un** enregistrement :

| Type | Nom | Valeur | Proxy |
|---|---|---|---|
| A | `trade` (ou `@`) | `IP_VPS` | **désactivé** (DNS only) |

> Le proxy Cloudflare doit rester **désactivé** au premier démarrage : Let's Encrypt doit joindre
> le serveur directement pour valider le domaine. Il pourra être réactivé ensuite.

Vérifier la propagation avant de continuer : `nslookup trade.mondomaine.com` doit répondre `IP_VPS`.

### Étape 4 — Sécuriser le serveur (10 min)

```bash
ssh root@IP_VPS

# 1. Mises à jour + utilisateur non-root
apt update && apt upgrade -y
adduser --disabled-password --gecos "" quantum
usermod -aG sudo quantum
rsync --archive --chown=quantum:quantum ~/.ssh /home/quantum

# 2. Pare-feu : SSH + HTTP + HTTPS, rien d'autre.
#    Postgres, Redis et l'API ne sont JAMAIS joignables depuis l'extérieur — ils ne parlent qu'au
#    réseau Docker interne. C'est la raison pour laquelle docker-compose.prod.yml ne publie aucun
#    port applicatif : un port ouvert est une porte, même quand on ne s'en sert pas.
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable

# 3. SSH par clé uniquement
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl restart ssh

# 4. Redémarrages de sécurité automatiques
apt install -y unattended-upgrades && dpkg-reconfigure -plow unattended-upgrades
```

### Étape 5 — Installer Docker (5 min)

```bash
ssh quantum@IP_VPS
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker quantum

# 2 Go de swap : le build Next.js dépasse ponctuellement les 4 Go de RAM. Sans swap, il se fait
# tuer par le noyau (OOM) au milieu du build, avec un message peu explicite.
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

exit   # puis se reconnecter pour que le groupe docker s'applique
```

### Étape 6 — Récupérer le projet (5 min)

```bash
ssh quantum@IP_VPS
# Jeton GitHub : Settings → Developer settings → Personal access tokens (scope `repo`)
git clone https://github.com/TON_USER/tradingIA.git
cd tradingIA
git checkout feat/playbook-strategy
```

### Étape 7 — Le fichier `.env` de production (10 min)

Depuis le PC :
```powershell
scp C:\Users\Hp\Desktop\tradingIA\.env quantum@IP_VPS:~/tradingIA/.env
```

Puis sur le VPS, `nano ~/tradingIA/.env` et **remplacer** ces lignes :

```ini
ENVIRONMENT=prod
# Secret de signature des jetons. À GÉNÉRER (openssl rand -hex 32) : le laisser à sa valeur
# d'exemple revient à publier la clé qui signe les sessions de tous les comptes.
SECRET_KEY=<coller le résultat de: openssl rand -hex 32>
ACCESS_TOKEN_EXPIRE_MINUTES=60

# Mot de passe Postgres — à changer aussi (openssl rand -hex 16). `quantum_dev_pwd` est public.
POSTGRES_PASSWORD=<coller le résultat de: openssl rand -hex 16>

USE_IN_MEMORY_DB=false

# Le domaine, utilisé à la fois par Caddy (certificat), le backend (CORS) et le build du frontend.
DOMAIN=trade.mondomaine.com
ACME_EMAIL=iliassnacer03@gmail.com
```

> ⚠️ Ne pas définir `DATABASE_URL` à la main : `docker-compose.prod.yml` la construit à partir de
> `POSTGRES_PASSWORD`. Une valeur codée en dur dans le `.env` prendrait le pas et pointerait sur
> l'ancien mot de passe.

### Étape 8 — Lancer (15 min, dont ~10 de build)

```bash
cd ~/tradingIA
docker compose --env-file .env -f infra/docker-compose.prod.yml up -d --build
docker compose -f infra/docker-compose.prod.yml ps      # tout doit être "running"/"healthy"
docker compose -f infra/docker-compose.prod.yml logs -f caddy   # doit annoncer le certificat obtenu
```

### Étape 9 — Créer le premier compte

Ouvrir **https://trade.mondomaine.com** → **S'inscrire**. Le premier compte créé est le tien.
L'auto-entrée en démo démarre toute seule (`playbook_auto_entry_enabled` est actif par défaut) et
n'engage **aucun argent réel** : le code refuse toute connexion broker autre que `paper`.

---

## 5. Vérification — la liste à cocher avant de considérer que c'est en ligne

| # | Vérification | Commande / geste | Attendu |
|---|---|---|---|
| 1 | HTTPS valide | ouvrir l'URL | cadenas fermé, pas d'avertissement |
| 2 | API vivante | `curl -s https://DOMAIN/health` | `{"status":"ok",...}` |
| 3 | Postgres actif | `docker compose -f infra/docker-compose.prod.yml exec backend env \| grep USE_IN_MEMORY` | `false` |
| 4 | Temps réel | ouvrir « Trades du jour » | les prix bougent sans rafraîchir la page |
| 5 | Données **réelles** | page d'un symbole | badge « données réelles », pas « démo » |
| 6 | Rien d'exposé | `nmap -Pn IP_VPS` depuis le PC | seuls 22, 80, 443 ouverts |
| 7 | Sauvegarde | `docker compose -f infra/docker-compose.prod.yml logs db-backup` | un dump créé |
| 8 | Survit au reboot | `sudo reboot`, attendre 2 min | le site répond seul |
| 9 | Métaux exclus | page « Trades du jour » | aucun XAU/XAG dans la liste |
| 10 | Univers complet | même page | « ~84 symbole(s) passés à la stratégie », plus 20 |

Tant que le point **8** n'est pas vérifié, le déploiement n'est pas terminé : un robot qui ne se
relève pas après un redémarrage de l'hébergeur s'arrêtera un jour, sans prévenir.

---

## 6. Exploitation courante

```bash
cd ~/tradingIA
C=" -f infra/docker-compose.prod.yml"

# Mettre à jour (après un git push depuis le PC)
git pull && docker compose --env-file .env $C up -d --build

# Journaux
docker compose $C logs backend --tail 100 -f
docker compose $C logs backend | grep -i "auto-entrée"     # ce que le robot a fait, et pourquoi pas

# Sauvegardes (rétention 14 jours, volume séparé de la base)
docker compose $C exec db-backup ls -lh /backups
# Restauration : voir docs/BACKUPS.md

# Ressources (surveiller la RAM les premiers jours)
docker stats --no-stream
```

**Rituel conseillé :** une fois par semaine, vérifier `docker stats` (RAM sous 3,5 Go), la présence
d'un dump récent, et la page « Journal » (le taux de réussite mesuré, pas ressenti).

**Si la RAM sature** (le symptôme est un conteneur `backend` redémarré tout seul) : passer au
**CX32** (8 Go, ≈ 90 DH/mois — toujours dans le budget) depuis la console Hetzner, redimensionnement
à chaud en 2 minutes, sans réinstallation.

---

## 7. Ce que ce plan ne couvre pas — dit franchement

- **Aucun argent réel.** Le déploiement met en ligne du *paper trading*. Passer en réel demande un
  courtier, un KYC, et une décision qui ne se prend pas sur un backtest.
- **Pas de haute disponibilité.** Un seul VPS : si Hetzner tombe, la plateforme tombe. C'est un
  compromis assumé à 60 DH/mois — le remède (deux machines + bascule) coûte le double.
- **Pas de sauvegarde hors-site.** Les dumps vivent sur le même serveur, dans un volume séparé.
  Perdre le VPS, c'est perdre les dumps. Correctif à ~0 DH quand ce sera utile : `rclone` vers un
  stockage objet, ou l'option snapshot Hetzner (+20 %, ≈ 10 DH/mois).
- **Pas de revue juridique.** Ouvrir l'accès à des tiers (même gratuitement) fait entrer des
  obligations d'information financière. Tant que le compte est personnel, la question ne se pose pas.

---

## 8. Récapitulatif du budget

| Poste | Mensuel | Annuel |
|---|---|---|
| VPS Hetzner CX22 | 50 DH | 600 DH |
| Domaine `.com` | 10 DH | 120 DH |
| HTTPS, données de marché, Docker | 0 DH | 0 DH |
| **Engagé** | **60 DH** | **720 DH** |
| *Marge disponible sur 150 DH* | *90 DH* | *1 080 DH* |

*Taux utilisé : 1 € ≈ 11 DH. Les prix Hetzner sont TTC.*

Options finançables par la marge, dans l'ordre d'utilité :
1. **Snapshots Hetzner** (+20 %, ≈ 10 DH/mois) — la seule protection contre la perte du serveur ;
2. **CX32** (8 Go, ≈ 90 DH/mois) si la RAM devient juste ;
3. **Clés LLM** (Anthropic/Google) pour le copilote — facultatif : sans clé, le copilote se
   désactive proprement, la stratégie et l'auto-entrée n'en dépendent pas.
