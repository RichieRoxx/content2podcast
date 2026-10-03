# content2podcast

Turn blog and news articles into German podcast episodes: a self-hosted tool that watches your
sources, lets an LLM write a two-person dialogue about each new article, voices it with
text-to-speech, and publishes the result as a podcast RSS feed you can subscribe to with any
podcast player.

## Features

- **Sources**: RSS/Atom feeds and HTML index pages (CSS selector), with include/exclude filters.
- **Baseline**: the first run of a source only records what is already there; episodes are made
  for articles that appear afterwards.
- **Episodes**: one episode per article (3-10 minutes, no daily limit) or an optional daily
  digest. Every episode links its source articles.
- **Dialogue**: a host (Mia) and an expert (Klaus), written by an LLM with structured output.
- **Audio**: TTS per speaker turn, joined and loudness-normalized with `ffmpeg`.
- **Feed**: static MP3 files and `feed.xml` with configurable retention, ready for any web server.
- **Robust**: failed episodes are retried, an interrupted run resumes, a lock prevents parallel runs.
- **Pluggable providers**: Azure AI Foundry (LLM) and Azure Speech / MAI-Voice (TTS) first, plus
  deterministic `fake` providers for tests and dry runs.
- **Deployment**: systemd timer (bare metal) or Docker / Compose with a built-in scheduler.

## How it works

1. **Discover**: new articles are detected in the sources and de-duplicated.
2. **Extract**: the article text is fetched and reduced to the content
   ([trafilatura](https://trafilatura.readthedocs.io/)); the feed summary is the fallback.
3. **Script**: the LLM turns an article (or a digest) into a dialogue.
4. **Speech**: the TTS provider voices every turn; `ffmpeg` assembles the MP3.
5. **Publish**: the MP3 and `feed.xml` are written to the output directory; old episodes are
   removed according to the retention settings.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (bare metal) or Docker
- [ffmpeg](https://ffmpeg.org/) on `PATH` (already included in the Docker image)
- An [Azure AI Foundry](https://ai.azure.com/) resource with an LLM deployment and Azure Speech
  (the Foundry key can be used for both, see [Providers](#providers))

## Quickstart (try it without any account)

```sh
git clone https://github.com/RichieRoxx/content2podcast && cd content2podcast
uv sync
cp config.example.yaml config.yaml
cp sources.example.yaml sources.yaml
```

In `config.yaml` select the fake providers and point `paths.output_dir` somewhere you like:

```yaml
llm: {provider: fake}
tts: {provider: fake}
```

```sh
uv run podcast doctor          # is everything ready?
uv run podcast check           # look for new articles (records them, the first time as baseline)
uv run podcast run --dry-run   # write scripts only, no speech and no feed
uv run podcast run             # publish episodes
```

For real episodes configure the Azure providers, see below.

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
endpoints) are read from the environment or `.env` only, never from `config.yaml`. A
configuration error is reported as one readable message and exit code 2.

| File | Purpose |
| --- | --- |
| [`config.example.yaml`](config.example.yaml) | Podcast metadata, paths, feed, retention, episode length, voices, audio, schedule, providers; documents every option |
| [`sources.example.yaml`](sources.example.yaml) | One RSS and one HTML source |
| [`.env.example`](.env.example) | Azure credentials and `C2P_` overrides |

### Sources

```yaml
sources:
  - name: Example Blog          # RSS or Atom feed
    url: https://example.com/feed.xml
    type: rss
    include: ["(?i)python"]     # optional: only titles/URLs matching any of these
    exclude: ["(?i)sponsored"]
  - name: Example News          # HTML index page, the selector picks the article links
    url: https://news.example.com/
    type: html
    selector: "article h2 a"
```

An HTML source without `selector` can let the LLM choose the article links: set
`llm_links: true` for the source or `link_extraction.enabled: true` for all of them. The page's
links (same site, with text, outside navigation, at most `link_extraction.max_candidates`) are
sent to the model, which answers with the ones that are articles; the answer is cached per
source and page content, so an unchanged page costs no call. `link_extraction.model` selects a
cheaper model for this task.

Prefer RSS where a site offers it: `podcast sources discover https://example.com/blog` looks for the
feed (`<link rel="alternate">` tags, then `/feed`, `/rss`, `/atom.xml`, ...) and prints a ready-to-paste
entry. `podcast sources list` shows the state of every source,
`podcast sources baseline` marks everything currently visible as seen.

### Episode modes

- `episode.mode: per_article` (default): one episode per new article, length between
  `min_minutes` and `max_minutes` depending on the article (`length_factor` scales it).
- `episode.mode: daily_digest`: at most one episode per day covering up to `max_articles`
  articles with a target length of `target_minutes`.

`max_episodes_per_run`, `max_article_age_days` and `max_chars_per_article` cap a run. Own prompt
templates can be placed in `episode.prompts_dir` (`per_article_de.md`, ...).

### Providers

| Role | `provider` | Needs |
| --- | --- | --- |
| LLM | `azure_foundry` | `AZURE_FOUNDRY_BASE_URL`, `AZURE_FOUNDRY_API_KEY`, deployment name in `llm.model` |
| TTS | `azure_speech` | `AZURE_SPEECH_REGION` (or `tts.region`/`AZURE_SPEECH_ENDPOINT`); `AZURE_SPEECH_KEY`, falling back to the Foundry key |
| both | `fake` | nothing (deterministic output for tests and dry runs) |

```yaml
llm:
  provider: azure_foundry
  model: gpt-5.4              # name of your deployment
tts:
  provider: azure_speech
  region: swedencentral
```

> The Azure providers are tested against mocked HTTP only; which endpoint and key variants work
> against a real resource is tracked in [#74](https://github.com/RichieRoxx/content2podcast/issues/74).
> `podcast doctor --online` sends one minimal request to each provider and shows what fails.

**Adding a provider**: implement the `LLMProvider` or `TTSProvider` interface
(`src/content2podcast/providers/{llm,tts}/base.py`), define an options model (a subclass of
`ProviderOptions`) and register the factory with `@register_llm("name", options=...)` or
`@register_tts(...)`. Use `providers/llm/fake.py` as a template; the registry validates the
options of the selected provider from the config file.

### Costs

Every episode costs one LLM call (input: the article, output: the script) and the speech
synthesis of the script (billed per character). Use `podcast run --dry-run` to see which
articles would be processed and to review the scripts without any speech costs, and
`max_episodes_per_run` to cap a run.

## Commands

| Command | Purpose |
| --- | --- |
| `podcast doctor [--online]` | Check config, sources, secrets (masked), providers, ffmpeg, directories, database |
| `podcast check` | Check the sources and list new articles (they are recorded as pending) |
| `podcast run [--dry-run]` | Fetch, write, voice and publish; exit code 3 if another run is active |
| `podcast script URL...` / `podcast tts script.json` | Tune prompts and voices on single articles |
| `podcast daemon` / `podcast health` | Built-in daily scheduler and its health check (Docker) |
| `podcast sources list\|baseline` | Inspect sources |
| `podcast sources discover URL` | Find the feed of a site (link tags, common paths) and print a `sources.yaml` entry; suggests CSS selectors if there is none |
| `podcast episodes list` | List published episodes |
| `podcast feed rebuild` | Regenerate `feed.xml`, e.g. after changing `feed.base_url` |

`-v` / `-vv` increase logging, `-q` reduces it.

## Deployment

Both variants need a web server for the output directory; see [deploy/README.md](deploy/README.md)
for the details, the Caddy configurations and HTTPS over Tailscale.

- **Bare metal (Debian 12)**: a systemd timer runs `podcast run` every morning, as an
  unprivileged user with a hardened unit. Install steps: [deploy/README.md](deploy/README.md).
- **Docker / Compose**: `deploy/docker-compose.example.yml` runs `podcast daemon` (one run a day
  at `schedule.time`, catch-up after downtime) together with Caddy.

  ```sh
  cp deploy/docker-compose.example.yml docker-compose.yml
  cp deploy/Caddyfile.example .
  mkdir config && cp config.example.yaml config/config.yaml && cp sources.example.yaml config/sources.yaml
  cp .env.example .env                      # add your keys
  # in config/config.yaml: paths are preset by the image, set feed.base_url and the providers
  PODCAST_IMAGE=ghcr.io/richieroxx/content2podcast:edge docker compose up -d
  ```

  Multi-arch images (amd64, arm64) are published to `ghcr.io/richieroxx/content2podcast`
  (`edge` follows `main`, plus version and `sha-` tags).

## Podcast players

`feed.base_url` must be an address your player can reach, for example `http://192.168.1.10:8080`
or `https://<machine>.<tailnet>.ts.net` over [Tailscale](deploy/README.md#https-over-tailscale).
Many popular apps (for example Pocket Casts or Overcast) fetch feeds through their own servers
and therefore cannot reach a feed that only exists on your LAN. Use a player that fetches on the
device, or expose the feed through Tailscale (Funnel makes it public, so think twice), or host
the static files on a public server. Phones usually insist on HTTPS once you leave the home
network. Subscribe with "add by URL" using `<base_url>/feed.xml`.

## Troubleshooting

- Start with `podcast doctor` (add `--online` to test the providers); it explains what is
  missing and exits non-zero if a check fails.
- Logs: `journalctl -u podcast.service -e` (systemd) or `docker compose logs podcast`.
- `podcast health` says why the daemon is considered unhealthy.
- Exit code 3 means another run holds the lock; 2 is a configuration error.
- Nothing is published for a new source: the first run only sets the baseline; episodes follow
  when new articles appear. `podcast sources list` shows the counts.
- The feed contains wrong links: fix `feed.base_url`, then `podcast feed rebuild`.
- A failed episode is retried on the next run; an interrupted episode is resumed.

## Development

```sh
uv sync
uv run pytest             # tests (ffmpeg tests are skipped when ffmpeg is missing)
uv run ruff check && uv run ruff format --check
```

The suite runs offline.

## Notes

Episodes are AI-generated summaries and discussions of articles by other people, voiced by a
synthetic voice. Every episode names and links its sources. Respect the terms of use and
copyright of the sources you add and tell your listeners that the voices are synthetic.

## License

[AGPL-3.0](LICENSE)
