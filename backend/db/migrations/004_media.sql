-- 004: типы файлов и жанр (раздел 1.3 контракта V2).
-- file_type ∈ ('audio','document','video','video_note','voice').
-- Раздел «Треки» — file_type = 'audio', раздел «Другое» — все остальные.
-- storage_message_id из V1 не переименовывается.
-- Инструкции ADD COLUMN раннер пропускает, если колонка уже есть
-- (проверка через PRAGMA table_info) — повторный запуск безопасен.

ALTER TABLE tracks ADD COLUMN file_type TEXT NOT NULL DEFAULT 'audio';
ALTER TABLE tracks ADD COLUMN genre TEXT;
ALTER TABLE artists ADD COLUMN total_plays INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_tracks_user_type ON tracks(user_id, file_type);

-- Первичный пересчёт суммарных прослушиваний исполнителя.
UPDATE artists SET total_plays = COALESCE(
    (SELECT SUM(t.play_count) FROM tracks t WHERE t.artist_id = artists.id), 0);
