/**
 * Страница состава плейлиста (маршрут `/playlists/:id`).
 *
 * Ключевая возможность — изменение порядка треков перетаскиванием (@dnd-kit):
 *  - DndContext: PointerSensor {distance: 5} + TouchSensor {delay: 200, tolerance: 5},
 *    closestCenter, модификаторы restrictToVerticalAxis + restrictToParentElement;
 *  - SortableContext с verticalListSortingStrategy;
 *  - отдельный SortableTrack на useSortable; attributes/listeners висят ТОЛЬКО на ручке «⠿»,
 *    поэтому клик по самой строке по-прежнему запускает воспроизведение;
 *  - после onDragEnd порядок применяется оптимистично (arrayMove) и отправляется
 *    в `PUT /playlists/{id}/order`; при ошибке — откат к прежнему порядку и тост.
 *
 * Дополнительно: «▶️ Проиграть всё», удаление трека из плейлиста, добавление треков
 * из библиотеки (модалка со списком и поиском), дружелюбное пустое состояние.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  TouchSensor,
  closestCenter,
  useSensor,
  useSensors,
} from '@dnd-kit/core'
import { restrictToParentElement, restrictToVerticalAxis } from '@dnd-kit/modifiers'
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'

import { api } from '../api/client.js'
import EmptyState from '../components/EmptyState.jsx'
import Loader from '../components/Loader.jsx'
import Modal from '../components/Modal.jsx'
import SearchBar from '../components/SearchBar.jsx'
import TrackRow from '../components/TrackRow.jsx'
import { usePlayer } from '../context/PlayerContext.jsx'
import { useToast } from '../context/ToastContext.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { hapticImpact, hapticNotification, hideBackButton, showBackButton } from '../telegram.js'
import { formatCount, formatDuration } from '../utils/format.js'

/** Общая длительность плейлиста человеческим языком: «1 ч 23 мин». */
function formatTotalDuration(seconds) {
  const total = Math.floor(Number(seconds) || 0)
  if (total <= 0) return ''
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  if (hours > 0) return minutes > 0 ? `${hours} ч ${minutes} мин` : `${hours} ч`
  if (minutes > 0) return `${minutes} мин`
  return `${total} с`
}

/**
 * Строка плейлиста, участвующая в перетаскивании.
 * attributes/listeners навешиваются исключительно на ручку «⠿»: остальная площадь
 * строки остаётся кликабельной и запускает воспроизведение (см. TrackRow).
 */
function SortableTrack({ track, index, queue, disabled, onRemove, onChanged }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: track.id,
    disabled,
  })

  const style = {
    transform: CSS.Transform.toString(transform),
    transition: transition || undefined,
  }

  // Гасим всплытие, иначе нажатие на ручку долетит до строки и включит трек.
  const handleHandleClick = (event) => {
    event.stopPropagation()
  }

  const handleHandleKeyDown = (event) => {
    event.stopPropagation()
    if (typeof listeners?.onKeyDown === 'function') listeners.onKeyDown(event)
  }

  const dragHandle = (
    <button
      type="button"
      className="drag-handle"
      title="Перетащите, чтобы изменить порядок"
      aria-label={`Изменить положение трека «${track.title || 'Без названия'}»`}
      disabled={disabled}
      {...attributes}
      {...listeners}
      onClick={handleHandleClick}
      onKeyDown={handleHandleKeyDown}
    >
      ⠿
    </button>
  )

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`sortable-item${isDragging ? ' sortable-item--dragging' : ''}`}
    >
      <TrackRow
        track={track}
        index={index}
        queue={queue}
        dragHandle={dragHandle}
        onChanged={onChanged}
        actions={[
          {
            key: 'remove',
            icon: '✖️',
            title: 'Убрать из плейлиста',
            onClick: () => onRemove(track),
          },
        ]}
      />
    </div>
  )
}

/** Стили выпадающего списка библиотеки внутри модалки. */
const pickerListStyle = { maxHeight: '46vh', overflowY: 'auto', marginTop: 12 }
const pickerRowStyle = {
  flex: '1 1 auto',
  minWidth: 0,
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'flex-start',
  gap: 2,
  padding: 0,
  border: 0,
  background: 'transparent',
  color: 'inherit',
  font: 'inherit',
  textAlign: 'left',
  cursor: 'pointer',
}

/**
 * Модальное окно выбора треков библиотеки для добавления в плейлист.
 * Пустой запрос — весь список по алфавиту; непустой — нечёткий поиск с debounce 300 мс.
 */
function AddTracksModal({ open, existingIds, onClose, onSubmit }) {
  const [query, setQuery] = useState('')
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState([])
  const [saving, setSaving] = useState(false)

  // Сбрасываем состояние при каждом открытии окна.
  useEffect(() => {
    if (open) {
      setQuery('')
      setSelected([])
      setError(null)
    }
  }, [open])

  useEffect(() => {
    if (!open) return undefined
    let cancelled = false
    const text = query.trim()

    const timer = setTimeout(async () => {
      setLoading(true)
      setError(null)
      try {
        const result = text
          ? await api.search.tracks(text, 40)
          : await api.tracks.list({ order: 'title', limit: 100 })
        if (!cancelled) setItems(Array.isArray(result) ? result : [])
      } catch (err) {
        if (!cancelled) {
          setItems([])
          setError(err?.message || 'Не удалось загрузить треки')
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }, text ? 300 : 0)

    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [open, query])

  const toggle = useCallback((trackId) => {
    hapticImpact('light')
    setSelected((previous) =>
      previous.includes(trackId)
        ? previous.filter((item) => item !== trackId)
        : [...previous, trackId],
    )
  }, [])

  const handleSubmit = async () => {
    if (!selected.length || saving) return
    setSaving(true)
    try {
      await onSubmit(selected)
    } finally {
      setSaving(false)
    }
  }

  const footer = (
    <>
      <button type="button" className="btn" onClick={onClose} disabled={saving}>
        Отмена
      </button>
      <button
        type="button"
        className="btn btn--primary"
        onClick={handleSubmit}
        disabled={saving || selected.length === 0}
      >
        {saving ? 'Добавляем…' : `Добавить (${selected.length})`}
      </button>
    </>
  )

  return (
    <Modal open={open} title="Добавить треки" onClose={saving ? () => {} : onClose} footer={footer}>
      <SearchBar
        value={query}
        onChange={setQuery}
        placeholder="Поиск по библиотеке…"
        autoFocus={false}
      />

      {loading ? <Loader label="Ищем треки…" /> : null}

      {!loading && error ? <p className="error">{error}</p> : null}

      {!loading && !error && items.length === 0 ? (
        <p className="muted" style={{ marginTop: 12 }}>
          {query.trim()
            ? 'Ничего не нашлось. Попробуйте изменить запрос.'
            : 'Библиотека пуста — пришлите боту аудиофайл, и трек появится здесь.'}
        </p>
      ) : null}

      {!loading && !error && items.length > 0 ? (
        <div className="list" style={pickerListStyle}>
          {items.map((track) => {
            const already = existingIds.has(track.id)
            const isSelected = selected.includes(track.id)
            return (
              <div className="list-item" key={track.id}>
                <button
                  type="button"
                  style={{ ...pickerRowStyle, cursor: already ? 'default' : 'pointer' }}
                  onClick={() => {
                    if (!already) toggle(track.id)
                  }}
                  disabled={already}
                  aria-pressed={isSelected}
                >
                  <span className="list-item__title" style={{ width: '100%' }}>
                    {track.title || 'Без названия'}
                  </span>
                  <span className="muted text-ellipsis" style={{ fontSize: 13 }}>
                    {track.artist || 'Неизвестный исполнитель'} ·{' '}
                    {track.duration_label || formatDuration(track.duration)}
                    {already ? ' · уже в плейлисте' : ''}
                  </span>
                </button>
                <span className="list-item__meta" aria-hidden="true">
                  {already ? '✔️' : isSelected ? '☑️' : '⬜'}
                </span>
              </div>
            )
          })}
        </div>
      ) : null}
    </Modal>
  )
}

export default function PlaylistPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const player = usePlayer()
  const { toast } = useToast()

  const playlistId = Number(id)
  const validId = Number.isFinite(playlistId) && playlistId > 0

  const { data, error, loading, reload, setData } = useAsync(() => {
    if (!validId) throw new Error('Плейлист не найден')
    return api.playlists.get(playlistId)
  }, [playlistId])

  // Локальная копия состава: нужна для оптимистичного перетаскивания и отката.
  const [tracks, setTracks] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [savingOrder, setSavingOrder] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [busy, setBusy] = useState(false)

  // Пока идёт перетаскивание или сохранение — фоновые перезагрузки запрещены.
  const busyRef = useRef(false)
  busyRef.current = activeId !== null || savingOrder || busy
  const tracksRef = useRef([])
  tracksRef.current = tracks

  useEffect(() => {
    if (data && Array.isArray(data.tracks)) setTracks(data.tracks)
    else if (data) setTracks([])
  }, [data])

  // Системная кнопка «Назад» Telegram возвращает к списку плейлистов.
  useEffect(() => {
    showBackButton(() => navigate('/playlists'))
    return () => hideBackButton()
  }, [navigate])

  // Счётчики прослушиваний обновляем после воспроизведения, но не во время DnD.
  const playVersionSeen = useRef(player.playVersion)
  useEffect(() => {
    if (playVersionSeen.current === player.playVersion) return
    playVersionSeen.current = player.playVersion
    if (busyRef.current) return
    reload()
  }, [player.playVersion, reload])

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 200, tolerance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  const trackIds = useMemo(() => tracks.map((track) => track.id), [tracks])
  const existingIds = useMemo(() => new Set(trackIds), [trackIds])

  const totalDuration = useMemo(
    () => tracks.reduce((sum, track) => sum + (Number(track.duration) || 0), 0),
    [tracks],
  )

  const applyDetail = useCallback(
    (detail) => {
      if (detail && Array.isArray(detail.tracks)) {
        setData(detail)
        setTracks(detail.tracks)
        return true
      }
      return false
    },
    [setData],
  )

  const handleDragStart = useCallback((event) => {
    setActiveId(event?.active?.id ?? null)
    hapticImpact('medium')
  }, [])

  const handleDragCancel = useCallback(() => {
    setActiveId(null)
  }, [])

  const handleDragEnd = useCallback(
    async (event) => {
      setActiveId(null)
      const { active, over } = event || {}
      if (!active || !over || active.id === over.id) return

      const previous = tracksRef.current
      const oldIndex = previous.findIndex((track) => track.id === active.id)
      const newIndex = previous.findIndex((track) => track.id === over.id)
      if (oldIndex < 0 || newIndex < 0) return

      // Оптимистично показываем новый порядок, затем сохраняем его на сервере.
      const next = arrayMove(previous, oldIndex, newIndex)
      setTracks(next)
      setSavingOrder(true)
      try {
        const detail = await api.playlists.reorder(
          playlistId,
          next.map((track) => track.id),
        )
        if (!applyDetail(detail)) {
          // Сервер вернул неожиданный ответ — перезапрашиваем состав.
          await reload()
        }
        hapticNotification('success')
      } catch (err) {
        setTracks(previous)
        hapticNotification('error')
        toast(err?.message || 'Не удалось сохранить новый порядок треков', 'error')
      } finally {
        setSavingOrder(false)
      }
    },
    [applyDetail, playlistId, reload, toast],
  )

  const handlePlayAll = useCallback(() => {
    if (!tracks.length) {
      toast('В плейлисте пока нет треков', 'info')
      return
    }
    hapticImpact('medium')
    player.playQueue(tracks, 0)
  }, [player, toast, tracks])

  const handleRemove = useCallback(
    async (track) => {
      if (busy) return
      setBusy(true)
      const previous = tracksRef.current
      // Оптимистично убираем строку — список не «моргает».
      setTracks(previous.filter((item) => item.id !== track.id))
      try {
        const detail = await api.playlists.removeTrack(playlistId, track.id)
        if (!applyDetail(detail)) await reload()
        hapticNotification('success')
        toast('Трек убран из плейлиста', 'success')
      } catch (err) {
        setTracks(previous)
        hapticNotification('error')
        toast(err?.message || 'Не удалось убрать трек из плейлиста', 'error')
      } finally {
        setBusy(false)
      }
    },
    [applyDetail, busy, playlistId, reload, toast],
  )

  /**
   * Встроенные действия TrackRow (⭐ / ✏️ / 🗑) меняют трек мимо состояния страницы.
   * Особенно важно удаление: строка из playlist_tracks исчезает по каскаду, и если
   * оставить устаревший id в `tracks`, следующее перетаскивание отправит лишний id
   * и получит 400 ORDER_MISMATCH (а счётчик и длительность останутся неверными).
   */
  const handleTrackChanged = useCallback((changed, isFavourite, detail) => {
    const changedId = changed ? changed.id : null
    if (!changedId) return
    if (detail && detail.type === 'deleted') {
      setTracks((previous) => previous.filter((item) => item.id !== changedId))
      return
    }
    setTracks((previous) =>
      previous.map((item) =>
        item.id === changedId ? { ...item, ...changed, is_favourite: isFavourite } : item,
      ),
    )
  }, [])

  const handleAddTracks = useCallback(
    async (ids) => {
      try {
        const detail = await api.playlists.addTracks(playlistId, ids)
        if (!applyDetail(detail)) await reload()
        hapticNotification('success')
        toast(`Добавлено: ${formatCount(ids.length, ['трек', 'трека', 'треков'])}`, 'success')
        setPickerOpen(false)
      } catch (err) {
        hapticNotification('error')
        toast(err?.message || 'Не удалось добавить треки', 'error')
      }
    },
    [applyDetail, playlistId, reload, toast],
  )

  // Подсказки для скринридера при перетаскивании.
  const announcements = useMemo(
    () => ({
      onDragStart({ active }) {
        const position = tracksRef.current.findIndex((track) => track.id === active.id) + 1
        return `Трек на позиции ${position} захвачен. Перемещайте стрелками, отпустите пробелом.`
      },
      onDragOver({ active, over }) {
        if (!over) return 'Трек вне списка.'
        const position = tracksRef.current.findIndex((track) => track.id === over.id) + 1
        return `Трек ${active.id} над позицией ${position}.`
      },
      onDragEnd({ over }) {
        if (!over) return 'Перемещение отменено.'
        const position = tracksRef.current.findIndex((track) => track.id === over.id) + 1
        return `Трек перемещён на позицию ${position}.`
      },
      onDragCancel() {
        return 'Перемещение отменено, порядок не изменился.'
      },
    }),
    [],
  )

  if (!validId) {
    return (
      <div className="page">
        <EmptyState
          icon="🤷"
          title="Плейлист не найден"
          description="Похоже, ссылка устарела."
          action={
            <button type="button" className="btn btn--primary" onClick={() => navigate('/playlists')}>
              К списку плейлистов
            </button>
          }
        />
      </div>
    )
  }

  if (loading && !data) {
    return (
      <div className="page">
        <Loader label="Загружаем плейлист…" />
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="page">
        <EmptyState
          icon="⚠️"
          title="Не удалось открыть плейлист"
          description={error.message || 'Проверьте подключение и попробуйте ещё раз.'}
          action={
            <button type="button" className="btn btn--primary" onClick={reload}>
              Повторить
            </button>
          }
        />
      </div>
    )
  }

  const playlist = data || {}
  const durationLabel = formatTotalDuration(totalDuration)
  const meta = durationLabel
    ? `${formatCount(tracks.length, ['трек', 'трека', 'треков'])} · ${durationLabel}`
    : formatCount(tracks.length, ['трек', 'трека', 'треков'])

  return (
    <div className="page">
      <div className="section__header">
        <h1 className="section__title">{playlist.name || 'Плейлист'}</h1>
        <button type="button" className="btn btn--ghost" onClick={() => navigate('/playlists')}>
          ⬅️ Плейлисты
        </button>
      </div>

      {playlist.description ? <p className="muted">{playlist.description}</p> : null}
      <p className="muted">{meta}</p>

      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
        <button
          type="button"
          className="btn btn--primary"
          onClick={handlePlayAll}
          disabled={tracks.length === 0}
        >
          ▶️ Проиграть всё
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => {
            hapticImpact('light')
            setPickerOpen(true)
          }}
        >
          ➕ Добавить треки
        </button>
      </div>

      {savingOrder ? <p className="muted">Сохраняем новый порядок…</p> : null}

      {tracks.length === 0 ? (
        <EmptyState
          icon="🎧"
          title="В плейлисте пока пусто"
          description="Добавьте треки из библиотеки — потом их можно будет перетаскивать за ручку «⠿»."
          action={
            <button type="button" className="btn btn--primary" onClick={() => setPickerOpen(true)}>
              ➕ Добавить треки
            </button>
          }
        />
      ) : (
        <>
          <p className="muted" style={{ fontSize: 13 }}>
            Удерживайте ручку «⠿» и перетащите трек, чтобы изменить порядок.
          </p>
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            modifiers={[restrictToVerticalAxis, restrictToParentElement]}
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
            onDragCancel={handleDragCancel}
            accessibility={{ announcements }}
          >
            <SortableContext items={trackIds} strategy={verticalListSortingStrategy}>
              <div className="list">
                {tracks.map((track, index) => (
                  <SortableTrack
                    key={track.id}
                    track={track}
                    index={index}
                    queue={tracks}
                    disabled={savingOrder}
                    onRemove={handleRemove}
                    onChanged={handleTrackChanged}
                  />
                ))}
              </div>
            </SortableContext>
          </DndContext>
        </>
      )}

      <AddTracksModal
        open={pickerOpen}
        existingIds={existingIds}
        onClose={() => setPickerOpen(false)}
        onSubmit={handleAddTracks}
      />
    </div>
  )
}

export { PlaylistPage }
