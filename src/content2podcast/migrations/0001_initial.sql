CREATE TABLE sources (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    url              TEXT NOT NULL,
    type             TEXT NOT NULL CHECK (type IN ('rss', 'html')),
    baseline_at      TEXT,
    last_checked_at  TEXT,
    last_success_at  TEXT,
    last_error       TEXT,
    etag             TEXT,
    last_modified    TEXT
);

CREATE TABLE articles (
    id             INTEGER PRIMARY KEY,
    source_id      INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    url            TEXT NOT NULL,
    url_norm       TEXT NOT NULL UNIQUE,
    title          TEXT,
    feed_summary   TEXT,
    published_at   TEXT,
    discovered_at  TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('baseline', 'pending', 'failed', 'skipped', 'processed')),
    content        TEXT,
    extracted_at   TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT
);

CREATE INDEX idx_articles_status ON articles (status);

CREATE TABLE episodes (
    id            INTEGER PRIMARY KEY,
    guid          TEXT NOT NULL UNIQUE,
    mode          TEXT NOT NULL,
    number        INTEGER,
    title         TEXT NOT NULL,
    summary       TEXT,
    script_json   TEXT,
    status        TEXT NOT NULL CHECK (status IN ('draft', 'published', 'pruned')),
    audio_file    TEXT,
    audio_bytes   INTEGER,
    duration_s    REAL,
    created_at    TEXT NOT NULL,
    published_at  TEXT
);

CREATE INDEX idx_episodes_status_published ON episodes (status, published_at);

CREATE TABLE episode_articles (
    episode_id  INTEGER NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    article_id  INTEGER NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('discussed', 'mentioned')),
    PRIMARY KEY (episode_id, article_id)
);
