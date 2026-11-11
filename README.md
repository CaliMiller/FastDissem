# Fast Dissem (Fast Dissemination)

A headless Python service that watches **YouTube channels** and **National Weather Service alerts** and posts them to **Bluesky** (and Mastodon), with a dark-mode web UI for settings. New networks plug in as single files.

## What it does

**YouTube → Bluesky**
- Watches any number of channels (enter `@handle`, a channel URL, or a `UC…` id — the id is looked up when you save).
- Posts the video title, your default hashtags, per-channel hashtags, then the video's own tags converted to hashtags (`severe weather` → `#SevereWeather`; pure numbers dropped).
- Attaches a link card with the thumbnail; Bluesky shows YouTube cards as a **playable video in the feed**.
- Without an API key it uses the channel RSS feed and takes tags from `#hashtags` in the description. With a free **YouTube Data API v3 key** it reads real video tags, waits for scheduled premieres to go live, and avoids RSS hiccups.

**NWS alerts → Bluesky**
- Watches county codes (`INC097` = Marion Co., IN) and/or forecast zones (`INZ040`).
- Example post:
  ```
  🔴 Tornado Warning
  📍 Boone, Hamilton, Marion counties, IN
  ⏰ Until 4:45 PM EST
  Tornado: radar indicated, 60 mph gusts, quarter-size (1") hail.

  #INwx #WeatherAlert
  ```
  Markers: 🔴 warning · 🟠 watch · 🟡 advisory · 🔵 statement · 🚨 emergency. Future alerts show a range: `⏰ From 7 PM EST Tue 11/10 until 7 AM EST Wed 11/11`. Winter/flood/etc. alerts use the NWS "WHAT…" summary.
- **Abbreviations** (`Severe Thunderstorm` → `Svr T-Storm`, `County` → `Co.`, `accumulations` → `accum.` …) are applied **only when a post would otherwise be too long** (or always/never — your choice). The list is editable in Settings.
- If it still doesn't fit, it trims in this order: video/extra tags → detail line → (shortens) area/time lines → long title → default hashtags.
- Filters: minimum severity, only/never these alert types, post updates or cancellations (off by default to avoid spam). Test messages and expired alerts are never posted.

**No duplicates**
- Every item has a stable id (YouTube video id, NWS alert id) stored in SQLite. An item is queued once, ever, and each network gets it at most once — across restarts, and even if two watched channels/areas both return it.
- When you add a channel or area, what already exists is **marked as handled, not posted** (so adding a channel doesn't dump its back catalog). Turn on "backfill" for a source to change that — it's on by default for NWS so alerts already in effect get posted.
- Failed posts retry with back-off (1, 2, 4… min, up to 5 tries); alerts that expire meanwhile are dropped. Dashboard rows have a **forget** button to allow a repost.

## Quick start

Requires Python 3.10+. (For Proxmox, see the next section instead.)

```bash
cd fast_dissem
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m fastdissem run
```
Open **http://127.0.0.1:8787**, then in **Settings**:
1. **Bluesky** – handle + an *app password* (Bluesky → Settings → Privacy and security → App passwords). Tick *Enabled*, save, click **Test connection**.
2. **YouTube** – add channel rows, default hashtags, optional API key. Tick *Enabled*.
3. **NWS Alerts** – add county/zone codes and a contact (website or email; NWS asks apps to identify themselves). Find codes at https://alerts.weather.gov or `https://api.weather.gov/zones?area=IN&type=county`.
4. Look at **Preview** to see exactly what would be posted for each network, with character counts.
5. Turn **Dry run** off in General when you're happy. (Things handled during dry run are recorded and won't be posted later.)

## Install on Proxmox (LXC) – recommended

A lightweight unprivileged Debian container (1 CPU, 512 MB RAM, 4 GB disk) is plenty.

**1. Put the code on GitHub** (one time). The repository must be **public** so the container can clone it.
```bash
git remote add origin https://github.com/CaliMiller/FastDissem.git
git push -u origin main
```
Then edit one line in `deploy/proxmox-lxc.sh` and push again:
```bash
DEFAULT_REPO="https://github.com/CaliMiller/FastDissem.git"
```

**2. On the Proxmox host shell** (Datacenter → your node → **Shell**), run:
```bash
bash -c "$(wget -qO- https://raw.githubusercontent.com/CaliMiller/FastDissem/main/deploy/proxmox-lxc.sh)"
```
Press Enter to accept each default (next free ID, `vmbr0`, DHCP, `local-lvm`) or type your own. It downloads the newest Debian template, creates and starts the container, installs everything, and prints the **web UI address and a generated password**.

Unattended, with a static IP:
```bash
IP=192.168.1.50/24 GATEWAY=192.168.1.1 bash -c "$(wget -qO- https://raw.githubusercontent.com/CaliMiller/FastDissem/main/deploy/proxmox-lxc.sh)" -- -y
```

**Afterwards**
- Update to the latest code from GitHub: `pct exec <CTID> -- bash /opt/fast_dissem/deploy/install.sh` — settings and history are kept. (Inside the container's console, `fastdissem-update` does the same.)
- Logs: `pct exec <CTID> -- journalctl -u fastdissem -f`
- Forgot the password: `pct exec <CTID> -- cat /root/fastdissem-web-password.txt`
- Proxmox backups/snapshots of the container include your settings and duplicate history (`/opt/fast_dissem/data`).

### In a VM (or any Debian/Ubuntu machine) instead
Inside the VM, as root:
```bash
bash -c "$(wget -qO- https://raw.githubusercontent.com/CaliMiller/FastDissem/main/deploy/install.sh)"
```
Same result: service `fastdissem`, code in `/opt/fast_dissem`, web UI on port 8787 with a generated password, `fastdissem-update` to update.

## Running it

| Command | What it does |
|---|---|
| `python -m fastdissem run` | engine + web UI (default) |
| `python -m fastdissem run --no-web` | headless, engine only |
| `python -m fastdissem once` | one check-and-post pass, then exit (for cron/Task Scheduler) |
| `python -m fastdissem preview` | print what would be posted; posts nothing |
| `python -m fastdissem web` | settings UI only |

Options: `--data-dir DIR` (default `./data`, holds `config.json` and `fastdissem.db`), `--dry-run` / `--live` to override the setting, `--host`, `--port`.

Docker: `docker build -t fastdissem . && docker run -v $PWD/data:/data -p 8787:8787 fastdissem`.

**Security:** the UI listens on `127.0.0.1` only by default. If you expose it (`0.0.0.0`, Docker), set a web password in Settings → Web UI. `config.json` contains your app password and is written with owner-only permissions. Any setting can come from the environment instead, e.g. `FASTDISSEM__PUBLISHERS__BLUESKY__APP_PASSWORD=xxxx-xxxx-xxxx-xxxx`.

Check intervals default to 5 min (YouTube) and 2 min (NWS); the minimum is 30 s. YouTube's RSS feed itself can lag new uploads by several minutes.

## Experimental networks (beta branch)

The `beta` branch adds **X (Twitter), Facebook Pages, Threads and Instagram**. They were written from each service's API documentation and pass offline tests, but **none has been tried with a real account yet**, so they're labelled *beta · untested* in Settings. Mastodon (also on `main`, and equally untested) gets the same label here.

| Network | What gets posted | Things to know |
|---|---|---|
| X (Twitter) | Text, optional link (X shows its own preview) | X bills every post through prepaid credits; posts with a link have cost far more than plain ones, so the link can be switched off. 280-character limit counted the way X does (emoji = 2, links = 23). Uses never-expiring OAuth 1.0a keys. |
| Facebook Page | Text + link preview | Must be a Page, not a personal profile. Needs a Page access token with `pages_manage_posts`. |
| Threads | Text + link preview | Only **one tag** per post: the first hashtag becomes the topic tag. Token renews itself every ~20 days. |
| Instagram | Video thumbnail + caption | Needs a Professional account. **Weather alerts are skipped** (Instagram requires a picture). Caption links aren't clickable. Token renews itself. |

**Try it on a separate test container first.** New install:
```bash
BRANCH=beta bash -c "$(wget -qO- https://raw.githubusercontent.com/CaliMiller/FastDissem/beta/deploy/proxmox-lxc.sh)"
```
Switch an existing container to beta, and back:
```bash
pct exec <CTID> -- env BRANCH=beta bash /opt/fast_dissem/deploy/install.sh
pct exec <CTID> -- env BRANCH=main bash /opt/fast_dissem/deploy/install.sh
```
Updates keep a container on whichever branch it's on. Settings work on both branches.

If you try one of these networks, Settings → **Test connection** checks the sign-in, and **Preview** shows exactly what would be posted. Problems show up on the Dashboard with the network's own error message.

## Adding another network

1. Copy `fastdissem/publishers/_template.py` to e.g. `facebook.py`.
2. Set `name`, `label`, `char_limit`, `fields` (these become the settings form) and implement `publish()` (raise on failure) and optionally `check()`.
3. Uncomment `@register_publisher` and restart. It appears in Settings and the Preview.

Text fitting, hashtags, duplicate tracking per network, retries and dry run come for free. `mastodon.py` (~60 lines) is the simplest complete example; the template points to the others. New **sources** work the same way in `fastdissem/sources/` (subclass `Source`, implement `poll()` returning `Item`s).

## Layout
```
fastdissem/
  engine.py        polling loop, duplicate checks, delivery + retries
  storage.py       SQLite (items, deliveries, activity)
  text.py          grapheme counting, hashtags, abbreviations, fitting to limits
  config.py        config.json + env overrides
  oauth1.py        request signing for X
  plugins.py       Source / Publisher base classes and registry
  sources/         youtube.py, nws.py
  publishers/      bluesky.py, mastodon.py, console.py, _template.py
                   beta: twitter.py, facebook.py, threads.py, instagram.py, _meta.py
  web/             Flask app + templates (dark theme)
deploy/
  proxmox-lxc.sh   run on a Proxmox host: creates an LXC and installs
  install.sh       installs/updates on any Debian/Ubuntu (used by the above)
tests/             offline tests: python -m pytest -q
```

## Notes and limits
- The NWS link card points to `forecast.weather.gov/showsigwx.php` for your watched zone/county; change it in NWS → Link URL if you prefer another page.
- "Include Shorts" off: detected from the RSS link, or (API mode) by checking youtube.com/shorts/<id>.
- Bluesky's 300 limit counts visible characters (graphemes); Fast Dissem uses a close built-in approximation, which can be off by a character for rare emoji sequences. Normal text, flags and common emoji are counted exactly as Bluesky does.
- Built-in web server is fine for a home machine; `pip install waitress` and it's used automatically.

## Credits
- Code and design developed in collaboration with Claude (Anthropic).
