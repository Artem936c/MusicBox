/**
 * Страница статистики.
 *
 * Сверху — плитки общих счётчиков (всего треков, всего прослушиваний, в избранном,
 * папок, исполнителей), ниже — ВСЕ ПЯТЬ разделов статистики списками TrackRow:
 * у каждого раздела в заголовке количество треков и ссылка «Все →» на /section/:key.
 *
 * Данные: api.stats.overview({ limit: 5 }). Перезагрузка при изменении playVersion
 * из usePlayer — счётчики прослушиваний обновляются в реальном времени.
 */

import React, { useMemo } from 'react'
import { Link } from 'react-router-dom'

import EmptyState from '../components/EmptyState.jsx'
import Loader from '../components/Loader.jsx'
import TrackRow from '../components/TrackRow.jsx'
import { api } from '../api/client.js'
import { usePlayer } from '../context/PlayerContext.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { formatCount } from '../utils/format.js'
import { SECTIONS, sectionEmptyText } from '../utils/sections.js'

/** Сколько треков показываем в каждом разделе — остальное открывается по «Все →». */
const PREVIEW_LIMIT = 5

/** Формы слова «трек» для склонения. */
const TRACK_FORMS = ['трек', 'трека', 'треков']

/**
 * Плитки общей статистики. `to` — необязательный маршрут для перехода.
 */
const SUMMARY_TILES = [
  { key: 'total', icon: '🎵', label: 'Всего треков', to: '/section/recent' },
  { key: 'total_plays', icon: '▶️', label: 'Всего прослушиваний', to: null },
  { key: 'favourites', icon: '⭐', label: 'В избранном', to: '/favourites' },
  { key: 'folders', icon: '📁', label: 'Папок', to: '/folders' },
  { key: 'artists', icon: '🎤', label: 'Исполнителей', to: '/artists' },
]

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

export function StatsPage() {
  const { playVersion } = usePlayer()

  const { data, error, loading, reload } = useAsync(
    () => api.stats.overview({ limit: PREVIEW_LIMIT }),
    [playVersion],
  )

  const counts = data?.counts ?? {}

  /** Разделы в порядке контракта: top, recent, unplayed, frequent, rare. */
  const sections = useMemo(() => {
    const fromApi = Array.isArray(data?.sections) ? data.sections : []
    const byKey = {}
    for (const section of fromApi) {
      if (section && section.key) byKey[section.key] = section
    }
    return SECTIONS.map((meta) => {
      const section = byKey[meta.key] || {}
      const items = Array.isArray(section.items) ? section.items.filter(Boolean) : []
      const rawCount = Number(section.count)
      return {
        key: meta.key,
        icon: meta.icon,
        title: section.title || meta.title,
        count: Number.isFinite(rawCount) ? rawCount : items.length,
        items,
      }
    })
  }, [data])

  if (loading && !data) {
    return <Loader label="Считаем статистику…" />
  }

  if (error && !data) {
    return (
      <EmptyState
        icon="⚠️"
        title="Не удалось загрузить статистику"
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

  if (totalTracks === 0) {
    return (
      <div className="page page--stats">
        <EmptyState
          icon="📊"
          title="Статистики пока нет"
          description="Пришлите боту аудиофайл — и как только вы начнёте слушать, здесь появятся любимые треки, редкие находки и всё, что ещё ни разу не играло."
        />
      </div>
    )
  }

  return (
    <div className="page page--stats">
      <div className="stat-grid stat-grid--summary">
        {SUMMARY_TILES.map((tile) => {
          const content = (
            <>
              <span className="stat-tile__icon" aria-hidden="true">
                {tile.icon}
              </span>
              <span className="stat-tile__value">{countOf(counts, tile.key)}</span>
              <span className="stat-tile__label">{tile.label}</span>
            </>
          )
          return tile.to ? (
            <Link key={tile.key} className="stat-tile" to={tile.to}>
              {content}
            </Link>
          ) : (
            <div key={tile.key} className="stat-tile">
              {content}
            </div>
          )
        })}
      </div>

      {sections.map((section) => (
        <section className="page-section" key={section.key}>
          <div className="section-head">
            <h2 className="section-title">
              <span aria-hidden="true">{section.icon} </span>
              {section.title}
              <span className="badge section-count" title="Треков в разделе">
                {section.count}
              </span>
            </h2>
            {section.count > 0 ? (
              <Link className="section-more" to={`/section/${section.key}`}>
                Все →
              </Link>
            ) : null}
          </div>

          {section.items.length === 0 ? (
            <p className="muted">{sectionEmptyText(section.key)}</p>
          ) : (
            <>
              <div className="track-list">
                {section.items.map((track, index) => (
                  <TrackRow
                    key={track.id ?? index}
                    track={track}
                    index={index}
                    queue={section.items}
                    showStats
                    onChanged={reload}
                  />
                ))}
              </div>

              {section.count > section.items.length ? (
                <Link className="btn btn--wide" to={`/section/${section.key}`}>
                  Показать все · {formatCount(section.count, TRACK_FORMS)}
                </Link>
              ) : null}
            </>
          )}
        </section>
      ))}
    </div>
  )
}

export default StatsPage
