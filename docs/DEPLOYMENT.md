# Deployment and configuration guide

This guide takes you from nothing to a running podcast feed, once with a bare-metal install
(systemd timer) and once with Docker Compose, and then explains how to configure and operate it.
Both variants end in the same place: new articles of your sources become episodes every morning
and `feed.xml` is served as static files.

Shorter reference pages: [README](../README.md), [deploy/README.md](../deploy/README.md)
(unit files, Caddy, Tailscale).

- [1. What you need](#1-what-you-need)
- [2. Prepare configuration (both variants)](#2-prepare-configuration-both-variants)
- [3. Variant A: bare metal (Debian 12, systemd)](#3-variant-a-bare-metal-debian-12-systemd)
- [4. Variant B: Docker Compose](#4-variant-b-docker-compose)
- [5. The first run and what to expect](#5-the-first-run-and-what-to-expect)
- [6. Subscribe in a podcast player](#6-subscribe-in-a-podcast-player)
- [7. Configuration reference](#7-configuration-reference)
- [8. Operating and updating](#8-operating-and-updating)
- [9. Troubleshooting](#9-troubleshooting)

## 1. What you need

| | |
| --- | --- |
| A machine that is on in the morning | A home server, NAS, Raspberry Pi (arm64) or small VPS. About 1 GB RAM is plenty. |
| Debian 12 **or** Docker | Bare metal is documented for Debian 12 "bookworm" (Python 3.11, systemd). Docker works anywhere (amd64, arm64). |
| Azure AI Foundry | A resource with a chat model deployment (the script writer). Note its **base URL**, **API key** and the **deployment name**. |
| Azure Speech | Text-to-speech, region e.g. `swedencentral`. The Foundry key may be used for both, see [Providers](#providers). |
| A way for your phone to reach the feed | Same Wi-Fi (LAN IP), or [Tailscale](../deploy/README.md#https-over-tailscale). Read [section 6](#6-subscribe-in-a-podcast-player) first, some apps cannot use LAN-only feeds. |

You do not need Azure to try the tool: set both providers to `fake` and you get deterministic
test episodes without any account (see [Try it without Azure](#try-it-without-azure)).

## 2. Prepare configuration (both variants)

Three files describe an installation. Start from the examples in the repository:

| File | Content | Secret? |
| --- | --- | --- |
| `config.yaml` | Podcast metadata, feed address, episode length, voices, schedule, providers | no |
| `sources.yaml` | The RSS feeds and web pages to watch | no |
| `.env` | Azure keys and endpoints | **yes**, never commit it |

Decide these values up front, you need them in the files below:

1. **Feed address** (`feed.base_url`): how a podcast app reaches this machine, for example
   `http://192.168.1.10:8080` (LAN IP) or `https://podcast.tailnet-name.ts.net` (Tailscale).
   It is written into every episode link, so use the final address from the start. If it
   changes later: edit `config.yaml`, then run `podcast feed rebuild`.
2. **Sources**: find the feed of a site with `podcast sources discover https://example.com/blog`
   (available after the install; it prints a ready-to-paste entry).
3. **Azure values**: see the table in [Providers](#providers).

A minimal `config.yaml` for production:

```yaml
podcast:
  title: My Daily Podcast
  author: Your Name

feed:
  base_url: http://192.168.1.10:8080

llm:
  provider: azure_foundry
  model: gpt-5.4            # the name of YOUR deployment

tts:
  provider: azure_speech
  region: swedencentral

schedule:
  time: "05:30"             # local time of the machine / container (TZ)
```

and a `.env`:

```sh
AZURE_FOUNDRY_BASE_URL=https://<your-resource>.services.ai.azure.com
AZURE_FOUNDRY_API_KEY=...
AZURE_SPEECH_REGION=swedencentral
# AZURE_SPEECH_KEY=...    # optional, defaults to the Foundry key
```

`config.example.yaml` documents every option, `sources.example.yaml` shows both source types.

### Try it without Azure

```yaml
llm: {provider: fake}
tts: {provider: fake}
```

With the fake providers `podcast run` produces short placeholder episodes. Use it to check the
whole chain (sources, feed, web server, player) before you add real keys.

## 3. Variant A: bare metal (Debian 12, systemd)

A systemd timer starts `podcast run` every morning; the web server is a separate static file
server (Caddy). Everything below is run as root (or with `sudo`).

### 3.1 Install the program

```sh
# Requirements: Python 3.11+, ffmpeg, git
apt install ffmpeg git python3 python3-venv

# An unprivileged service user and its directories
useradd --system --home-dir /var/lib/content2podcast --create-home \
    --shell /usr/sbin/nologin podcast
install -d -o podcast -g podcast /srv/podcast        # feed and episodes (served by the web server)
install -d /etc/content2podcast                       # configuration

# The program and its virtualenv (the unit expects /opt/content2podcast/.venv/bin/podcast)
git clone https://github.com/RichieRoxx/content2podcast /opt/content2podcast
python3 -m venv /opt/content2podcast/.venv
/opt/content2podcast/.venv/bin/pip install /opt/content2podcast
/opt/content2podcast/.venv/bin/podcast --version
```

| What | Where |
| --- | --- |
| Program | `/opt/content2podcast` |
| Configuration | `/etc/content2podcast/` (`config.yaml`, `sources.yaml`, `.env`) |
| State (database, scratch space) | `/var/lib/content2podcast` |
| Published files | `/srv/podcast` |

### 3.2 Configure

```sh
cd /opt/content2podcast
cp config.example.yaml /etc/content2podcast/config.yaml
cp sources.example.yaml /etc/content2podcast/sources.yaml
cp .env.example /etc/content2podcast/.env
```

Edit `/etc/content2podcast/config.yaml` as in [section 2](#2-prepare-configuration-both-variants)
and add the paths (relative paths are resolved against the config file's directory):

```yaml
paths:
  data_dir: /var/lib/content2podcast
  output_dir: /srv/podcast
```

Fill in `/etc/content2podcast/.env` and protect it. The service user has to read it:

```sh
chown root:podcast /etc/content2podcast/.env
chmod 640 /etc/content2podcast/.env
```

Edit `/etc/content2podcast/sources.yaml` (see [Sources](#sources)).

### 3.3 Check the installation

```sh
sudo -u podcast /opt/content2podcast/.venv/bin/podcast \
    --config /etc/content2podcast/config.yaml doctor --online
```

`doctor` checks the configuration, sources, secrets (shown masked), providers, ffmpeg, the
directories and the database. `--online` additionally sends one tiny request to the LLM and the
TTS provider (a few tokens and a second of speech) so that wrong keys show up now and not at
05:30. Fix everything it reports before continuing.

Tip: put an alias into your shell to avoid the long command:
`alias podcast='sudo -u podcast /opt/content2podcast/.venv/bin/podcast --config /etc/content2podcast/config.yaml'`.

### 3.4 Serve the files

```sh
apt install caddy
cp /opt/content2podcast/deploy/Caddyfile.example /etc/caddy/Caddyfile
systemctl restart caddy
```

Caddy now serves `/srv/podcast` on port 8080 with the right content types, range requests and no
directory listings. Other web servers work as well, it is just a directory of static files.
For HTTPS over Tailscale see [deploy/README.md](../deploy/README.md#https-over-tailscale).

### 3.5 Establish the baseline, then enable the timer

The first check of a source only records the articles that already exist ("baseline"), so that
the whole archive does not become episodes. Do this once by hand:

```sh
podcast check
```

Then install and start the timer:

```sh
cp /opt/content2podcast/deploy/podcast.service /opt/content2podcast/deploy/podcast.timer \
    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now podcast.timer
systemctl list-timers podcast.timer           # shows the next run
```

The timer fires at 05:30 in the machine's time zone (change with
`systemctl edit --full podcast.timer`) and catches up after downtime.

### 3.6 See it work right away

You do not have to wait for the first new article to see the chain work:

```sh
podcast run --dry-run          # writes scripts only, no speech costs: <data_dir>/dry-run/
podcast run                    # the real thing: speech, episodes, feed
systemctl start podcast.service && journalctl -u podcast.service -f    # same, via systemd
```

Both only act on articles that appeared after the baseline. With nothing new they report
"no pending articles". To get an episode for testing anyway, add a source that publishes often,
run `podcast check` (baseline) and run again once it has a new article, or try a single article
without any database: `podcast script <article-url>` followed by `podcast tts <script.json>`.

## 4. Variant B: Docker Compose

One Compose project runs the daemon (`podcast daemon`: one run a day at `schedule.time`, a
catch-up run on start if one was missed) and Caddy for the files.

### 4.1 Get the files

```sh
git clone https://github.com/RichieRoxx/content2podcast
cd content2podcast/deploy
```

All commands below run in the `deploy/` directory.

### 4.2 Choose the image

- **Build it yourself** (works everywhere, nothing to configure): Compose builds the image from
  the repository on the first `up --build`. If you bind-mount host directories later, build with
  `UID=$(id -u) GID=$(id -g)` so the container user may write to them.
- **Use the published image** `ghcr.io/richieroxx/content2podcast:edge` (amd64 and arm64, saves
  the build, handy on a Raspberry Pi): put `PODCAST_IMAGE=ghcr.io/richieroxx/content2podcast:edge`
  into the shell or a `.env`-style file as shown below. If the pull is denied the package is not
  public yet ([#94](https://github.com/RichieRoxx/content2podcast/issues/94)); build it yourself.

### 4.3 Configure

```sh
mkdir config
cp ../config.example.yaml config/config.yaml
cp ../sources.example.yaml config/sources.yaml
cp ../.env.example .env
```

- Edit `config/config.yaml` as in [section 2](#2-prepare-configuration-both-variants). The image
  presets the paths (`/config`, `/data`, `/srv/podcast`), do not set `paths:`.
- Edit `config/sources.yaml` ([Sources](#sources)).
- Fill in `.env` with the Azure values. Compose passes it to the container as environment
  variables. Compose settings may go into the same file:

  ```sh
  PODCAST_PORT=8080                                   # where Caddy listens on the host
  TZ=Europe/Berlin                                    # schedule.time is local time in this zone
  # PODCAST_IMAGE=ghcr.io/richieroxx/content2podcast:edge
  ```

### 4.4 Check, then start

```sh
docker compose -f docker-compose.example.yml build          # skip with PODCAST_IMAGE
docker compose -f docker-compose.example.yml run --rm podcast doctor --online
docker compose -f docker-compose.example.yml run --rm podcast check     # baseline
docker compose -f docker-compose.example.yml up -d
docker compose -f docker-compose.example.yml ps             # "healthy" after a few seconds
```

Tip: `export COMPOSE_FILE=docker-compose.example.yml` (or copy the file to `docker-compose.yml`)
shortens the commands to `docker compose ...`.

The feed is at `http://<this host>:<PODCAST_PORT>/feed.xml`. `docker compose logs -f podcast` shows
the daemon: it logs when the next run is due. A run starts on its own at `schedule.time`; to
start one now: `docker compose exec podcast podcast run`.

The `podcast` container is healthy while the daemon has a fresh heartbeat and its last run
succeeded less than 26 hours ago (`docker compose exec podcast podcast health` explains an
unhealthy state). `docker compose stop` waits up to 10 minutes for the episode in progress.

Prefer the host's cron or timer to the built-in scheduler? Then do not start the `podcast`
service and run `docker compose run --rm podcast run` from the host instead (same volumes).

## 5. The first run and what to expect

1. **Baseline**: the first check of each source records its existing articles as "baseline" and
   makes no episodes. This is intentional. `podcast sources list` shows `baseline` counts.
2. **New articles** found later become `pending`; every run turns each pending article into one
   episode (about 3-10 minutes, no daily limit unless you set `max_episodes_per_run`).
3. Each episode: article text, LLM script, speech per turn, loudness-normalized MP3, entry in
   `feed.xml` with links to the source article(s).
4. **Failures** are isolated: a failing source or article is logged and retried on the next run
   (after repeated failures the article is marked failed; `podcast run --force` retries).
5. **Retention** deletes episodes older than `feed.retention.max_age_days` (default 30).

Useful commands (prefix with `docker compose exec podcast` or your alias):

```sh
podcast sources list        # state of every source
podcast episodes list       # published episodes
podcast run --dry-run       # what would a run do? scripts only, no speech
```

Costs: one LLM call and the speech synthesis (billed per character) per episode.
`max_episodes_per_run` caps a run; `--dry-run` lets you review scripts for free.

## 6. Subscribe in a podcast player

Podcast apps need an address they can reach, that is your `feed.base_url`:

- **Same network**: `http://<lan-ip>:8080/feed.xml`. Works in apps that fetch on the
  phone itself (for example AntennaPod).
- **Apps that fetch through their own servers** (for example Pocket Casts, Overcast) cannot reach
  a LAN-only address. Use an on-device app, or publish via Tailscale.
- **Away from home**: use Tailscale and HTTPS, see
  [deploy/README.md](../deploy/README.md#https-over-tailscale), and set `feed.base_url` to the
  `https://<machine>.<tailnet>.ts.net` address.

Add the feed with "add by URL" and `<base_url>/feed.xml`. Check it in a browser first.

## 7. Configuration reference

### Layers

Later entries win:

1. built-in defaults
2. `config.yaml`
3. `.env` next to the config file
4. environment variables `C2P_...` (`__` for nesting), for example `C2P_FEED__BASE_URL=http://host:8080`
5. command line options

The config file is `--config`, else `$C2P_CONFIG`, else `./config.yaml`. Secrets are read from
the environment or `.env` only, never from `config.yaml`. A configuration error is reported as one
readable message and exit code 2.

### Providers

| Role | `provider` | Settings |
| --- | --- | --- |
| LLM | `azure_foundry` | `llm.model` = deployment name; env `AZURE_FOUNDRY_BASE_URL`, `AZURE_FOUNDRY_API_KEY`; optional `reasoning_effort`, `max_completion_tokens`, `timeout_s`, `max_retries` |
| TTS | `azure_speech` | `tts.region` or env `AZURE_SPEECH_REGION`; or `tts.endpoint` / `AZURE_SPEECH_ENDPOINT` (custom domain); key `AZURE_SPEECH_KEY`, falling back to the Foundry key |
| both | `fake` | nothing; deterministic output for tests |

Which endpoint and key combination works against a real resource is still being verified
([#74](https://github.com/RichieRoxx/content2podcast/issues/74)); `podcast doctor --online` tells
you what fails. Without a `tts` section you can still use `podcast run --dry-run`.

### Sources

```yaml
sources:
  - name: Example Blog           # unique
    url: https://example.com/feed.xml
    type: rss                    # rss (default) | html
    include: ["(?i)python"]      # optional regexes on the article URL
    exclude: ["(?i)sponsored"]
    enabled: true

  - name: Example News           # an overview page, the selector picks the article links
    url: https://news.example.com/
    type: html
    selector: "article h2 a"
```

- Prefer RSS. `podcast sources discover <url>` finds the feed of a site and prints an entry.
- An HTML source without `selector` can let the LLM choose the links: `llm_links: true` on the
  source or `link_extraction.enabled: true` for all (cached per page content).
- After changing a selector or URL: `podcast sources baseline --reset` to start clean.

### Episodes

| Key | Default | Meaning |
| --- | --- | --- |
| `episode.mode` | `per_article` | `per_article` or `daily_digest` (one episode per day) |
| `episode.min_minutes` / `max_minutes` | 3 / 10 | length range per article episode |
| `episode.max_articles` | 10 | digest: articles per episode |
| `episode.max_article_age_days` | 7 | older new articles are skipped |
| `episode.max_episodes_per_run` | off | cap per run (cost guard) |
| `episode.intro_file` / `outro_file` | none | audio files added before and after |
| `episode.prompts_dir` | none | your own prompt templates (`per_article_de.md`, ...) |

### Voices and audio

```yaml
roles:
  host:   {name: Mia,   voice: "de-DE-Mia:MAI-Voice-2.1"}
  expert: {name: Klaus, voice: "de-DE-Klaus:MAI-Voice-2.1"}
audio:
  loudness_lufs: -16
  bitrate: 128k
```

Try voices and prompts on a single article without touching the database:
`podcast script <url>` and `podcast tts <script.json>`.

### Feed, retention, schedule

```yaml
feed:
  base_url: http://192.168.1.10:8080
  retention: {max_age_days: 30, max_episodes: null}
schedule:
  time: "05:30"          # HH:MM local time (bare metal: the timer's OnCalendar is what counts)
```

Cover image: `podcast.cover_image` (relative to the config directory), copied to the output directory.

## 8. Operating and updating

| Task | Bare metal | Docker Compose |
| --- | --- | --- |
| Logs | `journalctl -u podcast.service -e` | `docker compose logs -f podcast` |
| Run now | `systemctl start podcast.service` | `docker compose exec podcast podcast run` |
| Next run | `systemctl list-timers podcast.timer` | `docker compose logs podcast` (daemon log) |
| Health | `podcast doctor` | `docker compose ps`, `podcast health` |
| After a config change | next run picks it up | `docker compose restart podcast` |
| Update | `cd /opt/content2podcast && git pull && .venv/bin/pip install .` | `git pull && docker compose build && docker compose up -d` (or pull the new image tag) |
| Back up | `/etc/content2podcast`, `/var/lib/content2podcast` (database) | `config/`, `.env`, volume `podcast-data` |

The database migrates itself on start; the episodes in the output directory can always be
re-listed with `podcast feed rebuild`. To uninstall bare metal, disable the timer
(`systemctl disable --now podcast.timer`) and remove the unit files, directories and the user;
for Compose, `docker compose down` (add `-v` to delete the volumes, including all episodes).

## 9. Troubleshooting

- **Start with `podcast doctor --online`.** It names what is missing and exits non-zero on a failure.
- **Nothing is published for a new source**: the first run only sets the baseline; episodes follow
  when new articles appear. `podcast sources list` shows counts and the last error per source.
- **Wrong links in the feed**: fix `feed.base_url`, run `podcast feed rebuild`.
- **Exit code 3**: another run still holds the lock; harmless, the unit treats it as success.
- **Exit code 2**: a configuration problem, the message names the key.
- **Permission denied on `.env` / data directory**: bare metal needs the `podcast` user to read
  `/etc/content2podcast/.env` (mode 640, group `podcast`) and write `/var/lib/content2podcast` and
  `/srv/podcast`; Docker with bind mounts needs matching `UID`/`GID` at build time.
- **Compose: `env file .env not found`**: create `deploy/.env` (an empty file is fine with the
  fake providers).
- **Podcast app cannot load the feed**: open `<base_url>/feed.xml` in the phone's browser; see
  [section 6](#6-subscribe-in-a-podcast-player) for LAN-only limits and HTTPS.
- **Unhealthy container**: `docker compose exec podcast podcast health` states why (no heartbeat,
  last run failed, last run too old).
