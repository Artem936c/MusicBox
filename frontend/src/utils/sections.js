/**
 * Разделы приложения и разделы статистики.
 *
 * SECTIONS — пять разделов статистики (контракт V1, раздел 16). Их ключи ДОЛЖНЫ
 * совпадать с backend/db/repositories/stats.py::SECTION_KEYS.
 * APP_SECTIONS — навигация по всему приложению (контракт V2, раздел 6): её
 * использует главная страница и блок «ещё», потому что в TabBar помещается
 * только пять вкладок.
 */

export const SECTIONS = [
  { key: 'top', title: 'Самые часто прослушиваемые', icon: '🔥' },
  { key: 'recent', title: 'Недавно добавленные', icon: '🆕' },
  { key: 'unplayed', title: 'Ни разу не проигранные', icon: '💤' },
  { key: 'frequent', title: 'Часто прослушиваемые', icon: '📈' },
  { key: 'rare', title: 'Редко прослушиваемые', icon: '📉' },
]

/** Только ключи разделов — удобно для проверки параметра маршрута. */
export const SECTION_KEYS = SECTIONS.map((section) => section.key)

/** Тексты для пустых разделов — показываются в EmptyState. */
export const SECTION_EMPTY_TEXTS = {
  top: 'Пока нечего показать: включите любой трек, и он появится здесь.',
  recent: 'Здесь появятся треки, которые вы недавно добавили. Пришлите боту аудиофайл.',
  unplayed: 'Отлично — не осталось ни одного непрослушанного трека!',
  frequent: 'Пока нет треков, которые вы слушаете часто. Всё впереди!',
  rare: 'Нет треков с редкими прослушиваниями.',
}

/**
 * Описание раздела по ключу.
 * @param {string} key top | recent | unplayed | frequent | rare
 * @returns {{key: string, title: string, icon: string}|null}
 */
export function sectionByKey(key) {
  return SECTIONS.find((section) => section.key === key) ?? null
}

/** Проверка, что ключ раздела корректен. */
export function isSectionKey(key) {
  return SECTION_KEYS.includes(key)
}

/** Текст-подсказка для пустого раздела. */
export function sectionEmptyText(key) {
  return SECTION_EMPTY_TEXTS[key] ?? 'Здесь пока пусто.'
}

// ---------------------------------------------------------------------------
// Разделы приложения (навигация с главной) — контракт V2, раздел 6
// ---------------------------------------------------------------------------

/**
 * Все разделы Mini App в том порядке, в каком их показывает главная страница.
 * Поля: key — стабильный ключ, title — подпись, icon — эмодзи, path — маршрут.
 * description — короткая подсказка под плиткой (необязательна для отрисовки).
 */
export const APP_SECTIONS = [
  {
    key: 'tracks',
    title: 'Треки',
    icon: '🎵',
    path: '/tracks',
    description: 'Вся музыка одним списком',
  },
  {
    key: 'folders',
    title: 'Папки',
    icon: '📁',
    path: '/folders',
    description: 'Библиотека по полочкам',
  },
  {
    key: 'artists',
    title: 'Исполнители',
    icon: '🎤',
    path: '/artists',
    description: 'Кого вы слушаете',
  },
  {
    key: 'playlists',
    title: 'Плейлисты',
    icon: '🎶',
    path: '/playlists',
    description: 'Свои подборки треков',
  },
  {
    key: 'favourites',
    title: 'Избранное',
    icon: '⭐',
    path: '/favourites',
    description: 'Отмеченное звёздочкой',
  },
  {
    key: 'stats',
    title: 'Статистика',
    icon: '📊',
    path: '/stats',
    description: 'Что и как часто играет',
  },
  {
    key: 'recommendations',
    title: 'Рекомендации',
    icon: '✨',
    path: '/recommendations',
    description: 'Кого послушать дальше',
  },
  {
    key: 'notes',
    title: 'Заметки',
    icon: '📝',
    path: '/notes',
    description: 'Списки и напоминания',
  },
  {
    key: 'other',
    title: 'Другое',
    icon: '📦',
    path: '/other',
    description: 'Документы, видео, голосовые',
  },
  {
    key: 'settings',
    title: 'Настройки',
    icon: '⚙️',
    path: '/settings',
    description: 'Поведение бота и приложения',
  },
]

/** Только ключи разделов приложения. */
export const APP_SECTION_KEYS = APP_SECTIONS.map((section) => section.key)

/**
 * Описание раздела приложения по ключу.
 * @param {string} key например 'notes'
 * @returns {{key: string, title: string, icon: string, path: string, description: string}|null}
 */
export function appSectionByKey(key) {
  return APP_SECTIONS.find((section) => section.key === key) ?? null
}

/**
 * Разделы приложения без тех, что уже есть в нижней навигации.
 * @param {string[]} paths маршруты вкладок TabBar
 */
export function appSectionsExcept(paths = []) {
  const skip = new Set(paths)
  return APP_SECTIONS.filter((section) => !skip.has(section.path))
}
