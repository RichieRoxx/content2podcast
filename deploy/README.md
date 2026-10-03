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

Serve `/srv/podcast` with any web server (a static file server is enough).

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
