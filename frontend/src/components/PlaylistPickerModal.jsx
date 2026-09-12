import { useEffect, useMemo, useState } from 'react'

import EmptyState from './EmptyState'
import Loader from './Loader'
import Modal from './Modal'
import SearchBar from './SearchBar'
import { api } from '../api/client'
import { hapticImpact, hapticNotification } from '../telegram'

/**
 * Выбор плейлиста: список загружается сам, есть поиск по названию
 * и пункт «🆕 Новый плейлист» (если allowCreate).
 *
 * Пропсы (контракт): PlaylistPickerModal({ open, onClose, onPick, allowCreate = true })
 * onPick(playlist). После выбора окно закрывается само (вызывается onClose).
 */

const TRACK_FORMS = ['трек', 'трека', 'треков']

/** Русское склонение количества треков: 1 трек / 2 трека / 5 треков. */
function formatTrackCount(count) {
  const value = Math.abs(Number(count) || 0)
  const mod10 = value % 10
  const mod100 = value % 100
  let form = TRACK_FORMS[2]
  if (mod10 === 1 && mod100 !== 11) form = TRACK_FORMS[0]
  else if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) form = TRACK_FORMS[1]
  return `${value} ${form}`
}

/** Нормализация для поиска: регистр, «ё» и лишние пробелы не мешают. */
function normalize(value) {
  return String(value ?? '')
    .toLowerCase()
    .replace(/ё/g, 'е')
    .replace(/\s+/g, ' ')
    .trim()
}

/** Человеческий текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  return error?.detail || error?.message || fallback
}

export function PlaylistPickerModal({ open, onClose, onPick, allowCreate = true }) {
  const [playlists, setPlaylists] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [query, setQuery] = useState('')
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [saving, setSaving] = useState(false)

  // Загружаем список при каждом открытии — плейлисты могли измениться.
  useEffect(() => {
    if (!open) return undefined
    let cancelled = false
    setLoading(true)
    setError(null)
    api.playlists
      .list()
      .then((items) => {
        if (cancelled) return
        setPlaylists(Array.isArray(items) ? items : [])
      })
      .catch((err) => {
        if (cancelled) return
        setPlaylists([])
        setError(errorText(err, 'Не удалось загрузить плейлисты'))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open])

  // Сбрасываем поиск и форму создания после закрытия.
  useEffect(() => {
    if (open) return
    setQuery('')
    setCreating(false)
    setNewName('')
    setSaving(false)
    setError(null)
  }, [open])

  const filtered = useMemo(() => {
    const needle = normalize(query)
    if (!needle) return playlists
    return playlists.filter((playlist) => normalize(playlist?.name).includes(needle))
  }, [playlists, query])

  const pick = (playlist) => {
    if (!playlist) return
    hapticImpact('light')
    onPick?.(playlist)
    onClose?.()
  }

  const handleCreate = async (event) => {
    event.preventDefault()
    const name = newName.trim()
    if (!name) {
      setError('Введите название плейлиста')
      return
    }
    setSaving(true)
    setError(null)
    try {
      const playlist = await api.playlists.create(name)
      hapticNotification('success')
      setPlaylists((prev) => {
        const rest = prev.filter((item) => item.id !== playlist?.id)
        return [...rest, playlist].filter(Boolean)
      })
      setNewName('')
      setCreating(false)
      onPick?.(playlist)
      onClose?.()
    } catch (err) {
      hapticNotification('error')
      setError(errorText(err, 'Не удалось создать плейлист'))
    } finally {
      setSaving(false)
    }
  }

  const nothingFound = !loading && filtered.length === 0

  return (
    <Modal open={open} title="Выберите плейлист" onClose={onClose}>
      <SearchBar value={query} onChange={setQuery} placeholder="Название плейлиста…" />

      {error ? (
        <p className="modal__error" role="alert">
          {error}
        </p>
      ) : null}

      {loading ? (
        <Loader label="Загружаем плейлисты…" />
      ) : (
        <>
          <ul className="list">
            {allowCreate ? (
              <li>
                {creating ? (
                  <form className="list-item list-item--form" onSubmit={handleCreate}>
                    <input
                      className="input"
                      type="text"
                      value={newName}
                      onChange={(event) => setNewName(event.target.value)}
                      placeholder="Название нового плейлиста"
                      autoFocus
                      maxLength={100}
                      disabled={saving}
                      aria-label="Название нового плейлиста"
                    />
                    <div className="modal__actions">
                      <button
                        type="button"
                        className="btn"
                        onClick={() => {
                          setCreating(false)
                          setNewName('')
                          setError(null)
                        }}
                        disabled={saving}
                      >
                        Отмена
                      </button>
                      <button type="submit" className="btn btn--primary" disabled={saving || !newName.trim()}>
                        {saving ? 'Создаём…' : 'Создать'}
                      </button>
                    </div>
                  </form>
                ) : (
                  <button
                    type="button"
                    className="list-item list-item--action"
                    onClick={() => {
                      hapticImpact('light')
                      setError(null)
                      setNewName(query.trim())
                      setCreating(true)
                    }}
                  >
                    <span className="list-item__icon" aria-hidden="true">
                      🆕
                    </span>
                    <span className="list-item__title">Новый плейлист</span>
                  </button>
                )}
              </li>
            ) : null}

            {filtered.map((playlist) => (
              <li key={playlist.id}>
                <button type="button" className="list-item" onClick={() => pick(playlist)}>
                  <span className="list-item__icon" aria-hidden="true">
                    🎼
                  </span>
                  <span className="list-item__title">{playlist.name}</span>
                  <span className="list-item__meta">{formatTrackCount(playlist.track_count)}</span>
                </button>
              </li>
            ))}
          </ul>

          {nothingFound ? (
            <EmptyState
              icon="🎼"
              title={query ? 'Ничего не нашлось' : 'Плейлистов пока нет'}
              description={
                query
                  ? 'Попробуйте изменить запрос или создайте новый плейлист.'
                  : 'Создайте первый плейлист и соберите в нём любимые треки.'
              }
            />
          ) : null}
        </>
      )}
    </Modal>
  )
}

export default PlaylistPickerModal
