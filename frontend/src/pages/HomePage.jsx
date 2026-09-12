/**
 * Главная страница Mini App.
 *
 * Сверху — виджет «🔥 Самые часто прослушиваемые» (горизонтальный скролл карточек
 * с ссылкой «Все» на /section/top), затем плитки быстрого перехода в разделы
 * приложения (Треки, Рекомендации, Заметки, Другое — контракт V2, раздел 6),
 * ниже — плитки разделов статистики с количеством треков, затем папки
 * (по алфавиту) и блок «⭐ Избранное».
 *
 * Данные: api.stats.overview({ limit: 10 }) + api.folders.list() + api.favourites.list().
 * Загрузка перезапускается при изменении playVersion из usePlayer — счётчики
 * прослушиваний обновляются сразу после того, как трек засчитан.
 */

import React, { useMemo } from 'react'
import { Link } from 'react-router-dom'

import EmptyState from '../components/EmptyState.jsx'
import Loader from '../components/Loader.jsx'
import SectionScroller from '../components/SectionScroller.jsx'
import { api } from '../api/client.js'
import { usePlayer } from '../context/PlayerContext.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { formatCount } from '../utils/format.js'
import { APP_SECTIONS, SECTIONS, sectionEmptyText } from '../utils/sections.js'

/** Сколько треков показываем в горизонтальных подборках. */
const SCROLLER_LIMIT = 10

/** Сколько папок показываем на главной (остальные — на странице «Папки»). */
const FOLDERS_PREVIEW = 8

/** Разделы для сетки быстрых ссылок (топ вынесен в отдельный виджет наверху). */
const QUICK_KEYS = ['recent', 'unplayed', 'frequent', 'rare']

/** Разделы приложения на плитках главной (контракт V2, раздел 6). */
const APP_TILE_KEYS = ['tracks', 'recommendations', 'notes', 'other']

/** Плитки разделов приложения в порядке APP_TILE_KEYS; неизвестные ключи пропускаем. */
const APP_TILES = APP_TILE_KEYS.map((key) =>
  APP_SECTIONS.find((section) => section.key === key),
).filter(Boolean)

/** Формы слова «трек» для склонения. */
const TRACK_FORMS = ['трек', 'трека', 'треков']

/** Алфавитное сравнение с учётом кириллицы и регистра (аналог name.casefold()). */
function compareNames(a, b) {
  return String(a ?? '').localeCompare(String(b ?? ''), 'ru', { sensitivity: 'base' })
}

/** Человекочитаемый текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  if (!error) return fallback
  if (typeof error.detail === 'string' && error.detail) return error.detail
  return error.message || fallback
}

/** Безопасное целое из объекта счётчиков. */
function countOf(counts, key) {
  const value = Number(counts?.[key])
  return Number.isFinite(value) ? value : 0
}

/**
 * Плитки быстрого перехода в разделы приложения.
 * Показываются и при пустой библиотеке: заметки и файлы раздела «Другое»
 * могут быть даже тогда, когда ни одного трека ещё не загружено.
 */
function AppSectionTiles() {
  if (APP_TILES.length === 0) return null
  return (
    <section className="page-section">
      <div className="section-head">
        <h2 className="section-title">🚀 Быстрый переход</h2>
      </div>

      <div className="stat-grid">
        {APP_TILES.map((section) => (
          <Link key={section.key} className="stat-tile stat-tile--nav" to={section.path}>
            <span className="stat-tile__icon" aria-hidden="true">
              {section.icon}
            </span>
            <span className="stat-tile__name">{section.title}</span>
            <span className="stat-tile__label">{section.description}</span>
          </Link>
        ))}
      </div>
    </section>
  )
}

export function HomePage() {
  const { playVersion } = usePlayer()

  const { data, error, loading, reload } = useAsync(async () => {
    const [overview, folders, favourites] = await Promise.all([
      api.stats.overview({ limit: SCROLLER_LIMIT }),
      api.folders.list(),
      // Избранное — вспомогательный блок: его недоступность не должна ломать главную.
      api.favourites.list({ limit: SCROLLER_LIMIT }).catch(() => []),
    ])
    return {
      overview: overview || { counts: {}, sections: [] },
      folders: Array.isArray(folders) ? folders : [],
      favourites: Array.isArray(favourites) ? favourites : [],
    }
  }, [playVersion])

  const counts = data?.overview?.counts ?? {}

  /** Секции статистики, разложенные по ключу. */
  const sectionsByKey = useMemo(() => {
    const map = {}
    const list = Array.isArray(data?.overview?.sections) ? data.overview.sections : []
    for (const section of list) {
      if (section && section.key) map[section.key] = section
    }
    return map
  }, [data])

  const topItems = useMemo(() => {
    const items = sectionsByKey.top?.items
    return Array.isArray(items) ? items.filter(Boolean) : []
  }, [sectionsByKey])

  const favourites = useMemo(
    () => (Array.isArray(data?.favourites) ? data.favourites.filter(Boolean) : []),
    [data],
  )

  /** Папки строго по алфавиту — сортируем на клиенте, как требует контракт. */
  const folders = useMemo(() => {
    const list = Array.isArray(data?.folders) ? data.folders.filter(Boolean) : []
    return [...list].sort((a, b) => compareNames(a.name, b.name))
  }, [data])

  const visibleFolders = folders.slice(0, FOLDERS_PREVIEW)

  if (loading && !data) {
    return <Loader label="Загружаем библиотеку…" />
  }

  if (error && !data) {
    return (
      <EmptyState
        icon="⚠️"
        title="Не удалось загрузить главную"
        description={errorText(error, 'Проверьте подключение и попробуйте ещё раз.')}
        action={
          <button type="button" className="btn btn--primary" onClick={reload}>
            Повторить
          </button>
        }
      />
    )
  }

  const totalTracks = countOf(counts, 'total')

  // Пустая библиотека — дружелюбная подсказка вместо пустых списков.
  if (totalTracks === 0) {
    return (
      <div className="page page--home">
        <EmptyState
          icon="🎧"
          title="Библиотека пока пуста"
          description="Пришлите боту любой аудиофайл или перешлите трек из канала — я сохраню его в облачном хранилище и разложу по папкам исполнителей. После этого здесь появятся ваши подборки и статистика."
          action={
            <Link className="btn btn--primary" to="/tgsearch">
              Найти музыку в Telegram
            </Link>
          }
        />

        <AppSectionTiles />
      </div>
    )
  }

  return (
    <div className="page page--home">
      <SectionScroller
        title="🔥 Самые часто прослушиваемые"
        items={topItems}
        moreHref="/section/top"
        emptyText={sectionEmptyText('top')}
        queue={topItems}
      />

      <AppSectionTiles />

      <section className="page-section">
        <div className="section-head">
          <h2 className="section-title">📊 Статистика</h2>
          <Link className="section-more" to="/stats">
            Вся статистика →
          </Link>
        </div>

        <div className="stat-grid">
          {SECTIONS.filter((section) => QUICK_KEYS.includes(section.key)).map((section) => (
            <Link key={section.key} className="stat-tile" to={`/section/${section.key}`}>
              <span className="stat-tile__icon" aria-hidden="true">
                {section.icon}
              </span>
              <span className="stat-tile__value">{countOf(counts, section.key)}</span>
              <span className="stat-tile__label">{section.title}</span>
            </Link>
          ))}
        </div>
      </section>

      <section className="page-section">
        <div className="section-head">
          <h2 className="section-title">📁 Папки</h2>
          <Link className="section-more" to="/folders">
            Все →
          </Link>
        </div>

        {visibleFolders.length === 0 ? (
          <p className="muted">
            Папок пока нет. Включите автосортировку — треки сами разложатся по исполнителям.
          </p>
        ) : (
          <ul className="list">
            {visibleFolders.map((folder) => (
              <li key={folder.id}>
                <Link className="list-item" to={`/folders/${folder.id}`}>
                  <span className="list-item__icon" aria-hidden="true">
                    {folder.is_artist_folder ? '🎤' : '📁'}
                  </span>
                  <span className="list-item__title">{folder.name}</span>
                  <span className="list-item__meta">
                    {formatCount(folder.track_count, TRACK_FORMS)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}

        {folders.length > visibleFolders.length ? (
          <Link className="btn btn--wide" to="/folders">
            Показать все папки ({folders.length})
          </Link>
        ) : null}
      </section>

      <SectionScroller
        title="⭐ Избранное"
        items={favourites}
        moreHref="/favourites"
        emptyText="В избранном пусто. Нажмите ☆ у любого трека — он появится здесь."
        queue={favourites}
      />
    </div>
  )
}

export default HomePage
