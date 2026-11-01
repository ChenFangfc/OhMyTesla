# AI access (MCP)

Ask *"how much did charging cost last month?"* or *"why did efficiency drop?"* and let the model
query the data itself:

```bash
claude mcp add --transport http ohmytesla http://localhost:8800/mcp \
  --header "Authorization: Basic $(printf 'ohmytesla:%s' \
    "$(grep '^OHMYTESLA_PASSWORD=' .env | cut -d= -f2-)" | base64)"
```

15 read-only tools — trips, charge curves, battery health, vampire drain, TPMS, alerts, sentry
sessions — plus a guarded `run_sql` with four independent layers:

| # | Guard |
|---|---|
| 1 | `SELECT` / `WITH` only; no write keywords, no comments |
| 2 | Runs as a dedicated **non-superuser role** (`omt_mcp_readonly`) |
| 3 | Read-only transaction, always rolled back |
| 4 | 200 rows and 8 seconds, enforced by a server-side cursor |

Layer 2 matters most: compose hands the hub TeslaMate's own account, which *is* a superuser.
Without the downgrade, `pg_read_file()` reads any file in the container and `private.tokens` is
readable. Both are `permission denied` now.

Nothing leaves your machine — which commercial apps cannot offer, since your data lives on their
servers.
