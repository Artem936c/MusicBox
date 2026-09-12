/**
 * Горизонтальная подборка треков с заголовком и ссылкой «Все».
 *
 * Пропсы по контракту (раздел 16):
 * SectionScroller({ title, items, moreHref = null, emptyText = null, queue = null })
 * queue — очередь воспроизведения для карточек (по умолчанию сами items).
 */

import React from 'react'
import { Link } from 'react-router-dom'

import TrackCard from './TrackCard.jsx'

export function SectionScroller({ title, items, moreHref = null, emptyText = null, queue = null }) {
  const list = Array.isArray(items) ? items.filter(Boolean) : []
  const playQueue = Array.isArray(queue) && queue.length ? queue.filter(Boolean) : list

  return (
    <section className="section">
      <div className="section__header">
        <h2 className="section__title">{title}</h2>
        {moreHref && list.length > 0 ? (
          <Link className="section__more" to={moreHref}>
            Все
          </Link>
        ) : null}
      </div>

      {list.length === 0 ? (
        <p className="muted">{emptyText || 'Пока пусто'}</p>
      ) : (
        <div className="section-scroller">
          {list.map((track, position) => {
            const queueIndex = playQueue.findIndex((item) => item && item.id === track.id)
            return (
              <TrackCard
                key={track.id ?? position}
                track={track}
                queue={playQueue}
                index={queueIndex >= 0 ? queueIndex : position}
              />
            )
          })}
        </div>
      )}
    </section>
  )
}

export default SectionScroller
