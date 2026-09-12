/**
 * Страница «Избранное» (маршрут `/favourites`).
 *
 * Возможности:
 *  - список избранных треков (очередь воспроизведения — весь список);
 *  - строка поиска — серверный нечёткий поиск `GET /favourites?q=` (ТЗ п. 12);
 *  - кнопка «Проиграть всё» (при активном поиске проигрывает найденное);
 *  - удаление из избранного (оптимистично, с откатом при ошибке);
 *  - добавление трека в плейлист;
 *  - дружелюбные пустые состояния.
 *
 * Данные приходят из `GET /favourites` (список TrackOut).
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client.js'
import EmptyState from '../components/EmptyState.jsx'
import Loader from '../components/Loader.jsx'
import PlaylistPickerModal from '../components/PlaylistPickerModal.jsx'
import SearchBar from '../components/SearchBar.jsx'
import TrackRow from '../components/TrackRow.jsx'
import { usePlayer } from '../context/PlayerContext.jsx'
import { useToast } from '../context/ToastContext.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { hapticImpact, hapticNotification } from '../telegram.js'
import { formatCount, formatDuration } from '../utils/format.js'

/** Сколько треков подгружаем за один запрос. */
const PAGE_SIZE = 50

/** Задержка перед отправкой поискового запроса, мс. */
const SEARCH_DEBOUNCE = 300

const TRACK_FORMS = ['трек', 'трека', 'треков']

/** Человеческий текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  const detail = error?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  const message = error?.message
  if (typeof message === 'string' && message.trim()) return message
  return fallback
}

export function FavouritesPage() {
  const { toast } = useToast()
  const player = usePlayer()
  const { playVersion } = player

  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [loadingMore, setLoadingMore] = useState(false)
  const [hasMore, setHasMore] = useState(true)
  const [pendingId, setPendingId] = useState(null)
  const [playlistTarget, setPlaylistTarget] = useState(null)

  // Поиск уходит на сервер с задержкой — не дёргаем API на каждый символ.
  useEffect(() => {
    const timer = setTimeout(() => setSearch(query.trim()), SEARCH_DEBOUNCE)
    return () => clearTimeout(timer)
  }, [query])

  // playVersion в зависимостях — счётчики прослушиваний остаются актуальными.
  const { data, error, loading, reload, setData } = useAsync(
    () => api.favourites.list({ q: search, limit: PAGE_SIZE, offset: 0 }),
    [search, playVersion],
  )

  useEffect(() => {
    if (error) toast(errorText(error, 'Не удалось загрузить избранное'), 'error')
  }, [error, toast])

  useEffect(() => {
    setHasMore(true)
  }, [search, playVersion])

  const tracks = useMemo(() => (Array.isArray(data) ? data.filter(Boolean) : []), [data])

  const totalDuration = useMemo(
    () => tracks.reduce((sum, track) => sum + (Number(track?.duration) || 0), 0),
    [tracks],
  )

  const initialLoading = loading && data === null
  const isSearching = Boolean(search)

  /** Подгрузка следующей страницы избранного (с учётом активного поиска). */
  const loadMore = useCallback(async () => {
    if (loadingMore) return
    setLoadingMore(true)
    try {
      const next = await api.favourites.list({
        q: search,
        limit: PAGE_SIZE,
        offset: tracks.length,
      })
      const items = Array.isArray(next) ? next.filter(Boolean) : []
      const known = new Set(tracks.map((track) => track.id))
      const fresh = items.filter((track) => !known.has(track.id))
      if (fresh.length === 0) {
        setHasMore(false)
        return
      }
      setData((prev) => [...(Array.isArray(prev) ? prev : []), ...fresh])
      if (items.length < PAGE_SIZE) setHasMore(false)
    } catch (err) {
      toast(errorText(err, 'Не удалось загрузить ещё треки'), 'error')
    } finally {
      setLoadingMore(false)
    }
  }, [loadingMore, search, setData, toast, tracks])

  const handlePlayAll = () => {
    if (tracks.length === 0) return
    hapticImpact('medium')
    player.playQueue(tracks, 0)
  }

  /** Удаление трека из избранного: сначала убираем из списка, при ошибке возвращаем. */
  const handleRemove = useCallback(
    async (track) => {
      if (!track || pendingId === track.id) return
      hapticImpact('light')
      setPendingId(track.id)
      const snapshot = Array.isArray(data) ? data : []
      setData((prev) => (Array.isArray(prev) ? prev.filter((item) => item.id !== track.id) : prev))
      try {
        await api.favourites.remove(track.id)
        hapticNotification('success')
        toast('Трек убран из избранного', 'success')
      } catch (err) {
        hapticNotification('error')
        setData(snapshot)
        toast(errorText(err, 'Не удалось убрать трек из избранного'), 'error')
      } finally {
        setPendingId(null)
      }
    },
    [data, pendingId, setData, toast],
  )

  /** Добавление трека в выбранный плейлист. */
  const handleAddToPlaylist = async (playlist) => {
    const track = playlistTarget
    if (!track || !playlist) return
    try {
      await api.playlists.addTracks(playlist.id, [track.id])
      hapticNotification('success')
      toast(`Трек добавлен в плейлист «${playlist.name}»`, 'success')
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось добавить трек в плейлист'), 'error')
    }
  }

  const trackActions = useMemo(
    () => [
      {
        key: 'playlist',
        icon: '➕',
        title: 'Добавить в плейлист',
        onClick: (track) => {
          hapticImpact('light')
          setPlaylistTarget(track)
        },
      },
      {
        key: 'unfav',
        icon: '💔',
        title: 'Убрать из избранного',
        onClick: (track) => handleRemove(track),
      },
    ],
    [handleRemove],
  )

  if (initialLoading) {
    return (
      <div className="page">
        <Loader label="Загружаем избранное…" />
      </div>
    )
  }

  if (error && tracks.length === 0) {
    return (
      <div className="page">
        <EmptyState
          icon="⚠️"
          title="Избранное недоступно"
          description={errorText(error, 'Не удалось загрузить список. Попробуйте ещё раз.')}
          action={
            <button type="button" className="btn btn--primary" onClick={reload}>
              Повторить
            </button>
          }
        />
      </div>
    )
  }

  // Пустая библиотека избранного: строка поиска не нужна — искать нечего.
  if (!isSearching && tracks.length === 0) {
    return (
      <div className="page">
        <EmptyState
          icon="⭐"
          title="Здесь пока пусто"
          description="Нажмите звёздочку у любого трека — и он окажется в избранном. Так проще собрать то, что хочется слушать снова и снова."
          action={
            <Link className="btn btn--primary" to="/">
              К библиотеке
            </Link>
          }
        />
      </div>
    )
  }

  const durationLabel = totalDuration > 0 ? ` · ${formatDuration(totalDuration)}` : ''

  return (
    <div className="page">
      <div className="section__header">
        <h1 className="section__title">⭐ Избранное</h1>
        {tracks.length > 0 ? (
          <span className="muted">
            {formatCount(tracks.length, TRACK_FORMS)}
            {durationLabel}
          </span>
        ) : null}
      </div>

      <SearchBar value={query} onChange={setQuery} placeholder="Поиск по избранному…" />

      {tracks.length > 0 ? (
        <>
          <div className="page__actions">
            <button type="button" className="btn btn--primary" onClick={handlePlayAll}>
              {isSearching ? '▶️ Проиграть найденное' : '▶️ Проиграть всё'}
            </button>
          </div>

          <div className="list">
            {tracks.map((track, index) => (
              <TrackRow
                key={track.id}
                track={track}
                index={index}
                queue={tracks}
                showStats
                actions={trackActions}
                onChanged={reload}
              />
            ))}
          </div>

          {hasMore && tracks.length >= PAGE_SIZE ? (
            <button
              type="button"
              className="btn btn--block"
              onClick={loadMore}
              disabled={loadingMore}
            >
              {loadingMore ? 'Загружаем…' : 'Показать ещё'}
            </button>
          ) : null}
        </>
      ) : (
        <EmptyState
          icon="🔎"
          title="Ничего не нашлось"
          description="В избранном нет треков по этому запросу. Попробуйте изменить его — опечатки и другая раскладка не помеха."
          action={
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => {
                hapticImpact('light')
                setQuery('')
              }}
            >
              Показать всё избранное
            </button>
          }
        />
      )}

      <PlaylistPickerModal
        open={Boolean(playlistTarget)}
        onClose={() => setPlaylistTarget(null)}
        onPick={handleAddToPlaylist}
        allowCreate
      />
    </div>
  )
}

export default FavouritesPage
