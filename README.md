# content2podcast

Turn blog and news articles into German podcast episodes: a self-hosted tool that watches your
sources, lets an LLM write a two-person dialogue about each new article, voices it with
text-to-speech, and publishes the result as a podcast RSS feed you can subscribe to with any
podcast player.

> **Status: work in progress.** Only the project scaffolding exists so far (configuration,
> logging, CLI skeleton, database, HTTP client, provider interfaces, Docker image). The
> pipeline itself is not implemented yet, and every command except `--version` / `--help` is a
> stub that prints "not implemented yet".

## How it will work

1. **Sources**: RSS feeds and HTML index pages listed in `sources.yaml`.
2. **Fetch and extract**: new articles are detected, de-duplicated and reduced to their text.
3. **Script**: an LLM turns an article (or a daily digest) into a dialogue between a host
   (Mia) and an expert (Klaus).
4. **Speech**: a TTS provider voices the dialogue, `ffmpeg` joins and loudness-normalizes it.
5. **Feed**: episodes are published as MP3 files plus an RSS feed, with configurable retention.

LLM and TTS are pluggable providers selected in the configuration. The first real
implementations target Azure AI Foundry (LLM) and Azure Speech (TTS).

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- [ffmpeg](https://ffmpeg.org/) on `PATH` (already included in the Docker image)
- An Azure AI Foundry resource with an LLM deployment and Azure Speech (needed once the
  providers land; not needed for development and tests)

## Development quickstart

```sh
uv sync                   # create the virtualenv and install dependencies
uv run pytest             # run the tests
uv run ruff check         # lint
uv run ruff format --check
uv run podcast --help     # list all commands
```

Tests that need ffmpeg are skipped automatically when it is not installed.

## Configuration

Settings come from several layers; later entries win:

1. built-in defaults
2. `config.yaml`
3. `.env` (next to the config file)
4. environment variables with the `C2P_` prefix and `__` for nesting,
   e.g. `C2P_FEED__BASE_URL=http://my-host:8080`
5. command line options

The config file is taken from `--config`, else `$C2P_CONFIG`, else `./config.yaml`. Relative
paths inside it are resolved against the config file's directory. Secrets (Azure keys and
endpoints) are read from the environment or `.env` only, never from `config.yaml`.

Start from the example files, which document every option:

| File | Purpose |
| --- | --- |
| [`config.example.yaml`](config.example.yaml) | Podcast metadata, paths, feed, episode length, voices, audio, schedule |
| [`sources.example.yaml`](sources.example.yaml) | One RSS and one HTML source |
| [`.env.example`](.env.example) | Azure credentials and `C2P_` overrides |

```sh
cp config.example.yaml config.yaml
cp sources.example.yaml sources.yaml
cp .env.example .env      # then fill in your keys
uv run podcast check      # loads and validates the configuration (stub for now)
```

A configuration error is reported as one readable message and exit code 2.

## Deployment

_To do._ Bare metal (systemd timer) and Docker deployment will be documented once the
pipeline works. Images for amd64 and arm64 are published to `ghcr.io/richieroxx/content2podcast` (tags `edge`, version, commit sha); see [deploy/README.md](deploy/README.md).

## Podcast players

_To do._ How to subscribe to the feed from common podcast apps, including access over a LAN
IP or Tailscale.

## License

[AGPL-3.0](LICENSE)
