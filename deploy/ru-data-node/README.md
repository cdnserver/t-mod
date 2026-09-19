# T-Mod RU data node

This deployment keeps the primary T-Mod PostgreSQL database on a Russian VDS.
PostgreSQL listens only on loopback and is reached through an authenticated SSH
tunnel. The public network never exposes port 5432.

The bootstrap installs PostgreSQL, SCRAM authentication, encrypted scheduled
backups, a restrictive nftables policy, Fail2ban and unattended security
updates. The application endpoint and pool are configured through
`TMOD_POSTGRES_*` environment variables.

Technical localization is only one part of 152-FZ compliance. Provider
documents confirming Russian placement, the operator notification, access
matrix, incident process, retention rules and the legal basis for any
cross-border AI processing must be maintained separately.
