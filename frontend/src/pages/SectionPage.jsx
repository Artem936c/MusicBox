/**
 * Страница раздела статистики: /section/:key (top | recent | unplayed | frequent | rare).
 *
 * Полный список раздела с постраничной подгрузкой («Показать ещё», limit/offset),
 * у каждого трека действия: добавить в плейлист (PlaylistPickerModal),
 * переместить в папку (FolderPickerModal), удалить (ConfirmDialog).
 * Очередь воспроизведения — весь загруженный список раздела.
 * После учтённого прослушивания (`playVersion`) показанный диапазон перезапрашивается,
 * поэтому счётчики и порядок раздела обновляются в реальном времени.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import ConfirmDialog from '../components/ConfirmDialog.jsx'
import EmptyState from '../components/EmptyState.jsx'
import FolderPickerModal from '../components/FolderPickerModal.jsx'
import Loader from '../components/Loader.jsx'
import PlaylistPickerModal from '../components/PlaylistPickerModal.jsx'
import TrackRow from '../components/TrackRow.jsx'
import { api } from '../api/client.js'
import { usePlayer } from '../context/PlayerContext.jsx'
import { useToast } from '../context/ToastContext.jsx'
import { hapticImpact } from '../telegram.js'
import { formatCount } from '../utils/format.js'
import { sectionByKey, sectionEmptyText } from '../utils/sections.js'

/** Размер страницы подгрузки. */
const PAGE_SIZE = 30

/** Максимальный limit одного запроса раздела на бэкенде. */
const MAX_SECTION_LIMIT = 200

/** Формы слова «трек» для склонения. */
const TRACK_FORMS = ['трек', 'трека', 'треков']

/** Человекочитаемый текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  if (!error) return fallback
  if (typeof error.detail === 'string' && error.detail) return error.detail
  return error.message || fallback
}

export function SectionPage() {
  const { key } = useParams()
  const meta = useMemo(() => sectionByKey(key), [key])
  const player = usePlayer()
  const { playVersion } = player
  const { toast } = useToast()

  const [items, setItems] = useState([])
  const [total, setTotal] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [error, setError] = useState(null)
  const [reloadKey, setReloadKey] = useState(0)

  // Треки, для которых открыты модальные окна действий.
  const [playlistTrack, setPlaylistTrack] = useState(null)
  const [folderTrack, setFolderTrack] = useState(null)
  const [deleteTarget, setDeleteTarget] = useState(null)
  const [deleting, setDeleting] = useState(false)

  // Первая страница раздела + общее количество треков для заголовка.
  useEffect(() => {
    if (!meta) {
      setLoading(false)
      setItems([])
      setTotal(null)
      setHasMore(false)
      setError(null)
      return undefined
    }

    let cancelled = false
    setLoading(true)
    setError(null)
    setItems([])
    setTotal(null)
    setHasMore(false)

    const load = async () => {
      try {
        const [page, counts] = await Promise.all([
          api.stats.section(key, { limit: PAGE_SIZE, offset: 0 }),
          // Счётчики полезны, но не критичны: без них просто не покажем «из N».
          api.stats.counts().catch(() => null),
        ])
        if (cancelled) return
        const list = Array.isArray(page) ? page.filter(Boolean) : []
        setItems(list)
        setHasMore(list.length >= PAGE_SIZE)
        const totalValue = Number(counts?.[key])
        setTotal(Number.isFinite(totalValue) ? totalValue : null)
      } catch (err) {
        if (!cancelled) setError(err)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    load()
    return () => {
      cancelled = true
    }
  }, [key, meta, reloadKey])

  // Сколько треков уже показано и не идёт ли удаление — для фонового обновления.
  const shownCountRef = useRef(0)
  shownCountRef.current = items.length
  const busyRef = useRef(false)
  busyRef.current = deleting

  // playVersion в зависимостях: после учтённого прослушивания перезапрашиваем уже
  // показанный диапазон раздела (offset = 0), поэтому счётчики «▶ N» и порядок
  // обновляются сами, а подгруженные страницы «Показать ещё» не теряются.
  const playVersionSeen = useRef(playVersion)
  useEffect(() => {
    if (playVersionSeen.current === playVersion) return
    playVersionSeen.current = playVersion
    if (!meta || loading || error || busyRef.current) return

    let cancelled = false
    const limit = Math.min(Math.max(shownCountRef.current, PAGE_SIZE), MAX_SECTION_LIMIT)

    const refresh = async () => {
      try {
        const [page, counts] = await Promise.all([
          api.stats.section(key, { limit, offset: 0 }),
          api.stats.counts().catch(() => null),
        ])
        if (cancelled) return
        const list = Array.isArray(page) ? page.filter(Boolean) : []
        setItems(list)
        setHasMore(list.length >= limit)
        const totalValue = Number(counts?.[key])
        setTotal(Number.isFinite(totalValue) ? totalValue : null)
      } catch {
        // Фоновое обновление статистики не критично: оставляем уже показанный список.
      }
    }

    refresh()
    return () => {
      cancelled = true
    }
  }, [error, key, loading, meta, playVersion])

  const handleLoadMore = useCallback(async () => {
    if (loadingMore || !hasMore) return
    setLoadingMore(true)
    try {
      const page = await api.stats.section(key, { limit: PAGE_SIZE, offset: items.length })
      const list = Array.isArray(page) ? page.filter(Boolean) : []
      setItems((prev) => {
        const seen = new Set(prev.map((item) => item.id))
        return [...prev, ...list.filter((item) => !seen.has(item.id))]
      })
      setHasMore(list.length >= PAGE_SIZE)
    } catch (err) {
      toast(errorText(err, 'Не удалось загрузить ещё треки'), 'error')
    } finally {
      setLoadingMore(false)
    }
  }, [hasMore, items.length, key, loadingMore, toast])

  const handleFavouriteChanged = useCallback((track, isFavourite) => {
    if (!track) return
    setItems((prev) =>
      prev.map((item) => (item.id === track.id ? { ...item, is_favourite: isFavourite } : item)),
    )
  }, [])

  const actions = useMemo(
    () => [
      {
        key: 'playlist',
        icon: '➕',
        title: 'Добавить в плейлист',
        onClick: (track) => setPlaylistTrack(track),
      },
      {
        key: 'folder',
        icon: '📂',
        title: 'Переместить в папку',
        onClick: (track) => setFolderTrack(track),
      },
      {
        key: 'delete',
        icon: '🗑',
        title: 'Удалить трек',
        onClick: (track) => setDeleteTarget(track),
      },
    ],
    [],
  )

  /** Добавление выбранного трека в плейлист. */
  const handlePickPlaylist = useCallback(
    async (playlist) => {
      const track = playlistTrack
      setPlaylistTrack(null)
      if (!playlist || !track) return
      try {
        await api.playlists.addTracks(playlist.id, [track.id])
        toast(`Трек добавлен в «${playlist.name}»`, 'success')
      } catch (err) {
        toast(errorText(err, 'Не удалось добавить трек в плейлист'), 'error')
      }
    },
    [playlistTrack, toast],
  )

  /** Перемещение трека в папку (null — «Без папки»). */
  const handlePickFolder = useCallback(
    async (folder) => {
      const track = folderTrack
      setFolderTrack(null)
      if (!track) return
      try {
        const updated = await api.tracks.move(track.id, folder ? folder.id : null)
        setItems((prev) =>
          prev.map((item) =>
            item.id === track.id
              ? {
                  ...item,
                  ...(updated && typeof updated === 'object' ? updated : {}),
                  folder_id: folder ? folder.id : null,
                  folder_name: folder ? folder.name : null,
                }
              : item,
          ),
        )
        toast(folder ? `Трек перемещён в «${folder.name}»` : 'Трек убран из папки', 'success')
      } catch (err) {
        toast(errorText(err, 'Не удалось переместить трек'), 'error')
      }
    },
    [folderTrack, toast],
  )

  /** Удаление трека из библиотеки (файл остаётся в канале-хранилище). */
  const handleConfirmDelete = useCallback(async () => {
    const track = deleteTarget
    if (!track || deleting) return
    setDeleting(true)
    try {
      await api.tracks.remove(track.id)
      setItems((prev) => prev.filter((item) => item.id !== track.id))
      setTotal((prev) => (typeof prev === 'number' ? Math.max(0, prev - 1) : prev))
      toast('Трек удалён из библиотеки', 'success')
      setDeleteTarget(null)
    } catch (err) {
      toast(errorText(err, 'Не удалось удалить трек'), 'error')
    } finally {
      setDeleting(false)
    }
  }, [deleteTarget, deleting, toast])

  const handlePlayAll = useCallback(() => {
    if (!items.length) return
    hapticImpact('medium')
    player.playQueue(items, 0)
  }, [items, player])

  // Неизвестный ключ раздела — мягко возвращаем пользователя к статистике.
  if (!meta) {
    return (
      <div className="page page--section">
        <EmptyState
          icon="🤔"
          title="Такого раздела нет"
          description="Возможно, ссылка устарела. Откройте статистику и выберите нужный раздел."
          action={
            <Link className="btn btn--primary" to="/stats">
              К статистике
            </Link>
          }
        />
      </div>
    )
  }

  if (loading) {
    return <Loader label={`Загружаем раздел «${meta.title}»…`} />
  }

  if (error) {
    return (
      <EmptyState
        icon="⚠️"
        title="Не удалось загрузить раздел"
        description={errorText(error, 'Проверьте подключение и попробуйте ещё раз.')}
        action={
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => setReloadKey((value) => value + 1)}
          >
            Повторить
          </button>
        }
      />
    )
  }

  const shownCount = typeof total === 'number' ? total : items.length

  return (
    <div className="page page--section">
      <div className="section-head">
        <h2 className="section-title">
          <span aria-hidden="true">{meta.icon} </span>
          {meta.title}
        </h2>
        {items.length > 0 ? (
          <span className="section-more muted">{formatCount(shownCount, TRACK_FORMS)}</span>
        ) : null}
      </div>

      {items.length === 0 ? (
        <EmptyState icon={meta.icon} title="Здесь пока пусто" description={sectionEmptyText(key)} />
      ) : (
        <>
          <button type="button" className="btn btn--primary btn--wide" onClick={handlePlayAll}>
            ▶ Проиграть раздел
          </button>

          <div className="track-list">
            {items.map((track, index) => (
              <TrackRow
                key={track.id ?? index}
                track={track}
                index={index}
                queue={items}
                showStats
                actions={actions}
                onChanged={handleFavouriteChanged}
              />
            ))}
          </div>

          {hasMore ? (
            <button
              type="button"
              className="btn btn--wide"
              onClick={handleLoadMore}
              disabled={loadingMore}
            >
              {loadingMore ? 'Загружаем…' : 'Показать ещё'}
            </button>
          ) : (
            <p className="muted list-footer">
              Показаны все треки раздела · {formatCount(items.length, TRACK_FORMS)}
            </p>
          )}
        </>
      )}

      <PlaylistPickerModal
        open={Boolean(playlistTrack)}
        onClose={() => setPlaylistTrack(null)}
        onPick={handlePickPlaylist}
        allowCreate
      />

      <FolderPickerModal
        open={Boolean(folderTrack)}
        onClose={() => setFolderTrack(null)}
        onPick={handlePickFolder}
        allowNone
        allowCreate
        title="Куда переместить трек"
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="Удалить трек?"
        message={
          deleteTarget
            ? `«${deleteTarget.title || 'Без названия'}» исчезнет из библиотеки и всех плейлистов. Файл останется в канале-хранилище.`
            : ''
        }
        confirmText={deleting ? 'Удаляем…' : 'Удалить'}
        cancelText="Отмена"
        onConfirm={handleConfirmDelete}
        onCancel={() => (deleting ? null : setDeleteTarget(null))}
      />
    </div>
  )
}

export default SectionPage
