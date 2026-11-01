# teslausb → OhmyTesla: automatic dashcam archiving

[teslausb](https://github.com/marcone/teslausb) (marcone's maintained fork) is a Raspberry Pi that
takes the place of the USB drive in the car. When the car gets home and the Pi can reach the
server, it copies new dashcam clips to a Samba share. OhmyTesla reads that share as its TeslaCam
folder. This guide covers four parts:

1. the Samba share on the Ubuntu server,
2. the teslausb settings on the Pi,
3. how the hub picks up new clips, and
4. how to check that it all works.

```
car ──USB──▶ Pi (teslausb) ──Wi-Fi, SMB 445──▶ /data/ohmytesla/data/teslacam  (Samba share "teslacam")
                    │                                        │  mounted in the hub container as /teslacam
                    └── curl …/api/dashcam/teslausb/notify ──▶ OhmyTesla hub (rescan + new-clip alert + SEI index)
```

Only one Pi setting changes how the files land. With `SHARE_NAME=teslacam`, the share root receives
`SentryClips/<event>/`, `SavedClips/<event>/` and, if enabled, `RecentClips/<YYYY-MM-DD>/`. There
is **no** `TeslaCam/` level. The hub detects this layout automatically.

---

## 1. Server: Samba share (Ubuntu)

The paths below assume the OhmyTesla stack lives in `/data/ohmytesla`. The hub container mounts
`./data/teslacam` at `/teslacam`; this is `TESLACAM_DIR` in `docker-compose.yml`, and its default is
right. Run these commands once:

```bash
sudo apt update && sudo apt install -y samba smbclient

# A dedicated account. Its password is stored in plaintext on the Pi, which lives in the car,
# so do not reuse any other password here.
sudo groupadd -f teslacam
sudo useradd --system --no-create-home --shell /usr/sbin/nologin -g teslacam teslausb
sudo smbpasswd -a teslausb            # this becomes SHARE_PASSWORD on the Pi
sudo usermod -aG teslacam "$USER"     # lets your own login browse/delete clips

sudo mkdir -p /data/ohmytesla/data/teslacam
sudo chown -R teslausb:teslacam /data/ohmytesla/data/teslacam
sudo chmod 2775 /data/ohmytesla/data/teslacam          # setgid: new folders stay in group teslacam
```

Append this to `/etc/samba/smb.conf`:

```ini
[teslacam]
   path = /data/ohmytesla/data/teslacam
   valid users = teslausb
   read only = no
   browseable = no
   force user = teslausb
   force group = teslacam
   create mask = 0664
   force create mode = 0664
   directory mask = 2775
   force directory mode = 2775
   # Keeps macOS clutter out if you browse the share from a Mac. rsync's in-flight files
   # (".teslausbtmp/.<name>.XXXXXX") are not affected.
   veto files = /._*/.DS_Store/
   delete veto files = yes
```

Then:

```bash
testparm -s >/dev/null && sudo systemctl restart smbd
# Allow SMB only from the home LAN (adjust the subnet):
sudo ufw allow from 192.168.0.0/24 to any port 445 proto tcp
# Prove the share is writable. teslausb setup stops if it is not:
smbclient //localhost/teslacam -U teslausb -c 'mkdir probe; rmdir probe' && echo share OK
```

Notes:

- Ubuntu's Samba defaults (SMB2 or newer) are fine. teslausb tries SMB 3.1.1 first.
- The hub container runs as root, so it can read and write the share no matter which user owns
  the files. Write access is needed for decrypting 2026.20+ clips in place.
- In the stack's `.env`, set `TESLACAM_TZ=America/Chicago` and `TZ=America/Chicago`. Clip file
  names use the car's local time, and the hub converts them to UTC per timestamp, so DST is handled.
  Also set `PUBLIC_URL` so alert links open from a phone.
- teslausb never deletes anything on the server, and the hub has no clip retention yet. HW4 records
  about 13 GB per driving hour when RecentClips are archived, so add your own cleanup. One example
  is a nightly cron that deletes `RecentClips/*` folders older than 14 days. Files deleted on the
  server are never uploaded again, because teslausb remembers what it archived.

---

## 2. Pi: teslausb

### Use 64-bit Raspberry Pi OS and current teslausb

Firmware 2025.44.3 and later writes an access time of **2107-12-31** on repeater and pillar clips.
On a 32-bit Pi OS, rsync's `stat` fails on that date. Those camera angles are then silently not
archived, and every run ends with "Error during archiving" (teslausb issue #1007). teslausb added a
workaround for 32-bit Debian 12 on 2026-01-20 (commit `4516dacc`). The prebuilt teslausb image
(v5.2, 2025-02-04) is 32-bit and predates that fix.

Recommended setup:

- Use a Pi Zero 2 W, 4B or 5. The original Pi Zero W can only run 32-bit, so avoid it.
- Use Raspberry Pi Imager → *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64-bit)**.
  Pre-configure Wi-Fi and SSH in Imager's advanced settings.
- Boot it, then:

  ```bash
  sudo -i
  apt update && apt install -y curl
  getconf LONG_BIT        # must print 64
  curl https://raw.githubusercontent.com/marcone/teslausb/main-dev/setup/generic/install.sh | bash
  ```
- When pre-setup finishes, the LEDs flash rapidly. Edit `/teslausb/teslausb_setup_variables.conf`
  (next section), then run `/etc/rc.local`. Follow progress with
  `tail -f /teslausb/teslausb-headless-setup.log` until it prints "All done."
- If you already run teslausb, update it with `sudo -i; /root/bin/setup-teslausb upgrade`. Check
  `getconf LONG_BIT`; if it prints 32, reinstall on a 64-bit OS.

### Variables to set

Put these in `teslausb_setup_variables.conf`. Replace `<server-ip>` with the OhmyTesla server's LAN
IP (an IP, not a host name), and `<notify token>` with the token shown in the hub under
**Settings → Dashcam → teslausb** (or `GET /api/dashcam/teslausb/settings` → `notify_token`).

```bash
export ARCHIVE_SYSTEM=cifs
export ARCHIVE_SERVER=<server-ip>
export SHARE_NAME=teslacam                  # share root = /data/ohmytesla/data/teslacam
export SHARE_USER=teslausb
export SHARE_PASSWORD='<samba password>'
export TIME_ZONE="America/Chicago"
export CAM_SIZE=40G                         # size to taste; HW4 writes ~13 GB per driving hour

# Defaults, shown for clarity: Saved and Sentry clips are archived.
# export ARCHIVE_SAVEDCLIPS=true
# export ARCHIVE_SENTRYCLIPS=true
# Optional: also archive RecentClips (≈800 files/day). The hub reads teslausb's
# RecentClips/<YYYY-MM-DD>/ layout and drops the duplicate copies of event clips.
# export ARCHIVE_RECENTCLIPS=true

# A marker written after each successful batch; the hub shows its time in /teslausb/status
export TRIGGER_FILE_ANY=ARCHIVE_UPLOADED

# Tell the hub when an archive run starts and finishes (finish also fires on failure).
# The notify token only lets the Pi ask for a rescan; it can't open anything else.
export NOTIFICATION_COMMAND_ENABLED=true
export NOTIFICATION_COMMAND_START='curl -fsS -m 10 -H "Authorization: Bearer <notify token>" --data-urlencode "msg=$message" http://<server-ip>:8800/api/dashcam/teslausb/notify'
export NOTIFICATION_COMMAND_FINISH='curl -fsS -m 10 -H "Authorization: Bearer <notify token>" --data-urlencode "msg=$message" http://<server-ip>:8800/api/dashcam/teslausb/notify'

# Recommended: protect the Pi's web UI and CGI endpoints. Enter the same values in the hub (§3).
export WEB_USERNAME='teslausb'
export WEB_PASSWORD='<pi web password>'
```

Quoting rules:

- Keep the outer **single** quotes on the two `NOTIFICATION_COMMAND_*` lines. teslausb `eval`s
  the string later, which is when `$message` becomes the archive summary.
- The token is URL-safe (letters, digits, `-` and `_`), so it needs no extra quoting.
- `SHARE_PASSWORD` follows teslausb's own quoting rules (see its `doc/OneStepSetup.md`).

To change these settings after setup:

```bash
sudo -i
/root/bin/remountfs_rw
nano /root/teslausb_setup_variables.conf
/root/bin/setup-teslausb
```

> **Why a token and not the portal password.** The Pi lives in the car. The portal password can
> also unlock the car through the portal, so it never goes on the Pi; the notify token can only
> trigger a rescan. If the Pi is lost, press **New token** in the hub and the old one stops
> working. The hook is optional: without it the hub still finds new clips within about a minute
> (see §3).

---

## 3. How the hub picks it up

**On every archive run, when the hook is configured:**

1. teslausb mounts the share and rsyncs the new files. Each file is first written to
   `.teslausbtmp/` and then renamed into place.
2. The files are sorted, so in each event folder the `*.mp4` files arrive first and `event.json`
   and `thumb.png` arrive last.
3. teslausb writes `ARCHIVE_UPLOADED`, then runs `NOTIFICATION_COMMAND_FINISH`.
4. `POST /api/dashcam/teslausb/notify` records the result (success, or "Error during archiving",
   with the file count and duration). It returns at once and, in the background:
   - rescans the library,
   - runs the new-clip watcher, telling it the run has finished so it does not wait for folders to
     settle,
   - and indexes the SEI telemetry of the new events.
5. The START message only records "archiving in progress".

**Without the hook**, the watcher scans every 60 seconds. It treats a new Sentry folder as ready
when either:

- it contains both `thumb.png` and `event.json`, or
- it has not changed for 15 seconds and no file in `.teslausbtmp/` is still being copied.

**The alert.** Each new Sentry event is filed once as rule `new_sentry_clip`:

- the title is one line, such as "🎬 Cybertruck — Sentry clip saved" (body: "Threat detected · Springfield · 9:41 PM"),
- the body is one line: the trigger reason and the time,
- it carries the event's thumbnail and a link to the player.

The alert engine decides whether it also goes to the phone; by default it is only recorded in the
app. If delivery fails, the event is retried later rather than lost. Events older than 7 days, for
example an imported backlog, and SavedClips are not announced.

**Sentry events while the car is at home.** teslausb only archives when the server *becomes*
reachable, which normally means when the car arrives. To fetch a Sentry event recorded in the
garage, ask the Pi to sync. Store its address once (and the web credentials, if you set
`WEB_USERNAME`):

```bash
curl -u ohmytesla:<portal password> -H 'Content-Type: application/json' \
     -d '{"pi_url":"<pi-ip>","username":"teslausb","password":"<pi web password>"}' \
     http://<server-ip>:8800/api/dashcam/teslausb/settings

curl -u ohmytesla:<portal password> -X POST http://<server-ip>:8800/api/dashcam/teslausb/sync
```

The sync request calls the Pi's `/cgi-bin/trigger_sync.sh`. It only takes effect while the Pi is
idle at home after a finished archive run. While the sync runs, teslausb briefly detaches the drive
from the car.

**Encrypted clips (firmware 2026.20+).** teslausb archives them unchanged; nobody can play them
without the key. Either turn off *Controls › Safety › Encrypt Dashcam Recordings* in the car,
where that option exists, or decrypt them in the hub with the `/api/dashcam/decrypt` routes. Those
routes need a dashcam.tesla.com sign-in token, and only header metadata is sent to Tesla.

---

## 4. Checking that it works

```bash
# Hub view: Pi health, last archive result, what the hub sees in the share
curl -s -u ohmytesla:<portal password> http://<server-ip>:8800/api/dashcam/teslausb/status | python3 -m json.tool

# Simulate a finished archive run from the server (exactly what teslausb sends)
curl -fsS -H "Authorization: Bearer <notify token>" --data-urlencode "msg=Archiving completed successfully. Archived 0 files in 1s" \
     http://localhost:8800/api/dashcam/teslausb/notify
```

Fields to check in `/teslausb/status`:

| field | expect |
|---|---|
| `ingest.layout` | `root` (files land at the share root) |
| `ingest.base` | `/teslacam` |
| `ingest.archive_markers` | `ARCHIVE_UPLOADED` with a recent time after the first run |
| `last_archive` | `ok: true`, with file count and duration |
| `pi` | CPU temperature, snapshots, free space, Wi-Fi (once the Pi address is set) |
| `pi_error` | Explains a wrong password or an unreachable Pi |

`GET /api/dashcam/status` shows two more things:

- `time_zone` should read `America/Chicago` (from `TESLACAM_TZ`).
- `scan.skipped_items` lists any names the scanner refused.

| symptom | likely cause |
|---|---|
| `last_archive.ok` is false and repeater/pillar clips are missing | 32-bit Pi OS with firmware 2025.44+. Reinstall on 64-bit (§2) |
| nothing arrives | Run `nc -zv <server-ip> 445` on the Pi. Check `ufw`. Run the `smbclient` write test |
| the notify hook gets HTTP 401 | Wrong or rotated notify token in `NOTIFICATION_COMMAND_*` |
| overlays are off by whole hours | `TESLACAM_TZ` is missing or wrong in `.env` |
| clips are listed but don't play | Encrypted (2026.20+) or truncated. See `/api/dashcam/status` → `unreadable` |
