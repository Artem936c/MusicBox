/**
 * Страница «Исполнители» (маршрут `/artists`).
 *
 * Возможности:
 *  - список исполнителей по алфавиту со счётчиками треков и прослушиваний;
 *  - строка поиска — серверный нечёткий поиск `GET /artists?q=` (ТЗ п. 12);
 *  - фильтр «Все / Прослушанные / Не прослушанные»;
 *  - отметка «прослушано» (✅/⬜) через `api.artists.setListened`;
 *  - создание исполнителя вручную — `POST /artists` (ТЗ п. 19);
 *  - переименование — `PATCH /artists/{id}`; при совпадении имён backend
 *    ОБЪЕДИНЯЕТ тёзок, поэтому пользователя предупреждаем заранее (ТЗ п. 10);
 *  - привязка к папке — `FolderPickerModal` → `POST /artists/{id}/folder` (ТЗ п. 8);
 *  - переход к странице исполнителя (`/artists/:id`).
 *
 * Данные приходят из `GET /artists` (схема ArtistOut):
 * { id, name, is_listened, track_count, play_count, folder_id }.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client.js'
import EmptyState from '../components/EmptyState.jsx'
import FolderPickerModal from '../components/FolderPickerModal.jsx'
import Loader from '../components/Loader.jsx'
import Modal from '../components/Modal.jsx'
import SearchBar from '../components/SearchBar.jsx'
import { usePlayer } from '../context/PlayerContext.jsx'
import { useToast } from '../context/ToastContext.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { hapticImpact, hapticNotification } from '../telegram.js'
import { formatCount } from '../utils/format.js'

const TRACK_FORMS = ['трек', 'трека', 'треков']
const PLAY_FORMS = ['прослушивание', 'прослушивания', 'прослушиваний']

/** Максимальная длина имени — та же, что в backend (artists_repo.MAX_NAME_LENGTH). */
const MAX_NAME_LENGTH = 200

/** Задержка перед отправкой поискового запроса, мс. */
const SEARCH_DEBOUNCE = 300

/**
 * Двухстрочная колонка «имя + счётчики» внутри строки списка: в одну строку
 * длинное имя исполнителя не помещается рядом с кнопками действий.
 */
const rowStackStyle = {
  flex: '1 1 auto',
  minWidth: 0,
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
}

/** Варианты фильтра по отметке «прослушано». */
const FILTERS = [
  { key: 'all', label: 'Все' },
  { key: 'listened', label: 'Прослушанные' },
  { key: 'unlistened', label: 'Не прослушанные' },
]

/** Человеческий текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  const detail = error?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  const message = error?.message
  if (typeof message === 'string' && message.trim()) return message
  return fallback
}

/** Алфавитная сортировка с учётом кириллицы (регистр не важен). */
function sortByName(items) {
  return [...items].sort((a, b) =>
    String(a?.name || '').localeCompare(String(b?.name || ''), 'ru', { sensitivity: 'base' }),
  )
}

export function ArtistsPage() {
  const { toast } = useToast()
  const { playVersion } = usePlayer()

  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('all')
  const [pendingId, setPendingId] = useState(null)

  // Модальные окна: создание, переименование, выбор папки.
  const [createOpen, setCreateOpen] = useState(false)
  const [createName, setCreateName] = useState('')
  const [renameTarget, setRenameTarget] = useState(null)
  const [renameName, setRenameName] = useState('')
  const [folderTarget, setFolderTarget] = useState(null)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState(null)

  // Поиск уходит на сервер с задержкой — не дёргаем API на каждый символ.
  useEffect(() => {
    const timer = setTimeout(() => setSearch(query.trim()), SEARCH_DEBOUNCE)
    return () => clearTimeout(timer)
  }, [query])

  // playVersion в зависимостях: при прослушивании backend сам помечает исполнителя
  // прослушанным — список должен обновиться без действий пользователя.
  const { data, error, loading, reload, setData } = useAsync(
    () => api.artists.list(search ? { q: search } : {}),
    [search, playVersion],
  )

  useEffect(() => {
    if (error) toast(errorText(error, 'Не удалось загрузить исполнителей'), 'error')
  }, [error, toast])

  const artists = useMemo(() => {
    const items = Array.isArray(data) ? data.filter(Boolean) : []
    // Результаты поиска приходят по релевантности — этот порядок не трогаем.
    return search ? items : sortByName(items)
  }, [data, search])

  const listenedCount = useMemo(
    () => artists.filter((artist) => Boolean(artist.is_listened)).length,
    [artists],
  )

  const filtered = useMemo(() => {
    if (filter === 'all') return artists
    return artists.filter((artist) =>
      filter === 'listened' ? Boolean(artist.is_listened) : !artist.is_listened,
    )
  }, [artists, filter])

  const initialLoading = loading && data === null
  const isSearching = Boolean(search)
  // Строку поиска и фильтры прячем только у совсем пустой библиотеки —
  // иначе пользователь не сможет очистить запрос, который ничего не нашёл.
  const showControls = artists.length > 0 || isSearching || filter !== 'all'

  /** Заменяет исполнителя в списке ответом сервера (или убирает, если он исчез). */
  const replaceArtist = useCallback(
    (artistId, updated) => {
      setData((prev) => {
        if (!Array.isArray(prev)) return prev
        if (!updated) return prev.filter((item) => item.id !== artistId)
        return prev.map((item) => (item.id === artistId ? updated : item))
      })
    },
    [setData],
  )

  /** Переключение отметки «прослушано» с оптимистичным обновлением и откатом. */
  const handleToggleListened = async (artist) => {
    if (!artist || pendingId === artist.id) return
    const nextValue = !artist.is_listened
    hapticImpact('light')
    setPendingId(artist.id)
    setData((prev) =>
      Array.isArray(prev)
        ? prev.map((item) => (item.id === artist.id ? { ...item, is_listened: nextValue } : item))
        : prev,
    )
    try {
      const updated = await api.artists.setListened(artist.id, nextValue)
      hapticNotification('success')
      if (updated && typeof updated === 'object' && updated.id) replaceArtist(artist.id, updated)
      toast(
        nextValue
          ? `«${artist.name}» отмечен как прослушанный`
          : `С «${artist.name}» снята отметка о прослушивании`,
        'success',
      )
    } catch (err) {
      hapticNotification('error')
      setData((prev) =>
        Array.isArray(prev)
          ? prev.map((item) =>
              item.id === artist.id ? { ...item, is_listened: artist.is_listened } : item,
            )
          : prev,
      )
      toast(errorText(err, 'Не удалось изменить отметку'), 'error')
    } finally {
      setPendingId(null)
    }
  }

  /** Создание исполнителя вручную (ТЗ п. 19). */
  const handleCreate = async (event) => {
    event.preventDefault()
    const name = createName.trim()
    if (!name) {
      setFormError('Введите имя исполнителя')
      return
    }
    setSaving(true)
    setFormError(null)
    try {
      const created = await api.artists.create(name)
      hapticNotification('success')
      // Backend идемпотентен: если тёзка уже был, вернётся он же.
      const existed = !search && artists.some((item) => item.id === created?.id)
      setCreateOpen(false)
      setCreateName('')
      if (search) {
        // Сбрасываем поиск, иначе новый исполнитель может не попасть в выдачу.
        setQuery('')
      } else {
        await reload()
      }
      toast(
        existed
          ? `Исполнитель «${created?.name || name}» уже был в списке`
          : `Исполнитель «${created?.name || name}» сохранён`,
        'success',
      )
    } catch (err) {
      hapticNotification('error')
      setFormError(errorText(err, 'Не удалось создать исполнителя'))
    } finally {
      setSaving(false)
    }
  }

  /** Переименование; тёзки объединяются на стороне backend. */
  const handleRename = async (event) => {
    event.preventDefault()
    const artist = renameTarget
    const name = renameName.trim()
    if (!artist) return
    if (!name) {
      setFormError('Введите имя исполнителя')
      return
    }
    if (name === artist.name) {
      setRenameTarget(null)
      return
    }
    setSaving(true)
    setFormError(null)
    try {
      const updated = await api.artists.rename(artist.id, name)
      hapticNotification('success')
      const merged = Boolean(updated && updated.id !== artist.id)
      setRenameTarget(null)
      await reload()
      toast(
        merged
          ? `«${artist.name}» объединён с «${updated?.name || name}»`
          : `Исполнитель переименован в «${updated?.name || name}»`,
        'success',
      )
    } catch (err) {
      hapticNotification('error')
      setFormError(errorText(err, 'Не удалось переименовать исполнителя'))
    } finally {
      setSaving(false)
    }
  }

  /** Привязка исполнителя к папке; folder = null — «без папки». */
  const handlePickFolder = async (folder) => {
    const artist = folderTarget
    if (!artist) return
    try {
      const updated = await api.artists.setFolder(artist.id, folder?.id ?? null)
      hapticNotification('success')
      if (updated && typeof updated === 'object' && updated.id) replaceArtist(artist.id, updated)
      toast(
        folder
          ? `«${artist.name}» привязан к папке «${folder.name}»`
          : `«${artist.name}» больше не привязан к папке`,
        'success',
      )
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось привязать исполнителя к папке'), 'error')
    } finally {
      setFolderTarget(null)
    }
  }

  const emptyTitle = error
    ? 'Не удалось загрузить исполнителей'
    : isSearching || filter !== 'all'
      ? 'Ничего не нашлось'
      : 'Исполнителей пока нет'

  const emptyDescription = error
    ? errorText(error, 'Проверьте подключение и попробуйте ещё раз.')
    : isSearching || filter !== 'all'
      ? 'Попробуйте изменить запрос или выбрать другой фильтр.'
      : 'Пришлите боту аудиофайл — исполнитель определится автоматически. Или создайте его вручную кнопкой ниже.'

  return (
    <div className="page">
      <div className="section__header">
        <h1 className="section__title">🎤 Исполнители</h1>
        {artists.length > 0 ? (
          <span className="muted">
            {listenedCount} из {artists.length} прослушано
          </span>
        ) : null}
      </div>

      <div className="page__actions">
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => {
            hapticImpact('light')
            setFormError(null)
            setCreateName(query.trim())
            setCreateOpen(true)
          }}
        >
          ＋ Создать исполнителя
        </button>
      </div>

      {showControls ? (
        <>
          <SearchBar value={query} onChange={setQuery} placeholder="Поиск по исполнителям…" />

          <div className="chip-row" role="group" aria-label="Фильтр исполнителей">
            {FILTERS.map((item) => (
              <button
                key={item.key}
                type="button"
                className={filter === item.key ? 'chip is-active' : 'chip'}
                aria-pressed={filter === item.key}
                onClick={() => {
                  hapticImpact('light')
                  setFilter(item.key)
                }}
              >
                {item.label}
              </button>
            ))}
          </div>
        </>
      ) : null}

      {initialLoading ? <Loader label="Загружаем исполнителей…" /> : null}

      {!initialLoading && filtered.length > 0 ? (
        <div className="list">
          {filtered.map((artist) => (
            <div className="list-item list-item--row" key={artist.id}>
              <button
                type="button"
                className="btn-icon list-item__check"
                title={
                  artist.is_listened ? 'Снять отметку «прослушано»' : 'Отметить как прослушанного'
                }
                aria-label={
                  artist.is_listened
                    ? `Снять отметку «прослушано» с ${artist.name}`
                    : `Отметить ${artist.name} как прослушанного`
                }
                aria-pressed={Boolean(artist.is_listened)}
                disabled={pendingId === artist.id}
                onClick={() => handleToggleListened(artist)}
              >
                {artist.is_listened ? '✅' : '⬜'}
              </button>

              <Link
                to={`/artists/${artist.id}`}
                className="list-item__main"
                onClick={() => hapticImpact('light')}
                aria-label={`Открыть исполнителя ${artist.name}`}
              >
                <span style={rowStackStyle}>
                  <span className="list-item__title">{artist.name}</span>
                  <span className="list-item__meta text-ellipsis">
                    {formatCount(artist.track_count, TRACK_FORMS)} ·{' '}
                    {formatCount(artist.play_count, PLAY_FORMS)}
                  </span>
                </span>
              </Link>

              <div className="list-item__actions">
                <button
                  type="button"
                  className="btn-icon"
                  title="Переименовать исполнителя"
                  aria-label={`Переименовать ${artist.name}`}
                  onClick={() => {
                    hapticImpact('light')
                    setFormError(null)
                    setRenameName(artist.name || '')
                    setRenameTarget(artist)
                  }}
                >
                  ✏️
                </button>
                <button
                  type="button"
                  className="btn-icon"
                  title={artist.folder_id ? 'Изменить папку исполнителя' : 'Привязать к папке'}
                  aria-label={`Папка исполнителя ${artist.name}`}
                  aria-pressed={Boolean(artist.folder_id)}
                  onClick={() => {
                    hapticImpact('light')
                    setFolderTarget(artist)
                  }}
                >
                  {artist.folder_id ? '📂' : '📁'}
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {!initialLoading && filtered.length === 0 ? (
        <EmptyState
          icon={error ? '⚠️' : '🎤'}
          title={emptyTitle}
          description={emptyDescription}
          action={
            error ? (
              <button type="button" className="btn btn--primary" onClick={reload}>
                Повторить
              </button>
            ) : null
          }
        />
      ) : null}

      <Modal
        open={createOpen}
        title="Новый исполнитель"
        onClose={() => {
          if (saving) return
          setCreateOpen(false)
          setFormError(null)
        }}
      >
        <form className="field" onSubmit={handleCreate}>
          <label className="field__label" htmlFor="artist-create-name">
            Имя исполнителя
          </label>
          <input
            id="artist-create-name"
            className="input"
            type="text"
            value={createName}
            onChange={(event) => setCreateName(event.target.value)}
            placeholder="Например, Кино"
            maxLength={MAX_NAME_LENGTH}
            autoFocus
            disabled={saving}
          />
          <p className="field__hint">
            Пустой исполнитель пригодится, чтобы заранее завести папку и складывать в неё треки.
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
                setCreateOpen(false)
                setFormError(null)
              }}
            >
              Отмена
            </button>
            <button
              type="submit"
              className="btn btn--primary"
              disabled={saving || !createName.trim()}
            >
              {saving ? 'Создаём…' : 'Создать'}
            </button>
          </div>
        </form>
      </Modal>

      <Modal
        open={Boolean(renameTarget)}
        title="Переименовать исполнителя"
        onClose={() => {
          if (saving) return
          setRenameTarget(null)
          setFormError(null)
        }}
      >
        <form className="field" onSubmit={handleRename}>
          <label className="field__label" htmlFor="artist-rename-name">
            Новое имя
          </label>
          <input
            id="artist-rename-name"
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
                setRenameTarget(null)
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
        open={Boolean(folderTarget)}
        onClose={() => setFolderTarget(null)}
        onPick={handlePickFolder}
        allowNone
        allowCreate
        title={folderTarget ? `Папка для «${folderTarget.name}»` : 'Папка исполнителя'}
      />
    </div>
  )
}

export default ArtistsPage
