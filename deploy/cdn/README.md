# T-Mod CDN node

This directory is a production-ready, stateless edge for public T-Mod assets.
It deliberately stores no accounts, sessions, databases, private documents or
application secrets. The application/API remains the source of truth; the CDN
may be removed and rebuilt without losing user data.

## Server prerequisites

- Debian 12 or newer;
- Docker Engine with Compose v2;
- public TCP ports 80 and 443 plus UDP 443;
- an `A`/`AAAA` record for `cdn.tvr.lat` pointing directly to the node.

## First deployment

```sh
sudo install -d -m 0755 /opt/tmod-cdn /srv/tmod-cdn
sudo cp -a deploy/cdn/. /opt/tmod-cdn/
cd /opt/tmod-cdn
sudo cp .env.example .env
sudo editor .env
sudo ./deploy.sh
```

The first TLS certificate is issued automatically after DNS and ports are
ready. Verify `https://cdn.tvr.lat/cdn-health` returns `ok`.

## Publishing assets

Stage a complete public asset tree and publish it under a unique release id:

```sh
sudo /opt/tmod-cdn/publish.sh /tmp/tmod-public-assets 2026.10.02-1
```

Publishing is atomic: clients see either the prior release or the complete new
release. Files under `/releases/` and `/immutable/` receive a one-year immutable
cache; other files use a five-minute cache. Never upload personal data, private
documents, account exports, environment files or database backups.

## Update and rollback

Copy the newer deployment directory over `/opt/tmod-cdn` and run
`sudo ./deploy.sh`. To roll back content, atomically repoint
`/srv/tmod-cdn/content/current` to an earlier directory in `releases/` and
check the health endpoint. Caddy logs are rotated in `/srv/tmod-cdn/logs`.

