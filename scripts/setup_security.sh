#!/usr/bin/env bash
# Sécurisation VPS bot-trading — à exécuter une seule fois en tant que root (sudo bash)
set -e

echo "=== Sécurisation VPS bot-trading ==="

# 1. UFW — firewall (ports 22 SSH + 80 Dashboard uniquement)
apt-get install -y ufw
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw limit 22/tcp       # SSH avec rate-limiting anti-brute-force
ufw allow 80/tcp       # Dashboard HTTP
ufw --force enable
echo "✓ UFW activé — ports 22 et 80 ouverts"

# 2. Fail2ban — ban après 5 tentatives SSH en 10 minutes (ban 1h)
apt-get install -y fail2ban
cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
port    = ssh
EOF
systemctl enable --now fail2ban
echo "✓ Fail2ban actif"

# 3. SSH hardening
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/'               /etc/ssh/sshd_config
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#*X11Forwarding.*/X11Forwarding no/'                   /etc/ssh/sshd_config
sed -i 's/^#*MaxAuthTries.*/MaxAuthTries 3/'                      /etc/ssh/sshd_config
systemctl reload sshd
echo "✓ SSH : root login désactivé, mot de passe désactivé, max 3 tentatives"

# 4. Mises à jour de sécurité automatiques
apt-get install -y unattended-upgrades
dpkg-reconfigure --frontend=noninteractive unattended-upgrades
echo "✓ Unattended-upgrades activé"

# 5. Désactiver services inutiles
for svc in bluetooth avahi-daemon cups; do
    systemctl disable --now "$svc" 2>/dev/null && echo "  $svc désactivé" || true
done

echo ""
echo "=== Vérification ==="
ufw status
fail2ban-client status sshd
echo ""
echo "✓ Sécurisation terminée. Ports ouverts : 22 (SSH limité), 80 (Dashboard)"
