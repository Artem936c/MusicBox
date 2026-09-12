/**
 * Страница одной папки (маршрут `/folders/:id`) — вложенные папки (контракт V2, раздел 6).
 *
 * Что показывает:
 *  - хлебные крошки от корня раздела (`GET /folders/{id}/path`);
 *  - список подпапок (`GET /folders?parent_id={id}&section=all`) с счётчиками;
 *  - треки папки (`GET /folders/{id}/tracks`) с переключателем «включая подпапки»;
 *  - кнопку «▶️ Воспроизвести всё» (`GET /folders/{id}/play?recursive=` → playQueue).
 *
 * Действия по треку (компактное меню «⋯», чтобы строка не разъезжалась на телефоне):
 * переименовать, добавить в плейлист, перенести в другую папку, удалить.
 * Избранное переключается встроенной звёздочкой TrackRow.
 *
 * playVersion из usePlayer() держим в зависимостях загрузки — после учтённого
 * прослушивания счётчики в списке обновляются сами.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client.js'
import ConfirmDialog from '../components/ConfirmDialog.jsx'
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

/** Максимальный limit одного запроса треков на бэкенде. */
const MAX_TRACKS_LIMIT = 500

/** Максимальная длина названия трека (совпадает с ограничением backend). */
const TITLE_MAX_LENGTH = 200

const TRACK_FORMS = ['трек', 'трека', 'треков']
const FOLDER_FORMS = ['папка', 'папки', 'папок']

/**
 * Типы файлов, которые плеер воспроизвести не может.
 * Список совпадает с TrackRow: всё неизвестное считаем аудио (совместимость с V1),
 * поэтому строка, которую TrackRow разрешает запускать, всегда есть и в очереди.
 */
const NON_AUDIO_FILE_TYPES = new Set(['document', 'video', 'video_note', 'voice'])

/** Можно ли отдать файл в плеер (GET /folders/{id}/tracks возвращает все типы). */
function isAudioTrack(track) {
  return !NON_AUDIO_FILE_TYPES.has(String(track?.file_type || 'audio').toLowerCase())
}

/** Человеческий текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  const detail = error?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  const message = error?.message
  if (typeof message === 'string' && message.trim()) return message
  return fallback
}

/** Алфавитная сортировка треков по названию с учётом кириллицы. */
function sortByTitle(items) {
  return [...items].sort((a, b) =>
    String(a?.title || '').localeCompare(String(b?.title || ''), 'ru', { sensitivity: 'base' }),
  )
}

/** Алфавитная сортировка папок по названию. */
function sortByName(items) {
  return [...items].sort((a, b) =>
    String(a?.name || '').localeCompare(String(b?.name || ''), 'ru', { sensitivity: 'base' }),
  )
}

export function FolderPage() {
  const { id } = useParams()
  const folderId = Number(id)
  const isValidId = Number.isInteger(folderId) && folderId > 0

  const { toast } = useToast()
  const player = usePlayer()
  const { playVersion } = player

  // «Включая подпапки»: влияет и на список треков, и на очередь плеера.
  const [recursive, setRecursive] = useState(false)

  const [actionsTarget, setActionsTarget] = useState(null)
  const [renameTarget, setRenameTarget] = useState(null)
  const [renameValue, setRenameValue] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [moveTarget, setMoveTarget] = useState(null)
  const [playlistTarget, setPlaylistTarget] = useState(null)
  const [deleteTarget, setDeleteTarget] = useState(null)
  const [deleting, setDeleting] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [hasMore, setHasMore] = useState(true)
  const [queueLoading, setQueueLoading] = useState(false)

  // Пагинация: какой набор загружен и сколько треков уже показано — чтобы перезагрузка
  // по playVersion возвращала весь список, а не только первую страницу.
  const loadedKeyRef = useRef('')
  const loadedCountRef = useRef(0)

  // playVersion в зависимостях: после учтённого прослушивания счётчики обновляются сами.
  const { data, error, loading, reload, setData } = useAsync(async () => {
    if (!isValidId) return { folder: null, crumbs: [], folders: [], tracks: [] }
    const key = `${folderId}:${recursive ? 1 : 0}`
    if (loadedKeyRef.current !== key) {
      loadedKeyRef.current = key
      loadedCountRef.current = 0
    }
    // Перезапрашиваем столько треков, сколько уже показано, иначе подгруженные
    // страницы «Показать ещё» схлопнулись бы до первой.
    const limit = Math.min(MAX_TRACKS_LIMIT, Math.max(PAGE_SIZE, loadedCountRef.current))
    const [folder, path, children, tracks] = await Promise.all([
      api.folders.get(folderId),
      // Крошки и подпапки не критичны: без них страница всё равно работает.
      api.folders.path(folderId).catch(() => null),
      api.folders.list({ parent_id: folderId, section: 'all' }).catch(() => []),
      api.folders.tracks(folderId, { limit, offset: 0, recursive }),
    ])
    const items = Array.isArray(tracks) ? tracks.filter(Boolean) : []
    setHasMore(items.length >= limit)
    const crumbs = Array.isArray(path?.items) ? path.items.filter(Boolean) : []
    return {
      folder: folder || null,
      crumbs,
      folders: sortByName(Array.isArray(children) ? children.filter(Boolean) : []),
      tracks: sortByTitle(items),
    }
  }, [folderId, recursive, playVersion])

  useEffect(() => {
    if (error) toast(errorText(error, 'Не удалось загрузить папку'), 'error')
  }, [error, toast])

  // Новая папка или смена режима «включая подпапки» — пагинацию начинаем заново.
  useEffect(() => {
    setHasMore(true)
  }, [folderId, recursive])

  const folder = data?.folder || null
  const crumbs = useMemo(() => (Array.isArray(data?.crumbs) ? data.crumbs : []), [data])
  const subfolders = useMemo(() => (Array.isArray(data?.folders) ? data.folders : []), [data])
  const tracks = useMemo(() => (Array.isArray(data?.tracks) ? data.tracks : []), [data])

  // Запоминаем размер показанного списка: следующая перезагрузка вернёт столько же треков.
  useEffect(() => {
    loadedCountRef.current = tracks.length
  }, [tracks])

  const totalDuration = useMemo(
    () => tracks.reduce((sum, track) => sum + (Number(track?.duration) || 0), 0),
    [tracks],
  )

  // Очередь плеера — только аудио. В папке могут лежать документы и видео
  // (эндпоинт треков папки отдаёт все типы), а автопереход плеера тип не проверяет
  // и на первом же не-аудио файле оборвал бы воспроизведение ошибкой.
  const audioQueue = useMemo(() => tracks.filter(isAudioTrack), [tracks])

  // Номер строки в очереди: в общем списке индексы сдвинуты файлами других типов.
  const queueIndexById = useMemo(() => {
    const map = new Map()
    audioQueue.forEach((track, position) => map.set(track.id, position))
    return map
  }, [audioQueue])

  const initialLoading = loading && data === null

  /** Подгрузка следующей страницы треков папки. */
  const loadMore = useCallback(async () => {
    if (!isValidId || loadingMore) return
    setLoadingMore(true)
    try {
      const next = await api.folders.tracks(folderId, {
        limit: PAGE_SIZE,
        offset: tracks.length,
        recursive,
      })
      const items = Array.isArray(next) ? next.filter(Boolean) : []
      const known = new Set(tracks.map((track) => track.id))
      const fresh = items.filter((track) => !known.has(track.id))
      if (fresh.length === 0) {
        setHasMore(false)
        return
      }
      setData((prev) => ({
        ...(prev || {}),
        tracks: sortByTitle([...(prev?.tracks || []), ...fresh]),
      }))
      if (items.length < PAGE_SIZE) setHasMore(false)
    } catch (err) {
      toast(errorText(err, 'Не удалось загрузить ещё треки'), 'error')
    } finally {
      setLoadingMore(false)
    }
  }, [folderId, isValidId, loadingMore, recursive, setData, toast, tracks])

  /** Очередь плеера собирается на бэкенде: только аудио, по желанию — с подпапками. */
  const handlePlayAll = async () => {
    if (queueLoading) return
    setQueueLoading(true)
    try {
      const queue = await api.folders.play(folderId, recursive)
      const list = Array.isArray(queue) ? queue.filter(Boolean) : []
      if (list.length === 0) {
        toast(
          recursive
            ? 'В этой папке и её подпапках пока нет аудио'
            : 'В этой папке пока нет аудио — попробуйте включить подпапки',
          'info',
        )
        return
      }
      hapticImpact('medium')
      player.playQueue(list, 0)
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось собрать очередь воспроизведения'), 'error')
    } finally {
      setQueueLoading(false)
    }
  }

  const toggleRecursive = () => {
    hapticImpact('light')
    setRecursive((prev) => !prev)
  }

  // --- Действия по треку ---------------------------------------------------

  const openActions = (track) => {
    hapticImpact('light')
    setActionsTarget(track)
  }

  const closeActions = () => setActionsTarget(null)

  const openRename = () => {
    const track = actionsTarget
    if (!track) return
    setActionsTarget(null)
    setRenameTarget(track)
    setRenameValue(track.title || '')
  }

  const closeRename = () => {
    if (renaming) return
    setRenameTarget(null)
    setRenameValue('')
  }

  const handleRename = async () => {
    const track = renameTarget
    if (!track || renaming) return
    const title = renameValue.trim()
    if (!title) {
      toast('Введите новое название трека', 'error')
      return
    }
    if (title === track.title) {
      closeRename()
      return
    }
    setRenaming(true)
    try {
      const updated = await api.tracks.rename(track.id, title)
      hapticNotification('success')
      toast(`Трек переименован в «${updated?.title || title}»`, 'success')
      setData((prev) => ({
        ...(prev || {}),
        tracks: sortByTitle(
          (prev?.tracks || []).map((item) =>
            item.id === track.id ? { ...item, ...(updated || {}), title: updated?.title || title } : item,
          ),
        ),
      }))
      setRenameTarget(null)
      setRenameValue('')
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось переименовать трек'), 'error')
    } finally {
      setRenaming(false)
    }
  }

  /** Перенос трека в выбранную папку (null — «без папки»). */
  const handleMove = async (target) => {
    const track = moveTarget
    if (!track) return
    const targetId = target?.id ?? null
    if (targetId === folderId) {
      toast('Трек уже в этой папке', 'info')
      return
    }
    try {
      await api.tracks.move(track.id, targetId)
      hapticNotification('success')
      toast(target ? `Трек перенесён в папку «${target.name}»` : 'Трек убран из папки', 'success')
      // Трек покинул текущую папку — сразу убираем его из списка.
      setData((prev) => ({
        ...(prev || {}),
        tracks: (prev?.tracks || []).filter((item) => item.id !== track.id),
      }))
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось перенести трек'), 'error')
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

  /** Удаление трека из библиотеки. */
  const handleDelete = async () => {
    const track = deleteTarget
    if (!track || deleting) return
    setDeleting(true)
    try {
      await api.tracks.remove(track.id)
      hapticNotification('success')
      toast('Трек удалён', 'success')
      setDeleteTarget(null)
      setData((prev) => ({
        ...(prev || {}),
        tracks: (prev?.tracks || []).filter((item) => item.id !== track.id),
      }))
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось удалить трек'), 'error')
    } finally {
      setDeleting(false)
    }
  }

  const trackActions = useMemo(
    () => [
      {
        key: 'more',
        icon: '⋯',
        title: 'Действия с треком',
        onClick: openActions,
      },
    ],
    [],
  )

  // --- Разметка ------------------------------------------------------------

  if (!isValidId) {
    return (
      <div className="page">
        <EmptyState
          icon="📁"
          title="Папка не найдена"
          description="Ссылка на папку выглядит неверной."
          action={
            <Link className="btn btn--primary" to="/folders">
              Ко всем папкам
            </Link>
          }
        />
      </div>
    )
  }

  if (initialLoading) {
    return (
      <div className="page">
        <Loader label="Загружаем папку…" />
      </div>
    )
  }

  if (error && !folder) {
    return (
      <div className="page">
        <EmptyState
          icon="⚠️"
          title="Папка недоступна"
          description={errorText(error, 'Не удалось загрузить папку. Попробуйте ещё раз.')}
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
  const ownCount = Number(folder?.track_count) || 0
  const totalCount = Number(folder?.total_track_count) || ownCount
  const headerCount = recursive ? totalCount : ownCount
  // Крошки без последнего элемента: последняя папка — это текущая, она не ссылка.
  const parentCrumbs = crumbs.slice(0, -1)

  const renameFooter = (
    <div className="modal__actions">
      <button type="button" className="btn" onClick={closeRename} disabled={renaming}>
        Отмена
      </button>
      <button
        type="button"
        className="btn btn--primary"
        onClick={handleRename}
        disabled={renaming || !renameValue.trim()}
      >
        {renaming ? 'Сохранение…' : 'Сохранить'}
      </button>
    </div>
  )

  return (
    <div className="page">
      {/* Разделители «›» рисует CSS (`.breadcrumbs__item + .breadcrumbs__item::before`),
          поэтому все крошки — прямые соседи без обёрток. */}
      <nav className="breadcrumbs" aria-label="Путь к папке">
        <Link className="breadcrumbs__item" to="/folders" onClick={() => hapticImpact('light')}>
          📁 Папки
        </Link>
        {parentCrumbs.map((crumb) => (
          <Link
            key={crumb.id}
            className="breadcrumbs__item"
            to={`/folders/${crumb.id}`}
            onClick={() => hapticImpact('light')}
          >
            {crumb.name}
          </Link>
        ))}
        <span className="breadcrumbs__item breadcrumbs__item--current" aria-current="page">
          {folder?.name || 'Папка'}
        </span>
      </nav>

      <div className="section__header">
        <h1 className="section__title">
          {folder?.is_artist_folder ? '🎤' : '📁'} {folder?.name || 'Папка'}
        </h1>
        <span className="muted">{formatCount(headerCount, TRACK_FORMS)}</span>
      </div>

      <div className="row">
        <button
          type="button"
          className="btn btn--primary"
          onClick={handlePlayAll}
          disabled={queueLoading}
        >
          {queueLoading ? 'Собираем очередь…' : '▶️ Воспроизвести всё'}
        </button>
        <button
          type="button"
          className={recursive ? 'chip is-active' : 'chip'}
          onClick={toggleRecursive}
          aria-pressed={recursive}
          title="Показывать и воспроизводить треки вложенных папок"
        >
          🗂 Включая подпапки
        </button>
      </div>

      {subfolders.length > 0 ? (
        <>
          <div className="section__header">
            <h2 className="section__title">Подпапки</h2>
            <span className="muted">{formatCount(subfolders.length, FOLDER_FORMS)}</span>
          </div>

          <div className="list">
            {subfolders.map((item) => {
              const own = Number(item.track_count) || 0
              const total = Number(item.total_track_count) || own
              return (
                <Link
                  className="list-item"
                  key={item.id}
                  to={`/folders/${item.id}`}
                  onClick={() => hapticImpact('light')}
                  aria-label={`Открыть папку ${item.name}`}
                >
                  <span className="list-item__icon" aria-hidden="true">
                    {item.is_artist_folder ? '🎤' : '📁'}
                  </span>
                  <span className="list-item__title">{item.name}</span>
                  <span className="list-item__meta">{formatCount(own, TRACK_FORMS)}</span>
                  {total > own ? (
                    <span className="badge" title="Всего вместе с подпапками">
                      Σ {total}
                    </span>
                  ) : null}
                </Link>
              )
            })}
          </div>
        </>
      ) : null}

      {tracks.length > 0 ? (
        <>
          <div className="section__header">
            <h2 className="section__title">Треки</h2>
            <span className="muted">
              {formatCount(tracks.length, TRACK_FORMS)}
              {durationLabel}
            </span>
          </div>

          <div className="list">
            {tracks.map((track, index) => (
              <TrackRow
                key={track.id}
                track={track}
                index={queueIndexById.has(track.id) ? queueIndexById.get(track.id) : index}
                queue={audioQueue}
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
          icon="🎧"
          title={subfolders.length > 0 ? 'В самой папке треков нет' : 'В папке пока пусто'}
          description={
            subfolders.length > 0
              ? 'Треки лежат во вложенных папках. Включите «Включая подпапки», чтобы увидеть их все сразу.'
              : 'Перенесите сюда треки из библиотеки или пришлите боту новый аудиофайл — он попадёт в папку исполнителя.'
          }
          action={
            subfolders.length > 0 && !recursive ? (
              <button type="button" className="btn btn--primary" onClick={toggleRecursive}>
                🗂 Показать с подпапками
              </button>
            ) : (
              <Link className="btn btn--primary" to="/folders">
                Ко всем папкам
              </Link>
            )
          }
        />
      )}

      {/* Действия с треком */}
      <Modal
        open={Boolean(actionsTarget)}
        title={actionsTarget?.title || 'Трек'}
        onClose={closeActions}
      >
        <ul className="list">
          <li>
            <button type="button" className="list-item list-item--action" onClick={openRename}>
              <span className="list-item__icon" aria-hidden="true">
                ✏️
              </span>
              <span className="list-item__title">Переименовать</span>
            </button>
          </li>
          <li>
            <button
              type="button"
              className="list-item"
              onClick={() => {
                const track = actionsTarget
                setActionsTarget(null)
                setPlaylistTarget(track)
              }}
            >
              <span className="list-item__icon" aria-hidden="true">
                ➕
              </span>
              <span className="list-item__title">Добавить в плейлист</span>
            </button>
          </li>
          <li>
            <button
              type="button"
              className="list-item"
              onClick={() => {
                const track = actionsTarget
                setActionsTarget(null)
                setMoveTarget(track)
              }}
            >
              <span className="list-item__icon" aria-hidden="true">
                📂
              </span>
              <span className="list-item__title">Перенести в другую папку</span>
            </button>
          </li>
          <li>
            <button
              type="button"
              className="list-item"
              onClick={() => {
                const track = actionsTarget
                setActionsTarget(null)
                setDeleteTarget(track)
              }}
            >
              <span className="list-item__icon" aria-hidden="true">
                🗑
              </span>
              <span className="list-item__title">Удалить трек</span>
            </button>
          </li>
        </ul>
        <p className="modal__hint">
          Добавить в избранное можно звёздочкой прямо в строке трека.
        </p>
      </Modal>

      {/* Переименование трека */}
      <Modal
        open={Boolean(renameTarget)}
        title="Переименовать трек"
        onClose={closeRename}
        footer={renameFooter}
      >
        <div className="field">
          <label className="field__label" htmlFor="track-rename">
            Новое название
          </label>
          <input
            id="track-rename"
            className="input"
            type="text"
            value={renameValue}
            maxLength={TITLE_MAX_LENGTH}
            placeholder="Например: Группа крови"
            onChange={(event) => setRenameValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                handleRename()
              }
            }}
            disabled={renaming}
            autoComplete="off"
          />
        </div>
      </Modal>

      <FolderPickerModal
        open={Boolean(moveTarget)}
        onClose={() => setMoveTarget(null)}
        onPick={handleMove}
        allowNone
        allowCreate
        title="Перенести в папку"
      />

      <PlaylistPickerModal
        open={Boolean(playlistTarget)}
        onClose={() => setPlaylistTarget(null)}
        onPick={handleAddToPlaylist}
        allowCreate
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="Удалить трек?"
        message={`Трек «${deleteTarget?.title || ''}» будет удалён из библиотеки. Файл останется в канале-хранилище.`}
        confirmText={deleting ? 'Удаляем…' : 'Удалить'}
        cancelText="Отмена"
        onConfirm={handleDelete}
        onCancel={() => {
          if (!deleting) setDeleteTarget(null)
        }}
      />
    </div>
  )
}

export default FolderPage
