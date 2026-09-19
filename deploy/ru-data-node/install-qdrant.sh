#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-cli curl gnupg python3
systemctl enable --now docker
install -d -m 0700 /var/lib/tmod/qdrant /var/lib/tmod/qdrant-snapshots
install -d -o postgres -g postgres -m 0700 /var/backups/tmod-postgres

docker pull qdrant/qdrant:v1.16.2
docker rm -f tmod-atlas-qdrant >/dev/null 2>&1 || true
docker run -d --name tmod-atlas-qdrant --restart unless-stopped \
  -p 127.0.0.1:6333:6333 \
  -v /var/lib/tmod/qdrant:/qdrant/storage \
  -v /var/lib/tmod/qdrant-snapshots:/qdrant/snapshots \
  --security-opt no-new-privileges:true \
  qdrant/qdrant:v1.16.2

cat > /etc/systemd/system/tmod-qdrant-backup.service <<'EOF'
[Unit]
Description=T-Mod encrypted Atlas Qdrant backup
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/tmod-qdrant-backup
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/backups/tmod-postgres /var/lib/tmod/qdrant-snapshots
ReadOnlyPaths=/root/.tmod/backup-passphrase
EOF

cat > /etc/systemd/system/tmod-qdrant-backup.timer <<'EOF'
[Unit]
Description=Run T-Mod encrypted Atlas Qdrant backup every six hours

[Timer]
OnCalendar=*-*-* 01,07,13,19:35:00 UTC
Persistent=true
RandomizedDelaySec=8m
Unit=tmod-qdrant-backup.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now tmod-qdrant-backup.timer
