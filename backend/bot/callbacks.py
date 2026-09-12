"""Callback-фабрики бота MusicBox (aiogram `CallbackData`).

Все инлайн-кнопки бота собираются ТОЛЬКО из этих классов — так любой хендлер может
разобрать нажатие через `SomeCB.filter(...)` без ручного парсинга строк.

Ограничения Telegram: `callback_data` не длиннее 64 байт, поэтому значения полей
держим короткими (числовые идентификаторы, ключи разделов, uuid-токены).
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

# --- специальные значения folder_id в MoveCB -------------------------------

#: «Без папки» — трек остаётся в библиотеке, но не привязан к папке.
FOLDER_NONE: int = -1
#: «Новая папка» — обработчик спрашивает название (UploadStates.waiting_folder_name)
#: либо создаёт папку с именем исполнителя, если оно известно из трека.
FOLDER_NEW: int = -2

#: Заглушка для строковых полей, когда значение не нужно (пустая строка недопустима).
EMPTY: str = "-"
#: Заглушка для числовых полей, когда значение не нужно.
ZERO: int = 0


class SectionCB(CallbackData, prefix="sec"):
    """Разделы статистики.

    action: ``open`` (открыть раздел) | ``page`` (перелистнуть страницу).
    key: ключ раздела (``top``/``recent``/``unplayed``/``frequent``/``rare``),
    а также служебные списки с такой же пагинацией: ``fav``, ``search``.
    page: номер страницы, 1-based.
    """

    action: str
    key: str
    page: int


class TrackCB(CallbackData, prefix="trk", sep="|"):
    """Действия над треком.

    action: ``play`` | ``fav`` | ``addpl`` | ``info`` | ``move`` | ``delete`` | ``back``.
    ctx: контекст списка, из которого пришло нажатие — ``top``, ``recent``,
    ``folder:3``, ``pl:2``, ``art:7``, ``fav``, ``search``, ``upload``.

    Разделитель полей — ``|``, потому что контекст сам содержит ``:``.
    """

    action: str
    track_id: int
    page: int
    ctx: str


class FolderCB(CallbackData, prefix="fld"):
    """Папки.

    action: ``open`` | ``page`` | ``create`` | ``rename`` | ``delete`` | ``pick``
    | ``back`` | ``list``.
    """

    action: str
    folder_id: int
    page: int


class PlaylistCB(CallbackData, prefix="pls"):
    """Плейлисты.

    action: ``open`` | ``page`` | ``create`` | ``delete`` | ``add`` | ``remove``
    | ``play`` | ``up`` | ``down`` | ``back`` | ``list``.
    """

    action: str
    playlist_id: int
    track_id: int
    page: int


class ArtistCB(CallbackData, prefix="art"):
    """Исполнители.

    action: ``open`` | ``page`` | ``listened`` | ``tracks`` | ``back`` | ``list``.
    """

    action: str
    artist_id: int
    page: int


class TgSearchCB(CallbackData, prefix="tgs"):
    """Результаты поиска аудио в Telegram.

    action: ``import`` (сохранить в хранилище) | ``info`` (подробности).
    token: короткий токен найденного аудио (`RemoteAudio.token`).
    """

    action: str
    token: str


class MoveCB(CallbackData, prefix="mv"):
    """Перемещение трека в папку.

    folder_id: реальный id папки, либо `FOLDER_NONE` (-1) — «Без папки»,
    либо `FOLDER_NEW` (-2) — «Новая папка».
    """

    track_id: int
    folder_id: int


class NavCB(CallbackData, prefix="nav"):
    """Навигация по главному меню.

    action: ``menu`` | ``stats`` | ``folders`` | ``playlists`` | ``fav``
    | ``artists`` | ``help`` | ``noop``, а также разделы V2: ``tracks``,
    ``notes``, ``reco``, ``other``.
    """

    action: str


# ---------------------------------------------------------------------------
# V2: заметки, рекомендации, «Другое», редактирование, пакетная раскладка
# ---------------------------------------------------------------------------


class NoteCB(CallbackData, prefix="nt"):
    """Заметки со списками пунктов.

    action: ``open`` | ``page`` | ``create`` | ``rename`` | ``delete``
    | ``additem`` | ``toggle`` | ``edititem`` | ``delitem`` | ``up`` | ``down``
    | ``list`` | ``back``.
    note_id: идентификатор заметки (0 — заметка ещё не выбрана).
    item_id: идентификатор пункта (0 — действие относится ко всей заметке).
    page: номер страницы списка, 1-based.
    """

    action: str
    note_id: int
    item_id: int
    page: int


class RecoCB(CallbackData, prefix="rc"):
    """Рекомендации исполнителей.

    action: ``open`` | ``page`` | ``refresh`` | ``back``.
    kind: категория — ``popular`` (популярные) | ``underground`` (менее известные);
    когда категория не важна, передаётся `EMPTY`.
    page: номер страницы, 1-based.
    """

    action: str
    kind: str
    page: int


class OtherCB(CallbackData, prefix="ot"):
    """Раздел «Другое»: документы, видео, кружочки и голосовые.

    action: ``open`` (папка) | ``page`` | ``file`` (карточка файла) | ``send``
    | ``move`` | ``delete`` | ``create`` (папка) | ``list`` | ``back``.
    folder_id: id папки раздела ``other`` (0 — корень раздела).
    track_id: id записи в `tracks` (0 — действие относится к папке).
    page: номер страницы, 1-based.
    """

    action: str
    folder_id: int
    track_id: int
    page: int


class EditCB(CallbackData, prefix="ed"):
    """Переименование объектов библиотеки.

    action: ``rename`` (спросить новое название) | ``delete`` | ``confirm``
    | ``cancel``.
    kind: тип объекта — ``track`` | ``artist``.
    target_id: id переименовываемого объекта.
    """

    action: str
    kind: str
    target_id: int


class BatchCB(CallbackData, prefix="bt"):
    """Пакетная раскладка загруженной группы файлов по папкам.

    action: ``pick`` (в существующую папку) | ``new`` (создать папку)
    | ``count`` (спросить количество) | ``all`` (все оставшиеся)
    | ``skip`` | ``done`` | ``cancel``.
    folder_id: id папки-назначения, либо `FOLDER_NONE` / `FOLDER_NEW`.
    count: сколько треков положить в папку (0 — все оставшиеся).
    """

    action: str
    folder_id: int
    count: int


class ArtistPickCB(CallbackData, prefix="ap"):
    """Мультивыбор исполнителей (фильтр поиска по нескольким исполнителям).

    action: ``toggle`` (отметить/снять) | ``page`` | ``done`` (применить)
    | ``clear`` (снять все отметки) | ``cancel``.
    artist_id: id исполнителя (0 — действие не относится к конкретному исполнителю).
    page: номер страницы списка, 1-based.
    """

    action: str
    artist_id: int
    page: int


__all__ = [
    "EMPTY",
    "FOLDER_NEW",
    "FOLDER_NONE",
    "ZERO",
    "ArtistCB",
    "ArtistPickCB",
    "BatchCB",
    "EditCB",
    "FolderCB",
    "MoveCB",
    "NavCB",
    "NoteCB",
    "OtherCB",
    "PlaylistCB",
    "RecoCB",
    "SectionCB",
    "TgSearchCB",
    "TrackCB",
]
