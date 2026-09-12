/**
 * Страница «Треки» (маршрут `/tracks`, контракт V2, раздел 6; ТЗ п. 11, 12, 16).
 *
 * Показывает ТОЛЬКО аудио (`GET /tracks?file_type=audio`), отсортированное по дате
 * добавления (сначала новые). Папки на этой странице не показываются намеренно —
 * для них есть раздел «Папки».
 *
 * Возможности:
 *  - строка поиска с debounce 300 мс → `GET /tracks?q=` (нечёткий поиск бэкенда);
 *  - постраничная подгрузка «Показать ещё» (limit/offset);
 *  - «▶️ Воспроизвести всё» → `GET /tracks/play_all` → `player.playQueue(список)`;
 *  - у каждого трека кнопка «⋯» открывает меню действий: в плейлист
 *    (PlaylistPickerModal), в папку (FolderPickerModal), переименовать
 *    (Modal → `PATCH /tracks/{id}`), удалить (ConfirmDialog →
 *    `DELETE /tracks/{id}?delete_from_channel=`); звёздочка избранного — внутри TrackRow.
 *    Меню вместо ряда иконок выбрано осознанно: на экране 375 px пять кнопок в
 *    строке не оставляли места названию трека (см. allowRename/allowDelete ниже).
 *
 * `playVersion` из `usePlayer()` держим в зависимостях: после учтённого прослушивания
 * уже показанный диапазон перезапрашивается, поэтому счётчики «▶ N» и порядок
 * (если выбран порядок по прослушиваниям) обновляются без перезагрузки страницы.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import ConfirmDialog from '../components/ConfirmDialog.jsx'
import EmptyState from '../components/EmptyState.jsx'
import FolderPickerModal from '../components/FolderPickerModal.jsx'
import Loader from '../components/Loader.jsx'
import Modal from '../components/Modal.jsx'
import PlaylistPickerModal from '../components/PlaylistPickerModal.jsx'
import SearchBar from '../components/SearchBar.jsx'
import TrackRow from '../components/TrackRow.jsx'
import { api } from '../api/client.js'
import { usePlayer } from '../context/PlayerContext.jsx'
import { useToast } from '../context/ToastContext.jsx'
import { hapticImpact, hapticNotification } from '../telegram.js'
import { formatCount } from '../utils/format.js'

/** Сколько треков запрашиваем за один раз. */
const PAGE_SIZE = 50

/** Максимальный `limit` одного запроса `GET /tracks` на бэкенде (Query(le=200)). */
const MAX_LIMIT = 200

/** Задержка поиска, мс (контракт: 300). */
const SEARCH_DELAY = 300

/** Порядок по дате добавления — сначала новые. */
const ORDER = 'created_at_desc'

/** Формы слова «трек» для склонения. */
const TRACK_FORMS = ['трек', 'трека', 'треков']

/** Человеческий текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  if (!error) return fallback
  const detail = error.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  const message = error.message
  if (typeof message === 'string' && message.trim()) return message
  return fallback
}

/** Ответ API → массив треков без «дырок». */
function toTracks(payload) {
  return Array.isArray(payload) ? payload.filter(Boolean) : []
}

/** Название трека для сообщений и заголовков окон. */
function trackTitle(track) {
  const title = typeof track?.title === 'string' ? track.title.trim() : ''
  return title || 'Без названия'
}

export function TracksPage() {
  const player = usePlayer()
  const { playVersion } = player
  const { toast } = useToast()

  // Поиск: queryInput — то, что видит пользователь, query — то, что ушло в API.
  const [queryInput, setQueryInput] = useState('')
  const [query, setQuery] = useState('')

  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [error, setError] = useState(null)
  const [reloadKey, setReloadKey] = useState(0)
  const [queueLoading, setQueueLoading] = useState(false)

  // Треки, для которых открыты меню и модальные окна действий.
  const [menuTrack, setMenuTrack] = useState(null)
  const [playlistTrack, setPlaylistTrack] = useState(null)
  const [folderTrack, setFolderTrack] = useState(null)
  const [renameTarget, setRenameTarget] = useState(null)
  const [renameValue, setRenameValue] = useState('')
  const [renameError, setRenameError] = useState('')
  const [renameSaving, setRenameSaving] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState(null)
  const [deleteFromChannel, setDeleteFromChannel] = useState(false)
  const [deleting, setDeleting] = useState(false)

  // Debounce строки поиска: запрос уходит через 300 мс после последнего нажатия.
  useEffect(() => {
    const trimmed = queryInput.trim()
    if (trimmed === query) return undefined
    const timer = setTimeout(() => setQuery(trimmed), SEARCH_DELAY)
    return () => clearTimeout(timer)
  }, [query, queryInput])

  // Первая страница списка: при смене запроса и при ручной перезагрузке.
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    const load = async () => {
      try {
        const page = await api.tracks.list({
          file_type: 'audio',
          order: ORDER,
          limit: PAGE_SIZE,
          offset: 0,
          q: query || null,
        })
        if (cancelled) return
        const list = toTracks(page)
        setItems(list)
        setHasMore(list.length >= PAGE_SIZE)
      } catch (err) {
        if (cancelled) return
        setItems([])
        setHasMore(false)
        setError(err)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    load()
    return () => {
      cancelled = true
    }
  }, [query, reloadKey])

  // Сколько треков уже показано и не идёт ли изменение списка — для фонового обновления.
  const shownCountRef = useRef(0)
  shownCountRef.current = items.length
  const busyRef = useRef(false)
  busyRef.current = deleting || renameSaving

  // playVersion в зависимостях (требование контракта): после учтённого прослушивания
  // перезапрашиваем УЖЕ показанный диапазон (offset = 0), поэтому счётчики
  // обновляются сами, а подгруженные страницы «Показать ещё» не теряются.
  const playVersionSeen = useRef(playVersion)
  useEffect(() => {
    if (playVersionSeen.current === playVersion) return undefined
    playVersionSeen.current = playVersion
    if (loading || error || busyRef.current) return undefined

    let cancelled = false
    const limit = Math.min(Math.max(shownCountRef.current, PAGE_SIZE), MAX_LIMIT)

    const refresh = async () => {
      try {
        const page = await api.tracks.list({
          file_type: 'audio',
          order: ORDER,
          limit,
          offset: 0,
          q: query || null,
        })
        if (cancelled) return
        const list = toTracks(page)
        setItems(list)
        setHasMore(list.length >= limit)
      } catch {
        // Фоновое обновление не критично: оставляем уже показанный список как есть.
      }
    }

    refresh()
    return () => {
      cancelled = true
    }
  }, [error, loading, playVersion, query])

  /** Подгрузка следующей страницы. */
  const handleLoadMore = useCallback(async () => {
    if (loadingMore || !hasMore) return
    setLoadingMore(true)
    try {
      const page = await api.tracks.list({
        file_type: 'audio',
        order: ORDER,
        limit: PAGE_SIZE,
        offset: items.length,
        q: query || null,
      })
      const list = toTracks(page)
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
  }, [hasMore, items.length, loadingMore, query, toast])

  /** «Воспроизвести всё»: очередь берём с бэкенда, а не из показанного куска списка. */
  const handlePlayAll = useCallback(async () => {
    if (queueLoading) return
    setQueueLoading(true)
    try {
      const queue = await api.tracks.playAll({ order: ORDER })
      const list = toTracks(queue)
      if (list.length === 0) {
        toast('Пока нечего проигрывать: в библиотеке нет аудио', 'info')
        return
      }
      hapticImpact('medium')
      player.playQueue(list, 0)
      toast(`Играем всё: ${formatCount(list.length, TRACK_FORMS)}`, 'success')
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось собрать очередь воспроизведения'), 'error')
    } finally {
      setQueueLoading(false)
    }
  }, [player, queueLoading, toast])

  /** Звёздочка избранного внутри TrackRow меняет флаг — синхронизируем список. */
  const handleFavouriteChanged = useCallback((track, isFavourite) => {
    if (!track) return
    setItems((prev) =>
      prev.map((item) => (item.id === track.id ? { ...item, is_favourite: isFavourite } : item)),
    )
  }, [])

  // Единственная кнопка-действие в строке: на экране 375 px четыре иконки рядом со
  // звёздочкой съедали всё место под название трека. Поэтому действия собраны в
  // меню с подписями — так и понятнее, и название видно целиком.
  const actions = useMemo(
    () => [
      {
        key: 'menu',
        icon: '⋯',
        title: 'Действия с треком',
        onClick: (track) => {
          hapticImpact('light')
          setMenuTrack(track)
        },
      },
    ],
    [],
  )

  /** Пункт меню «Добавить в плейлист». */
  const openPlaylistPicker = useCallback((track) => {
    hapticImpact('light')
    setMenuTrack(null)
    setPlaylistTrack(track)
  }, [])

  /** Пункт меню «Переместить в папку». */
  const openFolderPicker = useCallback((track) => {
    hapticImpact('light')
    setMenuTrack(null)
    setFolderTrack(track)
  }, [])

  /** Пункт меню «Переименовать». */
  const openRename = useCallback((track) => {
    hapticImpact('light')
    setMenuTrack(null)
    setRenameError('')
    setRenameValue(typeof track?.title === 'string' ? track.title : '')
    setRenameTarget(track)
  }, [])

  /** Пункт меню «Удалить». Галочка «и из канала» каждый раз начинается снятой. */
  const openDelete = useCallback((track) => {
    hapticImpact('light')
    setMenuTrack(null)
    setDeleteFromChannel(false)
    setDeleteTarget(track)
  }, [])

  /** Добавление выбранного трека в плейлист. */
  const handlePickPlaylist = useCallback(
    async (playlist) => {
      const track = playlistTrack
      setPlaylistTrack(null)
      if (!playlist || !track) return
      try {
        await api.playlists.addTracks(playlist.id, [track.id])
        hapticNotification('success')
        toast(`Трек добавлен в «${playlist.name}»`, 'success')
      } catch (err) {
        hapticNotification('error')
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
        hapticNotification('success')
        toast(folder ? `Трек перемещён в «${folder.name}»` : 'Трек убран из папки', 'success')
      } catch (err) {
        hapticNotification('error')
        toast(errorText(err, 'Не удалось переместить трек'), 'error')
      }
    },
    [folderTrack, toast],
  )

  const closeRename = useCallback(() => {
    if (renameSaving) return
    setRenameTarget(null)
    setRenameError('')
  }, [renameSaving])

  /** Сохранение нового названия трека. */
  const handleRenameSubmit = useCallback(async () => {
    const track = renameTarget
    if (!track || renameSaving) return
    const title = renameValue.trim()
    if (!title) {
      setRenameError('Название не может быть пустым')
      return
    }
    if (title === (track.title || '')) {
      setRenameTarget(null)
      setRenameError('')
      return
    }
    setRenameSaving(true)
    setRenameError('')
    try {
      const updated = await api.tracks.rename(track.id, title)
      setItems((prev) =>
        prev.map((item) =>
          item.id === track.id
            ? { ...item, ...(updated && typeof updated === 'object' ? updated : {}), title }
            : item,
        ),
      )
      hapticNotification('success')
      toast('Название обновлено', 'success')
      setRenameTarget(null)
    } catch (err) {
      hapticNotification('error')
      setRenameError(errorText(err, 'Не удалось переименовать трек'))
    } finally {
      setRenameSaving(false)
    }
  }, [renameSaving, renameTarget, renameValue, toast])

  /** Удаление трека: из библиотеки, при желании — и из канала-хранилища. */
  const handleConfirmDelete = useCallback(async () => {
    const track = deleteTarget
    if (!track || deleting) return
    setDeleting(true)
    try {
      await api.tracks.remove(track.id, { delete_from_channel: deleteFromChannel })
      setItems((prev) => prev.filter((item) => item.id !== track.id))
      hapticNotification('success')
      toast(
        deleteFromChannel ? 'Трек удалён из библиотеки и канала' : 'Трек удалён из библиотеки',
        'success',
      )
      setDeleteTarget(null)
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось удалить трек'), 'error')
    } finally {
      setDeleting(false)
    }
  }, [deleteFromChannel, deleteTarget, deleting, toast])

  const searching = Boolean(query)
  // Пустая библиотека (не результат поиска): убираем поиск и кнопки, оставляем
  // только дружелюбное пустое состояние — искать и проигрывать пока нечего.
  const libraryEmpty = !loading && !searching && !error && items.length === 0

  // Диалоги и окна нужны в любой ветке рендера — держим их одним блоком.
  const dialogs = (
    <>
      <Modal
        open={Boolean(menuTrack)}
        title={menuTrack ? trackTitle(menuTrack) : 'Действия с треком'}
        onClose={() => setMenuTrack(null)}
      >
        <div className="list">
          <button
            type="button"
            className="list-item"
            onClick={() => openPlaylistPicker(menuTrack)}
          >
            <span className="list-item__icon" aria-hidden="true">
              ➕
            </span>
            <span className="list-item__title">Добавить в плейлист</span>
          </button>
          <button type="button" className="list-item" onClick={() => openFolderPicker(menuTrack)}>
            <span className="list-item__icon" aria-hidden="true">
              📂
            </span>
            <span className="list-item__title">Переместить в папку</span>
            <span className="list-item__meta">{menuTrack?.folder_name || 'без папки'}</span>
          </button>
          <button type="button" className="list-item" onClick={() => openRename(menuTrack)}>
            <span className="list-item__icon" aria-hidden="true">
              ✏️
            </span>
            <span className="list-item__title">Переименовать</span>
          </button>
          <button type="button" className="list-item" onClick={() => openDelete(menuTrack)}>
            <span className="list-item__icon" aria-hidden="true">
              🗑
            </span>
            <span className="list-item__title">Удалить трек</span>
          </button>
        </div>
      </Modal>

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

      <Modal
        open={Boolean(renameTarget)}
        title="Переименовать трек"
        onClose={closeRename}
        footer={
          <div className="modal__actions">
            <button type="button" className="btn" onClick={closeRename} disabled={renameSaving}>
              Отмена
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={handleRenameSubmit}
              disabled={renameSaving}
            >
              {renameSaving ? 'Сохраняем…' : 'Сохранить'}
            </button>
          </div>
        }
      >
        {renameError ? <p className="modal__error">{renameError}</p> : null}
        <div className="field">
          <label className="field__label" htmlFor="track-rename-input">
            Название трека
          </label>
          <input
            id="track-rename-input"
            className="input"
            type="text"
            value={renameValue}
            onChange={(event) => setRenameValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                handleRenameSubmit()
              }
            }}
            placeholder="Например: Кино — Группа крови"
            autoComplete="off"
            disabled={renameSaving}
          />
          <span className="field__hint">Исполнитель и папка останутся прежними.</span>
        </div>
      </Modal>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="Удалить трек?"
        message={
          deleteTarget ? (
            <>
              {`«${trackTitle(deleteTarget)}» исчезнет из библиотеки, плейлистов и избранного.`}
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={deleteFromChannel}
                  onChange={(event) => setDeleteFromChannel(event.target.checked)}
                  disabled={deleting}
                />
                <span className="checkbox__label">Удалить файл и из канала-хранилища</span>
              </label>
              <span className="field__hint">
                Без галочки файл останется в канале — трек можно будет загрузить снова.
              </span>
            </>
          ) : (
            ''
          )
        }
        confirmText={deleting ? 'Удаляем…' : 'Удалить'}
        cancelText="Отмена"
        onConfirm={handleConfirmDelete}
        onCancel={() => (deleting ? null : setDeleteTarget(null))}
      />
    </>
  )

  // Ошибка на первой загрузке — предлагаем повторить.
  if (error && items.length === 0) {
    return (
      <div className="page">
        <div className="page__header">
          <h1 className="page__title">🎵 Треки</h1>
        </div>
        <EmptyState
          icon="⚠️"
          title="Не удалось загрузить треки"
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
        {dialogs}
      </div>
    )
  }

  return (
    <div className="page">
      <div className="page__header">
        <h1 className="page__title">🎵 Треки</h1>
        <p className="page__subtitle muted">
          {searching
            ? 'Поиск по названию и исполнителю'
            : 'Вся музыка одним списком — сначала новые'}
        </p>
      </div>

      {libraryEmpty ? null : (
        <>
          <SearchBar value={queryInput} onChange={setQueryInput} placeholder="Поиск по трекам…" />

          <div className="page__actions">
            <button
              type="button"
              className="btn btn--primary"
              onClick={handlePlayAll}
              disabled={queueLoading}
            >
              {queueLoading ? 'Собираем очередь…' : '▶️ Воспроизвести всё'}
            </button>
          </div>
        </>
      )}

      {loading ? (
        <Loader label={searching ? 'Ищем треки…' : 'Загружаем треки…'} />
      ) : items.length === 0 ? (
        searching ? (
          <EmptyState
            icon="🔍"
            title="Ничего не нашлось"
            description={`По запросу «${query}» треков нет. Попробуйте другое слово — поиск прощает опечатки и неправильную раскладку.`}
            action={
              <button type="button" className="btn" onClick={() => setQueryInput('')}>
                Сбросить поиск
              </button>
            }
          />
        ) : (
          <EmptyState
            icon="🎵"
            title="Здесь пока пусто"
            description="Отправьте боту любой аудиофайл — он сохранит его, разложит по папкам и трек появится в этом списке."
            action={
              <Link className="btn btn--primary" to="/tgsearch">
                Найти музыку в Telegram
              </Link>
            }
          />
        )
      ) : (
        <>
          <div className="row row--between">
            <span className="muted">{formatCount(items.length, TRACK_FORMS)}</span>
          </div>

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
                // Встроенные «✏️» и «🗑» TrackRow выключены намеренно: вместе со
                // звёздочкой и «⋯» получалось пять иконок в строке, и на экране
                // 375 px от названия трека оставалось несколько букв. Оба действия
                // есть в меню «⋯» — с подписями и подтверждением удаления.
                allowRename={false}
                allowDelete={false}
              />
            ))}
          </div>

          {hasMore ? (
            <button
              type="button"
              className="btn btn--block"
              onClick={handleLoadMore}
              disabled={loadingMore}
            >
              {loadingMore ? 'Загружаем…' : 'Показать ещё'}
            </button>
          ) : (
            <p className="muted list-footer">
              {searching
                ? `Найдено: ${formatCount(items.length, TRACK_FORMS)}`
                : `Это все треки · ${formatCount(items.length, TRACK_FORMS)}`}
            </p>
          )}
        </>
      )}

      {dialogs}
    </div>
  )
}

export default TracksPage
