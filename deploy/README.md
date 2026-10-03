# Bare-metal deployment with systemd (Debian 12 "bookworm")

A systemd timer runs `podcast run` every morning at 05:30. The service is a one-shot unit that
runs as an unprivileged user and may only write to its state and output directories.

| What | Where |
| --- | --- |
| Program and virtualenv | `/opt/content2podcast` |
| Configuration | `/etc/content2podcast/config.yaml`, `sources.yaml`, `.env` |
| State (database, scratch space) | `/var/lib/content2podcast` |
| Published files (feed, MP3s, cover) | `/srv/podcast` |

## Install

```sh
# 1. Requirements: Python 3.11+ (bookworm ships 3.11), ffmpeg and git
sudo apt install ffmpeg git python3 python3-venv

# 2. An unprivileged user and the directories
sudo useradd --system --home-dir /var/lib/content2podcast --create-home \
    --shell /usr/sbin/nologin podcast
sudo install -d -o podcast -g podcast /srv/podcast
sudo install -d /etc/content2podcast

# 3. The program and its virtualenv (the unit expects /opt/content2podcast/.venv/bin/podcast)
sudo git clone https://github.com/RichieRoxx/content2podcast /opt/content2podcast
sudo python3 -m venv /opt/content2podcast/.venv
sudo /opt/content2podcast/.venv/bin/pip install /opt/content2podcast
```

With [uv](https://docs.astral.sh/uv/) you can instead run `sudo uv sync --frozen --no-dev` in
`/opt/content2podcast`, which also creates `.venv` there and installs the locked versions.

## Configure

```sh
cd /opt/content2podcast
sudo cp config.example.yaml /etc/content2podcast/config.yaml
sudo cp sources.example.yaml /etc/content2podcast/sources.yaml
sudo cp .env.example /etc/content2podcast/.env
```

Edit `/etc/content2podcast/config.yaml` (relative paths are resolved against its directory):

```yaml
paths:
  data_dir: /var/lib/content2podcast
  output_dir: /srv/podcast
feed:
  base_url: http://<lan-ip-or-tailscale-name>:8080   # where podcast apps reach /srv/podcast
llm: {provider: azure_foundry}
tts: {provider: azure_speech}
```

Put the credentials into `/etc/content2podcast/.env`. systemd reads it as an `EnvironmentFile`
and the program reads it too, so the service user needs read access (but nobody else):

```sh
sudo chown root:podcast /etc/content2podcast/.env
sudo chmod 640 /etc/content2podcast/.env
```

Check the installation (`--online` sends one minimal request to the LLM and the TTS provider):

```sh
sudo -u podcast /opt/content2podcast/.venv/bin/podcast \
    --config /etc/content2podcast/config.yaml doctor --online
```

Serve `/srv/podcast` with a static web server, see [Serve the files](#serve-the-files).

## Enable the timer

```sh
sudo cp /opt/content2podcast/deploy/podcast.service /opt/content2podcast/deploy/podcast.timer \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now podcast.timer
```

The first run of a source only records what is already there (the "baseline"); episodes are
made for articles that appear afterwards. To see what a run would do without any speech
synthesis, use `podcast run --dry-run`.

## Operate

```sh
systemctl list-timers podcast.timer        # next and last run
sudo systemctl start podcast.service       # run now (waits until finished)
journalctl -u podcast.service -e           # logs of the last runs
journalctl -u podcast.service --since today
sudo -u podcast /opt/content2podcast/.venv/bin/podcast \
    --config /etc/content2podcast/config.yaml episodes list
```

- The service has no daily article limit; `TimeoutStartSec=4h` leaves room for long runs.
- Exit code 3 ("another run is still in progress") counts as success for systemd, any other
  non-zero exit code (for example 1 when every episode failed) makes the unit `failed`.
- `OnCalendar=*-*-* 05:30:00` uses the machine's time zone; `Persistent=true` catches up on a
  missed run after the machine was off, `RandomizedDelaySec=5min` spreads the start a little.
  Change the time with `sudo systemctl edit --full podcast.timer`.

## Serve the files

The output directory only contains static files, so any web server works. `Caddyfile.example`
is a ready-made configuration for [Caddy](https://caddyserver.com/): it serves `/srv/podcast`
on port 8080 and sets what podcast apps need (`application/rss+xml` for the feed, `audio/mpeg`
and long-lived caching for the MP3s, range requests for seeking and resuming, no directory
listings, no dotfiles).

```sh
sudo apt install caddy
sudo cp /opt/content2podcast/deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo systemctl restart caddy
curl -I http://localhost:8080/feed.xml
```

Set `feed.base_url` in `config.yaml` to the address the apps use, e.g.
`http://<lan-ip>:8080`, and run `podcast feed rebuild` to refresh the links in the feed. The
environment variables `PODCAST_ROOT` (default `/srv/podcast`) and `PODCAST_PORT` (default
`8080`) change the directory and the port without editing the file.

### HTTPS over Tailscale

Podcast apps on phones usually want HTTPS as soon as the feed is not on the local network.
Tailscale can issue a certificate for `<machine>.<tailnet>.ts.net` (enable "HTTPS
Certificates" in the admin console under DNS). Then set `feed.base_url` to
`https://<machine>.<tailnet>.ts.net`. Two ways:

- **Tailscale terminates TLS** (simplest): keep `Caddyfile.example` and put Tailscale in front
  of it:

  ```sh
  tailscale serve --bg --https=443 http://127.0.0.1:8080
  ```

  If Caddy should only be reachable through Tailscale, change the site address in the
  Caddyfile from `:8080` to `127.0.0.1:8080`.
- **Caddy terminates TLS** with Tailscale's certificate: use `Caddyfile.tailscale.example`.
  Caddy asks `tailscaled` for the certificate, which needs to be allowed for its user:

  ```sh
  sudo tailscale set --operator=caddy
  sudo cp /opt/content2podcast/deploy/Caddyfile.tailscale.example /etc/caddy/Caddyfile
  # set PODCAST_HOST=<machine>.<tailnet>.ts.net for the caddy service, e.g. with
  # `sudo systemctl edit caddy` (Environment=PODCAST_HOST=...)
  sudo systemctl restart caddy
  ```

`deploy/verify-caddy.sh` validates both files with `caddy validate` and smoke-tests the LAN
one against a real Caddy container (headers, `304`, range requests, hidden files); CI runs it.
It needs Docker on Linux.

## Docker

The image runs `podcast daemon` as an unprivileged user (UID/GID 1000 by default) and contains
ffmpeg. It has three volumes:

| Volume | Content |
| --- | --- |
| `/config` | `config.yaml`, `sources.yaml` and optionally `.env` (mount it read-only) |
| `/data` | state: database, scratch space, `status.json` |
| `/srv/podcast` | the published feed and episodes (serve it with a web server) |

The defaults are `C2P_CONFIG=/config/config.yaml`, `C2P_PATHS__DATA_DIR=/data`,
`C2P_PATHS__OUTPUT_DIR=/srv/podcast` and `TZ=UTC`; set `TZ` to your time zone so that
`schedule.time` means local time. Credentials can come from an `--env-file` or from
`/config/.env`.

```sh
docker build -t content2podcast --build-arg UID=$(id -u) --build-arg GID=$(id -g) .

# check the setup, then see what a run would do without speech synthesis
docker run --rm -v $PWD/config:/config:ro -v podcast-data:/data --env-file .env \
    content2podcast doctor --online
docker run --rm -v $PWD/config:/config:ro -v podcast-data:/data --env-file .env \
    content2podcast run --dry-run

# run the daemon: one run per day at schedule.time, a catch-up run on start if one was missed
docker run -d --name podcast --restart unless-stopped -e TZ=Europe/Berlin --env-file .env \
    -v $PWD/config:/config:ro -v podcast-data:/data -v podcast-site:/srv/podcast \
    content2podcast
```

Use the `UID`/`GID` build arguments if you bind-mount host directories, so that the container
user may write to them. `docker ps` shows the health: the container is healthy while the
daemon reports a fresh heartbeat and its last run succeeded less than 26 hours ago
(`podcast health` explains an unhealthy state). `docker stop` sends SIGTERM; the daemon
finishes the episode it is working on and exits cleanly (give it time with
`docker stop --time 600` if episodes take long; an interrupted episode is resumed next time).

`deploy/smoke-image.sh IMAGE` checks all of this against a built image (non-root user, version,
ffmpeg, a dry run with a read-only config, a healthy daemon that stops cleanly, an unhealthy one
without configuration); CI runs it.

## Docker Compose

`deploy/docker-compose.example.yml` runs the daemon together with Caddy (see
[Serve the files](#serve-the-files)); both share the `podcast-site` volume. Copy it next to a
`config/` directory (`config.yaml`, `sources.yaml`), the `.env` with the credentials and
`Caddyfile.example`:

```sh
cp deploy/docker-compose.example.yml docker-compose.yml
cp deploy/Caddyfile.example .
docker compose up -d --build         # or set PODCAST_IMAGE to a published image
docker compose ps                    # the podcast service shows its health
```

Settings (shell or a compose `.env`): `PODCAST_IMAGE` (default `content2podcast:local`),
`PODCAST_PORT` (default `8080`), `TZ` (default `Europe/Berlin`). One-off commands share the
volumes:

```sh
docker compose exec podcast podcast run --dry-run
docker compose run --rm podcast doctor --online
```

If you prefer a timer on the host over the built-in scheduler, override the command with
`podcast run` and start it from cron or a systemd timer via `docker compose run --rm podcast run`.

`deploy/smoke-compose.sh IMAGE` brings the stack up against a built image, waits for the healthy
daemon, fetches the feed through Caddy and runs `down -v`; CI runs it.

## Update and uninstall

```sh
cd /opt/content2podcast && sudo git pull
sudo /opt/content2podcast/.venv/bin/pip install --upgrade /opt/content2podcast
```

```sh
sudo systemctl disable --now podcast.timer
sudo rm /etc/systemd/system/podcast.service /etc/systemd/system/podcast.timer
sudo systemctl daemon-reload
```

State and published files stay in `/var/lib/content2podcast` and `/srv/podcast` until you
delete them.

## Verify the unit files

`deploy/verify-units.sh` runs `systemd-analyze verify` against the two unit files without
installing anything (CI does the same). `systemd-analyze` itself only prints warnings for typos
and still exits with 0, so the script treats any output as an error.
