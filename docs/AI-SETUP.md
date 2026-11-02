# Let an AI assistant install it

Using Claude Code or Cursor? Paste the block below and it installs everything for you.

````text
Deploy OhmyTesla (a self-hosted Tesla companion) on this machine and set up phone push.

Prerequisite: Docker Desktop must be running. If it isn't, tell me to start it first.

1. In the repo root, run `cp .env.example .env`
2. Generate and write these three — never leave them empty, never use the sample values:
   - TM_ENCRYPTION_KEY  = `openssl rand -base64 32`
   - TM_DB_PASS         = `openssl rand -base64 18 | tr -d '/+='`
   - OHMYTESLA_PASSWORD = `openssl rand -base64 32 | tr -d '/+=' | cut -c1-28`
     (portal password; the username is always `ohmytesla`. Empty means the portal refuses to serve.)
3. If a port is taken, change OHMYTESLA_PORT / GRAFANA_PORT / TESLAMATE_PORT in .env
4. Run `./omt up`. It returns only once the app is actually reachable.
5. Generate an ntfy topic: `echo "omt-$(openssl rand -hex 10)"`
   Write it to .env as NTFY_TOPIC, together with NTFY_URL=https://ntfy.sh
6. Put this machine's LAN IP into PUBLIC_URL as http://<IP>:8800
   (macOS: `ipconfig getifaddr en0`; Linux: `hostname -I | awk '{print $1}'`)
7. Run `./omt restart` (reloads .env, does not rebuild the image)
8. Verify. With the password set, -u is mandatory or every request is 401:
   `curl -s -u ohmytesla:<password> -X POST http://localhost:8800/api/alerts/test`
   expect {"ok":true,"channels":["ntfy"]}
   Then confirm it truly landed — the API return alone is not proof:
   `curl -s "https://ntfy.sh/<topic>/json?poll=1&since=5m"`
9. Tell me the topic name and the portal password, and remind me to install the
   ntfy app, tap +, and enter that topic. If I use an iPhone, also suggest switching
   to Bark later (step 2 of the setup guide); its pushes show the OhmyTesla icon.
10. Tell me to open http://localhost:8800 (the browser will ask for ohmytesla plus the
    password) and paste the Tesla tokens into the setup page. Get them with tesla_auth
    (github.com/adriankumpf/tesla_auth, on a computer) or Auth app for Tesla (iPhone) —
    Tesla rejects username/password login for third parties.
11. Run `./omt status` and confirm all five containers are up.

Optional email archive: ask me for SMTP details. Gmail requires an app password
(https://myaccount.google.com/apppasswords, 2FA first), then write
SMTP_HOST/PORT/USER/PASS/FROM/TO and run `./omt restart`. Setting SMTP_HOST without a
password makes the app report "half configured" rather than pretend to work.

Notes:
- Changed .env → `./omt restart`. Changed code → `./omt update` (rebuilds).
- The ntfy topic is a password. Never put it anywhere public; .env is already gitignored.
- OHMYTESLA_PASSWORD only guards :8800. Grafana (3000) and TeslaMate (4000) are separate
  containers with their own auth — securing one does not secure the others.
````
