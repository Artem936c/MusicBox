-- 001: базовая схема MusicBox (V1).
-- Повторяет backend/db/schema.sql. Для базы, созданной до появления миграций,
-- раннер помечает эту миграцию применённой без выполнения (см. BASELINE_MARKER_TABLE).
-- PRAGMA foreign_keys здесь не трогаем: она включается при подключении и внутри
-- транзакции всё равно не действует.

CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    last_name     TEXT,
    language_code TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id            INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    auto_sort_enabled  INTEGER NOT NULL DEFAULT 1,
    frequent_threshold INTEGER NOT NULL DEFAULT 10,
    rare_min           INTEGER NOT NULL DEFAULT 1,
    rare_max           INTEGER NOT NULL DEFAULT 5,
    fuzzy_threshold    INTEGER NOT NULL DEFAULT 60,
    updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS folders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    normalized_name  TEXT NOT NULL,
    is_artist_folder INTEGER NOT NULL DEFAULT 0,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS artists (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    is_listened     INTEGER NOT NULL DEFAULT 0,
    listened_at     DATETIME,
    folder_id       INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS albums (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    artist_id        INTEGER REFERENCES artists(id) ON DELETE SET NULL,
    year             INTEGER,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, normalized_title, artist_id)
);

CREATE TABLE IF NOT EXISTS tracks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title              TEXT NOT NULL,
    artist             TEXT,
    album              TEXT,
    duration           INTEGER NOT NULL DEFAULT 0,
    file_size          INTEGER NOT NULL DEFAULT 0,
    mime_type          TEXT,
    file_name          TEXT,
    file_id            TEXT NOT NULL,
    file_unique_id     TEXT,
    storage_chat_id    INTEGER,
    storage_message_id INTEGER,
    thumb_file_id      TEXT,
    folder_id          INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    artist_id          INTEGER REFERENCES artists(id) ON DELETE SET NULL,
    album_id           INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    source             TEXT NOT NULL DEFAULT 'upload',
    source_ref         TEXT,
    play_count         INTEGER NOT NULL DEFAULT 0,
    last_played_at     DATETIME,
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, file_unique_id)
);

CREATE TABLE IF NOT EXISTS favourites (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    added_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, track_id)
);

CREATE TABLE IF NOT EXISTS playlists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    added_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (playlist_id, track_id)
);

CREATE TABLE IF NOT EXISTS play_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    track_id  INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    played_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source    TEXT NOT NULL DEFAULT 'web'
);

CREATE INDEX IF NOT EXISTS idx_tracks_user            ON tracks(user_id);
CREATE INDEX IF NOT EXISTS idx_tracks_user_folder     ON tracks(user_id, folder_id);
CREATE INDEX IF NOT EXISTS idx_tracks_user_artist     ON tracks(user_id, artist_id);
CREATE INDEX IF NOT EXISTS idx_tracks_user_album      ON tracks(user_id, album_id);
CREATE INDEX IF NOT EXISTS idx_tracks_user_created    ON tracks(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tracks_user_playcount  ON tracks(user_id, play_count DESC);
CREATE INDEX IF NOT EXISTS idx_favourites_user        ON favourites(user_id, added_at DESC);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_pos    ON playlist_tracks(playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_play_history_user      ON play_history(user_id, played_at DESC);
CREATE INDEX IF NOT EXISTS idx_folders_user           ON folders(user_id);
CREATE INDEX IF NOT EXISTS idx_artists_user           ON artists(user_id);
CREATE INDEX IF NOT EXISTS idx_albums_user            ON albums(user_id);
