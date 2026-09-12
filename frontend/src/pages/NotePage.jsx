/**
 * Страница пунктов заметки (маршрут `/notes/:id`, контракт V2 раздел 6, ТЗ п. 5).
 *
 * Возможности:
 *  - чекбокс у каждого пункта: клик -> `PATCH /notes/{id}/items/{item_id}` c
 *    `{is_done}`; состояние меняется оптимистично, при ошибке — откат и тост;
 *  - добавление пункта (поле + Enter или кнопка «Добавить»);
 *  - редактирование текста пункта прямо в строке (Enter — сохранить, Esc — отменить);
 *  - удаление пункта (оптимистично, с откатом при ошибке);
 *  - изменение порядка перетаскиванием (@dnd-kit, как в PlaylistPage.jsx):
 *    DndContext c PointerSensor {distance: 5} + TouchSensor {delay: 200, tolerance: 5},
 *    SortableContext + verticalListSortingStrategy, listeners только на ручке «⠿».
 *    После onDragEnd порядок применяется оптимистично (arrayMove) и уходит в
 *    `PUT /notes/{id}/order` ПОЛНЫМ списком уникальных id (дубликаты сервер
 *    отвергает с 422, неполный список — с 400); при ошибке порядок откатывается,
 *    показывается тост, а заметка перечитывается с сервера.
 *
 * Все изменяющие маршруты backend возвращают заметку целиком (NoteDetailOut),
 * поэтому ответ просто заменяет состояние страницы. Ответ НЕ применяется, пока
 * идёт другая операция (перетаскивание, сохранение порядка, параллельный PATCH),
 * иначе он затёр бы более свежее оптимистичное состояние. Отброшенный ответ
 * помечает состояние устаревшим — заметка перечитывается, как только страница
 * освободится (см. resync).
 *
 * Откат оптимистичного изменения точечный: возвращается только своё поле (или
 * своя строка), а не снимок всего списка, иначе откат затёр бы параллельные
 * изменения. Ошибки расхождения с сервером (400/404/409/422 — пункт уже удалён
 * или изменён из бота) дополнительно перечитывают заметку, иначе страница
 * осталась бы с «призрачной» строкой навсегда.
 *
 * Плеер здесь не используется: заметки не зависят от прослушиваний, поэтому
 * playVersion в зависимостях загрузки не нужен.
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
import { useToast } from '../context/ToastContext.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { hapticImpact, hapticNotification, hideBackButton, showBackButton } from '../telegram.js'
import { formatCount } from '../utils/format.js'
import { NoteProgress } from './NotesPage.jsx'

/** Ограничения backend (`notes_repo`). */
const MAX_TITLE_LENGTH = 200
const MAX_ITEM_TEXT_LENGTH = 1000

/** Формы слова «пункт» для formatCount. */
const ITEM_FORMS = ['пункт', 'пункта', 'пунктов']

/**
 * Статусы, означающие расхождение состояния страницы с сервером: пункт уже
 * удалён или изменён (например, из бота), состав списка не совпал. Локальный
 * откат такие случаи не лечит, поэтому заметка перечитывается целиком.
 */
const RESYNC_STATUSES = new Set([400, 404, 409, 422])

/* --------------------------------------------------------------------------
   Инлайновые стили-фолбэки: классы note-item* ещё не описаны в styles.css
   (см. risks). Пока правил нет, строка пункта выглядит правильно за счёт этих
   объектов; после появления стилей инлайновые значения можно убрать.
   -------------------------------------------------------------------------- */

/** Текст пункта переносится по строкам (в отличие от .list-item__title). */
const itemTextStyle = {
  flex: '1 1 auto',
  minWidth: 0,
  padding: '2px 0',
  border: 0,
  background: 'transparent',
  color: 'inherit',
  font: 'inherit',
  fontSize: 15,
  lineHeight: 1.35,
  textAlign: 'left',
  whiteSpace: 'normal',
  overflowWrap: 'anywhere',
  cursor: 'pointer',
}

const itemTextDoneStyle = {
  color: 'var(--hint)',
  textDecoration: 'line-through',
}

/** Обёртка чекбокса: крупная тач-цель (не меньше 44px по требованию стилей). */
const checkWrapStyle = {
  flex: 'none',
  display: 'grid',
  placeItems: 'center',
  width: 40,
  minWidth: 40,
  minHeight: 'var(--tap)',
  cursor: 'pointer',
}

const checkboxStyle = { width: 18, height: 18, margin: 0, cursor: 'pointer' }

/**
 * Один пункт заметки: ручка «⠿», чекбокс, текст (или поле редактирования)
 * и кнопки «Изменить»/«Удалить».
 *
 * attributes/listeners навешаны ТОЛЬКО на ручку — клик по тексту начинает
 * редактирование, клик по чекбоксу переключает отметку.
 *
 * `disabled` гасит строку целиком, `dragDisabled` — только перетаскивание
 * (пока летят изменения пунктов, состав списка на клиенте ещё не совпадает с
 * серверным, и `PUT /notes/{id}/order` вернул бы 400).
 */
function SortableNoteItem({
  item,
  disabled,
  dragDisabled,
  editing,
  draft,
  onDraftChange,
  onStartEdit,
  onSubmitEdit,
  onCancelEdit,
  onToggle,
  onRemove,
}) {
  const dragLocked = disabled || dragDisabled || editing
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: item.id,
    disabled: dragLocked,
  })

  const style = {
    transform: CSS.Transform.toString(transform),
    transition: transition || undefined,
  }

  // Гасим всплытие, иначе нажатие на ручку долетит до строки.
  const handleHandleClick = (event) => {
    event.stopPropagation()
  }

  const handleHandleKeyDown = (event) => {
    event.stopPropagation()
    if (typeof listeners?.onKeyDown === 'function') listeners.onKeyDown(event)
  }

  const handleEditKeyDown = (event) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      onSubmitEdit(item)
      return
    }
    if (event.key === 'Escape' || event.key === 'Esc') {
      event.preventDefault()
      event.stopPropagation()
      onCancelEdit()
    }
  }

  const text = item.text || 'Без текста'

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`sortable-item${isDragging ? ' sortable-item--dragging' : ''}`}
    >
      <div className={`list-item note-item${item.is_done ? ' note-item--done' : ''}`}>
        <button
          type="button"
          className="drag-handle"
          title="Перетащите, чтобы изменить порядок"
          aria-label={`Изменить положение пункта «${text}»`}
          disabled={dragLocked}
          {...attributes}
          {...listeners}
          onClick={handleHandleClick}
          onKeyDown={handleHandleKeyDown}
        >
          ⠿
        </button>

        <label className="list-item__check note-item__check" style={checkWrapStyle}>
          <input
            type="checkbox"
            style={checkboxStyle}
            checked={Boolean(item.is_done)}
            disabled={disabled}
            onChange={() => onToggle(item)}
            aria-label={
              item.is_done ? `Снять отметку с «${text}»` : `Отметить «${text}» выполненным`
            }
          />
        </label>

        {editing ? (
          <>
            <input
              className="input"
              type="text"
              style={{ flex: '1 1 auto', minWidth: 0 }}
              value={draft}
              maxLength={MAX_ITEM_TEXT_LENGTH}
              placeholder="Текст пункта"
              autoComplete="off"
              autoFocus
              onChange={(event) => onDraftChange(event.target.value)}
              onKeyDown={handleEditKeyDown}
              aria-label="Текст пункта"
            />
            <button
              type="button"
              className="btn-icon"
              title="Сохранить"
              aria-label="Сохранить текст пункта"
              onClick={() => onSubmitEdit(item)}
            >
              ✔️
            </button>
            <button
              type="button"
              className="btn-icon"
              title="Отменить"
              aria-label="Отменить редактирование"
              onClick={onCancelEdit}
            >
              ✖️
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className={`note-item__text${item.is_done ? ' note-item__text--done' : ''}`}
              style={item.is_done ? { ...itemTextStyle, ...itemTextDoneStyle } : itemTextStyle}
              onClick={() => onStartEdit(item)}
              title="Нажмите, чтобы изменить текст"
            >
              {text}
            </button>
            <button
              type="button"
              className="btn-icon"
              title="Изменить текст"
              aria-label={`Изменить текст пункта «${text}»`}
              disabled={disabled}
              onClick={() => onStartEdit(item)}
            >
              ✏️
            </button>
            <button
              type="button"
              className="btn-icon"
              title="Удалить пункт"
              aria-label={`Удалить пункт «${text}»`}
              disabled={disabled}
              onClick={() => onRemove(item)}
            >
              🗑
            </button>
          </>
        )}
      </div>
    </div>
  )
}

/**
 * Приводит пункты с сервера к безопасному виду: только корректные id,
 * без повторов (повторы сломали бы SortableContext и вызвали бы 422 на
 * `PUT /notes/{id}/order`).
 */
function normalizeItems(list) {
  const seen = new Set()
  const result = []
  for (const raw of Array.isArray(list) ? list : []) {
    const id = Number(raw?.id)
    if (!Number.isInteger(id) || id <= 0 || seen.has(id)) continue
    seen.add(id)
    result.push({
      ...raw,
      id,
      text: String(raw?.text ?? ''),
      is_done: Boolean(raw?.is_done),
    })
  }
  return result
}

export default function NotePage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const { toast } = useToast()

  const noteId = Number(id)
  const validId = Number.isFinite(noteId) && noteId > 0

  const { data, error, loading, reload, setData } = useAsync(() => {
    if (!validId) throw new Error('Заметка не найдена')
    return api.notes.get(noteId)
  }, [noteId])

  // Локальная копия пунктов: нужна для оптимистичных изменений и отката.
  const [items, setItems] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [savingOrder, setSavingOrder] = useState(false)

  // Поле «новый пункт».
  const [newText, setNewText] = useState('')
  const [adding, setAdding] = useState(false)
  const newInputRef = useRef(null)

  // Редактирование текста пункта.
  const [editingId, setEditingId] = useState(null)
  const [draft, setDraft] = useState('')

  // Переименование заметки.
  const [renameOpen, setRenameOpen] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const [renaming, setRenaming] = useState(false)

  // Счётчики незавершённых операций — ответы сервера не применяем, пока занято.
  const pendingRef = useRef(0)
  // Та же величина в state: нужна для рендера (блокировка перетаскивания).
  const [pendingCount, setPendingCount] = useState(0)
  const draggingRef = useRef(false)
  const savingOrderRef = useRef(false)
  // Состояние страницы разошлось с сервером — нужен перечит, как только освободимся.
  const staleRef = useRef(false)
  const itemsRef = useRef([])
  itemsRef.current = items

  const isBusy = useCallback(
    () => pendingRef.current > 0 || draggingRef.current || savingOrderRef.current,
    [],
  )

  /** Меняет счётчик незавершённых операций (ref — для логики, state — для рендера). */
  const bumpPending = useCallback((delta) => {
    pendingRef.current = Math.max(0, pendingRef.current + delta)
    setPendingCount(pendingRef.current)
  }, [])

  /**
   * Помечает состояние устаревшим и перечитывает заметку. Если страница ещё
   * занята, перечит откладывается: его подхватит следующая операция (flushResync).
   */
  const resync = useCallback(async () => {
    staleRef.current = true
    if (isBusy()) return
    staleRef.current = false
    await reload()
  }, [isBusy, reload])

  /** Догоняет отложенный перечит, если он был запрошен во время занятости. */
  const flushResync = useCallback(() => {
    if (staleRef.current) resync()
  }, [resync])

  useEffect(() => {
    if (!data) return
    setItems(normalizeItems(data.items))
  }, [data])

  // Системная кнопка «Назад» Telegram возвращает к списку заметок.
  useEffect(() => {
    showBackButton(() => navigate('/notes'))
    return () => hideBackButton()
  }, [navigate])

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 200, tolerance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  const itemIds = useMemo(() => items.map((item) => item.id), [items])
  const doneCount = useMemo(() => items.filter((item) => item.is_done).length, [items])

  /** Заменяет состояние заметки ответом сервера (NoteDetailOut). */
  const applyDetail = useCallback(
    (detail) => {
      if (!detail || !Array.isArray(detail.items)) return false
      setData(detail)
      return true
    },
    [setData],
  )

  /**
   * Выполняет изменяющий запрос: ответ применяется, только если к этому моменту
   * не идёт других операций; при ошибке вызывается точечный rollback и тост.
   *
   * Отброшенный (из-за занятости) ответ и ошибки расхождения с сервером
   * помечают состояние устаревшим — заметка перечитывается, как только можно.
   */
  const mutate = useCallback(
    async (request, { rollback = null, errorText = 'Не удалось сохранить изменения' } = {}) => {
      bumpPending(1)
      try {
        const detail = await request()
        bumpPending(-1)
        if (isBusy()) {
          // Ответ применить нельзя (идёт другая операция) — он содержал бы
          // изменения, о которых страница так и не узнает.
          staleRef.current = true
        } else if (!applyDetail(detail)) {
          staleRef.current = false
          await reload()
        } else {
          // NoteDetailOut — полный свежий снимок заметки, расхождений больше нет.
          staleRef.current = false
        }
        return true
      } catch (err) {
        bumpPending(-1)
        if (typeof rollback === 'function') rollback()
        hapticNotification('error')
        toast(err?.message || errorText, 'error')
        // Откат возвращает лишь своё поле; расхождение с сервером (пункт удалён
        // из бота и т.п.) лечится только перечитом заметки.
        if (staleRef.current || RESYNC_STATUSES.has(err?.status)) await resync()
        return false
      }
    },
    [applyDetail, bumpPending, isBusy, reload, resync, toast],
  )

  // --- Пункты -------------------------------------------------------------

  const handleToggle = useCallback(
    (item) => {
      const prevDone = Boolean(item.is_done)
      const nextDone = !prevDone
      hapticImpact('light')
      // Оптимистично: галочка встаёт сразу, не дожидаясь ответа сервера.
      setItems((rows) =>
        rows.map((row) => (row.id === item.id ? { ...row, is_done: nextDone } : row)),
      )
      mutate(() => api.notes.updateItem(noteId, item.id, nextDone), {
        // Откатываем ТОЛЬКО свою отметку: снимок всего списка стёр бы чужие изменения.
        rollback: () =>
          setItems((rows) =>
            rows.map((row) => (row.id === item.id ? { ...row, is_done: prevDone } : row)),
          ),
        errorText: 'Не удалось изменить отметку пункта',
      })
    },
    [mutate, noteId],
  )

  const handleAdd = useCallback(async () => {
    if (adding) return
    const text = newText.trim()
    if (!text) {
      toast('Введите текст пункта', 'error')
      return
    }
    setAdding(true)
    const ok = await mutate(() => api.notes.addItem(noteId, text), {
      errorText: 'Не удалось добавить пункт',
    })
    setAdding(false)
    if (ok) {
      setNewText('')
      hapticNotification('success')
      // Возвращаем фокус в поле — удобно добавлять пункты подряд.
      newInputRef.current?.focus()
    }
  }, [adding, mutate, newText, noteId, toast])

  const handleStartEdit = useCallback((item) => {
    hapticImpact('light')
    setEditingId(item.id)
    setDraft(item.text || '')
  }, [])

  const handleCancelEdit = useCallback(() => {
    setEditingId(null)
    setDraft('')
  }, [])

  const handleSubmitEdit = useCallback(
    (item) => {
      const text = draft.trim()
      if (!text) {
        toast('Текст пункта не может быть пустым', 'error')
        return
      }
      if (text === (item.text || '')) {
        handleCancelEdit()
        return
      }
      const prevText = item.text || ''
      setItems((rows) => rows.map((row) => (row.id === item.id ? { ...row, text } : row)))
      setEditingId(null)
      setDraft('')
      mutate(() => api.notes.updateItem(noteId, item.id, { text }), {
        // Откатываем ТОЛЬКО текст своего пункта, а не список целиком.
        rollback: () =>
          setItems((rows) =>
            rows.map((row) => (row.id === item.id ? { ...row, text: prevText } : row)),
          ),
        errorText: 'Не удалось сохранить текст пункта',
      })
    },
    [draft, handleCancelEdit, mutate, noteId, toast],
  )

  const handleRemove = useCallback(
    (item) => {
      const previous = itemsRef.current
      const removedIndex = previous.findIndex((row) => row.id === item.id)
      const removed = removedIndex >= 0 ? previous[removedIndex] : item
      if (editingId === item.id) handleCancelEdit()
      // Оптимистично убираем строку — список не «моргает».
      setItems((rows) => rows.filter((row) => row.id !== item.id))
      mutate(() => api.notes.removeItem(noteId, item.id), {
        // Возвращаем на место ТОЛЬКО свою строку (по прежнему индексу),
        // иначе откат воскресил бы уже удалённые параллельно пункты.
        rollback: () =>
          setItems((rows) => {
            if (rows.some((row) => row.id === removed.id)) return rows
            const restored = rows.slice()
            const at = removedIndex < 0 ? rows.length : Math.min(removedIndex, rows.length)
            restored.splice(at, 0, removed)
            return restored
          }),
        errorText: 'Не удалось удалить пункт',
      }).then((ok) => {
        if (ok) toast('Пункт удалён', 'success')
      })
    },
    [editingId, handleCancelEdit, mutate, noteId, toast],
  )

  // --- Перетаскивание -----------------------------------------------------

  const handleDragStart = useCallback((event) => {
    draggingRef.current = true
    setActiveId(event?.active?.id ?? null)
    hapticImpact('medium')
  }, [])

  const handleDragCancel = useCallback(() => {
    draggingRef.current = false
    setActiveId(null)
    flushResync()
  }, [flushResync])

  const handleDragEnd = useCallback(
    async (event) => {
      draggingRef.current = false
      setActiveId(null)
      const { active, over } = event || {}
      if (!active || !over || active.id === over.id) {
        // Порядок не менялся: PUT не уйдёт, поэтому отложенный перечит
        // (например, отброшенный ответ на добавление пункта) догоняем здесь.
        flushResync()
        return
      }

      const previous = itemsRef.current
      const oldIndex = previous.findIndex((item) => item.id === active.id)
      const newIndex = previous.findIndex((item) => item.id === over.id)
      if (oldIndex < 0 || newIndex < 0) {
        flushResync()
        return
      }

      // Оптимистично показываем новый порядок, затем сохраняем его на сервере.
      const next = arrayMove(previous, oldIndex, newIndex)
      setItems(next)
      savingOrderRef.current = true
      setSavingOrder(true)
      try {
        // Полный список уникальных id: дубликаты сервер отвергает (422),
        // неполный список — 400 «состав не совпадает».
        const ids = []
        const seen = new Set()
        for (const item of next) {
          if (seen.has(item.id)) continue
          seen.add(item.id)
          ids.push(item.id)
        }
        const detail = await api.notes.reorder(noteId, ids)
        savingOrderRef.current = false
        if (isBusy()) {
          staleRef.current = true
        } else if (!applyDetail(detail)) {
          staleRef.current = false
          await reload()
        } else {
          staleRef.current = false
        }
        hapticNotification('success')
      } catch (err) {
        savingOrderRef.current = false
        setItems(previous)
        hapticNotification('error')
        toast(err?.message || 'Не удалось сохранить новый порядок пунктов', 'error')
        // 400/404/409/422 означают расхождение с сервером — перечитываем заметку
        // (resync сам отложит перечит, если страница ещё занята).
        if (staleRef.current || RESYNC_STATUSES.has(err?.status)) await resync()
      } finally {
        savingOrderRef.current = false
        setSavingOrder(false)
      }
    },
    [applyDetail, flushResync, isBusy, noteId, reload, resync, toast],
  )

  // Подсказки для скринридера при перетаскивании.
  const announcements = useMemo(
    () => ({
      onDragStart({ active }) {
        const position = itemsRef.current.findIndex((item) => item.id === active.id) + 1
        return `Пункт на позиции ${position} захвачен. Перемещайте стрелками, отпустите пробелом.`
      },
      onDragOver({ over }) {
        if (!over) return 'Пункт вне списка.'
        const position = itemsRef.current.findIndex((item) => item.id === over.id) + 1
        return `Пункт над позицией ${position}.`
      },
      onDragEnd({ over }) {
        if (!over) return 'Перемещение отменено.'
        const position = itemsRef.current.findIndex((item) => item.id === over.id) + 1
        return `Пункт перемещён на позицию ${position}.`
      },
      onDragCancel() {
        return 'Перемещение отменено, порядок не изменился.'
      },
    }),
    [],
  )

  // --- Переименование заметки --------------------------------------------

  const handleRename = useCallback(async () => {
    if (renaming) return
    const value = renameValue.trim()
    if (!value) {
      toast('Введите название заметки', 'error')
      return
    }
    setRenaming(true)
    try {
      const detail = await api.notes.update(noteId, value)
      if (!applyDetail(detail)) await reload()
      hapticNotification('success')
      toast('Заметка переименована', 'success')
      setRenameOpen(false)
    } catch (err) {
      hapticNotification('error')
      toast(err?.message || 'Не удалось переименовать заметку', 'error')
    } finally {
      setRenaming(false)
    }
  }, [applyDetail, noteId, reload, renameValue, renaming, toast])

  // --- Экраны состояния ---------------------------------------------------

  if (!validId) {
    return (
      <div className="page">
        <EmptyState
          icon="🤷"
          title="Заметка не найдена"
          description="Похоже, ссылка устарела."
          action={
            <button type="button" className="btn btn--primary" onClick={() => navigate('/notes')}>
              К списку заметок
            </button>
          }
        />
      </div>
    )
  }

  if (loading && !data) {
    return (
      <div className="page">
        <Loader label="Загружаем заметку…" />
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="page">
        <EmptyState
          icon="⚠️"
          title="Не удалось открыть заметку"
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

  const note = data || {}
  const busy = savingOrder
  // Перетаскивание запрещаем и на время изменений пунктов: список на клиенте
  // ещё не знает про незавершённый POST/DELETE, а `PUT /notes/{id}/order`
  // требует ТОЧНОГО совпадения состава и иначе отвечает 400.
  const dragDisabled = busy || pendingCount > 0

  return (
    <div className="page">
      <div className="section__header">
        <h1 className="section__title">📝 {note.title || 'Заметка'}</h1>
        <div className="row" style={{ flex: 'none' }}>
          <button
            type="button"
            className="btn-icon"
            title="Переименовать заметку"
            aria-label="Переименовать заметку"
            onClick={() => {
              hapticImpact('light')
              setRenameValue(note.title || '')
              setRenameOpen(true)
            }}
          >
            ✏️
          </button>
          <button type="button" className="btn btn--ghost" onClick={() => navigate('/notes')}>
            ⬅️ Заметки
          </button>
        </div>
      </div>

      <NoteProgress done={doneCount} total={items.length} />
      {items.length > 0 ? (
        <p className="muted" style={{ fontSize: 13 }}>
          {`${formatCount(items.length, ITEM_FORMS)} · выполнено ${doneCount}`}
        </p>
      ) : null}

      <div className="form-row">
        <input
          ref={newInputRef}
          className="input"
          type="text"
          value={newText}
          maxLength={MAX_ITEM_TEXT_LENGTH}
          placeholder="Новый пункт…"
          autoComplete="off"
          enterKeyHint="done"
          aria-label="Новый пункт"
          onChange={(event) => setNewText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              handleAdd()
            }
          }}
        />
        <button
          type="button"
          className="btn btn--primary"
          onClick={handleAdd}
          disabled={adding || !newText.trim()}
        >
          {adding ? 'Добавляем…' : '➕ Добавить'}
        </button>
      </div>

      {savingOrder ? <p className="muted">Сохраняем новый порядок…</p> : null}

      {items.length === 0 ? (
        <EmptyState
          icon="✅"
          title="Список пуст"
          description="Добавьте первый пункт в поле выше — потом его можно будет отметить галочкой и перетащить за ручку «⠿»."
          action={
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => newInputRef.current?.focus()}
            >
              ✍️ Добавить пункт
            </button>
          }
        />
      ) : (
        <>
          <p className="muted" style={{ fontSize: 13 }}>
            Нажмите на текст, чтобы изменить пункт; удерживайте ручку «⠿», чтобы
            переставить его.
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
            <SortableContext items={itemIds} strategy={verticalListSortingStrategy}>
              <div className="list">
                {items.map((item) => (
                  <SortableNoteItem
                    key={item.id}
                    item={item}
                    disabled={busy}
                    dragDisabled={dragDisabled}
                    editing={editingId === item.id}
                    draft={draft}
                    onDraftChange={setDraft}
                    onStartEdit={handleStartEdit}
                    onSubmitEdit={handleSubmitEdit}
                    onCancelEdit={handleCancelEdit}
                    onToggle={handleToggle}
                    onRemove={handleRemove}
                  />
                ))}
              </div>
            </SortableContext>
          </DndContext>
        </>
      )}

      <Modal
        open={renameOpen}
        title="Переименовать заметку"
        onClose={() => {
          if (!renaming) setRenameOpen(false)
        }}
        footer={
          <>
            <button
              type="button"
              className="btn"
              onClick={() => setRenameOpen(false)}
              disabled={renaming}
            >
              Отмена
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={handleRename}
              disabled={renaming}
            >
              {renaming ? 'Сохранение…' : 'Сохранить'}
            </button>
          </>
        }
      >
        <div className="field">
          <label className="field__label" htmlFor="note-rename">
            Название
          </label>
          <input
            id="note-rename"
            className="input"
            type="text"
            value={renameValue}
            maxLength={MAX_TITLE_LENGTH}
            placeholder="Например: Что послушать"
            autoComplete="off"
            onChange={(event) => setRenameValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                handleRename()
              }
            }}
          />
        </div>
      </Modal>
    </div>
  )
}

export { NotePage }
