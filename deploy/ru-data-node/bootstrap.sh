#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl fail2ban gnupg nftables postgresql postgresql-client \
  unattended-upgrades

install -d -m 0700 /root/.tmod
if [[ ! -s /root/.tmod/postgres-password ]]; then
  openssl rand -hex 32 > /root/.tmod/postgres-password
  chmod 0600 /root/.tmod/postgres-password
fi
if [[ ! -s /root/.tmod/backup-passphrase ]]; then
  openssl rand -base64 48 > /root/.tmod/backup-passphrase
  chmod 0600 /root/.tmod/backup-passphrase
fi

pg_version="$(pg_config --version | awk '{print $2}' | cut -d. -f1)"
pg_conf="/etc/postgresql/${pg_version}/main"
if [[ ! -d ${pg_conf} ]]; then
  echo "PostgreSQL configuration directory is missing: ${pg_conf}" >&2
  exit 1
fi

cat > "${pg_conf}/conf.d/90-tmod.conf" <<'EOF'
listen_addresses = '127.0.0.1'
port = 5432
password_encryption = 'scram-sha-256'
ssl = on
max_connections = 120
shared_buffers = '2GB'
effective_cache_size = '5GB'
maintenance_work_mem = '384MB'
work_mem = '8MB'
wal_compression = on
checkpoint_completion_target = 0.9
random_page_cost = 1.1
effective_io_concurrency = 200
idle_in_transaction_session_timeout = '60s'
statement_timeout = '60s'
log_connections = on
log_disconnections = on
log_lock_waits = on
log_min_duration_statement = 1000
log_statement = 'ddl'
EOF

# SSH forwarding arrives from loopback. No PostgreSQL socket is exposed on the
# public interface; all TCP clients must still authenticate with SCRAM.
if ! grep -Fq 'tmod-loopback' "${pg_conf}/pg_hba.conf"; then
  cat >> "${pg_conf}/pg_hba.conf" <<'EOF'

# tmod-loopback: encrypted SSH tunnel endpoint
hostssl all all 127.0.0.1/32 scram-sha-256
hostssl all all ::1/128 scram-sha-256
EOF
fi

systemctl restart postgresql
db_password="$(cat /root/.tmod/postgres-password)"
runuser -u postgres -- psql --set=ON_ERROR_STOP=1 --set=db_password="${db_password}" <<'SQL'
DO $block$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tmod') THEN
    CREATE ROLE tmod LOGIN CREATEDB;
  END IF;
END
$block$;
SELECT format('ALTER ROLE tmod PASSWORD %L', :'db_password') \gexec
SELECT 'CREATE DATABASE tmod OWNER tmod'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'tmod') \gexec
SELECT 'CREATE DATABASE tmod_global_log OWNER tmod'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'tmod_global_log') \gexec
SQL

install -d -o postgres -g postgres -m 0700 /var/backups/tmod-postgres
cat > /usr/local/sbin/tmod-postgres-backup <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
backup_dir=/var/backups/tmod-postgres
passphrase=/root/.tmod/backup-passphrase
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
temporary="${backup_dir}/.tmod-${stamp}.dump.gpg.tmp"
target="${backup_dir}/tmod-${stamp}.dump.gpg"
trap 'rm -f "${temporary}"' EXIT
runuser -u postgres -- pg_dump --format=custom --compress=6 --dbname=tmod \
  | gpg --batch --yes --pinentry-mode loopback --passphrase-file "${passphrase}" \
      --symmetric --cipher-algo AES256 --output "${temporary}"
gpg --batch --quiet --pinentry-mode loopback --passphrase-file "${passphrase}" \
  --decrypt "${temporary}" | pg_restore --list >/dev/null
chown postgres:postgres "${temporary}"
chmod 0600 "${temporary}"
mv "${temporary}" "${target}"
sha256sum "${target}" > "${target}.sha256"
chown postgres:postgres "${target}.sha256"
find "${backup_dir}" -type f -name 'tmod-*.dump.gpg' -mtime +14 -delete
find "${backup_dir}" -type f -name 'tmod-*.dump.gpg.sha256' -mtime +14 -delete
EOF
chmod 0750 /usr/local/sbin/tmod-postgres-backup

cat > /etc/systemd/system/tmod-postgres-backup.service <<'EOF'
[Unit]
Description=T-Mod encrypted PostgreSQL backup
After=postgresql.service
Requires=postgresql.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/tmod-postgres-backup
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/backups/tmod-postgres
ReadOnlyPaths=/root/.tmod/backup-passphrase
EOF

cat > /etc/systemd/system/tmod-postgres-backup.timer <<'EOF'
[Unit]
Description=Run T-Mod PostgreSQL backup every six hours

[Timer]
OnCalendar=*-*-* 00,06,12,18:17:00 UTC
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now tmod-postgres-backup.timer

cat > /etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    iifname "lo" accept
    ct state established,related accept
    ct state invalid drop
    tcp dport 22 ct state new limit rate 30/minute accept
    ip protocol icmp accept
    ip6 nexthdr ipv6-icmp accept
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output { type filter hook output priority 0; policy accept; }
}
EOF
nft -c -f /etc/nftables.conf
systemctl enable --now nftables

cat > /etc/fail2ban/jail.d/tmod-sshd.conf <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

systemctl is-active --quiet postgresql
runuser -u postgres -- psql -Atqc "SELECT current_setting('listen_addresses'), current_setting('ssl')"
ss -lntp | grep -F '127.0.0.1:5432'
echo "RU data node is ready"
