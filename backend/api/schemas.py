"""Схемы запросов и ответов API (pydantic v2) и конвертеры `dict` -> модель.

Репозитории и сервисы возвращают обычные словари; роутеры не должны дублировать
логику приведения — для этого здесь собраны функции `*_to_out`.

V2 (раздел 4 контракта ARCHITECTURE-V2):

* `TrackOut` дополнен `file_type`, `genre` и кратким списком исполнителей
  `artists` (несколько исполнителей у трека, таблица `track_artists`);
* `FolderOut` дополнен `parent_folder_id`, `section`, `has_children` и
  `total_track_count` (вложенные папки и разделы «Треки» / «Другое»);
* добавлены модели дерева папок (`FolderTreeOut`), хлебных крошек
  (`FolderPathOut`), заметок (`NoteOut`, `NoteItemOut`), рекомендаций
  (`RecommendationOut`, `RecommendationsOut`) и раздела «Другое» (`OtherFileOut`).

Сигнатуры конвертеров V1 (`track_to_out`, `tracks_to_out`, `folder_to_out`, ...)
НЕ менялись: новые поля добавлены со значениями по умолчанию, поэтому весь код
V1 продолжает работать без правок.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from backend.api.security import create_stream_token
from backend.db.repositories.folders import DEFAULT_SECTION, SECTIONS
from backend.db.repositories.tracks import artist_names_of, parse_artist_ids
from backend.services.media import FILE_TYPE_ICONS, FILE_TYPE_LABELS
from backend.services.metadata import format_duration

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from backend.services.telegram_search import RemoteAudio

logger = logging.getLogger(__name__)

#: Ограничения на пользовательский ввод (совпадают с проверками репозиториев).
MAX_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 500
MAX_TITLE_LENGTH = 300
MAX_SOURCE_LENGTH = 32

#: Ограничения заметок (совпадают с `backend.db.repositories.notes`).
MAX_NOTE_TITLE_LENGTH = 200
MAX_NOTE_ITEM_LENGTH = 1000

#: Ограничение длины имени исполнителя (совпадает с репозиторием исполнителей).
MAX_ARTIST_NAME_LENGTH = 200

#: Домены, с которых принимаются ссылки на сообщения Telegram.
TELEGRAM_LINK_HOSTS = ("t.me", "telegram.me", "telegram.dog")

#: Тип файла по умолчанию для раздела «Треки».
DEFAULT_FILE_TYPE = "audio"

#: Тип файла по умолчанию для раздела «Другое» (когда тип почему-то не заполнен).
DEFAULT_OTHER_FILE_TYPE = "document"

#: Значок файла неизвестного типа.
DEFAULT_FILE_ICON = "📎"

#: Предел глубины при разборе дерева папок: защита от испорченных данных.
#: Репозиторий не даёт вложенность больше `folders.MAX_FOLDER_DEPTH` (16).
MAX_TREE_DEPTH = 32


# ===========================================================================
# Ответы
# ===========================================================================


class ArtistBriefOut(BaseModel):
    """Краткое представление исполнителя внутри карточки трека."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class TrackOut(BaseModel):
    """Трек в том виде, в котором его получает Mini App."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    artist: str | None = None
    album: str | None = None
    duration: int = 0
    duration_label: str = "—"
    file_size: int = 0
    mime_type: str | None = None
    file_type: str = DEFAULT_FILE_TYPE
    genre: str | None = None
    folder_id: int | None = None
    folder_name: str | None = None
    artist_id: int | None = None
    artists: list[ArtistBriefOut] = Field(
        default_factory=list,
        description="Все исполнители трека, первым — основной",
    )
    album_id: int | None = None
    source: str = "upload"
    play_count: int = 0
    last_played_at: str | None = None
    created_at: str = ""
    is_favourite: bool = False
    stream_url: str
    cover_url: str | None = None
    position: int | None = None


class FolderOut(BaseModel):
    """Папка пользователя (вложенная, с разделом и агрегатами по трекам)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    parent_folder_id: int | None = None
    section: str = DEFAULT_SECTION
    is_artist_folder: bool = False
    has_children: bool = False
    track_count: int = Field(default=0, description="Треки самой папки")
    total_track_count: int = Field(default=0, description="Треки вместе с подпапками")
    created_at: str = ""


class FolderTreeOut(FolderOut):
    """Узел дерева папок: та же папка плюс вложенные узлы."""

    children: list["FolderTreeOut"] = Field(default_factory=list)


class FolderPathOut(BaseModel):
    """Хлебные крошки: путь от корня раздела до папки включительно."""

    folder_id: int
    section: str = DEFAULT_SECTION
    items: list[FolderOut] = Field(default_factory=list)


class NoteItemOut(BaseModel):
    """Пункт заметки со списком дел."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    note_id: int
    text: str
    is_done: bool = False
    position: int = 0
    created_at: str = ""


class NoteOut(BaseModel):
    """Заметка со списком пунктов и счётчиками «выполнено / всего»."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    items_total: int = 0
    items_done: int = 0
    created_at: str = ""
    updated_at: str = ""
    items: list[NoteItemOut] = Field(default_factory=list)


class RecommendationOut(BaseModel):
    """Один рекомендованный исполнитель (`services.recommendations.Recommendation`)."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    normalized_name: str = ""
    total_plays: int = 0
    listeners: int = 0
    reason: str = ""
    local_artist_id: int | None = None
    sample_track_ids: list[int] = Field(default_factory=list)


class RecommendationsOut(BaseModel):
    """Ответ раздела рекомендаций: две категории, недобор и честное пояснение."""

    popular: list[RecommendationOut] = Field(default_factory=list)
    underground: list[RecommendationOut] = Field(default_factory=list)
    shortfall: dict[str, int] = Field(default_factory=dict)
    note: str | None = None


class OtherFileOut(BaseModel):
    """Файл раздела «Другое»: документ, видео, кружочек или голосовое."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    file_type: str = DEFAULT_OTHER_FILE_TYPE
    file_type_label: str = ""
    icon: str = DEFAULT_FILE_ICON
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int = 0
    duration: int = 0
    duration_label: str = "—"
    folder_id: int | None = None
    folder_name: str | None = None
    created_at: str = ""
    download_url: str


class ArtistOut(BaseModel):
    """Исполнитель с агрегатами и отметкой «прослушано»."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    is_listened: bool = False
    track_count: int = 0
    play_count: int = 0
    folder_id: int | None = None


class AlbumOut(BaseModel):
    """Альбом («группа») с именем исполнителя и числом треков."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    artist_id: int | None = None
    artist_name: str | None = None
    year: int | None = None
    track_count: int = 0


class PlaylistOut(BaseModel):
    """Плейлист без состава треков."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    track_count: int = 0
    total_duration: int = 0
    created_at: str = ""
    updated_at: str = ""


class PlaylistDetailOut(PlaylistOut):
    """Плейлист вместе с треками в порядке `position`."""

    tracks: list[TrackOut] = Field(default_factory=list)


class SectionOut(BaseModel):
    """Раздел статистики: ключ, заголовок, общее число треков и первые элементы."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    title: str
    count: int = 0
    items: list[TrackOut] = Field(default_factory=list)


class StatsOverviewOut(BaseModel):
    """Сводка статистики для главной страницы Mini App."""

    counts: dict[str, int] = Field(default_factory=dict)
    sections: list[SectionOut] = Field(default_factory=list)


class SearchResultOut(BaseModel):
    """Результат нечёткого поиска по библиотеке."""

    query: str = ""
    tracks: list[TrackOut] = Field(default_factory=list)
    albums: list[AlbumOut] = Field(default_factory=list)
    artists: list[ArtistOut] = Field(default_factory=list)
    folders: list[FolderOut] = Field(default_factory=list)


class RemoteAudioOut(BaseModel):
    """Найденный в Telegram аудиофайл, ещё не импортированный в библиотеку."""

    model_config = ConfigDict(from_attributes=True)

    token: str
    title: str
    performer: str | None = None
    duration: int = 0
    duration_label: str = "—"
    file_size: int = 0
    chat_title: str = ""
    link: str = ""


class SettingsOut(BaseModel):
    """Персональные настройки пользователя."""

    model_config = ConfigDict(from_attributes=True)

    auto_sort_enabled: bool = True
    frequent_threshold: int = 10
    rare_min: int = 1
    rare_max: int = 5
    fuzzy_threshold: int = 60


# Рекурсивная модель: ссылку на саму себя pydantic разрешает отложенно.
FolderTreeOut.model_rebuild()


# ===========================================================================
# Запросы
# ===========================================================================


class FolderCreateIn(BaseModel):
    """Создание папки: корневой или вложенной, в разделе «Треки» или «Другое»."""

    name: str = Field(..., description="Название папки")
    parent_folder_id: int | None = Field(
        default=None, description="Родительская папка (null — корень раздела)"
    )
    section: str = Field(
        default=DEFAULT_SECTION, description="Раздел: music (Треки) или other (Другое)"
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _clean_required_text(value, "Название папки", MAX_NAME_LENGTH)

    @field_validator("parent_folder_id")
    @classmethod
    def _validate_parent(cls, value: int | None) -> int | None:
        return _positive_or_none(value, "Идентификатор родительской папки")

    @field_validator("section")
    @classmethod
    def _validate_section(cls, value: str | None) -> str:
        return _clean_section(value)


class FolderMoveIn(BaseModel):
    """Перенос папки к другому родителю (`parent_folder_id = null` — в корень)."""

    parent_folder_id: int | None = Field(
        default=None, description="Новая родительская папка (null — корень раздела)"
    )

    @field_validator("parent_folder_id")
    @classmethod
    def _validate_parent(cls, value: int | None) -> int | None:
        return _positive_or_none(value, "Идентификатор родительской папки")


class FolderUpdateIn(BaseModel):
    """Переименование папки."""

    name: str = Field(..., description="Новое название папки")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _clean_required_text(value, "Название папки", MAX_NAME_LENGTH)


class MoveTracksIn(BaseModel):
    """Групповое перемещение треков в папку (`folder_id = null` — без папки)."""

    track_ids: list[int] = Field(default_factory=list, description="Идентификаторы треков")
    folder_id: int | None = Field(default=None, description="Папка назначения")

    @field_validator("track_ids")
    @classmethod
    def _validate_track_ids(cls, value: list[int]) -> list[int]:
        return _unique_ids(value, allow_empty=False)


class TrackUpdateIn(BaseModel):
    """Редактирование трека. Переданы только изменяемые поля.

    `artist_ids` (состав исполнителей, V2) НЕ попадает в :meth:`updates`:
    его записывает отдельный вызов `tracks_repo.set_track_artists`, а не
    `tracks_repo.update_track`. Читать это поле нужно через :meth:`artists_update`.
    """

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
    folder_id: int | None = None
    artist_ids: list[int] | None = Field(
        default=None, description="Все исполнители трека, первым — основной"
    )

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_required_text(value, "Название трека", MAX_TITLE_LENGTH)

    @field_validator("artist", "album", "genre")
    @classmethod
    def _validate_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) > MAX_TITLE_LENGTH:
            raise ValueError(f"Значение слишком длинное (максимум {MAX_TITLE_LENGTH} символов)")
        return cleaned or None

    @field_validator("artist_ids")
    @classmethod
    def _validate_artist_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        return _unique_ids(value, allow_empty=True, label="исполнителей")

    def updates(self) -> dict[str, Any]:
        """Только явно переданные поля — чтобы отличить «не менять» от «очистить»."""
        return self.model_dump(exclude_unset=True, exclude={"artist_ids"})

    def artists_update(self) -> list[int] | None:
        """Новый состав исполнителей или `None`, если поле не передавали."""
        if "artist_ids" not in self.model_fields_set:
            return None
        return list(self.artist_ids or [])


class PlayIn(BaseModel):
    """Регистрация прослушивания."""

    source: str = Field(default="web", description="Источник: web, bot, bot_playlist")

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            return "web"
        return cleaned[:MAX_SOURCE_LENGTH]


class PlaylistCreateIn(BaseModel):
    """Создание плейлиста."""

    name: str = Field(..., description="Название плейлиста")
    description: str | None = Field(default=None, description="Описание")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _clean_required_text(value, "Название плейлиста", MAX_NAME_LENGTH)

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        return _clean_optional_text(value, "Описание плейлиста", MAX_DESCRIPTION_LENGTH)


class PlaylistUpdateIn(BaseModel):
    """Изменение плейлиста.

    Поле, которого нет в теле запроса, остаётся `None` — репозиторий трактует это
    как «не менять». Явно переданное `description` (пустая строка или `null`)
    означает «очистить»: валидатор приводит его к пустой строке, а
    `playlists_repo.update_playlist` записывает по ней `NULL`.
    """

    name: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_required_text(value, "Название плейлиста", MAX_NAME_LENGTH)

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str:
        # Валидатор вызывается только для явно переданного поля, поэтому и `null`,
        # и пустая строка здесь означают «очистить описание» — репозиторий ждёт
        # для этого именно пустую строку, а не `None` («не менять»).
        cleaned = _clean_optional_text(
            value, "Описание плейлиста", MAX_DESCRIPTION_LENGTH
        )
        return cleaned or ""

    def updates(self) -> dict[str, Any]:
        """Только явно переданные поля."""
        return self.model_dump(exclude_unset=True)


class PlaylistAddIn(BaseModel):
    """Добавление треков в плейлист."""

    track_ids: list[int] = Field(default_factory=list)

    @field_validator("track_ids")
    @classmethod
    def _validate_track_ids(cls, value: list[int]) -> list[int]:
        return _unique_ids(value, allow_empty=False)


class PlaylistOrderIn(BaseModel):
    """Новый порядок треков плейлиста (drag-and-drop)."""

    track_ids: list[int] = Field(default_factory=list)

    @field_validator("track_ids")
    @classmethod
    def _validate_track_ids(cls, value: list[int]) -> list[int]:
        return _unique_ids(value, allow_empty=True)


class SettingsUpdateIn(BaseModel):
    """Частичное обновление персональных настроек."""

    auto_sort_enabled: bool | None = None
    frequent_threshold: int | None = Field(default=None, ge=1, le=1_000_000)
    rare_min: int | None = Field(default=None, ge=1, le=1_000_000)
    rare_max: int | None = Field(default=None, ge=1, le=1_000_000)
    fuzzy_threshold: int | None = Field(default=None, ge=0, le=100)

    def updates(self) -> dict[str, Any]:
        """Только переданные и непустые поля — их принимает `users_repo.update_settings`."""
        return {
            key: value
            for key, value in self.model_dump(exclude_unset=True).items()
            if value is not None
        }


class ImportRemoteIn(BaseModel):
    """Импорт найденного в Telegram аудио по токену результата поиска."""

    token: str = Field(..., description="Токен результата поиска")

    @field_validator("token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("Не указан результат поиска для импорта")
        return cleaned


class ImportLinkIn(BaseModel):
    """Импорт аудио по ссылке на сообщение Telegram."""

    url: str = Field(..., description="Ссылка вида https://t.me/channel/123")

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("Не указана ссылка на сообщение Telegram")
        lowered = cleaned.lower()
        if not any(host in lowered for host in TELEGRAM_LINK_HOSTS):
            raise ValueError("Ссылка должна вести на сообщение Telegram, например https://t.me/channel/123")
        return cleaned


class AssignFolderIn(BaseModel):
    """Перемещение трека в папку по названию (папка создаётся при необходимости)."""

    folder_name: str = Field(..., description="Название папки")

    @field_validator("folder_name")
    @classmethod
    def _validate_folder_name(cls, value: str) -> str:
        return _clean_required_text(value, "Название папки", MAX_NAME_LENGTH)


class ListenedIn(BaseModel):
    """Отметка «прослушано» у исполнителя. `null` — переключить."""

    is_listened: bool | None = None


class MoveTrackIn(BaseModel):
    """Перемещение одного трека (`folder_id = null` — убрать из папки)."""

    folder_id: int | None = None


class TrackRenameIn(BaseModel):
    """Переименование трека (ТЗ п. 9)."""

    title: str = Field(..., description="Новое название трека")

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _clean_required_text(value, "Название трека", MAX_TITLE_LENGTH)


class NoteCreateIn(BaseModel):
    """Создание или переименование заметки."""

    title: str = Field(..., description="Название заметки")

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _clean_required_text(value, "Название заметки", MAX_NOTE_TITLE_LENGTH)


class NoteItemCreateIn(BaseModel):
    """Новый пункт заметки (добавляется в конец списка)."""

    text: str = Field(..., description="Текст пункта")

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _clean_required_text(value, "Текст пункта", MAX_NOTE_ITEM_LENGTH)


class NoteItemUpdateIn(BaseModel):
    """Изменение пункта заметки: текст и/или отметка «выполнено».

    Оба поля необязательны. `is_done = null` при отсутствии `text` роутер
    трактует как «переключить отметку» (`notes_repo.toggle_item`).
    """

    text: str | None = None
    is_done: bool | None = None

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_required_text(value, "Текст пункта", MAX_NOTE_ITEM_LENGTH)

    def updates(self) -> dict[str, Any]:
        """Только явно переданные поля."""
        return self.model_dump(exclude_unset=True)


class NoteOrderIn(BaseModel):
    """Новый порядок пунктов заметки (drag-and-drop).

    Принимает поле `item_ids`; ради удобства клиентов допустимы синонимы
    `ids` и `ordered_item_ids`.
    """

    model_config = ConfigDict(populate_by_name=True)

    item_ids: list[int] = Field(
        default_factory=list,
        validation_alias=AliasChoices("item_ids", "ids", "ordered_item_ids"),
        description="Идентификаторы пунктов в нужном порядке",
    )

    @field_validator("item_ids")
    @classmethod
    def _validate_item_ids(cls, value: list[int]) -> list[int]:
        return _unique_ids(value, allow_empty=True, label="пунктов")


class ArtistRenameIn(BaseModel):
    """Переименование исполнителя (ТЗ п. 10). При совпадении имён — слияние."""

    name: str = Field(..., description="Новое имя исполнителя")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _clean_required_text(value, "Имя исполнителя", MAX_ARTIST_NAME_LENGTH)


class ArtistFolderIn(BaseModel):
    """Привязка исполнителя к папке (`folder_id = null` — отвязать)."""

    folder_id: int | None = Field(default=None, description="Папка исполнителя")

    @field_validator("folder_id")
    @classmethod
    def _validate_folder_id(cls, value: int | None) -> int | None:
        return _positive_or_none(value, "Идентификатор папки")


class ArtistCreateIn(BaseModel):
    """Ручное создание исполнителя (ТЗ п. 19)."""

    name: str = Field(..., description="Имя исполнителя")
    folder_id: int | None = Field(
        default=None, description="Папка исполнителя (необязательно)"
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _clean_required_text(value, "Имя исполнителя", MAX_ARTIST_NAME_LENGTH)

    @field_validator("folder_id")
    @classmethod
    def _validate_folder_id(cls, value: int | None) -> int | None:
        return _positive_or_none(value, "Идентификатор папки")


# ===========================================================================
# Конвертеры dict -> модель
# ===========================================================================


def artist_brief_to_out(source: Any) -> ArtistBriefOut | None:
    """Собрать `ArtistBriefOut` из словаря или объекта; `None` — данных не хватает."""
    artist_id = _as_optional_int(_field(source, "id"))
    name = _as_text(_field(source, "name")).strip()
    if artist_id is None or not name:
        return None
    return ArtistBriefOut(id=artist_id, name=name)


def artists_brief_to_out(items: Iterable[Any]) -> list[ArtistBriefOut]:
    """Преобразовать список исполнителей в краткий вид (пропуская неполные записи)."""
    result: list[ArtistBriefOut] = []
    for item in items or []:
        brief = artist_brief_to_out(item)
        if brief is not None:
            result.append(brief)
    return result


def _track_artists(track: dict) -> list[ArtistBriefOut]:
    """Исполнители трека: готовый список, агрегаты `artist_ids`/имена или основной.

    Роутер может заранее положить в трек ключ `artists`
    (`tracks_repo.artists_by_tracks`) — тогда берётся он. Иначе исполнители
    собираются из агрегатов строки трека; порядок сохраняется (основной первым).

    Имена берутся из `tracks_repo.artist_names_of` — он разбирает машинный
    агрегат `artist_names_packed` (разделитель \\x1f), где имя с запятой
    («Earth, Wind & Fire») остаётся одним куском. Если строка трека собрана без
    этого агрегата и число имён разошлось с числом идентификаторов, пары
    НЕ восстанавливаются: позиционное сопоставление подписало бы идентификатор
    чужим именем и потеряло бы последнего исполнителя.
    """
    prepared = track.get("artists")
    if isinstance(prepared, (list, tuple)):
        briefs = artists_brief_to_out(prepared)
        if briefs:
            return briefs

    ids = parse_artist_ids(track.get("artist_ids"))
    names = artist_names_of(track)
    if ids and names and len(ids) != len(names):
        # Имя с запятой внутри разошлось с идентификаторами — не гадаем,
        # отдаём основного исполнителя (ниже), а не случайные пары.
        logger.warning(
            "У трека #%s %d идентификаторов исполнителей и %d имён — "
            "состав не восстановлен, отдаю основного исполнителя",
            track.get("id"),
            len(ids),
            len(names),
        )
    else:
        briefs = [
            ArtistBriefOut(id=artist_id, name=name.strip())
            for artist_id, name in zip(ids, names)
            if name.strip()
        ]
        if briefs:
            return briefs

    primary_id = _as_optional_int(track.get("artist_id"))
    primary_name = _as_text(track.get("artist")).strip()
    if primary_id is not None and primary_name:
        return [ArtistBriefOut(id=primary_id, name=primary_name)]
    return []


def track_to_out(track: dict, user_id: int) -> TrackOut:
    """Собрать `TrackOut` из словаря репозитория, добавив подписанные ссылки."""
    track_id = int(track["id"])
    duration = _as_int(track.get("duration"))
    token = create_stream_token(user_id, track_id)

    cover_url: str | None = None
    if track.get("thumb_file_id"):
        cover_url = f"/api/tracks/{track_id}/cover?token={token}"

    return TrackOut(
        id=track_id,
        title=_as_text(track.get("title")) or "Без названия",
        artist=_as_optional_text(track.get("artist")),
        album=_as_optional_text(track.get("album")),
        duration=duration,
        duration_label=format_duration(duration),
        file_size=_as_int(track.get("file_size")),
        mime_type=_as_optional_text(track.get("mime_type")),
        file_type=_as_text(track.get("file_type")) or DEFAULT_FILE_TYPE,
        genre=_as_optional_text(track.get("genre")),
        folder_id=_as_optional_int(track.get("folder_id")),
        folder_name=_as_optional_text(track.get("folder_name")),
        artist_id=_as_optional_int(track.get("artist_id")),
        artists=_track_artists(track),
        album_id=_as_optional_int(track.get("album_id")),
        source=_as_text(track.get("source")) or "upload",
        play_count=_as_int(track.get("play_count")),
        last_played_at=_as_optional_text(track.get("last_played_at")),
        created_at=_as_text(track.get("created_at")),
        is_favourite=bool(track.get("is_favourite")),
        stream_url=f"/api/tracks/{track_id}/stream?token={token}",
        cover_url=cover_url,
        position=_as_optional_int(track.get("position")),
    )


def other_to_out(track: dict, user_id: int) -> OtherFileOut:
    """Собрать `OtherFileOut` (раздел «Другое») с подписанной ссылкой на скачивание."""
    track_id = int(track["id"])
    duration = _as_int(track.get("duration"))
    token = create_stream_token(user_id, track_id)
    file_type = _as_text(track.get("file_type")) or DEFAULT_OTHER_FILE_TYPE

    return OtherFileOut(
        id=track_id,
        title=_as_text(track.get("title")) or "Файл",
        file_type=file_type,
        file_type_label=FILE_TYPE_LABELS.get(file_type, file_type),
        icon=FILE_TYPE_ICONS.get(file_type, DEFAULT_FILE_ICON),
        file_name=_as_optional_text(track.get("file_name")),
        mime_type=_as_optional_text(track.get("mime_type")),
        file_size=_as_int(track.get("file_size")),
        duration=duration,
        duration_label=format_duration(duration),
        folder_id=_as_optional_int(track.get("folder_id")),
        folder_name=_as_optional_text(track.get("folder_name")),
        created_at=_as_text(track.get("created_at")),
        download_url=f"/api/other/{track_id}/download?token={token}",
    )


def others_to_out(tracks: Iterable[dict], user_id: int) -> list[OtherFileOut]:
    """Преобразовать список файлов раздела «Другое»."""
    return [other_to_out(track, user_id) for track in tracks or []]


def tracks_to_out(tracks: Iterable[dict], user_id: int) -> list[TrackOut]:
    """Преобразовать список треков."""
    return [track_to_out(track, user_id) for track in tracks or []]


def folder_to_out(folder: dict) -> FolderOut:
    """Собрать `FolderOut` из словаря репозитория.

    `total_track_count` при отсутствии в данных приравнивается к `track_count`
    (папка без подпапок), `has_children` — к непустому списку `children`.
    """
    own_count = _as_int(folder.get("track_count"))
    has_children = bool(folder.get("has_children")) or bool(folder.get("children"))
    return FolderOut(
        id=int(folder["id"]),
        name=_as_text(folder.get("name")),
        parent_folder_id=_as_optional_int(folder.get("parent_folder_id")),
        section=_as_text(folder.get("section")) or DEFAULT_SECTION,
        is_artist_folder=bool(folder.get("is_artist_folder")),
        has_children=has_children,
        track_count=own_count,
        total_track_count=_as_int(folder.get("total_track_count"), default=own_count),
        created_at=_as_text(folder.get("created_at")),
    )


def folders_to_out(folders: Iterable[dict]) -> list[FolderOut]:
    """Преобразовать список папок."""
    return [folder_to_out(folder) for folder in folders or []]


def folder_node_to_out(node: dict, *, depth: int = 0) -> FolderTreeOut:
    """Собрать узел дерева папок вместе с подпапками (`folders_repo.folder_tree`)."""
    base = folder_to_out(node)
    children: list[FolderTreeOut] = []
    raw_children = node.get("children")
    if isinstance(raw_children, (list, tuple)) and raw_children:
        if depth >= MAX_TREE_DEPTH:
            logger.warning(
                "Дерево папок глубже %s уровней — ветка папки %s обрезана",
                MAX_TREE_DEPTH,
                base.id,
            )
        else:
            children = [
                folder_node_to_out(child, depth=depth + 1) for child in raw_children
            ]
    return FolderTreeOut(**base.model_dump(), children=children)


def folder_tree_to_out(nodes: Iterable[dict]) -> list[FolderTreeOut]:
    """Преобразовать дерево папок (список корней с ключом `children`)."""
    return [folder_node_to_out(node) for node in nodes or []]


def folder_path_to_out(
    crumbs: Iterable[dict],
    *,
    folder_id: int | None = None,
    section: str | None = None,
) -> FolderPathOut:
    """Собрать хлебные крошки (`folders_repo.folder_path`).

    `folder_id` и `section` берутся из последней крошки, если не заданы явно.
    """
    items = folders_to_out(crumbs)
    resolved_id = folder_id if folder_id is not None else (items[-1].id if items else 0)
    resolved_section = section or (items[-1].section if items else DEFAULT_SECTION)
    return FolderPathOut(
        folder_id=int(resolved_id),
        section=resolved_section,
        items=items,
    )


def note_item_to_out(item: dict) -> NoteItemOut:
    """Собрать `NoteItemOut` из словаря репозитория заметок."""
    return NoteItemOut(
        id=int(item["id"]),
        note_id=_as_int(item.get("note_id")),
        text=_as_text(item.get("text")),
        is_done=bool(item.get("is_done")),
        position=_as_int(item.get("position")),
        created_at=_as_text(item.get("created_at")),
    )


def note_items_to_out(items: Iterable[dict]) -> list[NoteItemOut]:
    """Преобразовать список пунктов заметки."""
    return [note_item_to_out(item) for item in items or []]


def note_to_out(note: dict) -> NoteOut:
    """Собрать `NoteOut`; пункты берутся из ключа `items`, если он есть."""
    items = note_items_to_out(note.get("items") or [])
    total = _as_int(note.get("items_total"), default=len(items))
    done = _as_int(
        note.get("items_done"),
        default=sum(1 for item in items if item.is_done),
    )
    return NoteOut(
        id=int(note["id"]),
        title=_as_text(note.get("title")),
        items_total=total,
        items_done=done,
        created_at=_as_text(note.get("created_at")),
        updated_at=_as_text(note.get("updated_at")),
        items=items,
    )


def notes_to_out(notes: Iterable[dict]) -> list[NoteOut]:
    """Преобразовать список заметок."""
    return [note_to_out(note) for note in notes or []]


def recommendation_to_out(item: Any) -> RecommendationOut:
    """Собрать `RecommendationOut` из dataclass `Recommendation` или словаря."""
    raw_ids = _field(item, "sample_track_ids") or []
    sample_ids: list[int] = []
    for value in raw_ids:
        number = _as_optional_int(value)
        if number is not None:
            sample_ids.append(number)

    return RecommendationOut(
        name=_as_text(_field(item, "name")),
        normalized_name=_as_text(_field(item, "normalized_name")),
        total_plays=_as_int(_field(item, "total_plays")),
        listeners=_as_int(_field(item, "listeners")),
        reason=_as_text(_field(item, "reason")),
        local_artist_id=_as_optional_int(_field(item, "local_artist_id")),
        sample_track_ids=sample_ids,
    )


def recommendations_to_out(items: Iterable[Any]) -> list[RecommendationOut]:
    """Преобразовать список рекомендаций."""
    return [recommendation_to_out(item) for item in items or []]


def recommendations_result_to_out(result: dict) -> RecommendationsOut:
    """Собрать ответ раздела рекомендаций (`services.recommendations.build`)."""
    shortfall = {
        str(key): _as_int(value)
        for key, value in (result.get("shortfall") or {}).items()
    }
    return RecommendationsOut(
        popular=recommendations_to_out(result.get("popular") or []),
        underground=recommendations_to_out(result.get("underground") or []),
        shortfall=shortfall,
        note=_as_optional_text(result.get("note")),
    )


def artist_to_out(artist: dict) -> ArtistOut:
    """Собрать `ArtistOut` из словаря репозитория."""
    return ArtistOut(
        id=int(artist["id"]),
        name=_as_text(artist.get("name")),
        is_listened=bool(artist.get("is_listened")),
        track_count=_as_int(artist.get("track_count")),
        play_count=_as_int(artist.get("play_count")),
        folder_id=_as_optional_int(artist.get("folder_id")),
    )


def artists_to_out(artists: Iterable[dict]) -> list[ArtistOut]:
    """Преобразовать список исполнителей."""
    return [artist_to_out(artist) for artist in artists or []]


def album_to_out(album: dict) -> AlbumOut:
    """Собрать `AlbumOut` из словаря репозитория."""
    return AlbumOut(
        id=int(album["id"]),
        title=_as_text(album.get("title")),
        artist_id=_as_optional_int(album.get("artist_id")),
        artist_name=_as_optional_text(album.get("artist_name")),
        year=_as_optional_int(album.get("year")),
        track_count=_as_int(album.get("track_count")),
    )


def albums_to_out(albums: Iterable[dict]) -> list[AlbumOut]:
    """Преобразовать список альбомов."""
    return [album_to_out(album) for album in albums or []]


def playlist_to_out(playlist: dict) -> PlaylistOut:
    """Собрать `PlaylistOut` из словаря репозитория."""
    return PlaylistOut(
        id=int(playlist["id"]),
        name=_as_text(playlist.get("name")),
        description=_as_optional_text(playlist.get("description")),
        track_count=_as_int(playlist.get("track_count")),
        total_duration=_as_int(playlist.get("total_duration")),
        created_at=_as_text(playlist.get("created_at")),
        updated_at=_as_text(playlist.get("updated_at")),
    )


def playlists_to_out(playlists: Iterable[dict]) -> list[PlaylistOut]:
    """Преобразовать список плейлистов."""
    return [playlist_to_out(playlist) for playlist in playlists or []]


def playlist_detail_to_out(
    playlist: dict, tracks: Iterable[dict], user_id: int
) -> PlaylistDetailOut:
    """Собрать плейлист вместе с составом треков."""
    base = playlist_to_out(playlist)
    items = tracks_to_out(tracks, user_id)
    data = base.model_dump()
    if not playlist.get("track_count"):
        data["track_count"] = len(items)
    return PlaylistDetailOut(**data, tracks=items)


def section_to_out(section: dict, user_id: int) -> SectionOut:
    """Собрать раздел статистики."""
    items = tracks_to_out(section.get("items") or [], user_id)
    return SectionOut(
        key=_as_text(section.get("key")),
        title=_as_text(section.get("title")),
        count=_as_int(section.get("count")),
        items=items,
    )


def overview_to_out(overview: dict, user_id: int) -> StatsOverviewOut:
    """Собрать сводку статистики (`stats_repo.overview`)."""
    counts = {
        str(key): _as_int(value) for key, value in (overview.get("counts") or {}).items()
    }
    sections = [
        section_to_out(section, user_id) for section in (overview.get("sections") or [])
    ]
    return StatsOverviewOut(counts=counts, sections=sections)


def search_to_out(result: dict, user_id: int) -> SearchResultOut:
    """Собрать результат поиска (`search.search_all`)."""
    return SearchResultOut(
        query=_as_text(result.get("query")),
        tracks=tracks_to_out(result.get("tracks") or [], user_id),
        albums=albums_to_out(result.get("albums") or []),
        artists=artists_to_out(result.get("artists") or []),
        folders=folders_to_out(result.get("folders") or []),
    )


def remote_to_out(remote: "RemoteAudio | dict[str, Any]") -> RemoteAudioOut:
    """Собрать `RemoteAudioOut` из `RemoteAudio` (dataclass) или словаря."""
    duration = _as_int(_field(remote, "duration"))
    return RemoteAudioOut(
        token=_as_text(_field(remote, "token")),
        title=_as_text(_field(remote, "title")) or "Без названия",
        performer=_as_optional_text(_field(remote, "performer")),
        duration=duration,
        duration_label=format_duration(duration),
        file_size=_as_int(_field(remote, "file_size")),
        chat_title=_as_text(_field(remote, "chat_title")),
        link=_as_text(_field(remote, "link")),
    )


def remotes_to_out(items: Iterable[Any]) -> list[RemoteAudioOut]:
    """Преобразовать список результатов поиска по Telegram."""
    return [remote_to_out(item) for item in items or []]


def settings_to_out(row: dict) -> SettingsOut:
    """Собрать `SettingsOut` из строки `user_settings`."""
    return SettingsOut(
        auto_sort_enabled=bool(row.get("auto_sort_enabled", True)),
        frequent_threshold=_as_int(row.get("frequent_threshold"), default=10),
        rare_min=_as_int(row.get("rare_min"), default=1),
        rare_max=_as_int(row.get("rare_max"), default=5),
        fuzzy_threshold=_as_int(row.get("fuzzy_threshold"), default=60),
    )


# ===========================================================================
# Вспомогательные функции
# ===========================================================================


def _field(source: Any, name: str, default: Any = None) -> Any:
    """Прочитать поле у объекта или словаря."""
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _as_int(value: Any, default: int = 0) -> int:
    """Мягко привести значение к int."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.debug("Ожидалось число, получено %r — используется %s", value, default)
        return default


def _as_optional_int(value: Any) -> int | None:
    """Привести значение к int или вернуть None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str:
    """Привести значение к строке (None -> пустая строка)."""
    if value is None:
        return ""
    return str(value)


def _as_optional_text(value: Any) -> str | None:
    """Привести значение к непустой строке или None."""
    if value is None:
        return None
    text = str(value)
    return text or None


def _clean_required_text(value: str | None, label: str, max_length: int) -> str:
    """Проверить обязательное текстовое поле."""
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{label} не может быть пустым")
    if len(cleaned) > max_length:
        raise ValueError(f"{label} слишком длинное (максимум {max_length} символов)")
    return cleaned


def _clean_optional_text(value: str | None, label: str, max_length: int) -> str | None:
    """Проверить необязательное текстовое поле (пустая строка -> None)."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        raise ValueError(f"{label} слишком длинное (максимум {max_length} символов)")
    return cleaned


def _unique_ids(
    values: Sequence[int], *, allow_empty: bool, label: str = "треков"
) -> list[int]:
    """Убрать дубликаты, сохранив порядок; проверить, что список не пуст."""
    result: list[int] = []
    seen: set[int] = set()
    for raw in values or []:
        try:
            number = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Идентификаторы {label} должны быть числами") from exc
        if number in seen:
            continue
        seen.add(number)
        result.append(number)
    if not result and not allow_empty:
        raise ValueError("Не выбран ни один трек")
    return result


def _positive_or_none(value: Any, label: str) -> int | None:
    """Проверить необязательный идентификатор: `None` или положительное число."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} должен быть числом") from exc
    if number <= 0:
        raise ValueError(f"{label} должен быть положительным числом")
    return number


def _clean_section(value: Any) -> str:
    """Проверить раздел библиотеки (`music` | `other`); пусто — раздел по умолчанию."""
    text = str(value or "").strip().casefold() or DEFAULT_SECTION
    if text not in SECTIONS:
        raise ValueError(
            "Неизвестный раздел. Допустимые значения: " + ", ".join(SECTIONS)
        )
    return text


__all__ = [
    "AlbumOut",
    "ArtistBriefOut",
    "ArtistCreateIn",
    "ArtistFolderIn",
    "ArtistOut",
    "ArtistRenameIn",
    "AssignFolderIn",
    "FolderCreateIn",
    "FolderMoveIn",
    "FolderOut",
    "FolderPathOut",
    "FolderTreeOut",
    "FolderUpdateIn",
    "ImportLinkIn",
    "ImportRemoteIn",
    "ListenedIn",
    "MoveTrackIn",
    "MoveTracksIn",
    "NoteCreateIn",
    "NoteItemCreateIn",
    "NoteItemOut",
    "NoteItemUpdateIn",
    "NoteOrderIn",
    "NoteOut",
    "OtherFileOut",
    "PlayIn",
    "PlaylistAddIn",
    "PlaylistCreateIn",
    "PlaylistDetailOut",
    "PlaylistOrderIn",
    "PlaylistOut",
    "PlaylistUpdateIn",
    "RecommendationOut",
    "RecommendationsOut",
    "RemoteAudioOut",
    "SearchResultOut",
    "SectionOut",
    "SettingsOut",
    "SettingsUpdateIn",
    "StatsOverviewOut",
    "TrackOut",
    "TrackRenameIn",
    "TrackUpdateIn",
    "album_to_out",
    "albums_to_out",
    "artist_brief_to_out",
    "artist_to_out",
    "artists_brief_to_out",
    "artists_to_out",
    "folder_node_to_out",
    "folder_path_to_out",
    "folder_to_out",
    "folder_tree_to_out",
    "folders_to_out",
    "note_item_to_out",
    "note_items_to_out",
    "note_to_out",
    "notes_to_out",
    "other_to_out",
    "others_to_out",
    "overview_to_out",
    "playlist_detail_to_out",
    "playlist_to_out",
    "playlists_to_out",
    "recommendation_to_out",
    "recommendations_result_to_out",
    "recommendations_to_out",
    "remote_to_out",
    "remotes_to_out",
    "search_to_out",
    "section_to_out",
    "settings_to_out",
    "track_to_out",
    "tracks_to_out",
]
