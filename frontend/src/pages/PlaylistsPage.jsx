/**
 * Страница «Плейлисты» (маршрут `/playlists`).
 *
 * Возможности:
 *  - список плейлистов по алфавиту с количеством треков и общей длительностью;
 *  - создание плейлиста (модальное окно с полями «Название» и «Описание»);
 *  - переименование (то же окно в режиме редактирования);
 *  - удаление с подтверждением (ConfirmDialog);
 *  - переход к составу плейлиста (`/playlists/:id`).
 *
 * Данные приходят из `GET /playlists` (схема PlaylistOut):
 * { id, name, description, track_count, total_duration, created_at, updated_at }.
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
import { formatCount } from '../utils/format.js'

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

/** Алфавитная сортировка с учётом кириллицы (регистр не важен). */
function sortByName(items) {
  return [...items].sort((a, b) =>
    String(a?.name || '').localeCompare(String(b?.name || ''), 'ru', { sensitivity: 'base' }),
  )
}

/** Стили, которых нет в styles.css: строка плейлиста должна быть кликабельной колонкой. */
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

export default function PlaylistsPage() {
  const navigate = useNavigate()
  const { toast } = useToast()

  const { data, error, loading, reload } = useAsync(() => api.playlists.list(), [])

  // Окно создания/переименования: mode = 'create' | 'edit'.
  const [editor, setEditor] = useState(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [saving, setSaving] = useState(false)

  // Плейлист, для которого запрошено удаление.
  const [pendingDelete, setPendingDelete] = useState(null)
  const [deleting, setDeleting] = useState(false)

  const playlists = useMemo(() => sortByName(Array.isArray(data) ? data : []), [data])

  const openCreate = useCallback(() => {
    hapticImpact('light')
    setName('')
    setDescription('')
    setEditor({ mode: 'create', playlist: null })
  }, [])

  const openEdit = useCallback((playlist) => {
    hapticImpact('light')
    setName(playlist?.name || '')
    setDescription(playlist?.description || '')
    setEditor({ mode: 'edit', playlist })
  }, [])

  const closeEditor = useCallback(() => {
    if (saving) return
    setEditor(null)
  }, [saving])

  const handleSubmit = useCallback(async () => {
    if (saving || !editor) return
    const title = name.trim()
    const note = description.trim()
    if (!title) {
      toast('Введите название плейлиста', 'error')
      return
    }
    setSaving(true)
    try {
      if (editor.mode === 'create') {
        await api.playlists.create(title, note || null)
        toast(`Плейлист «${title}» создан`, 'success')
      } else {
        await api.playlists.update(editor.playlist.id, { name: title, description: note || null })
        toast('Плейлист обновлён', 'success')
      }
      hapticNotification('success')
      setEditor(null)
      await reload()
    } catch (err) {
      hapticNotification('error')
      toast(err?.message || 'Не удалось сохранить плейлист', 'error')
    } finally {
      setSaving(false)
    }
  }, [description, editor, name, reload, saving, toast])

  const handleDelete = useCallback(async () => {
    if (!pendingDelete || deleting) return
    setDeleting(true)
    try {
      await api.playlists.remove(pendingDelete.id)
      hapticNotification('success')
      toast(`Плейлист «${pendingDelete.name}» удалён`, 'success')
      setPendingDelete(null)
      await reload()
    } catch (err) {
      hapticNotification('error')
      toast(err?.message || 'Не удалось удалить плейлист', 'error')
    } finally {
      setDeleting(false)
    }
  }, [deleting, pendingDelete, reload, toast])

  // Enter в поле «Название» сразу сохраняет.
  const handleNameKeyDown = (event) => {
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
        <h1 className="section__title">🎼 Плейлисты</h1>
        <button type="button" className="btn btn--primary" onClick={openCreate}>
          ➕ Новый
        </button>
      </div>

      {loading ? <Loader label="Загружаем плейлисты…" /> : null}

      {!loading && error ? (
        <EmptyState
          icon="⚠️"
          title="Не удалось загрузить плейлисты"
          description={error.message || 'Проверьте подключение и попробуйте ещё раз.'}
          action={
            <button type="button" className="btn btn--primary" onClick={reload}>
              Повторить
            </button>
          }
        />
      ) : null}

      {!loading && !error && playlists.length === 0 ? (
        <EmptyState
          icon="🎼"
          title="Плейлистов пока нет"
          description="Создайте первый плейлист и соберите в нём любимые треки — порядок можно менять перетаскиванием."
          action={
            <button type="button" className="btn btn--primary" onClick={openCreate}>
              ➕ Создать плейлист
            </button>
          }
        />
      ) : null}

      {!loading && !error && playlists.length > 0 ? (
        <div className="list">
          {playlists.map((playlist) => {
            const count = Number(playlist.track_count) || 0
            const durationLabel = formatTotalDuration(playlist.total_duration)
            const meta = durationLabel
              ? `${formatCount(count, ['трек', 'трека', 'треков'])} · ${durationLabel}`
              : formatCount(count, ['трек', 'трека', 'треков'])
            return (
              <div className="list-item" key={playlist.id}>
                <button
                  type="button"
                  style={openButtonStyle}
                  onClick={() => {
                    hapticImpact('light')
                    navigate(`/playlists/${playlist.id}`)
                  }}
                  aria-label={`Открыть плейлист ${playlist.name}`}
                >
                  <span className="list-item__title" style={{ width: '100%' }}>
                    {playlist.name}
                  </span>
                  <span className="muted text-ellipsis" style={metaStyle}>
                    {meta}
                  </span>
                  {playlist.description ? (
                    <span className="muted text-ellipsis" style={metaStyle}>
                      {playlist.description}
                    </span>
                  ) : null}
                </button>

                <button
                  type="button"
                  className="btn-icon"
                  title="Переименовать"
                  aria-label={`Переименовать плейлист ${playlist.name}`}
                  onClick={() => openEdit(playlist)}
                >
                  ✏️
                </button>
                <button
                  type="button"
                  className="btn-icon"
                  title="Удалить"
                  aria-label={`Удалить плейлист ${playlist.name}`}
                  onClick={() => {
                    hapticImpact('light')
                    setPendingDelete(playlist)
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
        title={editor?.mode === 'edit' ? 'Переименовать плейлист' : 'Новый плейлист'}
        onClose={closeEditor}
        footer={editorFooter}
      >
        <div className="field">
          <label className="field__label" htmlFor="playlist-name">
            Название
          </label>
          <input
            id="playlist-name"
            className="input"
            type="text"
            value={name}
            maxLength={80}
            placeholder="Например: Для пробежки"
            onChange={(event) => setName(event.target.value)}
            onKeyDown={handleNameKeyDown}
            autoComplete="off"
          />
        </div>

        <div className="field" style={{ marginTop: 12 }}>
          <label className="field__label" htmlFor="playlist-description">
            Описание
          </label>
          <textarea
            id="playlist-description"
            className="input"
            rows={3}
            value={description}
            maxLength={300}
            placeholder="Необязательно"
            onChange={(event) => setDescription(event.target.value)}
          />
          <span className="field__hint">Описание видно только вам — можно оставить пустым.</span>
        </div>
      </Modal>

      <ConfirmDialog
        open={Boolean(pendingDelete)}
        title="Удалить плейлист?"
        message={
          pendingDelete
            ? `Плейлист «${pendingDelete.name}» будет удалён. Сами треки останутся в библиотеке.`
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

export { PlaylistsPage }
