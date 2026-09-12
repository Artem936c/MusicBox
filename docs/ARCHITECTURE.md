# MusicBox — архитектурный контракт (единый источник истины)

Этот документ — ОБЯЗАТЕЛЬНЫЙ контракт для всех модулей проекта. Имена файлов, функций,
полей БД, маршрутов API и callback-данных менять НЕЛЬЗЯ: модули пишутся параллельно и
должны состыковаться без правок.

Корень проекта: `C:\Users\Bolshakov\CurrentProject\MusicBox`

## 0. Что это

Telegram-бот + Telegram Mini App (WebApp), который использует приватный Telegram-канал
как облачное хранилище аудиофайлов. Функции: папки, загрузка аудио с автосортировкой по
исполнителю, нечёткий поиск треков/альбомов/исполнителей, список исполнителей с отметкой
«прослушано», поиск аудио в Telegram (публичные каналы/ссылки) с импортом в хранилище,
плейлисты с drag-and-drop, избранное, статистика прослушиваний с пятью разделами.

## 1. Стек

- Python 3.10+ (в проекте 3.12), aiogram 3.x, FastAPI, uvicorn, aiosqlite, rapidfuzz,
  pydantic v2 + pydantic-settings, httpx, mutagen (опционально), Telethon (опционально).
- Frontend: React 18 + Vite + react-router-dom (HashRouter) + @dnd-kit + Telegram WebApp SDK.
- Один процесс: FastAPI (uvicorn) + aiogram (polling или webhook через тот же FastAPI).

## 2. Дерево файлов

```
MusicBox/
├── README.md                     # подробная инструкция по развёртыванию
├── .env.example
├── .gitignore
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
├── Dockerfile
├── docker-compose.yml
├── docs/ARCHITECTURE.md          # этот файл
├── backend/
│   ├── __init__.py
│   ├── main.py                   # точка входа: python -m backend.main
│   ├── config.py
│   ├── logging_config.py
│   ├── errors.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── database.py
│   │   ├── schema.sql
│   │   └── repositories/
│   │       ├── __init__.py
│   │       ├── users.py
│   │       ├── folders.py
│   │       ├── artists.py
│   │       ├── albums.py
│   │       ├── tracks.py
│   │       ├── stats.py
│   │       ├── playlists.py
│   │       └── favourites.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── metadata.py
│   │   ├── autosort.py
│   │   ├── search.py
│   │   ├── storage.py
│   │   └── telegram_search.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── app.py                # create_app()
│   │   ├── deps.py
│   │   ├── security.py
│   │   ├── schemas.py
│   │   └── routers/
│   │       ├── __init__.py       # api_router: собирает все роутеры
│   │       ├── folders.py
│   │       ├── tracks.py
│   │       ├── artists.py
│   │       ├── albums.py
│   │       ├── playlists.py
│   │       ├── favourites.py
│   │       ├── search.py
│   │       ├── stats.py
│   │       └── settings.py
│   └── bot/
│       ├── __init__.py
│       ├── bot.py                # create_bot/create_dispatcher/setup_commands
│       ├── callbacks.py          # CallbackData-фабрики
│       ├── keyboards.py
│       ├── states.py
│       ├── middlewares.py
│       ├── texts.py              # все пользовательские тексты (RU)
│       ├── utils.py
│       └── handlers/
│           ├── __init__.py       # register_handlers(dp)
│           ├── start.py
│           ├── stats.py
│           ├── upload.py
│           ├── folders.py
│           ├── artists.py
│           ├── playlists.py
│           ├── favourites.py
│           ├── search.py
│           └── tg_search.py
├── tests/
│   ├── conftest.py
│   ├── test_repositories.py
│   ├── test_stats.py
│   ├── test_api.py
│   ├── test_security.py
│   ├── test_metadata.py
│   └── test_search.py
└── frontend/
    ├── package.json
    ├── vite.config.js
    ├── index.html
    ├── .env.example
    └── src/
        ├── main.jsx
        ├── App.jsx
        ├── telegram.js
        ├── styles.css
        ├── api/client.js
        ├── hooks/useAsync.js
        ├── context/PlayerContext.jsx
        ├── context/ToastContext.jsx
        ├── components/{Layout,TabBar,TrackRow,TrackCard,Player,SectionScroller,Loader,EmptyState,Modal,SearchBar,FolderPickerModal,PlaylistPickerModal,ConfirmDialog}.jsx
        └── pages/{HomePage,StatsPage,SectionPage,FoldersPage,FolderPage,SearchPage,TelegramSearchPage,PlaylistsPage,PlaylistPage,FavouritesPage,ArtistsPage,ArtistPage,SettingsPage}.jsx
```

## 3. Конвенции

- Идентификаторы кода — английские; ВСЕ строки, видимые пользователю (бот, Mini App) — русские.
- Docstrings и комментарии — русские, кратко и по делу.
- Везде async/await; блокирующих вызовов в event loop нет (mutagen — через `asyncio.to_thread`).
- Логирование: `logger = logging.getLogger(__name__)`, никаких print.
- Ошибки: собственные исключения в `backend/errors.py`; в API — `HTTPException` c русским `detail`.
- Типизация: аннотации типов обязательны для публичных функций. `from __future__ import annotations`
  в каждом модуле backend (чтобы `int | None` работал на 3.10).
- Импорты абсолютные: `from backend.db.repositories import tracks as tracks_repo`.
- Сортировка по алфавиту с учётом кириллицы: НЕ полагаться на `COLLATE NOCASE`,
  сортировать в Python: `sorted(rows, key=lambda r: r["name"].casefold())`.
- Все репозитории и сервисы возвращают `dict` (не `aiosqlite.Row`) — конвертировать через
  `backend.db.database.row_to_dict` / `rows_to_dicts`.
- Даты в БД хранятся как `DATETIME DEFAULT CURRENT_TIMESTAMP` (UTC, строка `YYYY-MM-DD HH:MM:SS`).
  В API отдаются как строка (`str`), фронтенд парсит `new Date(value.replace(' ', 'T') + 'Z')`.

## 4. backend/errors.py

```python
class MusicBoxError(Exception): ...
class NotFoundError(MusicBoxError): ...
class ValidationError(MusicBoxError): ...
class StorageError(MusicBoxError): ...
class AuthError(MusicBoxError): ...
class TelegramSearchUnavailable(MusicBoxError): ...
class TelegramSearchError(MusicBoxError): ...
class FileTooLargeError(StorageError): ...
```

## 5. backend/config.py

`pydantic_settings.BaseSettings` (v2), класс `Settings`,
`model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")`.
Функция `@lru_cache def get_settings() -> Settings` и модульная переменная `settings = get_settings()`.

Поля (имя — тип — дефолт):

| поле | тип | дефолт |
|---|---|---|
| bot_token | str | "" (обязателен при запуске) |
| storage_channel_id | int | 0 |
| webapp_url | str | "" |
| secret_key | str | "change-me" |
| database_path | str | "data/musicbox.db" |
| api_host | str | "0.0.0.0" |
| api_port | int | 8000 |
| bot_mode | Literal["polling","webhook"] | "polling" |
| webhook_base_url | str | "" |
| webhook_path | str | "/telegram/webhook" |
| webhook_secret | str | "" |
| telegram_api_base | str | "https://api.telegram.org" |
| init_data_ttl | int | 86400 |
| stream_token_ttl | int | 21600 |
| dev_mode | bool | False |
| dev_user_id | int | 0 |
| allowed_user_ids | str | "" (CSV; пусто = все) |
| frequent_threshold | int | 10 |
| rare_min | int | 1 |
| rare_max | int | 5 |
| top_limit | int | 10 |
| recent_limit | int | 20 |
| page_size | int | 10 |
| fuzzy_threshold | int | 60 |
| max_download_size | int | 20971520 |
| telegram_search_enabled | bool | False |
| tg_api_id | int | 0 |
| tg_api_hash | str | "" |
| tg_session_string | str | "" |
| tg_search_chats | str | "" (CSV юзернеймов каналов по умолчанию) |
| log_level | str | "INFO" |
| log_file | str | "" |
| cors_origins | str | "*" |
| frontend_dist | str | "frontend/dist" |

Дополнительно свойства: `allowed_user_ids_set -> set[int]`, `cors_origins_list -> list[str]`,
`tg_search_chats_list -> list[str]`, `webhook_url -> str`, `database_dir`.
Метод `validate_runtime()` — бросает `ValueError` с понятным русским текстом, если не задан
`bot_token` / `storage_channel_id` / (`secret_key` == "change-me" вне dev_mode).

## 6. backend/db/database.py — точный API

```python
class Database:
    def __init__(self) -> None: ...          # self._conn: aiosqlite.Connection | None; self._write_lock = asyncio.Lock()
    @property
    def connection(self) -> aiosqlite.Connection   # RuntimeError если не подключена
    async def connect(self, path: str | None = None) -> None
        # создаёт каталог, connect, row_factory = aiosqlite.Row,
        # PRAGMA journal_mode=WAL; foreign_keys=ON; synchronous=NORMAL; busy_timeout=5000
    async def close(self) -> None
    async def executescript(self, script: str) -> None
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int      # rowcount, с commit, под write_lock
    async def execute_insert(self, sql: str, params: Sequence[Any] = ()) -> int  # lastrowid
    async def execute_many(self, sql: str, params_seq: Iterable[Sequence[Any]]) -> None
    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None
    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict]
    async def fetch_val(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]   # под write_lock, commit/rollback

db = Database()                       # СИНГЛТОН, импортируется всеми репозиториями
def row_to_dict(row) -> dict | None
def rows_to_dicts(rows) -> list[dict]
async def init_db(path: str | None = None) -> None   # connect + applies schema.sql + apply_migrations
async def apply_migrations() -> None  # идемпотентно: ALTER TABLE ... ADD COLUMN для play_count/last_played_at и пр.
                                      # (проверять через PRAGMA table_info, игнорировать существующие)
async def shutdown_db() -> None
```

`fetch_one/fetch_all` возвращают уже `dict`. `execute` коммитит сразу.

## 7. backend/db/schema.sql — ТОЧНОЕ содержимое (скопировать как есть)

```sql
PRAGMA foreign_keys = ON;

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
```

## 8. Репозитории (`backend/db/repositories/*.py`)

Все функции — `async`, все принимают `user_id: int` первым аргументом (кроме отмеченных),
возвращают `dict` / `list[dict]` / `bool` / `int`. Все запросы ОБЯЗАТЕЛЬНО фильтруются по `user_id`.

### tracks.py (ключевой модуль — экспортирует SQL-фрагменты для остальных)

```python
UNSET = object()   # сентинел для «фильтр не задан»

TRACK_COLUMNS = """
        t.id, t.user_id, t.title, t.artist, t.album, t.duration, t.file_size,
        t.mime_type, t.file_name, t.file_id, t.file_unique_id,
        t.storage_chat_id, t.storage_message_id, t.thumb_file_id,
        t.folder_id, t.artist_id, t.album_id, t.source, t.source_ref,
        t.play_count, t.last_played_at, t.created_at,
        CASE WHEN fav.id IS NULL THEN 0 ELSE 1 END AS is_favourite,
        fo.name AS folder_name
"""
TRACK_FROM = """
    FROM tracks t
    LEFT JOIN favourites fav ON fav.track_id = t.id AND fav.user_id = t.user_id
    LEFT JOIN folders   fo  ON fo.id = t.folder_id
"""
def track_query(where: str = "", order: str = "t.created_at DESC", limit_clause: str = "") -> str
    # собирает: SELECT {TRACK_COLUMNS} {TRACK_FROM} WHERE t.user_id = ? {where} ORDER BY {order} {limit_clause}

async def create_track(user_id, *, title, artist=None, album=None, duration=0, file_size=0,
                       mime_type=None, file_name=None, file_id, file_unique_id=None,
                       storage_chat_id=None, storage_message_id=None, thumb_file_id=None,
                       folder_id=None, artist_id=None, album_id=None,
                       source="upload", source_ref=None) -> dict
    # play_count = 0, last_played_at = NULL. При конфликте (user_id, file_unique_id) —
    # вернуть существующий трек (INSERT ... ON CONFLICT DO NOTHING + повторный SELECT).
async def get_track(user_id: int, track_id: int) -> dict | None
async def get_tracks_by_ids(user_id: int, ids: Sequence[int]) -> list[dict]   # порядок как в ids
async def get_track_by_unique_id(user_id: int, file_unique_id: str) -> dict | None
async def list_tracks(user_id, *, folder_id=UNSET, artist_id=None, album_id=None,
                      order: str = "created_at_desc", limit: int = 50, offset: int = 0) -> list[dict]
    # order: created_at_desc | created_at_asc | title | artist | play_count_desc | last_played_desc
    # folder_id=UNSET — без фильтра; folder_id=None — только треки без папки (t.folder_id IS NULL)
async def count_tracks(user_id, *, folder_id=UNSET, artist_id=None, album_id=None) -> int
async def update_track(user_id, track_id, **fields) -> dict | None   # белый список полей
async def move_track(user_id, track_id, folder_id: int | None) -> dict | None
async def move_tracks(user_id, track_ids: Sequence[int], folder_id: int | None) -> int
async def delete_track(user_id, track_id) -> dict | None   # возвращает удалённый трек (для удаления из канала)
async def register_play(user_id, track_id, source: str = "web") -> dict | None
    # АТОМАРНО: UPDATE tracks SET play_count = play_count + 1,
    #           last_played_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?
    # + INSERT INTO play_history(...) + пометить исполнителя прослушанным
    #   (artists.is_listened = 1, listened_at = CURRENT_TIMESTAMP, если artist_id задан)
    # возвращает обновлённый трек или None
async def total_tracks(user_id) -> int
async def total_plays(user_id) -> int
```

### stats.py (импортирует `track_query` из tracks.py)

```python
SECTION_KEYS = ("top", "recent", "unplayed", "frequent", "rare")
SECTION_TITLES = {"top": "Самые часто прослушиваемые", "recent": "Недавно добавленные",
                  "unplayed": "Ни разу не проигранные", "frequent": "Часто прослушиваемые",
                  "rare": "Редко прослушиваемые"}
async def recent(user_id, limit=20, offset=0) -> list[dict]        # ORDER BY t.created_at DESC, t.id DESC
async def unplayed(user_id, limit=50, offset=0) -> list[dict]      # play_count = 0, ORDER BY created_at DESC
async def frequent(user_id, threshold=None, limit=50, offset=0)    # play_count >= threshold (default — из настроек пользователя)
async def rare(user_id, min_count=None, max_count=None, limit=50, offset=0)  # BETWEEN, ORDER BY play_count DESC
async def top(user_id, limit=10, offset=0) -> list[dict]
    # play_count > 0, ORDER BY play_count DESC, last_played_at DESC, id DESC
async def section(user_id, key: str, limit=..., offset=...) -> list[dict]   # диспетчер по SECTION_KEYS
async def counts(user_id) -> dict
    # {"total": int, "total_plays": int, "top": int, "recent": int,
    #  "unplayed": int, "frequent": int, "rare": int, "favourites": int, "folders": int, "artists": int}
    # "recent" = общее число треков, "top" = число треков с play_count > 0
async def overview(user_id, limit: int = 10) -> dict
    # {"counts": {...}, "sections": [{"key","title","count","items":[track,...]}, ...]}
    # порядок секций: top, recent, unplayed, frequent, rare
```

### users.py
```python
async def ensure_user(user_id, username=None, first_name=None, last_name=None, language_code=None) -> dict
async def get_user(user_id) -> dict | None
async def touch_user(user_id) -> None
async def get_settings(user_id) -> dict          # создаёт строку с дефолтами при отсутствии
async def update_settings(user_id, **fields) -> dict   # белый список: auto_sort_enabled, frequent_threshold, rare_min, rare_max, fuzzy_threshold
async def toggle_auto_sort(user_id) -> dict
```

### folders.py
```python
async def create_folder(user_id, name: str, is_artist_folder: bool = False) -> dict   # идемпотентно
async def get_folder(user_id, folder_id) -> dict | None
async def find_folder_by_name(user_id, name) -> dict | None      # по normalized_name
async def list_folders(user_id, include_counts: bool = True) -> list[dict]
    # dict: id,name,normalized_name,is_artist_folder,created_at,track_count
    # СОРТИРОВКА В PYTHON по name.casefold()
async def rename_folder(user_id, folder_id, new_name) -> dict | None
async def delete_folder(user_id, folder_id, delete_tracks: bool = False) -> bool
    # по умолчанию треки не удаляются (folder_id -> NULL благодаря ON DELETE SET NULL)
async def folder_track_count(user_id, folder_id) -> int
```

### artists.py
```python
async def ensure_artist(user_id, name: str, folder_id: int | None = None) -> dict
async def get_artist(user_id, artist_id) -> dict | None
async def find_artist_by_name(user_id, name) -> dict | None
async def list_artists(user_id, *, only_listened: bool | None = None) -> list[dict]
    # + track_count, play_count (SUM), сортировка в Python по name.casefold()
async def set_listened(user_id, artist_id, is_listened: bool) -> dict | None
async def toggle_listened(user_id, artist_id) -> dict | None
async def attach_folder(user_id, artist_id, folder_id) -> dict | None
async def mark_listened_by_track(user_id, track_id) -> None
```

### albums.py
```python
async def ensure_album(user_id, title: str, artist_id=None, year=None) -> dict | None  # None если title пустой
async def get_album(user_id, album_id) -> dict | None
async def list_albums(user_id) -> list[dict]   # + artist_name, track_count; сортировка в Python по title.casefold()
async def album_tracks(user_id, album_id, limit=100, offset=0) -> list[dict]
```

### playlists.py
```python
async def create_playlist(user_id, name, description=None) -> dict
async def get_playlist(user_id, playlist_id) -> dict | None       # + track_count, total_duration
async def list_playlists(user_id) -> list[dict]                   # + track_count; сортировка в Python по name.casefold()
async def update_playlist(user_id, playlist_id, *, name=None, description=None) -> dict | None
async def delete_playlist(user_id, playlist_id) -> bool
async def playlist_tracks(user_id, playlist_id) -> list[dict]     # трек-dict + "position"; ORDER BY pt.position
async def add_track(user_id, playlist_id, track_id) -> bool       # в конец; False если уже есть
async def add_tracks(user_id, playlist_id, track_ids) -> int
async def remove_track(user_id, playlist_id, track_id) -> bool    # + перенумерация позиций
async def reorder(user_id, playlist_id, ordered_track_ids: Sequence[int]) -> bool
    # В ОДНОЙ транзакции: проверить, что множество id совпадает с текущим составом;
    # обновить position = index. Возвращает False при несовпадении состава.
async def move_track_position(user_id, playlist_id, track_id, new_position: int) -> bool
```

### favourites.py
```python
async def add(user_id, track_id) -> bool
async def remove(user_id, track_id) -> bool
async def toggle(user_id, track_id) -> bool     # возвращает НОВОЕ состояние (True = в избранном)
async def is_favourite(user_id, track_id) -> bool
async def list_favourites(user_id, limit=100, offset=0) -> list[dict]   # ORDER BY fav.added_at DESC
async def count(user_id) -> int
```

## 9. Сервисы

### services/metadata.py
```python
@dataclass(slots=True)
class AudioMetadata:
    title: str; artist: str | None; album: str | None; duration: int
    year: int | None = None; genre: str | None = None; file_name: str | None = None

def normalize_name(value: str | None) -> str          # casefold, strip, схлопывание пробелов, ё->е
def clean_title(value: str | None) -> str             # убрать расширение, подчёркивания, «[official video]» и т.п.
def split_artists(performer: str | None) -> list[str] # по "feat.", "ft.", " & ", ",", " x ", " vs "
def primary_artist(performer: str | None) -> str | None
def guess_from_filename(file_name: str | None) -> tuple[str | None, str | None]  # (artist, title) из "Artist - Title.mp3"
def parse_telegram_audio(audio, file_name: str | None = None) -> AudioMetadata
    # audio: aiogram.types.Audio | Document; использует title/performer/duration/file_name,
    # fallback на guess_from_filename, в крайнем случае title = "Без названия"
async def extract_tags(path: str) -> AudioMetadata | None   # mutagen через asyncio.to_thread; None если mutagen не установлен
def format_duration(seconds: int | None) -> str            # "3:07" / "1:02:03" / "—"
```

### services/autosort.py
```python
@dataclass(slots=True)
class SortResult:
    artist: dict | None; folder: dict | None; folder_created: bool; applied: bool

async def suggest_folder(user_id: int, artist_name: str | None) -> tuple[dict | None, bool]
    # (существующая папка | None, нужно ли создавать). Ищет folders по normalized_name,
    # затем — нечётко (rapidfuzz, >= 88) среди существующих папок.
async def apply_autosort(user_id: int, track: dict, *, artist_name: str | None = None,
                         create_folder: bool = True, force: bool = False) -> SortResult
    # 1) ensure_artist; 2) если user_settings.auto_sort_enabled или force —
    #    найти/создать папку с именем исполнителя (is_artist_folder=1),
    #    привязать artists.folder_id, tracks.folder_id/artist_id/album_id.
async def assign_to_folder(user_id: int, track_id: int, folder_name: str) -> dict | None  # создать папку при отсутствии и перенести
```

### services/search.py
```python
LAYOUT_RU = "йцукенгшщзхъфывапролджэячсмитьбю"
LAYOUT_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,."
def swap_layout(text: str) -> str            # ru<->en раскладка в обе стороны
def normalize_query(text: str) -> str
def variants(query: str) -> list[str]        # [normalized, swapped] без дублей
def score(query_variants: list[str], candidate: str) -> int   # max по WRatio / partial_ratio / token_set_ratio
async def search_tracks(user_id, query, limit=30, threshold=None) -> list[dict]   # трек-dict + "score"
async def search_albums(user_id, query, limit=30, threshold=None) -> list[dict]   # альбом-dict + "score"
async def search_artists(user_id, query, limit=30, threshold=None) -> list[dict]
async def search_folders(user_id, query, limit=30, threshold=None) -> list[dict]
async def search_all(user_id, query, limit=20, threshold=None) -> dict
    # {"query":..., "tracks":[...], "albums":[...], "artists":[...], "folders":[...]}
```
Алгоритм: выбрать кандидатов SQL-ом (все треки пользователя, поля id/title/artist/album),
посчитать нечёткий скор по `f"{title} {artist} {album}"`, отфильтровать по threshold
(из настроек пользователя, иначе `settings.fuzzy_threshold`), отсортировать по score DESC,
дозагрузить полные dict через `tracks_repo.get_tracks_by_ids`. Пустой запрос → пустые списки.
Альбомы («группы») при равном score сортируются по алфавиту `title.casefold()`.

### services/storage.py
```python
@dataclass(slots=True)
class StoredAudio:
    file_id: str; file_unique_id: str | None; message_id: int | None; chat_id: int | None
    title: str; artist: str | None; album: str | None; duration: int
    file_size: int; mime_type: str | None; file_name: str | None; thumb_file_id: str | None

async def store_from_message(bot: Bot, message: Message) -> StoredAudio
    # копирует сообщение с аудио в приватный канал (bot.copy_message или send_audio по file_id),
    # берёт file_id ИЗ СООБЩЕНИЯ В КАНАЛЕ, возвращает StoredAudio. StorageError при неудаче.
async def store_from_bytes(bot: Bot, data: bytes, *, file_name, title, performer,
                           duration=0, mime_type=None, caption=None) -> StoredAudio
async def store_from_file_id(bot: Bot, file_id: str, *, title=None, performer=None,
                             duration=0, caption=None) -> StoredAudio
async def delete_from_channel(bot: Bot, message_id: int | None) -> bool   # ошибки логируем, не пробрасываем
async def resolve_file_path(bot: Bot, file_id: str) -> str   # bot.get_file + кэш {file_id: (path, expires_at)} на 50 минут
def file_download_url(file_path: str) -> str                 # f"{telegram_api_base}/file/bot{token}/{file_path}" — НИКОГДА не отдавать наружу
async def stream_file(bot: Bot, file_id: str, range_header: str | None = None) -> tuple[AsyncIterator[bytes], dict[str, str], int]
    # (генератор чанков, заголовки Content-Type/Content-Length/Content-Range/Accept-Ranges, код 200|206)
    # httpx.AsyncClient(stream=True), проброс Range. Ошибки -> StorageError.
async def send_track_to_user(bot: Bot, chat_id: int, track: dict, *, caption=None, reply_markup=None) -> Message
async def close_http_client() -> None
```
Учитывать лимит Bot API: скачивание файла через getFile ограничено 20 МБ
(`settings.max_download_size`); при превышении — `FileTooLargeError` c русским сообщением
и подсказкой про локальный Bot API server.

### services/telegram_search.py
```python
@dataclass(slots=True)
class RemoteAudio:
    token: str            # uuid4().hex[:16]
    chat_id: int; chat_title: str; chat_username: str | None; message_id: int
    title: str; performer: str | None; duration: int; file_size: int
    mime_type: str | None; file_name: str | None; link: str

class TelegramSearchService:
    @property
    def available(self) -> bool               # telegram_search_enabled и Telethon сконфигурирован
    async def start(self) -> None             # подключение Telethon (StringSession), безопасно при выключенной фиче
    async def stop(self) -> None
    async def search(self, query: str, *, chats: list[str] | None = None, limit: int = 20) -> list[RemoteAudio]
    async def resolve_link(self, url: str) -> RemoteAudio | None   # https://t.me/<name>/<id>, t.me/c/<internal>/<id>
    async def download(self, remote: RemoteAudio) -> bytes
    def get_cached(self, token: str) -> RemoteAudio | None         # in-memory LRU 500
    def cache(self, items: list[RemoteAudio]) -> None

telegram_search = TelegramSearchService()

async def import_remote(bot, user_id: int, remote: RemoteAudio) -> dict
    # download -> storage.store_from_bytes -> tracks_repo.create_track(source="telegram_search",
    # source_ref=remote.link) -> autosort.apply_autosort -> вернуть трек-dict
```
Если Telethon не установлен/не настроен — `available = False`, методы поиска бросают
`TelegramSearchUnavailable` с текстом-подсказкой (RU): пользователю предлагается переслать
аудио боту вручную. Импорт пересланного сообщения работает ВСЕГДА (bot/handlers/upload.py).

## 10. API

Все маршруты собираются в `backend/api/routers/__init__.py` в `api_router = APIRouter()`.
`create_app()` подключает его ДВАЖДЫ: `app.include_router(api_router, prefix="/api")` и
`app.include_router(api_router, include_in_schema=False)` — чтобы работали и `/api/stats/top`,
и `/stats/top` из ТЗ. Статика фронтенда монтируется ПОСЛЕДНЕЙ на `/`.

### security.py
```python
def validate_init_data(init_data: str, bot_token: str, ttl: int) -> dict
    # secret = HMAC_SHA256(key=b"WebAppData", msg=bot_token)
    # data_check_string = "\n".join(f"{k}={v}" for k,v in sorted(pairs) if k != "hash")
    # hmac.compare_digest; проверка auth_date + ttl; json.loads(user)
    # -> {"id","first_name","last_name","username","language_code"}; иначе AuthError
def create_stream_token(user_id: int, track_id: int, ttl: int | None = None) -> str
    # f"{user_id}.{track_id}.{exp}.{sig}", sig = HMAC_SHA256(secret_key, f"{user_id}.{track_id}.{exp}")
def verify_stream_token(token: str) -> tuple[int, int]     # (user_id, track_id) или AuthError
```

### deps.py
```python
async def get_current_user(request: Request) -> dict
    # initData из заголовка "X-Telegram-Init-Data" или "Authorization: tma <initData>"
    # dev_mode: если заголовка нет и settings.dev_mode -> dev_user_id.
    # allowed_user_ids: если задан и id не входит -> 403.
    # ensure_user + touch_user; возвращает dict пользователя из БД.
CurrentUser = Annotated[dict, Depends(get_current_user)]
def get_bot(request: Request) -> Bot        # request.app.state.bot; 503 если нет
```

### schemas.py (pydantic v2)
- `TrackOut`: id, title, artist|None, album|None, duration, duration_label, file_size, mime_type|None,
  folder_id|None, folder_name|None, artist_id|None, album_id|None, source, play_count,
  last_played_at|None, created_at, is_favourite: bool, stream_url: str, cover_url: str|None, position|None
- `FolderOut`: id, name, is_artist_folder: bool, track_count: int, created_at
- `ArtistOut`: id, name, is_listened: bool, track_count, play_count, folder_id|None
- `AlbumOut`: id, title, artist_id|None, artist_name|None, year|None, track_count
- `PlaylistOut`: id, name, description|None, track_count, total_duration, created_at, updated_at
- `PlaylistDetailOut`: PlaylistOut + tracks: list[TrackOut]
- `SectionOut`: key, title, count, items: list[TrackOut]
- `StatsOverviewOut`: counts: dict[str,int], sections: list[SectionOut]
- `SearchResultOut`: query, tracks, albums, artists, folders
- `RemoteAudioOut`: token, title, performer|None, duration, duration_label, file_size, chat_title, link
- `SettingsOut`: auto_sort_enabled: bool, frequent_threshold, rare_min, rare_max, fuzzy_threshold
- Запросы: `FolderCreateIn(name)`, `FolderUpdateIn(name)`, `MoveTracksIn(track_ids: list[int], folder_id: int|None)`,
  `TrackUpdateIn(title|None, artist|None, album|None, folder_id|None)`, `PlayIn(source: str = "web")`,
  `PlaylistCreateIn(name, description|None)`, `PlaylistUpdateIn(name|None, description|None)`,
  `PlaylistAddIn(track_ids: list[int])`, `PlaylistOrderIn(track_ids: list[int])`,
  `SettingsUpdateIn(...все поля Optional)`, `ImportRemoteIn(token: str)`, `ImportLinkIn(url: str)`,
  `AssignFolderIn(folder_name: str)`, `ListenedIn(is_listened: bool | None = None)`,
  `MoveTrackIn(folder_id: int | None)`
- **Обязательные конвертеры** в `schemas.py`:
  ```python
  def track_to_out(track: dict, user_id: int) -> TrackOut
      # duration_label = format_duration; is_favourite = bool(track.get("is_favourite"));
      # stream_url = f"/api/tracks/{id}/stream?token={create_stream_token(user_id, id)}"
      # cover_url = f"/api/tracks/{id}/cover?token=..." если thumb_file_id иначе None
  def tracks_to_out(tracks: list[dict], user_id: int) -> list[TrackOut]
  ```

### Маршруты (все с `Depends(get_current_user)`, кроме `/tracks/{id}/stream` и `/cover` — там авторизация по токену)

**folders.py** (`prefix="/folders"`)
- `GET /folders` → list[FolderOut]
- `POST /folders` (FolderCreateIn) → FolderOut (201)
- `GET /folders/{folder_id}` → FolderOut
- `PATCH /folders/{folder_id}` (FolderUpdateIn) → FolderOut
- `DELETE /folders/{folder_id}?delete_tracks=false` → `{"ok": true}`
- `GET /folders/{folder_id}/tracks?limit&offset&order` → list[TrackOut]
- `POST /folders/{folder_id}/tracks` (MoveTracksIn) → `{"moved": int}`

**tracks.py** (`prefix="/tracks"`)
- `GET /tracks?folder_id&artist_id&album_id&order&limit&offset` → list[TrackOut]
- `GET /tracks/{track_id}` → TrackOut
- `PATCH /tracks/{track_id}` (TrackUpdateIn) → TrackOut
- `DELETE /tracks/{track_id}?delete_from_channel=false` → `{"ok": true}`
- `POST /tracks/{track_id}/play` (PlayIn, тело необязательно) → TrackOut  ← увеличивает счётчик
- `POST /tracks/{track_id}/move` (MoveTrackIn) → TrackOut
- `POST /tracks/{track_id}/folder` (AssignFolderIn) → TrackOut
- `GET /tracks/{track_id}/stream?token=...` → StreamingResponse (200/206, Accept-Ranges, Content-Range)
- `GET /tracks/{track_id}/cover?token=...` → StreamingResponse (image)
- `POST /tracks/{track_id}/favourite` → `{"is_favourite": bool}` (toggle)

**favourites.py** (`prefix="/favourites"`)
- `GET /favourites?limit&offset` → list[TrackOut]
- `POST /favourites/{track_id}` → `{"is_favourite": true}`
- `DELETE /favourites/{track_id}` → `{"is_favourite": false}`

**playlists.py** (`prefix="/playlists"`)
- `GET /playlists` → list[PlaylistOut]
- `POST /playlists` → PlaylistOut (201)
- `GET /playlists/{id}` → PlaylistDetailOut
- `PATCH /playlists/{id}` → PlaylistOut
- `DELETE /playlists/{id}` → `{"ok": true}`
- `POST /playlists/{id}/tracks` (PlaylistAddIn) → PlaylistDetailOut
- `DELETE /playlists/{id}/tracks/{track_id}` → PlaylistDetailOut
- `PUT /playlists/{id}/order` (PlaylistOrderIn) → PlaylistDetailOut  ← drag-and-drop

**artists.py** (`prefix="/artists"`)
- `GET /artists?only_listened=` → list[ArtistOut]
- `GET /artists/{id}` → ArtistOut
- `GET /artists/{id}/tracks` → list[TrackOut]
- `POST /artists/{id}/listened` (ListenedIn; если None — toggle) → ArtistOut

**albums.py** (`prefix="/albums"`)
- `GET /albums` → list[AlbumOut]   (алфавитный порядок)
- `GET /albums/{id}/tracks` → list[TrackOut]

**search.py** (`prefix="/search"`)
- `GET /search?q=&limit=` → SearchResultOut
- `GET /search/tracks?q=&limit=` → list[TrackOut]
- `GET /search/albums?q=&limit=` → list[AlbumOut]
- `GET /search/telegram?q=&limit=&chats=` → list[RemoteAudioOut]  (503 если недоступно)
- `POST /search/telegram/import` (ImportRemoteIn) → TrackOut
- `POST /search/telegram/link` (ImportLinkIn) → TrackOut

**stats.py** (`prefix="/stats"`)
- `GET /stats/overview?limit=10` → StatsOverviewOut
- `GET /stats/recent?limit=20&offset=0` → list[TrackOut]
- `GET /stats/unplayed?limit=50&offset=0` → list[TrackOut]
- `GET /stats/frequent?threshold=10&limit&offset` → list[TrackOut]
- `GET /stats/rare?min_count=1&max_count=5&limit&offset` → list[TrackOut]
- `GET /stats/top?limit=10&offset=0` → list[TrackOut]
- `GET /stats/counts` → dict[str,int]

**settings.py** (`prefix="/settings"`)
- `GET /settings` → SettingsOut
- `PATCH /settings` (SettingsUpdateIn) → SettingsOut

Плюс вне `api_router`: `GET /health` → `{"status":"ok"}` и (в webhook-режиме) `POST {webhook_path}`.

### app.py
```python
def create_app(bot: Bot | None = None, dispatcher: Dispatcher | None = None) -> FastAPI
```
- `lifespan`: `init_db()` → старт `telegram_search` (если включён) → в polling-режиме
  `asyncio.create_task(dp.start_polling(bot))`; при остановке — корректное завершение
  (отмена задачи, `bot.session.close()`, `close_http_client()`, `shutdown_db()`).
- CORS (`settings.cors_origins_list`), `GZipMiddleware`.
- Глобальные обработчики: `NotFoundError` → 404, `ValidationError` → 400, `AuthError` → 401,
  `StorageError` → 502, `TelegramSearchUnavailable` → 503, `Exception` → 500 (логировать traceback).
- Middleware логирования запросов (метод, путь, статус, длительность).
- `app.state.bot`, `app.state.dispatcher`.
- Статика: если `settings.frontend_dist` существует — `StaticFiles(html=True)` на `/`
  + SPA-fallback на `index.html` (кроме путей API).

## 11. Бот

### callbacks.py (aiogram `CallbackData`)
```python
class SectionCB(CallbackData, prefix="sec"):   action: str; key: str; page: int          # open|page
class TrackCB(CallbackData, prefix="trk"):     action: str; track_id: int; page: int; ctx: str
    # action: play|fav|addpl|info|move|delete|back ; ctx: "top","recent","folder:3","pl:2","fav","search"
class FolderCB(CallbackData, prefix="fld"):    action: str; folder_id: int; page: int    # open|page|create|rename|delete|pick|back|list
class PlaylistCB(CallbackData, prefix="pls"):  action: str; playlist_id: int; track_id: int; page: int
    # open|page|create|delete|add|remove|play|up|down|back|list
class ArtistCB(CallbackData, prefix="art"):    action: str; artist_id: int; page: int    # open|page|listened|tracks|back|list
class TgSearchCB(CallbackData, prefix="tgs"):  action: str; token: str                   # import|info
class MoveCB(CallbackData, prefix="mv"):       track_id: int; folder_id: int             # -1 → «без папки», -2 → «новая папка»
class NavCB(CallbackData, prefix="nav"):       action: str                               # menu|stats|folders|playlists|fav|artists|help|noop
```
`page` — 1-based. Для «пустых» значений — 0 / "-".

### keyboards.py
```python
def main_menu_kb() -> ReplyKeyboardMarkup            # WebApp-кнопка «🎵 Открыть MusicBox» (если webapp_url) + разделы
def sections_kb(active: str | None = None) -> InlineKeyboardMarkup   # 5 разделов + «🎛 Открыть приложение»
def tracks_page_kb(tracks, *, ctx: str, page: int, total_pages: int, extra_rows=None) -> InlineKeyboardMarkup
    # строка на трек: «▶️ N» (play), «⭐/☆» (fav), «➕» (addpl) + пагинация «◀️ N/M ▶️» + «⬅️ Назад»
def track_actions_kb(track: dict, ctx: str, page: int) -> InlineKeyboardMarkup
def folders_kb(folders, page, total_pages) -> InlineKeyboardMarkup
def folder_pick_kb(folders, track_id) -> InlineKeyboardMarkup   # + «🆕 Новая папка», «🚫 Без папки»
def playlists_kb(playlists, page, total_pages) -> InlineKeyboardMarkup
def playlist_pick_kb(playlists, track_id) -> InlineKeyboardMarkup
def playlist_tracks_kb(tracks, playlist_id, page, total_pages) -> InlineKeyboardMarkup   # ⬆️/⬇️/▶️/❌
def artists_kb(artists, page, total_pages) -> InlineKeyboardMarkup
def confirm_kb(yes_data: str, no_data: str) -> InlineKeyboardMarkup
def autosort_kb(track_id: int, folder: dict | None, artist_name: str) -> InlineKeyboardMarkup
def tg_results_kb(items) -> InlineKeyboardMarkup
```

### utils.py
```python
def paginate(items: list, page: int, per_page: int = None) -> tuple[list, int]   # (срез, total_pages >= 1)
def format_track_line(index: int, track: dict) -> str    # "1. <b>Title</b> — Artist · 3:07 · ▶️5 ⭐"
def render_track_list(title: str, tracks, page: int, total_pages: int, empty_text: str) -> str
def escape(text: str | None) -> str
async def answer_or_edit(event, text: str, reply_markup=None) -> None
async def safe_edit(message, text, reply_markup=None) -> None   # глотает TelegramBadRequest "message is not modified"
def section_context(key: str) -> str
```
Везде HTML-разметка (`DefaultBotProperties(parse_mode=ParseMode.HTML)`).

### texts.py
Русские константы: `WELCOME`, `HELP`, `EMPTY_LIBRARY`, `SECTION_EMPTY_HINTS` (по ключам разделов) и др.
Дружелюбные сообщения о пустоте ОБЯЗАТЕЛЬНЫ для каждого раздела.

### Команды (через `set_my_commands`)
`/start` `/help` `/stats` `/top` `/recent` `/unplayed` `/frequent` `/rare` `/folders`
`/playlists` `/favourites` `/artists` `/search` `/tgsearch` `/settings` `/app`

### handlers/start.py
`/start`: приветствие + СРАЗУ «🔥 Самые часто прослушиваемые» (топ-10 через `stats_repo.top`)
с кнопками «Прослушать»/«Подробнее», инлайн-кнопки разделов («Недавно добавленные»,
«Ни разу не проигранные», «Часто», «Редко»), напоминание о командах, кнопка WebApp.
Если треков нет — дружелюбное сообщение с предложением прислать аудиофайл. Также `/help`, `/app`, `NavCB`.

### handlers/stats.py
`/stats` (= топ + меню разделов), `/recent`, `/unplayed`, `/frequent`, `/rare`, `/top`,
`SectionCB` (open/page) — пагинация по 10, у каждого трека кнопки «Прослушать»,
«Добавить в плейлист», «В избранное». `TrackCB` action=play → `tracks_repo.register_play(source="bot")`
+ `storage.send_track_to_user`. action=addpl → `playlist_pick_kb`.

### handlers/upload.py
Приём `Message` c `audio`/`document`(audio mime), в т.ч. ПЕРЕСЛАННЫХ из публичных каналов:
1. `storage.store_from_message` → 2. метаданные → 3. `ensure_artist`/`ensure_album` →
4. `tracks_repo.create_track(source="upload"|"telegram_forward", source_ref=...)` →
5. автосортировка: если `auto_sort_enabled` — сразу в папку исполнителя (создав при
   необходимости) с сообщением «Трек сохранён в папку «X»»; иначе — `autosort_kb`
   («Сохранить в «X»» / «Создать папку «X»» / «Другая папка» / «Без папки»).
Обработка `MoveCB` и FSM ввода имени новой папки. Ошибки: дубликат («Этот трек уже есть в
библиотеке»), слишком большой файл, ошибка канала.

### handlers/folders.py
`/folders`, `FolderCB`: список папок (алфавит, пагинация), открытие папки (треки с пагинацией),
создание (FSM), переименование (FSM), удаление (с подтверждением), перемещение трека.

### handlers/artists.py
`/artists`, `ArtistCB`: список исполнителей (алфавит) с отметкой «✅/⬜ прослушано»,
переключение отметки, треки исполнителя.

### handlers/playlists.py
`/playlists`, `PlaylistCB`: список, создание (FSM), удаление, состав, добавление/удаление трека,
перемещение вверх/вниз (`move_track_position`), «▶️ Проиграть плейлист» — отправляет треки
по очереди (до 10 за раз) и на КАЖДЫЙ вызывает `register_play(source="bot_playlist")`.

### handlers/favourites.py
`/favourites` + общий toggle-хендлер `TrackCB(action="fav")` (регистрировать ТОЛЬКО здесь).

### handlers/search.py
`/search <запрос>` и FSM (если запроса нет); нечёткий поиск: треки, альбомы (группы, по алфавиту),
исполнители, папки. Опционально — инлайн-режим (`InlineQuery`) по своей библиотеке.

### handlers/tg_search.py
`/tgsearch <запрос>` — поиск в публичных каналах через `telegram_search`; результаты + «⬇️ Добавить».
Приём ссылки `https://t.me/...` — импорт трека. Если сервис недоступен — понятное сообщение:
«Поиск по Telegram не настроен. Перешлите аудио боту — я сохраню его в хранилище и разложу по папкам».

### middlewares.py
- `UserMiddleware` — `ensure_user` + `touch_user`, кладёт `data["user"]`, `data["settings"]`.
- `AccessMiddleware` — проверка `allowed_user_ids_set`.
- `ThrottlingMiddleware` — простой rate-limit (~0.4 c на пользователя).
- Обработчик `dp.errors` — логирование и ответ «Произошла ошибка, попробуйте позже».

### bot.py
```python
def create_bot() -> Bot        # DefaultBotProperties(parse_mode=ParseMode.HTML)
def create_dispatcher() -> Dispatcher   # MemoryStorage, middlewares, register_handlers
async def setup_commands(bot: Bot) -> None
async def setup_webhook(bot: Bot) -> None
```

## 12. backend/main.py
```python
def main() -> None
    # setup_logging(); settings.validate_runtime(); bot = create_bot(); dp = create_dispatcher();
    # app = create_app(bot, dp); uvicorn.run(app, host=..., port=..., log_config=None)
if __name__ == "__main__": main()
```

## 13. Frontend

`package.json`: react, react-dom, react-router-dom, @dnd-kit/core, @dnd-kit/sortable,
@dnd-kit/modifiers, @dnd-kit/utilities; devDeps: vite, @vitejs/plugin-react.
Скрипты: dev / build / preview. `vite.config.js`: `base: './'`, proxy `/api` → `http://localhost:8000`.
`index.html` подключает `https://telegram.org/js/telegram-web-app.js` ПЕРЕД `/src/main.jsx`.

`src/telegram.js`:
```js
export const tg = window.Telegram?.WebApp ?? null
export function initTelegram()      // ready(), expand(), тема
export function getInitData()       // tg?.initData || import.meta.env.VITE_DEV_INIT_DATA || ''
export function hapticImpact(style) / hapticNotification(type)
export function showBackButton(onClick) / hideBackButton()
export function applyTheme()        // CSS-переменные из tg.themeParams
export const isTelegram
```

`src/api/client.js`:
```js
const BASE = import.meta.env.VITE_API_BASE || '/api'
export class ApiError extends Error { status; detail }
export const api = {
  stats: { overview, recent, unplayed, frequent, rare, top, counts, section(key, params) },
  tracks: { list, get, play(id, source), update, remove, move, assignFolder, toggleFavourite },
  folders: { list, create, get, tracks, rename, remove, moveTracks },
  playlists: { list, create, get, update, remove, addTracks, removeTrack, reorder },
  favourites: { list, add, remove },
  artists: { list, get, tracks, setListened },
  albums: { list, tracks },
  search: { all, tracks, albums, telegram, importRemote, importLink },
  settings: { get, update },
}
export function streamUrl(track)   // (import.meta.env.VITE_API_ORIGIN || '') + track.stream_url
```
Каждый запрос добавляет заголовок `X-Telegram-Init-Data`.

`src/context/PlayerContext.jsx`:
- Состояние: `current`, `queue`, `index`, `isPlaying`, `progress`, `duration`, `repeat`, `shuffle`, `playVersion`.
- `playTrack(track, queue = [track], startIndex = 0)`:
  1) `api.tracks.play(track.id, 'web')` (ошибку только логировать, воспроизведение не блокировать),
  2) `audio.src = streamUrl(track)`, `audio.play()`, `playVersion++`.
- `next()/prev()/toggle()/seek(sec)`; при автопереходе (`ended`) СНОВА вызывать `api.tracks.play`
  для следующего трека — так учитывается каждый трек плейлиста.
- `playVersion` — счётчик учтённых прослушиваний; страницы статистики держат его в зависимостях
  `useEffect` → «обновление в реальном времени».
- Один общий `<audio>` через `useRef`, рендерится в `Player`.

Страницы:
- **HomePage** — сверху виджет «🔥 Самые часто прослушиваемые» (горизонтальный скролл `TrackCard`,
  ссылка «Все» → `/section/top`), затем быстрые ссылки на разделы (Недавно добавленные / Ни разу
  не проигранные / Часто / Редко) с количествами, затем папки (алфавит) и блок «Избранное».
  Пустая библиотека → `EmptyState` с дружелюбным текстом.
- **StatsPage** — все ПЯТЬ разделов списками с счётчиками и действиями по треку.
- **SectionPage** (`/section/:key`) — полный список раздела с подгрузкой.
- **FoldersPage / FolderPage** — папки по алфавиту, создание/переименование/удаление,
  перемещение треков (`FolderPickerModal`).
- **SearchPage** — нечёткий поиск (debounce 300 мс) по трекам/альбомам(группам)/исполнителям/папкам.
- **TelegramSearchPage** — поиск в Telegram + импорт по ссылке; корректная обработка 503.
- **PlaylistsPage / PlaylistPage** — DnD через `@dnd-kit` (`DndContext`, `SortableContext`,
  `verticalListSortingStrategy`, `PointerSensor` + `TouchSensor` с
  `activationConstraint: {delay: 200, tolerance: 5}`), оптимистичное обновление +
  `PUT /playlists/{id}/order`, удаление трека, воспроизведение плейлиста.
- **FavouritesPage**, **ArtistsPage** (алфавит + «прослушано»), **ArtistPage**, **SettingsPage**.
- Нижняя навигация `TabBar`: Главная / Статистика / Папки / Поиск / Плейлисты; над ней — мини-плеер.

Стили: одна `styles.css` с CSS-переменными Telegram (`--tg-theme-bg-color` и т.д.),
светлая/тёмная тема, `env(safe-area-inset-bottom)`, крупные тач-цели.

## 14. Тесты
`pytest` + `pytest-asyncio` (`asyncio_mode=auto`) + `httpx.ASGITransport`.
`conftest.py`: временная БД (`tmp_path`), `init_db(path)`, фикстуры `app`/`client` с `dev_mode=True`.
Тесты: репозитории (CRUD, перемещение, дубликаты), статистика (границы 0 / 1..5 / >=10, порядок top),
API (все /stats/*, `/tracks/{id}/play` увеличивает счётчик, реордер плейлиста), security
(валидный/просроченный/подделанный initData, stream-токен), metadata («Artist - Title.mp3»),
search (опечатки, смена раскладки).

## 15. README.md (обязательные разделы)
Возможности · Архитектура · Требования · Быстрый старт · Создание бота в @BotFather ·
Создание приватного канала-хранилища и получение его ID · Права бота в канале ·
Настройка `.env` (таблица ВСЕХ переменных) · Установка backend · Инициализация БД ·
Сборка frontend · Запуск (polling / webhook) · Публикация Mini App (menu button, HTTPS,
пример nginx + systemd) · Docker · Ограничение 20 МБ Bot API и локальный Bot API server ·
Настройка поиска по Telegram (Telethon: API_ID/API_HASH/StringSession, скрипт получения сессии) ·
Команды бота · Таблица эндпоинтов API · Логика разделов статистики · Тесты ·
Резервное копирование БД · Безопасность · Устранение неполадок · FAQ.

---

## 16. Приложение А. Точные контракты фронтенда (обязательны к соблюдению)

### Маршруты (`HashRouter`, `src/App.jsx`)
```
/                 HomePage
/stats            StatsPage
/section/:key     SectionPage      (key ∈ top|recent|unplayed|frequent|rare)
/folders          FoldersPage
/folders/:id      FolderPage
/search           SearchPage
/tgsearch         TelegramSearchPage
/playlists        PlaylistsPage
/playlists/:id    PlaylistPage
/favourites       FavouritesPage
/artists          ArtistsPage
/artists/:id      ArtistPage
/settings         SettingsPage
*                 → редирект на /
```
`App.jsx` оборачивает всё в `<ToastProvider><PlayerProvider><Layout>…routes…</Layout></PlayerProvider></ToastProvider>`.

### `src/utils/format.js`
```js
export function formatDuration(seconds)     // 187 -> "3:07"; null/0 -> "—"
export function formatDate(value)           // "2026-09-06 10:00:00" -> "6 сент. 2026"
export function formatCount(n, forms)       // forms = ['трек','трека','треков']
export function formatFileSize(bytes)
```

### `src/hooks/useAsync.js`
```js
export function useAsync(fn, deps = [], { immediate = true } = {})
// -> { data, error, loading, reload, setData }
```

### `src/context/ToastContext.jsx`
```js
export function ToastProvider({ children })
export function useToast()   // -> { toast(message, type = 'info') }  type: info|success|error
```

### `src/context/PlayerContext.jsx`
```js
export function PlayerProvider({ children })
export function usePlayer()
// -> { current, queue, index, isPlaying, progress, duration, repeat, shuffle, playVersion,
//      playTrack(track, queue = [track], startIndex = 0), playQueue(tracks, startIndex = 0),
//      toggle(), next(), prev(), seek(seconds), stop(), setRepeat(v), setShuffle(v) }
```
`playVersion` увеличивается ПОСЛЕ каждого успешно учтённого прослушивания
(`POST /tracks/{id}/play`). Страницы со статистикой обязаны держать `playVersion`
в массиве зависимостей загрузки данных.

### Компоненты — точные пропсы
```js
Layout({ children })                       // шапка + <main> + <Player/> + <TabBar/>
TabBar()                                   // NavLink: / , /stats , /folders , /search , /playlists
Loader({ label = 'Загрузка…' })
EmptyState({ icon = '🎧', title, description = null, action = null })
Modal({ open, title, onClose, children, footer = null })
ConfirmDialog({ open, title, message, confirmText = 'Удалить', cancelText = 'Отмена', onConfirm, onCancel })
SearchBar({ value, onChange, placeholder = 'Поиск…', autoFocus = false })
TrackRow({ track, index = null, queue = null, showStats = true, actions = [], onChanged = null,
           dragHandle = null, selected = false })
// actions: [{ key, icon, title, onClick(track) }]
// клик по строке → player.playTrack(track, queue ?? [track], index ?? 0)
// встроенная кнопка ⭐ → api.tracks.toggleFavourite(track.id) → onChanged?.()
TrackCard({ track, queue = null, index = 0 })       // карточка для горизонтального скролла
SectionScroller({ title, items, moreHref = null, emptyText = null, queue = null })
Player()                                            // мини-плеер, единственный <audio>
FolderPickerModal({ open, onClose, onPick, allowNone = true, allowCreate = true,
                    title = 'Выберите папку' })     // onPick(folder | null)
PlaylistPickerModal({ open, onClose, onPick, allowCreate = true })   // onPick(playlist)
```

### `src/api/client.js` — точные сигнатуры
```js
api.stats.overview({ limit } = {})
api.stats.recent({ limit, offset } = {})
api.stats.unplayed({ limit, offset } = {})
api.stats.frequent({ threshold, limit, offset } = {})
api.stats.rare({ min_count, max_count, limit, offset } = {})
api.stats.top({ limit, offset } = {})
api.stats.counts()
api.stats.section(key, params = {})              // диспетчер по ключу раздела
api.tracks.list(params = {})
api.tracks.get(id)
api.tracks.play(id, source = 'web')
api.tracks.update(id, data)
api.tracks.remove(id, { delete_from_channel = false } = {})
api.tracks.move(id, folderId)                    // POST /tracks/{id}/move {folder_id}
api.tracks.assignFolder(id, folderName)          // POST /tracks/{id}/folder {folder_name}
api.tracks.toggleFavourite(id)                   // POST /tracks/{id}/favourite -> {is_favourite}
api.folders.list()
api.folders.create(name)
api.folders.get(id)
api.folders.tracks(id, params = {})
api.folders.rename(id, name)
api.folders.remove(id, { delete_tracks = false } = {})
api.folders.moveTracks(id, trackIds)             // POST /folders/{id}/tracks {track_ids, folder_id}
api.playlists.list()
api.playlists.create(name, description = null)
api.playlists.get(id)
api.playlists.update(id, data)
api.playlists.remove(id)
api.playlists.addTracks(id, trackIds)
api.playlists.removeTrack(id, trackId)
api.playlists.reorder(id, trackIds)              // PUT /playlists/{id}/order {track_ids}
api.favourites.list(params = {})
api.favourites.add(id)
api.favourites.remove(id)
api.artists.list(params = {})
api.artists.get(id)
api.artists.tracks(id, params = {})
api.artists.setListened(id, isListened = null)   // null -> toggle
api.albums.list()
api.albums.tracks(id)
api.search.all(q, limit = 20)
api.search.tracks(q, limit = 30)
api.search.albums(q, limit = 30)
api.search.telegram(q, { limit, chats } = {})
api.search.importRemote(token)
api.search.importLink(url)
api.settings.get()
api.settings.update(data)
```
Все методы возвращают распарсенный JSON и бросают `ApiError` при статусе >= 400.

### Ключи и заголовки разделов на фронте (`src/utils/sections.js` — создаёт агент fe-core)
```js
export const SECTIONS = [
  { key: 'top',      title: 'Самые часто прослушиваемые', icon: '🔥' },
  { key: 'recent',   title: 'Недавно добавленные',        icon: '🆕' },
  { key: 'unplayed', title: 'Ни разу не проигранные',     icon: '💤' },
  { key: 'frequent', title: 'Часто прослушиваемые',       icon: '📈' },
  { key: 'rare',     title: 'Редко прослушиваемые',       icon: '📉' },
]
export function sectionByKey(key)
```
