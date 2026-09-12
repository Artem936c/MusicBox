/**
 * Мини-плеер над нижней навигацией (TabBar).
 *
 * Здесь живёт единственный тег <audio> приложения: он всегда в DOM, но управляется
 * PlayerContext через общий audioRef. Панель плеера скрыта, пока current === null.
 */

import React from 'react'

import { usePlayer } from '../context/PlayerContext.jsx'
import { hapticImpact } from '../telegram.js'

const API_ORIGIN = import.meta.env.VITE_API_ORIGIN || ''

/** Секунды → «3:07» / «1:02:03»; для плеера ноль показываем как «0:00». */
function formatTime(seconds) {
  const total = Number(seconds)
  if (!Number.isFinite(total) || total < 0) return '0:00'
  const value = Math.floor(total)
  const hours = Math.floor(value / 3600)
  const minutes = Math.floor((value % 3600) / 60)
  const secs = value % 60
  const pad = (num) => String(num).padStart(2, '0')
  if (hours > 0) return `${hours}:${pad(minutes)}:${pad(secs)}`
  return `${minutes}:${pad(secs)}`
}

export function Player() {
  const { audioRef, current, isPlaying, progress, duration, toggle, next, prev, seek, stop } =
    usePlayer()

  const total = duration > 0 ? duration : Number(current && current.duration) || 0
  const percent = total > 0 ? Math.max(0, Math.min(100, (progress / total) * 100)) : 0
  const cover = current && current.cover_url ? API_ORIGIN + current.cover_url : null

  /** Клик по полосе прогресса — перемотка в выбранную точку. */
  const handleSeekClick = (event) => {
    if (!total) return
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width) return
    const ratio = (event.clientX - rect.left) / rect.width
    seek(Math.max(0, Math.min(1, ratio)) * total)
  }

  /** Стрелками — перемотка на ±5 секунд (доступность с клавиатуры). */
  const handleSeekKeyDown = (event) => {
    if (!total) return
    if (event.key === 'ArrowRight') {
      event.preventDefault()
      seek(Math.min(total, progress + 5))
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault()
      seek(Math.max(0, progress - 5))
    } else if (event.key === 'Home') {
      event.preventDefault()
      seek(0)
    }
  }

  const handleToggle = () => {
    hapticImpact('light')
    toggle()
  }

  const handleNext = () => {
    hapticImpact('light')
    next()
  }

  const handlePrev = () => {
    hapticImpact('light')
    prev()
  }

  const handleStop = () => {
    hapticImpact('light')
    stop()
  }

  return (
    <>
      {/* Единственный <audio> приложения; управляется PlayerContext. */}
      <audio ref={audioRef} preload="metadata" playsInline style={{ display: 'none' }} />

      {current ? (
        <div className="player" role="region" aria-label="Мини-плеер">
          {cover ? (
            <div className="player__cover">
              <img key={current.id} src={cover} alt="" />
            </div>
          ) : (
            <div className="player__cover" aria-hidden="true">
              🎵
            </div>
          )}

          <div className="player__info">
            <div className="player__title" title={current.title || 'Без названия'}>
              {current.title || 'Без названия'}
            </div>
            <div className="player__artist" title={current.artist || 'Неизвестный исполнитель'}>
              {current.artist || 'Неизвестный исполнитель'}
            </div>
          </div>

          <div className="player__controls">
            <button
              type="button"
              className="btn-icon"
              onClick={handlePrev}
              title="Предыдущий трек"
              aria-label="Предыдущий трек"
            >
              ⏮
            </button>
            <button
              type="button"
              className="btn-icon"
              onClick={handleToggle}
              title={isPlaying ? 'Пауза' : 'Воспроизвести'}
              aria-label={isPlaying ? 'Пауза' : 'Воспроизвести'}
            >
              {isPlaying ? '⏸' : '▶️'}
            </button>
            <button
              type="button"
              className="btn-icon"
              onClick={handleNext}
              title="Следующий трек"
              aria-label="Следующий трек"
            >
              ⏭
            </button>
            <button
              type="button"
              className="btn-icon"
              onClick={handleStop}
              title="Закрыть плеер"
              aria-label="Закрыть плеер"
            >
              ✕
            </button>
          </div>

          {/* Вторая строка плеера: время — полоса прогресса — время. */}
          <span className="player__time">{formatTime(progress)}</span>
          <div
            className="player__progress"
            role="slider"
            tabIndex={0}
            aria-label="Позиция воспроизведения"
            aria-valuemin={0}
            aria-valuemax={Math.round(total)}
            aria-valuenow={Math.round(progress)}
            aria-valuetext={`${formatTime(progress)} из ${formatTime(total)}`}
            onClick={handleSeekClick}
            onKeyDown={handleSeekKeyDown}
          >
            <div className="player__bar" style={{ width: `${percent}%` }} />
          </div>
          <span className="player__time">{formatTime(total)}</span>
        </div>
      ) : null}
    </>
  )
}

export default Player
