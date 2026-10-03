-- Cache of the LLM's article-link choice per HTML source: unchanged candidate list, no new call.
CREATE TABLE link_selections (
    source_id     INTEGER PRIMARY KEY REFERENCES sources (id) ON DELETE CASCADE,
    content_hash  TEXT NOT NULL,
    urls          TEXT NOT NULL,  -- JSON array of the selected URLs, in page order
    created_at    TEXT NOT NULL
);
