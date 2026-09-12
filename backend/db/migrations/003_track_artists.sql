-- 003: несколько исполнителей у трека (раздел 1.2 контракта V2).
-- tracks.artist_id СОХРАНЯЕТСЯ как основной исполнитель (денормализация ради
-- скорости и совместимости кода V1), track_artists — источник истины для
-- фильтра-пересечения по нескольким исполнителям.

CREATE TABLE IF NOT EXISTS track_artists (
    track_id  INTEGER NOT NULL REFERENCES tracks(id)  ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (track_id, artist_id)
);

CREATE INDEX IF NOT EXISTS idx_track_artists_artist ON track_artists(artist_id);

-- Перенос уже существующих связей: основной исполнитель получает position = 0.
-- Осиротевшие ссылки (tracks.artist_id указывает на несуществующего исполнителя —
-- такое остаётся после правки базы извне с выключенными внешними ключами)
-- отсеиваются проверкой EXISTS: OR IGNORE гасит только конфликты по ключу и
-- CHECK/NOT NULL, но НЕ нарушение внешнего ключа, поэтому без этой проверки
-- миграция падала бы при каждом запуске приложения. Сам tracks.artist_id не
-- трогаем: чинить битую базу должен человек (см. сообщение миграции 002).
INSERT OR IGNORE INTO track_artists (track_id, artist_id, position)
    SELECT t.id, t.artist_id, 0 FROM tracks t
     WHERE t.artist_id IS NOT NULL
       AND EXISTS (SELECT 1 FROM artists a WHERE a.id = t.artist_id);
