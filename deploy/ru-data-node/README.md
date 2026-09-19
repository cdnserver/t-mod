# T-Mod RU data node

This deployment keeps the primary T-Mod PostgreSQL databases and Atlas Qdrant
index on a Russian VDS. Both services listen only on loopback and are reached
through authenticated SSH forwarding. The public network never exposes ports
5432 or 6333.

The bootstrap installs PostgreSQL, SCRAM authentication, encrypted scheduled
backups, a restrictive nftables policy, Fail2ban and unattended security
updates. The application endpoint and pool are configured through
`TMOD_POSTGRES_*` environment variables.

`install-qdrant.sh` provisions the pinned Qdrant image and its encrypted
six-hour snapshot timer. `tmod-postgres-tunnel.ps1` forwards PostgreSQL and
Qdrant together, so a single supervised tunnel controls the private data plane.

Technical localization is only one part of 152-FZ compliance. Provider
documents confirming Russian placement, the operator notification, access
matrix, incident process, retention rules and the legal basis for any
cross-border AI processing must be maintained separately.
