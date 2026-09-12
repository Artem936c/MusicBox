"""FSM-состояния бота MusicBox.

Состояния хранятся в `MemoryStorage` (см. `backend.bot.bot.create_dispatcher`),
поэтому после перезапуска процесса незавершённые диалоги сбрасываются — это нормально.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class FolderStates(StatesGroup):
    """Диалоги работы с папками."""

    #: Ожидание названия новой папки (`/folders` → «🆕 Новая папка»).
    waiting_name = State()
    #: Ожидание нового названия существующей папки (переименование).
    waiting_rename = State()
    #: То же переименование под альтернативным именем — используется
    #: `handlers/folders.py`. Отдельное состояние, а не псевдоним: `StatesGroup`
    #: присваивает имя объекту `State`, поэтому две ссылки на один объект
    #: сломали бы `waiting_rename`.
    waiting_new_name = State()


class PlaylistStates(StatesGroup):
    """Диалоги работы с плейлистами."""

    #: Ожидание названия нового плейлиста.
    waiting_name = State()


class SearchStates(StatesGroup):
    """Поиск по личной библиотеке."""

    #: Ожидание поискового запроса (команда `/search` без аргументов).
    waiting_query = State()
    #: Мультивыбор исполнителей-фильтров: пользователь отмечает исполнителей
    #: клавиатурой `keyboards.artist_multiselect_kb` (накопленные id лежат
    #: в данных FSM под ключом `SELECTED_ARTISTS_KEY`), затем жмёт «Готово».
    waiting_artists = State()


class TgSearchStates(StatesGroup):
    """Поиск аудио в публичных каналах Telegram."""

    #: Ожидание поискового запроса (команда `/tgsearch` без аргументов).
    waiting_query = State()


class UploadStates(StatesGroup):
    """Загрузка аудио и раскладка по папкам."""

    #: Ожидание названия новой папки для только что загруженного трека
    #: (нажата кнопка «Новая папка», `MoveCB.folder_id == FOLDER_NEW`).
    waiting_folder_name = State()


class NoteStates(StatesGroup):
    """Заметки со списками (`/notes`)."""

    #: Ожидание названия новой заметки или нового названия существующей.
    waiting_title = State()
    #: Ожидание текста пункта заметки (добавление или правка существующего;
    #: какой именно пункт правится, хендлер хранит в данных FSM).
    waiting_item = State()


class EditStates(StatesGroup):
    """Переименование объектов библиотеки (`/edit_track`, `/edit_artist`)."""

    #: Ожидание нового названия трека.
    waiting_title = State()
    #: Ожидание нового имени исполнителя (при совпадении имён произойдёт слияние).
    waiting_artist_name = State()


class OtherStates(StatesGroup):
    """Раздел «Другое»: документы, видео, кружочки, голосовые (`/other`)."""

    #: Ожидание нового названия файла раздела «Другое».
    waiting_title = State()
    #: Ожидание названия новой папки раздела «Другое» (`section='other'`).
    waiting_folder_name = State()


class BatchStates(StatesGroup):
    """Пакетная раскладка группы загруженных файлов по папкам."""

    #: Ожидание количества треков, которые нужно положить в выбранную папку.
    waiting_count = State()
    #: Ожидание названия новой папки для очередной порции треков.
    waiting_folder_name = State()


#: Ключ данных FSM со списком отмеченных исполнителей (`SearchStates.waiting_artists`).
SELECTED_ARTISTS_KEY = "selected_artist_ids"

#: Ключ данных FSM с состоянием пакетной раскладки (`services.batch_sort.BatchState`).
BATCH_STATE_KEY = "batch_state"

__all__ = [
    "BATCH_STATE_KEY",
    "SELECTED_ARTISTS_KEY",
    "BatchStates",
    "EditStates",
    "FolderStates",
    "NoteStates",
    "OtherStates",
    "PlaylistStates",
    "SearchStates",
    "TgSearchStates",
    "UploadStates",
]
