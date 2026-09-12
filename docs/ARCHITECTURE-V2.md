# MusicBox V2 — контракт доработки

Дополнение к `docs/ARCHITECTURE.md` (далее — V1). V1 остаётся в силе: имена, схемы и
маршруты оттуда НЕ меняются, если здесь явно не сказано обратное.

Корень: `C:\Users\Bolshakov\CurrentProject\MusicBox`

## 0. Что уже реализовано в V1 (НЕ переделывать)

- Папки (плоские), загрузка аудио с метаданными и автосортировкой по исполнителю.
- Нечёткий поиск (rapidfuzz + смена раскладки) по трекам, альбомам, исполнителям, папкам.
- Исполнители с отметкой «прослушано», альбомы, плейлисты с drag-and-drop, избранное.
- **Статистика с полными списками треков** (5 разделов, пагинация по 10, кнопки
  «Прослушать» / «В плейлист» / «В избранное») — и в боте, и в Mini App.
- Хранение файлов в приватном канале, стриминг с Range и подписанными токенами.
- **Mini App уже существует**: React 18 + Vite, HashRouter, 13 страниц, `PlayerContext`
  с `POST /tracks/{id}/play` на каждый трек, валидация `initData`.
- Удаление трека из БД и канала: `DELETE /api/tracks/{id}?delete_from_channel=true`
  и кнопка «🗑 Удалить» в карточке трека в боте.

## 1. Схема БД: миграции

Все миграции — в `backend/db/migrations/` (новый пакет), применяются из
`backend.db.database.apply_migrations()` по порядку номеров, идемпотентно, с таблицей
учёта `schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at DATETIME)`.
**Существующие данные обязаны сохраниться** (в рабочей базе уже есть треки, папки и плейлисты).

Итоговый состав: `001_baseline.sql`, `002_nested_folders.py`, `003_track_artists.sql`,
`004_media.sql`, `005_notes.sql`. Раннер понимает два вида файлов: `.sql` (выполняется
через `executescript`; инструкции `ALTER TABLE … ADD COLUMN` вырезаются, если колонка
уже есть — иначе SQLite падает при повторном запуске) и `.py` (модуль с
`async def migrate(conn)`; флаг `ATOMIC = False` означает, что транзакцией управляет
сама миграция). Базу, созданную до появления миграций, раннер узнаёт по таблице
`tracks` и помечает 001 применённой, не выполняя её.

**Миграция 002 необратима**: таблица `folders` пересоздаётся. Перед обновлением
рабочей базы нужен бэкап файла `data/musicbox.db`.

### 1.1. Вложенные папки + разделы (`002_nested_folders.py`)

У `folders` в V1 стоит inline-ограничение `UNIQUE (user_id, normalized_name)`, которое
в SQLite нельзя снять без пересоздания таблицы. Миграция написана целиком на Python
(`ATOMIC = False`): ей нужно переключать `PRAGMA foreign_keys` вокруг транзакции, а
внутри транзакции этот PRAGMA не действует. Она выполняет rebuild:

```sql
CREATE TABLE folders_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    normalized_name  TEXT NOT NULL,
    parent_folder_id INTEGER REFERENCES folders_new(id) ON DELETE CASCADE,
    section          TEXT NOT NULL DEFAULT 'music',   -- music | other
    is_artist_folder INTEGER NOT NULL DEFAULT 0,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, section, parent_folder_id, normalized_name)
);
INSERT INTO folders_new (id, user_id, name, normalized_name, parent_folder_id,
                         section, is_artist_folder, created_at)
    SELECT id, user_id, name, normalized_name, NULL, 'music', is_artist_folder, created_at
    FROM folders;
DROP TABLE folders;
ALTER TABLE folders_new RENAME TO folders;
CREATE INDEX IF NOT EXISTS idx_folders_user       ON folders(user_id);
CREATE INDEX IF NOT EXISTS idx_folders_parent     ON folders(user_id, parent_folder_id);
CREATE INDEX IF NOT EXISTS idx_folders_section    ON folders(user_id, section);
```

ВАЖНО: перед rebuild выключить `PRAGMA foreign_keys` (иначе `DROP TABLE` порвёт ссылки
из `tracks.folder_id` и `artists.folder_id`), включить обратно — уже после выхода из
транзакции. Rebuild — внутри одной транзакции, и `PRAGMA foreign_key_check` выполняется
ВНУТРИ неё, до `COMMIT`: иначе перестройка с нарушениями осталась бы зафиксированной на
диске. Результат сверяется со снимком нарушений, снятым ДО rebuild: `foreign_key_check`
сканирует всю базу, поэтому застарелые чужие нарушения (например в `play_history`) идут
предупреждением в журнал, а не выдаются за последствие миграции. Идентификаторы папок
сохраняются — на них ссылаются `tracks.folder_id` и `artists.folder_id`.

Ограничение: `UNIQUE` с `parent_folder_id IS NULL` в SQLite не ловит дубли корневых папок
(NULL != NULL). Поэтому уникальность корневых папок проверяется в коде репозитория
(`find_folder_by_name` перед вставкой) — это зафиксировано и покрыто тестом.

### 1.2. Несколько исполнителей у трека (`003_track_artists.sql`)

```sql
CREATE TABLE IF NOT EXISTS track_artists (
    track_id  INTEGER NOT NULL REFERENCES tracks(id)  ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL DEFAULT 0,   -- 0 = основной исполнитель
    PRIMARY KEY (track_id, artist_id)
);
CREATE INDEX IF NOT EXISTS idx_track_artists_artist ON track_artists(artist_id);

INSERT OR IGNORE INTO track_artists (track_id, artist_id, position)
    SELECT t.id, t.artist_id, 0 FROM tracks t
     WHERE t.artist_id IS NOT NULL
       AND EXISTS (SELECT 1 FROM artists a WHERE a.id = t.artist_id);
```

Проверка `EXISTS` обязательна: `INSERT OR IGNORE` гасит конфликт по ключу и
CHECK/NOT NULL, но НЕ нарушение внешнего ключа. Без неё осиротевший `tracks.artist_id`
(остаётся после правки базы извне с выключенными внешними ключами) ронял бы миграцию
при каждом запуске приложения. Сам `tracks.artist_id` не трогаем — чинить битую базу
должен человек.

`tracks.artist_id` СОХРАНЯЕТСЯ как основной исполнитель (денормализация ради скорости
и совместимости всего кода V1). `track_artists` — источник истины для фильтра-пересечения.
При создании трека заполняются обе структуры: `metadata.split_artists` даёт список,
первый становится `artist_id` и `position=0`, остальные — `position=1,2,…`.

### 1.3. Типы файлов и жанр (`004_media.sql`)

```sql
ALTER TABLE tracks  ADD COLUMN file_type TEXT NOT NULL DEFAULT 'audio';
ALTER TABLE tracks  ADD COLUMN genre     TEXT;
ALTER TABLE artists ADD COLUMN total_plays INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_tracks_user_type ON tracks(user_id, file_type);
UPDATE artists SET total_plays = COALESCE(
    (SELECT SUM(t.play_count) FROM tracks t WHERE t.artist_id = artists.id), 0);
```

`file_type ∈ ('audio','document','video','video_note','voice')`.
Раздел «Треки» = `file_type='audio'`; раздел «Другое» = все остальные.
`storage_message_id` из V1 — это и есть `message_id` из ТЗ, переименования НЕ делаем.

Первичный `UPDATE artists SET total_plays = …` в самой миграции считает только по
`tracks.artist_id` (основной исполнитель): `track_artists` к этому моменту уже есть, но
смысл колонки задаётся рабочим кодом, а он пересчитывает `total_plays` ПО УЧАСТИЮ —
`artists.recalc_total_plays()` (см. раздел 2). Расхождение снимается первым же
пересчётом и не влияет на данные.

### 1.4. Заметки (`005_notes.sql`)

```sql
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS note_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id    INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    is_done    INTEGER NOT NULL DEFAULT 0,
    position   INTEGER NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_notes_user      ON notes(user_id);
CREATE INDEX IF NOT EXISTS idx_note_items_note ON note_items(note_id, position);
```

## 2. Репозитории — новые и изменённые функции

### folders.py (изменения)
```python
async def create_folder(user_id, name, *, parent_folder_id=None, section="music",
                        is_artist_folder=False) -> dict
    # идемпотентно: тёзка на том же уровне возвращается, а не дублируется
    # раздел ПОДПАПКИ всегда наследуется от родителя (аргумент section игнорируется)
async def list_folders(user_id, *, parent_folder_id=UNSET, section="music",
                       include_counts=True) -> list[dict]
    # parent_folder_id=UNSET — все папки раздела; None — только корневые; N — дети N
    # section=None — снять фильтр по разделу (этим живёт режим section=all в API)
    # + has_children, track_count (собственный), total_track_count (с подпапками),
    #   depth и path («Рок / Русский рок»)
    # порядок при UNSET — обход дерева (подпапка сразу за родителем), а не алфавит;
    # на плоских данных V1 это тот же алфавитный список, что и раньше
async def folder_tree(user_id, *, section="music") -> list[dict]   # вложенный список children[]
async def folder_path(user_id, folder_id) -> list[dict]            # хлебные крошки от корня
async def descendant_ids(user_id, folder_id, *, include_self=True) -> list[int]
    # рекурсивный CTE: WITH RECURSIVE sub(id) AS (...)
async def move_folder(user_id, folder_id, new_parent_id: int | None) -> dict
    # ЗАПРЕТ цикла: new_parent не может быть потомком folder_id -> ValidationError
    # так же запрещены переезд в другой раздел, выход за MAX_FOLDER_DEPTH и тёзка
async def delete_folder(user_id, folder_id, *, delete_tracks=False, recursive=True) -> bool
    # recursive=False у папки с подпапками -> ValidationError (в API 400):
    # иначе каскад по parent_folder_id тихо унёс бы всё поддерево
async def folder_track_count(user_id, folder_id, *, recursive=False) -> int
MAX_FOLDER_DEPTH = 16   # защита от бесконечной вложенности
```

### tracks.py (изменения)
```python
# TRACK_COLUMNS дополняется: t.file_type, t.genre
DEFAULT_FILE_TYPE = "audio"                                          # раздел «Треки»
NON_AUDIO_FILE_TYPES = ("document", "video", "video_note", "voice")  # раздел «Другое»

async def create_track(..., file_type="audio", genre=None, artist_ids: Sequence[int] | None = None)
    # заполняет track_artists (основной = artist_id, position 0); у дубликата по
    # (user_id, file_unique_id) состав исполнителей НЕ перезаписывается
async def set_track_artists(user_id, track_id, artist_ids: Sequence[int]) -> None
    # полная замена состава; синхронизирует денормализованный tracks.artist_id
async def track_artists(user_id, track_id) -> list[dict]
async def list_tracks(..., file_type: Any = "audio", folder_recursive: bool = False)
    # file_type=None — без фильтра; кортеж типов — раздел «Другое»;
    # folder_recursive=True — вместе с подпапками (через descendant_ids)
async def count_tracks(...)   # те же фильтры, что и у list_tracks
async def tracks_by_artists_intersection(user_id, artist_ids: Sequence[int], *,
                                         query=None, limit=50, offset=0,
                                         order="created_at_desc") -> list[dict]
    # HAVING COUNT(DISTINCT ta.artist_id) = len(artist_ids)
    # query — фильтр ПО ПОДСТРОКЕ (не нечёткий) поверх найденного множества
async def rename_track(user_id, track_id, new_title: str) -> dict | None
async def search_in(user_id, query, *, scope: str, limit=30, threshold: int | None = None)
    # scope: tracks | favourites | other; в каждом dict появляется score 0..100
```

### artists.py (изменения)
```python
async def rename_artist(user_id, artist_id, new_name: str) -> dict | None   # + слияние при конфликте
async def set_folder(user_id, artist_id, folder_id: int | None) -> dict | None
async def artist_tracks(user_id, artist_id, *, order=..., limit=50, offset=0) -> list[dict]
async def artist_unplayed_tracks(user_id, artist_id, limit=50, *, offset=0) -> list[dict]
async def recalc_total_plays(user_id, artist_id=None) -> None
async def bump_total_plays(user_id, artist_id, amount=1) -> None   # инкремент при прослушивании
async def global_artist_stats() -> list[dict]
    # кросс-пользовательская агрегация по normalized_name (единственный запрос
    # репозитория без фильтра по user_id; чужие user_id наружу не отдаются):
    # {normalized_name, name, total_plays, listeners}
```

**Семантика «трек исполнителя» — УЧАСТИЕ, а не только основной исполнитель.**
`ARTIST_COLUMNS` (`track_count`, `play_count`), `artist_tracks`,
`artist_unplayed_tracks`, `count_artist_tracks` и пересчёт `total_plays` отбирают треки
условием «`tracks.artist_id = <исполнитель>` ИЛИ есть строка в `track_artists`».
Приглашённый исполнитель видит свои треки так же, как основной, и `track_count` в
карточке всегда совпадает с длиной списка треков. Считается коррелированными
подзапросами, а не JOIN к `track_artists`: иначе трек с несколькими связями
учитывался бы дважды.

### notes.py (новый)
```python
async def create_note(user_id, title) -> dict
async def list_notes(user_id) -> list[dict]                # + items_total, items_done
async def get_note(user_id, note_id) -> dict | None        # + items: list[dict]
async def rename_note(user_id, note_id, title) -> dict | None
async def delete_note(user_id, note_id) -> bool
async def add_item(user_id, note_id, text) -> dict         # в конец, позиции 1..N
async def toggle_item(user_id, note_id, item_id) -> dict | None
async def set_item_done(user_id, note_id, item_id, is_done: bool) -> dict | None
async def update_item(user_id, note_id, item_id, text) -> dict | None
async def delete_item(user_id, note_id, item_id) -> bool   # + перенумерация
async def reorder_items(user_id, note_id, ordered_item_ids) -> bool
```

### recommendations.py (новый репозиторий-запросник)
```python
async def played_artist_names(user_id, *, limit=2000) -> set[str]   # normalized_name
async def user_genres(user_id, *, limit=50) -> dict[str, int]
    # нормализованный жанр -> сумма tracks.play_count; жанры без прослушиваний
    # остаются в конце со значением 0
async def co_listener_artists(user_id, limit=200) -> list[dict]
    # исполнители пользователей, у которых есть общие с нами прослушанные исполнители
async def artists_by_genre(genres: Sequence[str], exclude: set[str], limit=200,
                           *, user_id: int | None = None) -> list[dict]
```

## 3. Сервисы

### services/recommendations.py (новый)
```python
@dataclass(slots=True)
class Recommendation:
    name: str; normalized_name: str; total_plays: int; listeners: int
    reason: str            # «жанр: rock», «слушают вместе с «Кино»», «из вашей библиотеки»,
                           # «слушают те, у кого похожие вкусы» (соседи есть, но общий
                           # исполнитель не определился), «часто слушают другие
                           # пользователи» (ни жанра, ни соседей — осталась популярность)
    local_artist_id: int | None    # если исполнитель уже есть в библиотеке пользователя
    sample_track_ids: list[int]

async def build(user_id: int, *, limit: int = 15) -> dict
    # {"popular": [Recommendation...], "underground": [...],
    #  "shortfall": {"popular": int, "underground": int}, "note": str | None}
```
Алгоритм (реализовать полностью):
1. `played = played_artist_names(user_id)`; если пусто — вернуть «нет данных» с подсказкой.
2. `genres = user_genres(user_id)` — топ-5 жанров пользователя.
3. Кандидаты: `co_listener_artists` (коллаборативная фильтрация) ∪ `artists_by_genre`,
   с исключением всего, что уже в `played`.
4. Скор: `3*совпадение_жанра + 2*число_общих_слушателей + 1*нормированная_популярность`.
5. Две непересекающиеся категории строятся ИЗ ОДНОГО отсортированного списка
   кандидатов с `total_plays > 0`: верхняя часть (по `total_plays DESC`) — `popular`,
   остаток (пересортированный по `total_plays ASC`) — `underground`. Граница —
   `limit`, если кандидатов хватает на две полные категории, иначе примерно половина:
   при делении «просто по порядку» первая категория забрала бы всех, а вторая осталась
   бы пустой. Кандидат, которого в базе ещё никто не слушал (`total_plays = 0`), не
   попадает НИ В ОДНУ категорию: «Менее известные» по контракту требуют
   `total_plays > 0`, а в «Популярных» такой исполнитель выдавал бы себя за находку.
   Освободившиеся места уходят шагу 6.
6. **Деградация (обязательна):** если кандидатов меньше `limit`, добить непрослушанными
   исполнителями из библиотеки самого пользователя (`reason="из вашей библиотеки"`),
   а в `shortfall`/`note` честно сообщить, сколько не хватило и почему
   («в базе пока мало пользователей и прослушиваний»). НЕ выдумывать исполнителей
   и НЕ обращаться во внешние сервисы.

### services/media.py (новый)
```python
FILE_TYPES = ("audio", "document", "video", "video_note", "voice")
FILE_TYPE_LABELS / FILE_TYPE_ICONS                   # русские названия и эмодзи разделов
def detect_file_type(message) -> str | None          # по наличию audio/document/video/video_note/voice
def is_audio_document(document) -> bool              # mime_type audio/* или расширение
def normalize_file_type(value) -> str                # мусор -> "audio"
async def store_media(bot, message, *, title=None) -> StoredMedia
    # расширение storage.store_from_message на не-аудио: send_document / send_video /
    # send_video_note / send_voice в канал-хранилище, возврат file_id из канала
async def send_media_to_user(bot, chat_id, track: dict, **kwargs)
    # по file_type выбирает send_audio / send_document / send_video / send_video_note / send_voice
```
Аудио отправляется КАК ЕСТЬ, с исходным `mime_type` — конвертации нет (п. 14, 18 ТЗ).

### services/recognition.py (новый, заглушка по ТЗ п. 7)
```python
class RecognitionError(MusicBoxError): ...            # сбой сети или ошибка провайдера
class RecognitionUnavailable(RecognitionError): ...   # провайдер не выбран / нет ключей
@dataclass(slots=True)
class RecognitionResult:
    artist: str | None; title: str | None; provider: str; confidence: float

PROVIDERS = ("audd", "acrcloud", "genius")
MAX_SAMPLE_BYTES = 1_048_576                # фрагмент аудио обрезается до 1 МБ
def current_provider() -> str               # пустая строка = распознавание выключено
def is_configured(provider: str | None = None) -> bool
    # провайдер выбран И его ключи заполнены: AUDD_API_TOKEN / GENIUS_ACCESS_TOKEN,
    # для ACRCloud нужны все три (ACRCLOUD_HOST, ACRCLOUD_KEY, ACRCLOUD_SECRET).
    # Хендлерам удобнее спросить заранее, чем ловить исключение.
async def recognize(audio_bytes: bytes | None = None, *, hint: str | None = None) -> RecognitionResult
    # Если settings.recognition_provider не задан -> RecognitionUnavailable с русским текстом.
    # Реализованы подготовленные HTTP-запросы к AudD, ACRCloud (HMAC-подпись) и Genius
    # (поиск по названию — по ЗВУКУ Genius не ищет), но БЕЗ ключей они не выполняются.
    # Код честно помечен как заглушка в docstring.
```
Новые настройки: `recognition_provider: str = ""`, `audd_api_token: str = ""`,
`acrcloud_host/key/secret: str = ""`, `genius_access_token: str = ""`.

### services/search.py (изменения)
```python
async def search_all(user_id, query, limit=20, threshold=None, *,
                     artist_ids: Sequence[int] | None = None,
                     section: str = "music") -> dict
    # limit и threshold остались позиционными — вызовы V1 вида search_all(uid, q, 20)
    # продолжают работать; artist_ids и section — только по имени
    # если artist_ids заданы — сначала пересечение по исполнителям, потом нечёткий поиск
    # внутри полученного множества; альбомы сужаются до альбомов отобранных треков,
    # а список исполнителей — до самого фильтра; папки ищутся всегда (п. 17 ТЗ)
    # пустой запрос С фильтром отдаёт все треки выбранных исполнителей
```

### services/batch_sort.py (новый, ТЗ п. 8)
```python
@dataclass(slots=True)
class BatchState:
    user_id: int; track_ids: list[int]; pending: list[int]; assigned: dict[int, int | None]
    # pending — ещё не распределённые, assigned — track_id -> folder_id,
    # где None означает осознанное «оставлен без папки», а не «не обработан».
    # __post_init__ достраивает pending по assigned: состояние возвращается из
    # хранилища FSM, где ключи стали строками, и ни один трек пачки не должен потеряться

async def group_by_artist(user_id, track_ids) -> dict[int | None, list[int]]
    # ключ None — треки без исполнителя, такая группа идёт последней
def select_batch(track_ids, limit: int | None = None) -> list[int]   # первые limit штук
async def assign(user_id, track_ids, folder_id, *, limit: int | None = None) -> int
    # limit — «указать количество треков для добавления в папку» (ТЗ п. 8.4)
async def assign_state(state: BatchState, folder_id, *, limit=None) -> list[int]
    # то же, но сразу двигает pending/assigned; возвращает ушедшие в папку id
    # (user_id берётся из состояния, отдельным аргументом не передаётся)
async def remaining_summary(user_id, state: BatchState) -> str
```

## 4. API — новые эндпоинты (все с `Depends(get_current_user)`)

**folders** (расширение)
- `GET /folders?parent_id=&section=music` — плоский список. `parent_id` НЕ передан —
  весь раздел (поведение V1), `parent_id=0` — только корневые, `parent_id=N` — дети
  папки N (нет такой папки → 404). `section` ∈ `music` | `other` | `all`.
- `GET /folders/tree?section=music` → дерево (те же три значения `section`)
- `GET /folders/{id}/path` → `{folder_id, section, items: [FolderOut, …]}` — крошки от корня
- `POST /folders` — тело `FolderCreateIn(name, parent_folder_id=None, section="music")`;
  повтор с тем же именем на том же уровне вернёт существующую папку (201)
- `POST /folders/{id}/move` — `{"parent_folder_id": int|null}`; цикл, чужой раздел,
  выход за `MAX_FOLDER_DEPTH` и тёзка на новом уровне → 400
- `DELETE /folders/{id}?delete_tracks=false&recursive=true` — `recursive=false`
  у папки с подпапками → 400
- `GET /folders/{id}/tracks?recursive=false&file_type=all&order=&limit=&offset=` —
  по умолчанию только свои файлы и ВСЕ типы: так же, как в V1
- `GET /folders/{id}/play?recursive=true` → очередь треков для плеера (ТЗ п. 16):
  только `file_type='audio'`, прослушивания здесь НЕ засчитываются — их регистрирует
  `POST /tracks/{id}/play` в момент фактического воспроизведения

**tracks**
- `GET /tracks?file_type=audio&q=&folder_id=&artist_id=&album_id=&order=&limit=&offset=` —
  раздел «Треки» + поиск (ТЗ п. 11, 12). `file_type` по умолчанию `audio`; `other` —
  весь раздел «Другое», `all` — без фильтра, либо один конкретный тип.
  С `q` порядок задаёт релевантность, а `order` игнорируется.
- `PATCH /tracks/{id}` — уже есть, добавлены `title` и `artist_ids` (первый id —
  основной исполнитель, `tracks.artist_id` синхронизируется). Все проверки идут ДО
  первой записи: поля, состав исполнителей и название пишутся тремя транзакциями,
  и падение на последней оставило бы трек изменённым наполовину.
- `DELETE /tracks/{id}?delete_from_channel=true` — есть
- `GET /tracks/play_all?order=&limit=&offset=` → очередь всех аудио пользователя
  (ТЗ п. 16). Маршрут объявлен ДО `/{track_id}`: FastAPI берёт первый подошедший путь
  по порядку регистрации, и при обратном порядке `/tracks/play_all` ушёл бы в карточку
  трека и вернул 422.

**artists**
- `GET /artists?q=&only_listened=&limit=` — поиск (ТЗ п. 12); без `q` — весь список
  в алфавитном порядке, `limit` действует только вместе с `q`
- `PATCH /artists/{id}` — `{"name": str}` переименование (ТЗ п. 10); если имя совпало
  с другим исполнителем — СЛИЯНИЕ (треки, связи `track_artists` и альбомы переезжают)
- `POST /artists/{id}/folder` — `{"folder_id": int|null}` (ТЗ п. 8)
- `GET /artists/{id}/unplayed?limit=&offset=` (ТЗ п. 2)
- `POST /artists` — ручное создание (ТЗ п. 19), идемпотентно: новый — 201,
  уже существовавший — 200 (и при необходимости перепривязывается к папке)
- `GET /artists/{id}/tracks` — как и `track_count`, считает УЧАСТИЕ (см. раздел 2)

**favourites**: `GET /favourites?q=` (ТЗ п. 12)

**search**: `GET /search?q=&artist_ids=1,2,3&section=` (ТЗ п. 1, 17) — `artist_ids`
передаётся ОДНОЙ строкой через запятую, а не повторяющимся параметром; `section`
(`music` | `other`) влияет на поиск папок, а папки ищутся всегда, в том числе при
активном фильтре по исполнителям.

**recommendations** (новый роутер): `GET /recommendations?limit=15` →
`{popular: [...], underground: [...], shortfall: {popular, underground}, note}`

**notes** (новый роутер)
- `GET /notes`, `POST /notes`, `GET /notes/{id}`, `PATCH /notes/{id}`, `DELETE /notes/{id}`
- `POST /notes/{id}/items`, `PATCH /notes/{id}/items/{item_id}`,
  `DELETE /notes/{id}/items/{item_id}`, `PUT /notes/{id}/order`
- Все изменения пунктов возвращают заметку ЦЕЛИКОМ (`NoteDetailOut`), а не изменённый
  пункт: клиенту всё равно нужны пересчитанные позиции и счётчики `items_total` /
  `items_done`, и лишний GET после каждого клика не нужен. Позиции — 1..N, после
  удаления пункта они перенумеровываются.

**other** (новый роутер, ТЗ п. 13)
- `GET /other?folder_id=&q=&file_type=&recursive=&order=&limit=&offset=` — файлы
  не-аудио. `folder_id=0` — только файлы без папки, параметр не передан — все.
- `GET /other/folders`, `POST /other/folders` (раздел `section='other'`)
- `GET /other/{id}/download?token=&inline=false` — отдача файла потоком с Range.
  Авторизация — тем же подписанным stream-токеном, что и `GET /tracks/{id}/stream`:
  `<a download>` и `<video>` не умеют слать заголовок `X-Telegram-Init-Data`.

**playlists**: `POST /playlists/{id}/tracks` — уже есть (ТЗ п. 20)

Схемы: `TrackOut` дополняется `file_type`, `genre`, `artists: list[ArtistBriefOut]`.
`FolderOut` дополняется `parent_folder_id`, `section`, `has_children`, `total_track_count`;
`FolderTreeOut` = `FolderOut` + `children[]`, `FolderPathOut` = `{folder_id, section, items[]}`.
`depth` и `path`, которые считает репозиторий, в схему намеренно не выведены — Mini App
строит навигацию по `parent_folder_id` и крошкам.

Раздел «Другое» отдаёт тот же `TrackOut` (в нём уже есть `file_type` и `stream_url`),
поэтому один и тот же компонент карточки работает в обоих разделах. Отдельная схема
`OtherFileOut` (с `icon`, `file_type_label` и `download_url`) в `schemas.py` описана,
но роутером пока не используется.

## 5. Бот — новые команды и хендлеры

Новые команды (добавлены в `texts.COMMANDS`; итого 31 = 16 из V1 + 15 новых):
```
/tracks            — раздел «Треки» (только аудио, сортировка по дате)
/tracks_search     — поиск по трекам
/artists_search    — поиск по исполнителям
/favourites_search — поиск по избранному
/other             — раздел «Другое» (документы, видео, кружочки, голосовые)
/notes             — заметки со списками
/recommendations   — рекомендации
/play_all          — воспроизвести все треки
/play_folder       — воспроизвести папку (с подпапками)
/create_folder     — создать папку
/create_playlist   — создать плейлист
/create_artist     — создать исполнителя
/edit_track        — переименовать трек
/edit_artist       — переименовать исполнителя
/delete_track      — удалить трек (из БД и канала, с подтверждением)
```

Новые модули хендлеров (каждый экспортирует `router: Router`):
`tracks.py`, `other.py`, `notes.py`, `recommendations.py`, `edit.py`, `batch.py`.
Порядок в `HANDLER_MODULES`: start, settings, stats, tracks, folders, artists,
playlists, favourites, notes, recommendations, other, edit, batch, search, tg_search, upload.

Новые callback-фабрики в `callbacks.py` (следить за лимитом 64 байта):
```python
class NoteCB(CallbackData, prefix="nt"):    action: str; note_id: int; item_id: int; page: int
class RecoCB(CallbackData, prefix="rc"):    action: str; kind: str; page: int
class OtherCB(CallbackData, prefix="ot"):   action: str; folder_id: int; track_id: int; page: int
class EditCB(CallbackData, prefix="ed"):    action: str; kind: str; target_id: int
class BatchCB(CallbackData, prefix="bt"):   action: str; folder_id: int; count: int
class ArtistPickCB(CallbackData, prefix="ap"): action: str; artist_id: int; page: int
```

Поведение, требующее внимания:
- **Множественный выбор исполнителей** (`/search`): FSM-состояние с накоплением
  `selected_artist_ids`, клавиатура с «✅»-отметками, кнопка «Готово».
- **Пакетное распределение** (`/upload` нескольких файлов): после загрузки группы
  показать «Остались нераспределённые (N). Создать новую папку или добавить в существующую?»,
  поддержать ввод количества, повторять до полного распределения.
- **`/play_all` и `/play_folder`**: отправка пачками не более 10 треков
  (`PLAY_BATCH`) с `asyncio.sleep(0.3)` (`PLAY_DELAY`) между ними и кнопкой «Ещё 10».
  На КАЖДЫЙ отправленный трек — `register_play`, но источники РАЗНЫЕ:
  `source="bot_play_all"` у `/play_all` и `source="bot_play_folder"` у `/play_folder`,
  иначе в истории прослушиваний два разных сценария было бы не отличить.

## 6. Mini App — новые страницы

Добавить в роутер (`App.jsx`):
```
/tracks              TracksPage        — только аудио, поиск, плеер, «Воспроизвести всё»
/other               OtherPage         — документы/видео/кружочки, вложенные папки
/notes               NotesPage         — список заметок
/notes/:id           NotePage          — чекбоксы + drag-and-drop пунктов
/recommendations     RecommendationsPage — карточки: «Популярные» / «Менее известные»
```
Изменения существующих:
- `FoldersPage` — древовидная структура (раскрытие узлов, хлебные крошки, перемещение).
- `SearchPage` — мультивыбор исполнителей (чипы с «×»), поиск папок в результатах.
- `ArtistsPage`/`ArtistPage` — поиск, переименование, привязка к папке, непрослушанные треки.
- `FavouritesPage` — строка поиска.
- `TabBar` — 5 вкладок остаются, остальные разделы доступны с главной и из «ещё».
- Все карточки трека получают «✏️ Переименовать» и «🗑 Удалить» (с подтверждением).

`api/client.js` дополняется: `notes.*`, `recommendations.get()`, `other.*`,
`tracks.playAll()`, `folders.tree/path/move/play`, `artists.rename/setFolder/unplayed/create`,
`search.all(q, {artist_ids, section})`.

## 7. Аватарка бота (ТЗ п. 6)

В Bot API **нет** метода установки фото профиля бота (`setMyPhoto` не существует;
есть только `setMyName` / `setMyDescription` / `setMyShortDescription`).
Поэтому:
1. Скрипт `scripts/make_avatar.py` генерирует PNG 512×512 со скрипичным ключом
   на градиентном фоне. Он работает на ЧИСТОЙ стандартной библиотеке: геометрия
   описана в коде, растеризатор свой. Pillow не обязателен — если он установлен,
   используется только ради более мягкого сглаживания.
2. Файлы кладутся в `assets/bot_avatar.png` и `assets/bot_avatar.svg` (тот же рисунок
   в векторе — пригодится для других размеров).
3. В README — раздел: @BotFather → `/setuserpic` → выбрать бота → отправить файл.
4. Дополнительно скрипт `scripts/setup_profile.py` ставит через API то, что можно:
   `setMyName("MusicBox")`, `setMyShortDescription`, `setMyDescription`.

## 8. Тесты (обязательно)

`tests/test_migrations.py` — миграции на копии боевой базы не теряют данные;
`tests/test_nested_folders.py` — дерево, крошки, рекурсивный обход, запрет цикла;
`tests/test_multi_artist.py` — пересечение по нескольким исполнителям;
`tests/test_notes.py` — CRUD и порядок пунктов;
`tests/test_recommendations.py` — две категории, отсутствие пересечений, деградация;
`tests/test_media.py` — file_type, разделение «Треки» / «Другое».

## 9. Что изменилось по сравнению с первоначальным планом

Раздел собран по итогам реализации: ниже только те места, где код осознанно разошёлся
с тем, что было записано выше, и почему. Всё остальное сделано как задумано.

### Схема и миграции

1. **`002_nested_folders` — не SQL, а Python** (`.py`, `ATOMIC = False`). Перестройка
   таблицы требует переключать `PRAGMA foreign_keys` вокруг транзакции, а внутри
   транзакции этот PRAGMA не работает — значит, транзакцией должна управлять сама
   миграция, чего `.sql`-раннер не умеет.
2. **`PRAGMA foreign_key_check` выполняется ВНУТРИ транзакции**, а не после неё
   (в плане было «после — включить и выполнить»). Проверка после `COMMIT` уже ничего
   не спасает: битая перестройка была бы записана на диск. Результат сверяется со
   снимком нарушений, снятым ДО rebuild, — иначе чужие застарелые нарушения в других
   таблицах выглядели бы как последствие миграции папок.
3. **Появилась `001_baseline.sql`**, которой не было в плане: раннеру нужна стартовая
   точка. Для базы, созданной до системы миграций, она помечается применённой без
   выполнения (признак — наличие таблицы `tracks`).
4. **В `003` добавлена проверка `EXISTS`** при переносе связей в `track_artists`:
   `INSERT OR IGNORE` гасит конфликт по ключу, но НЕ нарушение внешнего ключа. Без
   проверки осиротевший `tracks.artist_id` (следствие правки базы извне с выключенными
   FK) ронял бы миграцию при каждом запуске приложения.
5. **`schema_migrations` получила колонку `name`** — по одному номеру в журнале
   непонятно, что именно применилось.

### Семантика данных

6. **«Трек исполнителя» = УЧАСТИЕ, а не только основной исполнитель.** `track_count`,
   `play_count`, список треков, непрослушанные и пересчёт `total_plays` считают треки,
   где исполнитель либо основной (`tracks.artist_id`), либо связан через
   `track_artists`. Иначе приглашённый исполнитель показывал бы `track_count = 0` при
   непустом списке треков, а карточка противоречила бы сама себе. Реализовано
   коррелированными подзапросами, а не JOIN, чтобы трек с несколькими связями
   не удваивался.
7. **`GET /folders` без `parent_id` отдаёт ВЕСЬ раздел, а не корни.** Так работал V1, и
   ломать его вызовы не стали. «Только корневые» — это явный `parent_id=0` (значение,
   которое всё равно не может быть id папки), `parent_id=N` — дети N. Плоский список
   всего раздела при этом идёт в порядке обхода дерева, а не по алфавиту: иначе
   подпапки перемешивались бы с корневыми и родителя было бы не угадать.
8. **Добавлено `section=all`**, которого в плане не было: Mini App и поиск иногда
   работают сразу с обоими разделами, а перебирать их двумя запросами незачем.
9. **`file_type` по умолчанию зависит от эндпоинта, а не один на весь API.**
   `GET /tracks` и все очереди плеера — `audio` (раздел «Треки»). А вот
   `GET /folders/{id}/tracks` по умолчанию отдаёт ВСЕ типы: в V1 он возвращал всё
   содержимое папки, и новый фильтр по умолчанию молча спрятал бы часть файлов.
10. **Значение `other` у `file_type`** — сокращение для «весь раздел Другое»
    (кортеж из четырёх типов), чтобы клиенту не приходилось перечислять их руками.
11. **Первичный `UPDATE total_plays` в миграции 004 считает только по
    `tracks.artist_id`.** Это расхождение с п. 6 снимается первым же вызовом
    `recalc_total_plays()` в рабочем коде и на данные не влияет.

### Рекомендации

12. **Категории делятся не «двумя сортировками», а одним разрезом.** В плане было:
    `popular` — по `total_plays DESC`, `underground` — по `total_plays ASC` среди тех,
    у кого есть прослушивания. Буквально это даёт пересечение: при малом числе
    кандидатов один и тот же исполнитель попадает в оба списка. Поэтому кандидаты с
    `total_plays > 0` сортируются один раз, верхняя часть уходит в «Популярные»,
    остаток (пересортированный по возрастанию) — в «Менее известные»; граница —
    `limit`, а при нехватке кандидатов — половина, иначе вторая категория была бы
    пустой.
13. **Кандидат с `total_plays = 0` не попадает ни в одну категорию.** В «Менее
    известные» по контракту нужен `total_plays > 0`, а в «Популярных» никем не
    слушанный исполнитель выдавал бы себя за находку. Освободившиеся места честно
    уходят в добивку «из вашей библиотеки» и в `shortfall`.
14. **Формулировок `reason` стало больше, чем три из плана.** Добавились «слушают те,
    у кого похожие вкусы» (соседи есть, но конкретного общего исполнителя не назвать)
    и «часто слушают другие пользователи» (ни жанра, ни соседей — осталась голая
    популярность). Писать «жанр: …» там, где жанр не совпал, было бы неправдой.
15. **`limit` не гарантирует 15 позиций.** На базе с одним пользователем и малым
    числом прослушиваний списки короче, а `shortfall` и `note` объясняют, сколько
    не хватило и почему. Выдумывать исполнителей и ходить во внешние сервисы
    запрещено, и это осталось так.

### API и клиент

16. **`artist_ids` в `GET /search` — одна строка через запятую**, а не повторяющийся
    параметр: так же это выглядит в ссылке Mini App и в примерах контракта.
17. **Изменения пунктов заметки возвращают заметку целиком** (`NoteDetailOut`).
    Клиенту после каждого клика нужны пересчитанные позиции и счётчики
    `items_total` / `items_done`, и отдельный GET за ними — лишний round-trip.
18. **`POST /artists` идемпотентен**: новый исполнитель — 201, уже существовавший —
    200. Ошибка на повторе заставила бы клиента сначала искать, потом создавать.
19. **`GET /tracks/play_all` объявлен ДО `/tracks/{track_id}`.** FastAPI выбирает
    первый подошедший путь по порядку регистрации, и при обратном порядке запрос
    ушёл бы в карточку трека и вернул 422 на разборе `int`.
20. **Раздел «Другое» отдаёт `TrackOut`, а не собственную схему.** В `TrackOut` уже
    есть `file_type`, и один компонент карточки обслуживает оба раздела. Схема
    `OtherFileOut` в `schemas.py` описана, но роутером не используется.
21. **`search_all` сохранил позиционные `limit` и `threshold`** (в плане они были
    только именованными) — иначе сломались бы все вызовы V1 вида
    `search_all(uid, q, 20)`.
22. **`ArtistOut` не отдаёт `artists.total_plays`.** Наружу идёт `play_count`,
    посчитанный по трекам; `total_plays` — денормализованный счётчик для
    рекомендаций, и показывать в карточке два почти одинаковых числа незачем.

### Бот, Mini App и инструменты

23. **Команд стало 31, а не 30** (16 из V1 + 15 новых) — в плане была арифметическая
    ошибка, состав команд не менялся.
24. **У `/play_all` и `/play_folder` разные `source`** прослушивания
    (`bot_play_all` и `bot_play_folder`): в плане для обоих был `bot_play_all`, но
    тогда два сценария было бы не отличить в истории.
25. **`scripts/make_avatar.py` не требует Pillow.** В плане Pillow был основным
    инструментом с откатом на SVG; в итоге растеризатор свой, на стандартной
    библиотеке, а Pillow — необязательное улучшение сглаживания. PNG и SVG пишутся
    оба.
26. **Распознавание (`services/recognition.py`) осталось ЗАГЛУШКОЙ.** Запросы к AudD,
    ACRCloud и Genius написаны целиком, включая HMAC-подпись ACRCloud, но без ключей
    не выполняются и на живых ключах не проверялись — формат ответов нужно сверить с
    документацией сервиса перед боевым включением. Genius при этом ищет по ТЕКСТУ:
    распознавания по звуку у него нет в принципе.
27. **Стилизация Mini App — один `frontend/src/styles.css` на CSS-переменных темы
    Telegram** (светлая и тёмная). Tailwind CSS и Material-UI в проекте не
    используются.
