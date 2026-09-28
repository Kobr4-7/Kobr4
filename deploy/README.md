# Mise en ligne sur un serveur

Résultat : le site Kobr4 FX accessible en HTTPS sur ton domaine, depuis ordinateur et téléphone, avec les bots qui tournent 24h/24 et redémarrent seuls.

## Ce qu'il te faut

| Élément | Recommandation |
|---|---|
| Serveur (VPS) | Linux Ubuntu 24.04, 2 vCPU, 4 Go de RAM, 40 Go de disque, en Europe. Ex. Hetzner, OVHcloud, Vultr, DigitalOcean. 10 à 25 €/mois. |
| Nom de domaine | Un domaine ou sous-domaine (ex. `trading.tondomaine.fr`) avec un enregistrement DNS **A** vers l'adresse IP du serveur. |
| Accès | Connexion SSH par clé (pas par mot de passe). |

## 1. Préparer le serveur

```bash
ssh root@IP_DU_SERVEUR
adduser kobr4 && usermod -aG sudo kobr4
# Pare-feu : SSH, HTTP, HTTPS uniquement
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
# Docker
curl -fsSL https://get.docker.com | sh
usermod -aG docker kobr4
# Mises à jour de sécurité automatiques
apt install -y unattended-upgrades && dpkg-reconfigure -plow unattended-upgrades
```

Dans `/etc/ssh/sshd_config`, mets `PasswordAuthentication no` et `PermitRootLogin no`, puis `systemctl restart ssh` (après avoir vérifié que ta clé fonctionne avec l'utilisateur `kobr4`).

## 2. Installer Kobr4

```bash
su - kobr4
git clone https://github.com/Kobr4-7/Kobr4.git && cd Kobr4
cp deploy/.env.example deploy/.env
```

Remplis `deploy/.env` :

```bash
openssl rand -base64 32          # → POSTGRES_PASSWORD
docker compose --env-file deploy/.env run --rm app kobr4 server gen-key   # → KOBR4_MASTER_KEY
openssl rand -hex 16             # → KOBR4_INVITE_CODE (si d'autres personnes s'inscriront)
```

**Sauvegarde `KOBR4_MASTER_KEY` hors du serveur** (gestionnaire de mots de passe). Sans elle, les jetons courtier enregistrés sont illisibles.

## 3. Démarrer

```bash
docker compose --env-file deploy/.env up -d --build
docker compose --env-file deploy/.env logs -f app     # Ctrl+C pour quitter
```

Ouvre `https://ton-domaine` : le certificat HTTPS est obtenu automatiquement en quelques secondes. Crée ton compte (le premier compte est administrateur), puis suis le parcours d'accueil : double authentification, compte démo (clé gratuite [Finnhub](https://finnhub.io/register) pour les prix en direct), alertes Telegram, premier bot.

## 4. Historique pour les backtests et le laboratoire

```bash
docker compose --env-file deploy/.env exec app \
  kobr4 data download --symbols EUR/USD,GBP/USD,USD/JPY,USD/CHF,AUD/USD,USD/CAD,EUR/GBP --start 2019-01-01
docker compose --env-file deploy/.env exec app kobr4 data info
```

Le téléchargement reprend là où il s'est arrêté si on le relance.

## 5. Supervision (facultatif)

```bash
mkdir -p deploy/secrets && grep KOBR4_METRICS_TOKEN deploy/.env | cut -d= -f2 > deploy/secrets/metrics_token
docker compose --env-file deploy/.env --profile monitoring up -d
ssh -L 3000:127.0.0.1:3000 kobr4@IP_DU_SERVEUR   # puis http://localhost:3000 (Grafana)
```

Grafana n'est accessible que par tunnel SSH. Ajoute la source de données Prometheus `http://prometheus:9090`.

## Mise à jour automatique et depuis le site

Le script `deploy/auto-update.sh` installe les nouveaux commits de la branche (toutes les 5 minutes) et traite les demandes du bouton « Mettre à jour » des réglages du site (dans la minute). Pour le lancer chaque minute (en tant que `kobr4`) :

```bash
(crontab -l 2>/dev/null | grep -v auto-update.sh; echo "* * * * * $HOME/Kobr4/deploy/auto-update.sh >> $HOME/auto-update.log 2>&1") | crontab -
```

Mise à jour immédiate à la main : `~/Kobr4/deploy/auto-update.sh`. Journal : `tail -f ~/auto-update.log`. Les bots sont arrêtés proprement puis relancés à chaque mise à jour.

## Mettre à jour à la main

```bash
cd ~/Kobr4 && git pull
docker compose --env-file deploy/.env up -d --build
```

Les migrations de la base s'appliquent au démarrage. Les bots en marche sont arrêtés proprement (leurs positions gardent leurs stops chez le courtier) puis relancés automatiquement.

## Sauvegardes

Le service `backup` fait un export complet de la base chaque jour dans `./backups` (30 jours gardés). Copie ce dossier ailleurs régulièrement, par exemple :

```bash
rsync -a kobr4@IP_DU_SERVEUR:Kobr4/backups/ ~/sauvegardes-kobr4/
```

Restaurer :

```bash
docker compose --env-file deploy/.env stop app
docker compose --env-file deploy/.env exec -T db pg_restore -U kobr4 -d kobr4 --clean < backups/kobr4-AAAAMMJJ-HHMMSS.dump
docker compose --env-file deploy/.env start app
```
