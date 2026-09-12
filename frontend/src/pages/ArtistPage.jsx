/**
 * Страница исполнителя (маршрут `/artists/:id`).
 *
 * Карточка исполнителя (отметка «прослушано», счётчики треков и прослушиваний,
 * ссылка на его папку) + список треков + отдельный блок «Ещё не слушали».
 *
 * Возможности V2:
 *  - «▶️ Воспроизвести всё» — очередь из треков исполнителя;
 *  - блок «💤 Ещё не слушали» — `GET /artists/{id}/unplayed` (ТЗ п. 2) со своей
 *    кнопкой воспроизведения;
 *  - переименование — `PATCH /artists/{id}` (ТЗ п. 10). Backend объединяет
 *    тёзок, поэтому предупреждаем заранее и переходим на «выжившего»;
 *  - привязка к папке — `FolderPickerModal` → `POST /artists/{id}/folder` (ТЗ п. 8).
 *
 * Данные: `GET /artists/{id}` (ArtistOut), `GET /artists/{id}/tracks` и
 * `GET /artists/{id}/unplayed` (TrackOut).
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { api } from '../api/client.js'
import EmptyState from '../components/EmptyState.jsx'
import FolderPickerModal from '../components/FolderPickerModal.jsx'
import Loader from '../components/Loader.jsx'
import Modal from '../components/Modal.jsx'
import PlaylistPickerModal from '../components/PlaylistPickerModal.jsx'
import TrackRow from '../components/TrackRow.jsx'
import { usePlayer } from '../context/PlayerContext.jsx'
import { useToast } from '../context/ToastContext.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { hapticImpact, hapticNotification } from '../telegram.js'
import { formatCount, formatDuration } from '../utils/format.js'

/** Сколько треков подгружаем за один запрос. */
const PAGE_SIZE = 50

/** Сколько непрослушанных треков показываем в отдельном блоке. */
const UNPLAYED_LIMIT = 50

/** Максимальная длина имени — та же, что в backend (artists_repo.MAX_NAME_LENGTH). */
const MAX_NAME_LENGTH = 200

const TRACK_FORMS = ['трек', 'трека', 'треков']
const PLAY_FORMS = ['прослушивание', 'прослушивания', 'прослушиваний']

/** Человеческий текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  const detail = error?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  const message = error?.message
  if (typeof message === 'string' && message.trim()) return message
  return fallback
}

export function ArtistPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const artistId = Number(id)
  const isValidId = Number.isInteger(artistId) && artistId > 0

  const { toast } = useToast()
  const player = usePlayer()
  const { playVersion } = player

  const [pending, setPending] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [hasMore, setHasMore] = useState(true)
  const [playlistTarget, setPlaylistTarget] = useState(null)

  // Переименование и привязка к папке.
  const [renameOpen, setRenameOpen] = useState(false)
  const [renameName, setRenameName] = useState('')
  const [folderOpen, setFolderOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState(null)

  // playVersion в зависимостях: счётчики прослушиваний, отметка «прослушано»
  // и блок «Ещё не слушали» обновляются сразу после воспроизведения.
  const { data, error, loading, reload, setData } = useAsync(async () => {
    if (!isValidId) return { artist: null, tracks: [], unplayed: [] }
    const [artist, tracks, unplayed] = await Promise.all([
      api.artists.get(artistId),
      api.artists.tracks(artistId, { limit: PAGE_SIZE, offset: 0 }),
      api.artists.unplayed(artistId, { limit: UNPLAYED_LIMIT }),
    ])
    return {
      artist: artist || null,
      tracks: Array.isArray(tracks) ? tracks.filter(Boolean) : [],
      unplayed: Array.isArray(unplayed) ? unplayed.filter(Boolean) : [],
    }
  }, [artistId, playVersion])

  useEffect(() => {
    if (error) toast(errorText(error, 'Не удалось загрузить исполнителя'), 'error')
  }, [error, toast])

  useEffect(() => {
    setHasMore(true)
  }, [artistId, playVersion])

  const artist = data?.artist || null
  const tracks = useMemo(() => (Array.isArray(data?.tracks) ? data.tracks : []), [data])
  const unplayed = useMemo(() => (Array.isArray(data?.unplayed) ? data.unplayed : []), [data])

  const totalDuration = useMemo(
    () => tracks.reduce((sum, track) => sum + (Number(track?.duration) || 0), 0),
    [tracks],
  )

  const initialLoading = loading && data === null

  /** Подгрузка следующей страницы треков исполнителя. */
  const loadMore = useCallback(async () => {
    if (!isValidId || loadingMore) return
    setLoadingMore(true)
    try {
      const next = await api.artists.tracks(artistId, { limit: PAGE_SIZE, offset: tracks.length })
      const items = Array.isArray(next) ? next.filter(Boolean) : []
      const known = new Set(tracks.map((track) => track.id))
      const fresh = items.filter((track) => !known.has(track.id))
      if (fresh.length === 0) {
        setHasMore(false)
        return
      }
      setData((prev) => ({
        artist: prev?.artist || artist,
        tracks: [...(prev?.tracks || []), ...fresh],
        unplayed: prev?.unplayed || [],
      }))
      if (items.length < PAGE_SIZE) setHasMore(false)
    } catch (err) {
      toast(errorText(err, 'Не удалось загрузить ещё треки'), 'error')
    } finally {
      setLoadingMore(false)
    }
  }, [artist, artistId, isValidId, loadingMore, setData, toast, tracks])

  const handlePlayAll = () => {
    if (tracks.length === 0) return
    hapticImpact('medium')
    player.playQueue(tracks, 0)
  }

  const handlePlayUnplayed = () => {
    if (unplayed.length === 0) return
    hapticImpact('medium')
    player.playQueue(unplayed, 0)
  }

  /** Переключение отметки «прослушано» у исполнителя. */
  const handleToggleListened = async () => {
    if (!artist || pending) return
    const nextValue = !artist.is_listened
    hapticImpact('light')
    setPending(true)
    try {
      const updated = await api.artists.setListened(artist.id, nextValue)
      hapticNotification('success')
      setData((prev) => ({
        artist:
          updated && typeof updated === 'object' && updated.id
            ? updated
            : { ...artist, is_listened: nextValue },
        tracks: prev?.tracks || tracks,
        unplayed: prev?.unplayed || unplayed,
      }))
      toast(nextValue ? 'Отмечено как прослушанное' : 'Отметка о прослушивании снята', 'success')
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось изменить отметку'), 'error')
    } finally {
      setPending(false)
    }
  }

  /** Переименование исполнителя; тёзки объединяются на стороне backend. */
  const handleRename = async (event) => {
    event.preventDefault()
    if (!artist) return
    const name = renameName.trim()
    if (!name) {
      setFormError('Введите имя исполнителя')
      return
    }
    if (name === artist.name) {
      setRenameOpen(false)
      return
    }
    setSaving(true)
    setFormError(null)
    try {
      const updated = await api.artists.rename(artist.id, name)
      hapticNotification('success')
      setRenameOpen(false)
      const survivorId = Number(updated?.id)
      if (Number.isInteger(survivorId) && survivorId > 0 && survivorId !== artist.id) {
        // Произошло слияние с тёзкой — открываем того исполнителя, который остался.
        toast(`«${artist.name}» объединён с «${updated?.name || name}»`, 'success')
        navigate(`/artists/${survivorId}`, { replace: true })
        return
      }
      setData((prev) => ({
        artist: updated && typeof updated === 'object' && updated.id ? updated : { ...artist, name },
        tracks: prev?.tracks || tracks,
        unplayed: prev?.unplayed || unplayed,
      }))
      toast(`Исполнитель переименован в «${updated?.name || name}»`, 'success')
    } catch (err) {
      hapticNotification('error')
      setFormError(errorText(err, 'Не удалось переименовать исполнителя'))
    } finally {
      setSaving(false)
    }
  }

  /** Привязка исполнителя к папке; folder = null — «без папки». */
  const handlePickFolder = async (folder) => {
    if (!artist) return
    try {
      const updated = await api.artists.setFolder(artist.id, folder?.id ?? null)
      hapticNotification('success')
      setData((prev) => ({
        artist:
          updated && typeof updated === 'object' && updated.id
            ? updated
            : { ...artist, folder_id: folder?.id ?? null },
        tracks: prev?.tracks || tracks,
        unplayed: prev?.unplayed || unplayed,
      }))
      toast(
        folder ? `Исполнитель привязан к папке «${folder.name}»` : 'Папка исполнителя убрана',
        'success',
      )
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось привязать исполнителя к папке'), 'error')
    } finally {
      setFolderOpen(false)
    }
  }

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
    ],
    [],
  )

  if (!isValidId) {
    return (
      <div className="page">
        <EmptyState
          icon="🎤"
          title="Исполнитель не найден"
          description="Ссылка на исполнителя выглядит неверной."
          action={
            <Link className="btn btn--primary" to="/artists">
              Ко всем исполнителям
            </Link>
          }
        />
      </div>
    )
  }

  if (initialLoading) {
    return (
      <div className="page">
        <Loader label="Загружаем исполнителя…" />
      </div>
    )
  }

  if (error && !artist) {
    return (
      <div className="page">
        <EmptyState
          icon="⚠️"
          title="Исполнитель недоступен"
          description={errorText(error, 'Не удалось загрузить данные. Попробуйте ещё раз.')}
          action={
            <button type="button" className="btn btn--primary" onClick={reload}>
              Повторить
            </button>
          }
        />
      </div>
    )
  }

  const durationLabel = totalDuration > 0 ? ` · ${formatDuration(totalDuration)}` : ''

  return (
    <div className="page">
      <section className="card artist-card">
        <h1 className="card__title">🎤 {artist?.name || 'Исполнитель'}</h1>
        <p className="card__meta">
          {formatCount(artist?.track_count ?? tracks.length, TRACK_FORMS)} ·{' '}
          {formatCount(artist?.play_count, PLAY_FORMS)}
          {durationLabel}
        </p>
        <p className="card__meta">
          {artist?.is_listened ? '✅ Отмечен как прослушанный' : '⬜ Ещё не прослушан'}
        </p>

        <div className="card__actions">
          <button
            type="button"
            className="btn btn--primary"
            onClick={handlePlayAll}
            disabled={tracks.length === 0}
          >
            ▶️ Воспроизвести всё
          </button>
          <button
            type="button"
            className="btn"
            aria-pressed={Boolean(artist?.is_listened)}
            disabled={pending || !artist}
            onClick={handleToggleListened}
          >
            {artist?.is_listened ? '⬜ Снять отметку' : '✅ Прослушано'}
          </button>
        </div>

        <div className="card__actions">
          <button
            type="button"
            className="btn"
            disabled={!artist}
            onClick={() => {
              hapticImpact('light')
              setFormError(null)
              setRenameName(artist?.name || '')
              setRenameOpen(true)
            }}
          >
            ✏️ Переименовать
          </button>
          <button
            type="button"
            className="btn"
            disabled={!artist}
            onClick={() => {
              hapticImpact('light')
              setFolderOpen(true)
            }}
          >
            📁 {artist?.folder_id ? 'Изменить папку' : 'Привязать к папке'}
          </button>
        </div>

        {artist?.folder_id ? (
          <Link className="btn btn--ghost" to={`/folders/${artist.folder_id}`}>
            📂 Открыть папку исполнителя
          </Link>
        ) : null}
      </section>

      {tracks.length > 0 ? (
        <section className="page-section">
          <div className="section-head">
            <h2 className="section-title">💤 Ещё не слушали</h2>
            {unplayed.length > 0 ? (
              <span className="badge section-count">{unplayed.length}</span>
            ) : null}
          </div>

          {unplayed.length > 0 ? (
            <>
              <div className="page__actions">
                <button type="button" className="btn btn--primary" onClick={handlePlayUnplayed}>
                  ▶️ Включить непрослушанное
                </button>
              </div>
              <div className="list">
                {unplayed.map((track, index) => (
                  <TrackRow
                    key={`unplayed-${track.id}`}
                    track={track}
                    index={index}
                    queue={unplayed}
                    showStats
                    actions={trackActions}
                    onChanged={reload}
                  />
                ))}
              </div>
            </>
          ) : (
            <EmptyState
              icon="🎉"
              title="Вы послушали всё"
              description="У этого исполнителя не осталось треков, которые ни разу не включали."
            />
          )}
        </section>
      ) : null}

      {tracks.length > 0 ? (
        <section className="page-section">
          <div className="section-head">
            <h2 className="section-title">🎧 Все треки</h2>
            <span className="badge section-count">{tracks.length}</span>
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
        </section>
      ) : (
        <EmptyState
          icon="🎧"
          title="У исполнителя пока нет треков"
          description="Пришлите боту аудиофайл этого исполнителя — он появится здесь автоматически."
          action={
            <Link className="btn btn--primary" to="/artists">
              Ко всем исполнителям
            </Link>
          }
        />
      )}

      <Modal
        open={renameOpen}
        title="Переименовать исполнителя"
        onClose={() => {
          if (saving) return
          setRenameOpen(false)
          setFormError(null)
        }}
      >
        <form className="field" onSubmit={handleRename}>
          <label className="field__label" htmlFor="artist-page-rename">
            Новое имя
          </label>
          <input
            id="artist-page-rename"
            className="input"
            type="text"
            value={renameName}
            onChange={(event) => setRenameName(event.target.value)}
            placeholder="Имя исполнителя"
            maxLength={MAX_NAME_LENGTH}
            autoFocus
            disabled={saving}
          />
          <p className="field__hint">
            Если исполнитель с таким именем уже есть, записи будут объединены: все треки перейдут к
            нему, а дубликат исчезнет.
          </p>
          {formError ? (
            <p className="modal__error" role="alert">
              {formError}
            </p>
          ) : null}
          <div className="modal__actions">
            <button
              type="button"
              className="btn"
              disabled={saving}
              onClick={() => {
                setRenameOpen(false)
                setFormError(null)
              }}
            >
              Отмена
            </button>
            <button
              type="submit"
              className="btn btn--primary"
              disabled={saving || !renameName.trim()}
            >
              {saving ? 'Сохраняем…' : 'Сохранить'}
            </button>
          </div>
        </form>
      </Modal>

      <FolderPickerModal
        open={folderOpen}
        onClose={() => setFolderOpen(false)}
        onPick={handlePickFolder}
        allowNone
        allowCreate
        title={artist ? `Папка для «${artist.name}»` : 'Папка исполнителя'}
      />

      <PlaylistPickerModal
        open={Boolean(playlistTarget)}
        onClose={() => setPlaylistTarget(null)}
        onPick={handleAddToPlaylist}
        allowCreate
      />
    </div>
  )
}

export default ArtistPage
