# Polymarket CopyTrade Live — container minimal
# Tourne via le réseau du container vpn (bubuntux/nordlynx)
FROM python:3.12-slim

# Outils utiles + DNS resolution propre + ssh client (encore utile pour fetch initial)
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps Python d'abord (cache layer)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Code du bot
COPY copytrade_live/ /app/copytrade_live/

# Container Docker = déjà isolé. On run en root pour éviter les
# headaches de volume perms avec les named volumes Docker.
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "copytrade_live.poller"]
