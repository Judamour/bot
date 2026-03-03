#!/bin/bash
# Script de setup serveur Ubuntu 22.04
# À exécuter en root sur le VPS fraîchement créé

set -e
echo "=== Setup Bot Trading Server ==="

# 1. Mise à jour système
apt update && apt upgrade -y

# 2. Dépendances
apt install -y python3.11 python3.11-venv python3-pip nginx apache2-utils git

# 3. Utilisateur dédié (sécurité)
useradd -m -s /bin/bash botuser

# 4. Cloner le repo (remplacer l'URL)
cd /home/botuser
git clone https://github.com/Judamour/bot.git bot-trading
chown -R botuser:botuser bot-trading

# 5. Venv + dépendances
cd bot-trading
su botuser -c "python3.11 -m venv venv"
su botuser -c "venv/bin/pip install -r requirements.txt"

# 6. Fichier .env
echo "⚠ Créer le fichier .env :"
echo "  nano /home/botuser/bot-trading/.env"

# 7. Dossier logs
su botuser -c "mkdir -p /home/botuser/bot-trading/logs"

# 8. Services systemd
cp deploy/bot.service /etc/systemd/system/
cp deploy/dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable bot dashboard
systemctl start bot dashboard

# 9. Nginx
echo "⚠ Créer le mot de passe dashboard :"
echo "  htpasswd -c /etc/nginx/.htpasswd admin"
cp deploy/nginx.conf /etc/nginx/sites-available/bot-trading
ln -sf /etc/nginx/sites-available/bot-trading /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

# 10. Firewall
ufw allow 22
ufw allow 80
ufw --force enable

echo ""
echo "=== Setup terminé ==="
echo "Dashboard : http://$(curl -s ifconfig.me)"
