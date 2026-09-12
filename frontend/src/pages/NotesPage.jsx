/**
 * Страница «Заметки» (маршрут `/notes`, контракт V2 раздел 6, ТЗ п. 5).
 *
 * Возможности:
 *  - список заметок в порядке `GET /notes` (сначала недавно изменённые);
 *  - прогресс «сделано / всего» полоской (.note-progress) и подписью «3 из 7»;
 *  - создание заметки (Modal, Enter в поле — сразу сохранить);
 *  - переименование (то же окно в режиме редактирования);
 *  - удаление с подтверждением (ConfirmDialog);
 *  - переход к пунктам заметки (`/notes/:id`).
 *
 * Данные приходят из `GET /notes` (схема NoteOut роутера backend/api/routers/notes.py):
 * { id, title, items_total, items_done, created_at, updated_at }.
 * Плеер здесь не используется: заметки не зависят от прослушиваний, поэтому
 * playVersion в зависимостях загрузки не нужен.
 */

import { useCallback, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '../api/client.js'
import ConfirmDialog from '../components/ConfirmDialog.jsx'
import EmptyState from '../components/EmptyState.jsx'
import Loader from '../components/Loader.jsx'
import Modal from '../components/Modal.jsx'
import { useToast } from '../context/ToastContext.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { hapticImpact, hapticNotification } from '../telegram.js'
import { formatCount, formatDateTime } from '../utils/format.js'

/** Ограничение backend (`notes_repo.MAX_TITLE_LENGTH`). */
const MAX_TITLE_LENGTH = 200

/** Формы слова «пункт» для formatCount. */
const ITEM_FORMS = ['пункт', 'пункта', 'пунктов']

/* --------------------------------------------------------------------------
   Инлайновые стили-фолбэки.
   Классы note-progress* пока не описаны в styles.css (см. risks). Пока их нет,
   полоска прогресса рисуется этими объектами; после появления правил в
   styles.css инлайновые стили из файла можно убрать.
   -------------------------------------------------------------------------- */

const progressStyle = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  width: '100%',
  minWidth: 0,
  marginTop: 2,
}

const progressBarStyle = {
  display: 'block',
  flex: '1 1 auto',
  minWidth: 40,
  height: 6,
  borderRadius: 'var(--radius-pill)',
  background: 'var(--secondary-bg)',
  overflow: 'hidden',
}

const progressFillStyle = {
  display: 'block',
  height: '100%',
  borderRadius: 'var(--radius-pill)',
  transition: 'width var(--transition)',
}

const progressLabelStyle = {
  flex: 'none',
  fontSize: 13,
  fontVariantNumeric: 'tabular-nums',
}

/** Кликабельное «тело» строки: колонка с названием, прогрессом и датой. */
const openButtonStyle = {
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

const metaStyle = { fontSize: 13, maxWidth: '100%' }

/**
 * Полоска прогресса заметки: «сделано / всего».
 * Свёрстана на <span>, потому что рендерится внутри <button> строки списка.
 */
export function NoteProgress({ done, total }) {
  const totalCount = Math.max(0, Math.floor(Number(total) || 0))
  const doneCount = Math.min(totalCount, Math.max(0, Math.floor(Number(done) || 0)))
  const percent = totalCount > 0 ? Math.round((doneCount / totalCount) * 100) : 0
  const complete = totalCount > 0 && doneCount === totalCount

  const label = totalCount === 0 ? 'пунктов пока нет' : `${doneCount} из ${totalCount}`

  return (
    <span
      className={`note-progress${complete ? ' note-progress--complete' : ''}`}
      style={progressStyle}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
      aria-label={
        totalCount === 0 ? 'Пунктов пока нет' : `Выполнено ${doneCount} из ${totalCount}`
      }
    >
      <span className="note-progress__bar" style={progressBarStyle}>
        <span
          className="note-progress__fill"
          style={{
            ...progressFillStyle,
            width: `${percent}%`,
            background: complete ? 'var(--success)' : 'var(--link)',
          }}
        />
      </span>
      <span className="note-progress__label muted" style={progressLabelStyle}>
        {complete ? `${label} ✅` : label}
      </span>
    </span>
  )
}

export default function NotesPage() {
  const navigate = useNavigate()
  const { toast } = useToast()

  const { data, error, loading, reload } = useAsync(() => api.notes.list(), [])

  // Окно создания/переименования: mode = 'create' | 'edit'.
  const [editor, setEditor] = useState(null)
  const [title, setTitle] = useState('')
  const [saving, setSaving] = useState(false)

  // Заметка, для которой запрошено удаление.
  const [pendingDelete, setPendingDelete] = useState(null)
  const [deleting, setDeleting] = useState(false)

  // Порядок с сервера (недавно изменённые сверху) сохраняем как есть.
  const notes = useMemo(() => (Array.isArray(data) ? data : []), [data])

  const openCreate = useCallback(() => {
    hapticImpact('light')
    setTitle('')
    setEditor({ mode: 'create', note: null })
  }, [])

  const openEdit = useCallback((note) => {
    hapticImpact('light')
    setTitle(note?.title || '')
    setEditor({ mode: 'edit', note })
  }, [])

  const closeEditor = useCallback(() => {
    if (saving) return
    setEditor(null)
  }, [saving])

  const handleSubmit = useCallback(async () => {
    if (saving || !editor) return
    const value = title.trim()
    if (!value) {
      toast('Введите название заметки', 'error')
      return
    }
    setSaving(true)
    try {
      if (editor.mode === 'create') {
        const created = await api.notes.create(value)
        hapticNotification('success')
        toast(`Заметка «${value}» создана`, 'success')
        setEditor(null)
        await reload()
        // Сразу открываем новую заметку — пункты добавляют внутри неё.
        if (created?.id) navigate(`/notes/${created.id}`)
        return
      }
      await api.notes.update(editor.note.id, value)
      hapticNotification('success')
      toast('Заметка переименована', 'success')
      setEditor(null)
      await reload()
    } catch (err) {
      hapticNotification('error')
      toast(err?.message || 'Не удалось сохранить заметку', 'error')
    } finally {
      setSaving(false)
    }
  }, [editor, navigate, reload, saving, title, toast])

  const handleDelete = useCallback(async () => {
    if (!pendingDelete || deleting) return
    setDeleting(true)
    try {
      await api.notes.remove(pendingDelete.id)
      hapticNotification('success')
      toast(`Заметка «${pendingDelete.title}» удалена`, 'success')
      setPendingDelete(null)
      await reload()
    } catch (err) {
      hapticNotification('error')
      toast(err?.message || 'Не удалось удалить заметку', 'error')
    } finally {
      setDeleting(false)
    }
  }, [deleting, pendingDelete, reload, toast])

  // Enter в поле названия сразу сохраняет.
  const handleTitleKeyDown = (event) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      handleSubmit()
    }
  }

  const editorFooter = (
    <>
      <button type="button" className="btn" onClick={closeEditor} disabled={saving}>
        Отмена
      </button>
      <button type="button" className="btn btn--primary" onClick={handleSubmit} disabled={saving}>
        {saving ? 'Сохранение…' : editor?.mode === 'edit' ? 'Сохранить' : 'Создать'}
      </button>
    </>
  )

  return (
    <div className="page">
      <div className="section__header">
        <h1 className="section__title">📝 Заметки</h1>
        <button type="button" className="btn btn--primary" onClick={openCreate}>
          ➕ Новая
        </button>
      </div>

      {loading ? <Loader label="Загружаем заметки…" /> : null}

      {!loading && error ? (
        <EmptyState
          icon="⚠️"
          title="Не удалось загрузить заметки"
          description={error.message || 'Проверьте подключение и попробуйте ещё раз.'}
          action={
            <button type="button" className="btn btn--primary" onClick={reload}>
              Повторить
            </button>
          }
        />
      ) : null}

      {!loading && !error && notes.length === 0 ? (
        <EmptyState
          icon="📝"
          title="Заметок пока нет"
          description="Создайте первый список дел: пункты отмечаются галочками, а порядок меняется перетаскиванием."
          action={
            <button type="button" className="btn btn--primary" onClick={openCreate}>
              ➕ Создать заметку
            </button>
          }
        />
      ) : null}

      {!loading && !error && notes.length > 0 ? (
        <div className="list">
          {notes.map((note) => {
            const total = Number(note.items_total) || 0
            const done = Number(note.items_done) || 0
            const updated = formatDateTime(note.updated_at)
            return (
              <div className="list-item" key={note.id}>
                <button
                  type="button"
                  style={openButtonStyle}
                  onClick={() => {
                    hapticImpact('light')
                    navigate(`/notes/${note.id}`)
                  }}
                  aria-label={`Открыть заметку ${note.title}`}
                >
                  <span className="list-item__title" style={{ width: '100%' }}>
                    {note.title || 'Без названия'}
                  </span>
                  <NoteProgress done={done} total={total} />
                  <span className="muted text-ellipsis" style={metaStyle}>
                    {formatCount(total, ITEM_FORMS)}
                    {updated && updated !== '—' ? ` · изменена ${updated}` : ''}
                  </span>
                </button>

                <button
                  type="button"
                  className="btn-icon"
                  title="Переименовать"
                  aria-label={`Переименовать заметку ${note.title}`}
                  onClick={() => openEdit(note)}
                >
                  ✏️
                </button>
                <button
                  type="button"
                  className="btn-icon"
                  title="Удалить"
                  aria-label={`Удалить заметку ${note.title}`}
                  onClick={() => {
                    hapticImpact('light')
                    setPendingDelete(note)
                  }}
                >
                  🗑
                </button>
              </div>
            )
          })}
        </div>
      ) : null}

      <Modal
        open={Boolean(editor)}
        title={editor?.mode === 'edit' ? 'Переименовать заметку' : 'Новая заметка'}
        onClose={closeEditor}
        footer={editorFooter}
      >
        <div className="field">
          <label className="field__label" htmlFor="note-title">
            Название
          </label>
          <input
            id="note-title"
            className="input"
            type="text"
            value={title}
            maxLength={MAX_TITLE_LENGTH}
            placeholder="Например: Что послушать"
            onChange={(event) => setTitle(event.target.value)}
            onKeyDown={handleTitleKeyDown}
            autoComplete="off"
          />
          <span className="field__hint">
            Пункты добавляются внутри заметки — их можно отмечать галочками и менять
            местами.
          </span>
        </div>
      </Modal>

      <ConfirmDialog
        open={Boolean(pendingDelete)}
        title="Удалить заметку?"
        message={
          pendingDelete
            ? `Заметка «${pendingDelete.title}» и все её пункты (${formatCount(
                Number(pendingDelete.items_total) || 0,
                ITEM_FORMS,
              )}) будут удалены. Действие нельзя отменить.`
            : ''
        }
        confirmText={deleting ? 'Удаление…' : 'Удалить'}
        cancelText="Отмена"
        onConfirm={handleDelete}
        onCancel={() => {
          if (!deleting) setPendingDelete(null)
        }}
      />
    </div>
  )
}

export { NotesPage }
