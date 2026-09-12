/**
 * Карточка трека для горизонтальных подборок (SectionScroller).
 *
 * Пропсы по контракту (раздел 16): TrackCard({ track, queue = null, index = 0 }).
 * Клик по карточке запускает трек в контексте переданной очереди.
 */

import React from 'react'

import { usePlayer } from '../context/PlayerContext.jsx'
import { hapticImpact } from '../telegram.js'

const API_ORIGIN = import.meta.env.VITE_API_ORIGIN || ''

/** Секунды → «3:07»; 0 и пустое значение → «—». */
function formatDuration(seconds) {
  const total = Number(seconds)
  if (!Number.isFinite(total) || total <= 0) return '—'
  const value = Math.floor(total)
  const hours = Math.floor(value / 3600)
  const minutes = Math.floor((value % 3600) / 60)
  const secs = value % 60
  const pad = (num) => String(num).padStart(2, '0')
  if (hours > 0) return `${hours}:${pad(minutes)}:${pad(secs)}`
  return `${minutes}:${pad(secs)}`
}

export function TrackCard({ track, queue = null, index = 0 }) {
  const player = usePlayer()

  if (!track) return null

  const isCurrent = Boolean(player.current && player.current.id === track.id)
  const isPlayingThis = isCurrent && player.isPlaying
  const cover = track.cover_url ? API_ORIGIN + track.cover_url : null
  const durationLabel = track.duration_label || formatDuration(track.duration)
  const playCount = Number(track.play_count) || 0

  const handlePlay = () => {
    hapticImpact('light')
    player.playTrack(track, queue || [track], Number.isInteger(index) ? index : 0)
  }

  const handleKeyDown = (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      handlePlay()
    }
  }

  const className = ['track-card', isCurrent ? 'track-card--active' : ''].filter(Boolean).join(' ')

  return (
    <div
      className={className}
      role="button"
      tabIndex={0}
      onClick={handlePlay}
      onKeyDown={handleKeyDown}
      title={`${track.title || 'Без названия'} — ${track.artist || 'Неизвестный исполнитель'}`}
      aria-label={`Воспроизвести: ${track.title || 'Без названия'}`}
    >
      {cover ? (
        <img key={track.id} src={cover} alt="" className="track-card__cover" loading="lazy" />
      ) : (
        <div className="track-card__cover" aria-hidden="true">
          {isPlayingThis ? '▶' : '🎵'}
        </div>
      )}

      <span className="track-card__badge" title="Прослушиваний">
        ▶ {playCount}
      </span>

      <div className="track-card__title">{track.title || 'Без названия'}</div>
      <div className="track-card__artist">{track.artist || 'Неизвестный исполнитель'}</div>
      <div className="track-card__artist track-card__duration">{durationLabel}</div>
    </div>
  )
}

export default TrackCard
