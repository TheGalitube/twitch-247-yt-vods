-- Twitch247 SQLite Schema
-- Playback state and video catalog

CREATE TABLE IF NOT EXISTS videos (
    video_id                TEXT PRIMARY KEY,
    title                   TEXT NOT NULL,
    duration                INTEGER NOT NULL DEFAULT 0,
    upload_date             TEXT,
    played_status           TEXT NOT NULL DEFAULT 'unplayed'
                            CHECK (played_status IN ('unplayed', 'playing', 'played')),
    current_position_seconds REAL NOT NULL DEFAULT 0.0,
    last_played_timestamp   TEXT,
    discovered_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_videos_played_status ON videos (played_status);
CREATE INDEX IF NOT EXISTS idx_videos_upload_date ON videos (upload_date);
CREATE INDEX IF NOT EXISTS idx_videos_last_played ON videos (last_played_timestamp);

CREATE TABLE IF NOT EXISTS playback_state (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    current_video_id        TEXT,
    current_position_seconds REAL NOT NULL DEFAULT 0.0,
    stream_started_at       TEXT,
    last_save_at            TEXT,
    uptime_started_at       TEXT NOT NULL DEFAULT (datetime('now')),
    last_error              TEXT,
    last_error_at           TEXT,
    is_streaming            INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (current_video_id) REFERENCES videos (video_id)
);

INSERT OR IGNORE INTO playback_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at   TEXT NOT NULL DEFAULT (datetime('now')),
    new_videos  INTEGER NOT NULL DEFAULT 0,
    total_videos INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    message     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
