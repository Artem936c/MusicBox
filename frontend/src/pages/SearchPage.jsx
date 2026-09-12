/**
 * Страница поиска по библиотеке (маршрут /search).
 *
 * Поле поиска с задержкой 300 мс → `api.search.all(q, { artistIds })`.
 * Результаты разложены по четырём разделам: «Треки», «Группы (альбомы)»
 * (в алфавитном порядке), «Исполнители», «Папки»; переключение — чипами-вкладками.
 *
 * V2 (ТЗ п. 1, 17): над строкой поиска — фильтр по НЕСКОЛЬКИМ исполнителям.
 * Кнопка «🎤 Исполнители» открывает модалку со списком (`GET /artists?q=`)
 * и множественным выбором; выбранные показываются чипами с крестиком.
 * Запрос уходит как `GET /search?q=&artist_ids=1,2,3`, и backend отбирает
 * ПЕРЕСЕЧЕНИЕ: остаются только треки, у которых есть ВСЕ выбранные исполнители,
 * а нечёткий поиск работает уже внутри этого множества. Про это правило прямо
 * написано в подписи под фильтром — иначе оно неочевидно.
 *
 * Клик по исполнителю или папке открывает соответствующую страницу
 * (`/artists/:id`, `/folders/:id`), клик по альбому разворачивает список его
 * треков прямо в выдаче. Отсюда же ведёт ссылка на поиск аудио в Telegram.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

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
import { useAsync } from '../hooks/useAsync.js'
import { hapticImpact } from '../telegram.js'
import { formatCount } from '../utils/format.js'

/** Задержка перед запросом, мс (контракт: 300). */
const DEBOUNCE_MS = 300

/** Сколько результатов запрашивать в каждой категории. */
const SEARCH_LIMIT = 20

/** Пустая выдача — показывается, пока нет ни запроса, ни фильтра. */
const EMPTY_RESULT = { query: '', tracks: [], albums: [], artists: [], folders: [] }

/** Вкладки результатов. */
const TABS = [
  { key: 'all', title: 'Всё', icon: '✨' },
  { key: 'tracks', title: 'Треки', icon: '🎵' },
  { key: 'albums', title: 'Группы (альбомы)', icon: '💿' },
  { key: 'artists', title: 'Исполнители', icon: '🎤' },
  { key: 'folders', title: 'Папки', icon: '📁' },
]

/** Подсказки для случая «во вкладке пусто, а в других разделах есть». */
const EMPTY_TAB_TEXTS = {
  tracks: 'Среди треков ничего не нашлось.',
  albums: 'Среди групп (альбомов) ничего не нашлось.',
  artists: 'Среди исполнителей ничего не нашлось.',
  folders: 'Среди папок ничего не нашлось.',
}

const TRACK_FORMS = ['трек', 'трека', 'треков']
const ARTIST_FORMS = ['исполнитель', 'исполнителя', 'исполнителей']

/** Человеческий текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  const detail = error?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  const message = error?.message
  if (typeof message === 'string' && message.trim()) return message
  return fallback
}

/** Приводит ответ API к безопасной форме со всеми четырьмя списками. */
function normalizeResult(payload, query) {
  const source = payload && typeof payload === 'object' ? payload : {}
  const list = (value) => (Array.isArray(value) ? value.filter(Boolean) : [])
  return {
    query: typeof source.query === 'string' ? source.query : query,
    tracks: list(source.tracks),
    albums: list(source.albums),
    artists: list(source.artists),
    folders: list(source.folders),
  }
}

/** Альбомы («группы») показываем в алфавитном порядке с учётом кириллицы. */
function sortAlbums(albums) {
  return [...albums].sort((left, right) =>
    String(left?.title ?? '').localeCompare(String(right?.title ?? ''), 'ru', {
      sensitivity: 'base',
      numeric: true,
    }),
  )
}

/** Исполнители в модалке — по алфавиту, чтобы список не «прыгал» между запросами. */
function sortByName(items) {
  return [...items].sort((left, right) =>
    String(left?.name ?? '').localeCompare(String(right?.name ?? ''), 'ru', {
      sensitivity: 'base',
      numeric: true,
    }),
  )
}

/**
 * Есть ли у трека ВСЕ выбранные исполнители (то же правило, что и на backend).
 * Нужно только для треков, полученных мимо `/search` — например, для развёрнутого
 * альбома. Если TrackOut пришёл без списка `artists`, трек не прячем.
 */
function matchesArtists(track, artistIds) {
  if (!artistIds.length) return true
  const list = Array.isArray(track?.artists) ? track.artists : null
  if (!list) return true
  const own = new Set(
    list.map((item) => Number(item?.id)).filter((id) => Number.isInteger(id)),
  )
  return artistIds.every((id) => own.has(id))
}

/** Перечисление выбранных исполнителей для подписей. */
function joinNames(artists) {
  return artists.map((artist) => artist.name).join(', ')
}

/**
 * Модалка множественного выбора исполнителей.
 *
 * Пропсы: ArtistFilterModal({ open, selected, onToggle, onReset, onClose })
 *  - selected — массив { id, name } (выбранные на странице);
 *  - onToggle(artist) — добавить/убрать исполнителя: состояние применяется сразу,
 *    чипы под фильтром обновляются, окно не закрывается;
 *  - список грузится из `GET /artists?q=` с той же задержкой 300 мс.
 */
function ArtistFilterModal({ open, selected, onToggle, onReset, onClose }) {
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [artists, setArtists] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  // Задержка перед запросом — как в основном поиске.
  useEffect(() => {
    if (!open) return undefined
    const trimmed = query.trim()
    if (trimmed === debouncedQuery) return undefined
    const timer = setTimeout(() => setDebouncedQuery(trimmed), DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [open, query, debouncedQuery])

  // Список перезагружается при каждом открытии и при смене запроса.
  useEffect(() => {
    if (!open) return undefined
    let cancelled = false
    setLoading(true)
    setError(null)
    api.artists
      .list(debouncedQuery ? { q: debouncedQuery } : {})
      .then((items) => {
        if (cancelled) return
        setArtists(Array.isArray(items) ? items.filter(Boolean) : [])
      })
      .catch((err) => {
        if (cancelled) return
        setArtists([])
        setError(errorText(err, 'Не удалось загрузить исполнителей'))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, debouncedQuery])

  // После закрытия — чистое состояние на следующий раз.
  useEffect(() => {
    if (open) return
    setQuery('')
    setDebouncedQuery('')
    setError(null)
  }, [open])

  const selectedIds = useMemo(() => new Set(selected.map((artist) => artist.id)), [selected])
  const items = useMemo(() => sortByName(artists), [artists])
  const nothingFound = !loading && !error && items.length === 0

  return (
    <Modal
      open={open}
      title="Фильтр по исполнителям"
      onClose={onClose}
      footer={
        <div className="modal__actions">
          <button
            type="button"
            className="btn"
            onClick={onReset}
            disabled={selected.length === 0}
          >
            Сбросить
          </button>
          <button type="button" className="btn btn--primary" onClick={onClose}>
            Готово
          </button>
        </div>
      }
    >
      <SearchBar value={query} onChange={setQuery} placeholder="Имя исполнителя…" />

      <p className="modal__hint">
        Можно отметить нескольких: в выдаче останутся только те треки, у которых есть
        ВСЕ отмеченные исполнители сразу.
      </p>

      {selected.length > 0 ? (
        <div className="chips" aria-label="Выбранные исполнители">
          {selected.map((artist) => (
            <span key={artist.id} className="chip chip--selected is-active">
              <span aria-hidden="true">🎤</span>
              {artist.name}
              <button
                type="button"
                className="chip__remove"
                onClick={() => onToggle(artist)}
                aria-label={`Убрать «${artist.name}» из фильтра`}
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      ) : null}

      {error ? (
        <p className="modal__error" role="alert">
          {error}
        </p>
      ) : null}

      {loading ? <Loader label="Загружаем исполнителей…" /> : null}

      {!loading && items.length > 0 ? (
        <ul className="list">
          {items.map((artist) => {
            const picked = selectedIds.has(artist.id)
            return (
              <li key={artist.id}>
                <button
                  type="button"
                  className="list-item"
                  aria-pressed={picked}
                  onClick={() => onToggle(artist)}
                >
                  <span className="list-item__icon" aria-hidden="true">
                    {picked ? '☑️' : '⬜'}
                  </span>
                  <span className="list-item__title">{artist.name || 'Без имени'}</span>
                  <span className="list-item__meta">
                    {formatCount(artist.track_count || 0, TRACK_FORMS)}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      ) : null}

      {nothingFound ? (
        <EmptyState
          icon="🎤"
          title={query ? 'Никого не нашли' : 'Исполнителей пока нет'}
          description={
            query
              ? 'Проверьте раскладку клавиатуры или введите пару первых букв имени.'
              : 'Как только в библиотеке появятся треки с исполнителями, они окажутся здесь.'
          }
        />
      ) : null}
    </Modal>
  )
}

export function SearchPage() {
  const navigate = useNavigate()
  const { toast } = useToast()
  // playVersion в зависимостях загрузки: после учтённого прослушивания счётчики
  // в выдаче обновляются сами, без действий пользователя.
  const { playVersion } = usePlayer()

  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [tab, setTab] = useState('all')

  // Фильтр по исполнителям: массив { id, name } и открытая модалка выбора.
  const [selectedArtists, setSelectedArtists] = useState([])
  const [artistModalOpen, setArtistModalOpen] = useState(false)

  // Развёрнутый альбом и кэш его треков: { [albumId]: { loading, error, items } }.
  const [expandedAlbum, setExpandedAlbum] = useState(null)
  const [albumTracks, setAlbumTracks] = useState({})

  // Трек, для которого открыт выбор папки или плейлиста.
  const [pickerTrack, setPickerTrack] = useState(null)
  const [folderPickerOpen, setFolderPickerOpen] = useState(false)
  const [playlistPickerOpen, setPlaylistPickerOpen] = useState(false)

  // Задержка 300 мс: запрос уходит, только когда пользователь перестал печатать.
  useEffect(() => {
    const trimmed = query.trim()
    if (trimmed === debouncedQuery) return undefined
    const timer = setTimeout(() => setDebouncedQuery(trimmed), DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query, debouncedQuery])

  const selectedIds = useMemo(
    () => selectedArtists.map((artist) => artist.id),
    [selectedArtists],
  )
  // Ключ фильтра для зависимостей загрузки: сам массив меняется каждый рендер.
  const artistKey = useMemo(() => selectedIds.join(','), [selectedIds])
  const hasFilter = selectedIds.length > 0
  const hasQuery = Boolean(debouncedQuery)
  // Показывать есть что, если задан запрос ИЛИ выбран хотя бы один исполнитель:
  // пустая строка поиска с фильтром возвращает все треки этих исполнителей.
  const active = hasQuery || hasFilter

  const { data, error, loading, setData } = useAsync(
    () =>
      debouncedQuery || selectedIds.length
        ? api.search.all(debouncedQuery, { limit: SEARCH_LIMIT, artistIds: selectedIds })
        : Promise.resolve(EMPTY_RESULT),
    [debouncedQuery, artistKey, playVersion],
  )

  // Новый запрос или другой фильтр — сворачиваем ранее раскрытый альбом.
  useEffect(() => {
    setExpandedAlbum(null)
  }, [debouncedQuery, artistKey])

  const result = useMemo(() => normalizeResult(data, debouncedQuery), [data, debouncedQuery])
  const albums = useMemo(() => sortAlbums(result.albums), [result.albums])

  const counts = useMemo(
    () => ({
      all:
        result.tracks.length +
        result.albums.length +
        result.artists.length +
        result.folders.length,
      tracks: result.tracks.length,
      albums: result.albums.length,
      artists: result.artists.length,
      folders: result.folders.length,
    }),
    [result],
  )

  // Пользователь ещё печатает — показываем «ищем…», не сбрасывая прошлую выдачу.
  const typing = query.trim() !== debouncedQuery
  const busy = loading || typing
  const nothingFound = active && !busy && !error && counts.all === 0
  const activeTabEmpty =
    active && !busy && !error && !nothingFound && tab !== 'all' && counts[tab] === 0

  /** Добавляет исполнителя в фильтр или убирает его оттуда. */
  const toggleArtist = useCallback((artist) => {
    if (!artist?.id) return
    hapticImpact('light')
    setSelectedArtists((prev) =>
      prev.some((item) => item.id === artist.id)
        ? prev.filter((item) => item.id !== artist.id)
        : [...prev, { id: artist.id, name: artist.name || 'Без имени' }],
    )
  }, [])

  /** Полный сброс фильтра по исполнителям. */
  const resetArtists = useCallback(() => {
    hapticImpact('light')
    setSelectedArtists([])
  }, [])

  /** Обновляет флаг «в избранном» в уже загруженной выдаче, без нового запроса. */
  const handleFavouriteChanged = useCallback(
    (track, value) => {
      if (!track) return
      const patch = (items) =>
        items.map((item) => (item.id === track.id ? { ...item, is_favourite: value } : item))
      setData((prev) => (prev ? { ...prev, tracks: patch(prev.tracks || []) } : prev))
      setAlbumTracks((prev) => {
        const entries = Object.entries(prev)
        if (!entries.length) return prev
        const next = {}
        for (const [albumId, entry] of entries) {
          next[albumId] = entry?.items?.length ? { ...entry, items: patch(entry.items) } : entry
        }
        return next
      })
    },
    [setData],
  )

  /** Разворачивает альбом и подгружает его треки (один раз на альбом). */
  const toggleAlbum = useCallback(
    async (album) => {
      if (!album?.id) return
      hapticImpact('light')
      const albumId = album.id
      if (expandedAlbum === albumId) {
        setExpandedAlbum(null)
        return
      }
      setExpandedAlbum(albumId)
      if (albumTracks[albumId]?.items) return

      setAlbumTracks((prev) => ({
        ...prev,
        [albumId]: { loading: true, error: null, items: null },
      }))
      try {
        const items = await api.albums.tracks(albumId)
        setAlbumTracks((prev) => ({
          ...prev,
          [albumId]: {
            loading: false,
            error: null,
            items: Array.isArray(items) ? items.filter(Boolean) : [],
          },
        }))
      } catch (err) {
        setAlbumTracks((prev) => ({
          ...prev,
          [albumId]: {
            loading: false,
            error: errorText(err, 'Не удалось загрузить треки альбома'),
            items: null,
          },
        }))
      }
    },
    [albumTracks, expandedAlbum],
  )

  const openFolderPicker = useCallback((track) => {
    setPickerTrack(track)
    setFolderPickerOpen(true)
  }, [])

  const openPlaylistPicker = useCallback((track) => {
    setPickerTrack(track)
    setPlaylistPickerOpen(true)
  }, [])

  /** Перенос трека в выбранную папку (null — «без папки»). */
  const handlePickFolder = useCallback(
    async (folder) => {
      const track = pickerTrack
      setFolderPickerOpen(false)
      if (!track) return
      try {
        const updated = await api.tracks.move(track.id, folder ? folder.id : null)
        const folderId = updated?.folder_id ?? (folder ? folder.id : null)
        const folderName = updated?.folder_name ?? (folder ? folder.name : null)
        const patch = (items) =>
          items.map((item) =>
            item.id === track.id
              ? { ...item, folder_id: folderId, folder_name: folderName }
              : item,
          )
        setData((prev) => (prev ? { ...prev, tracks: patch(prev.tracks || []) } : prev))
        toast(
          folder ? `Трек перемещён в папку «${folder.name}»` : 'Трек убран из папки',
          'success',
        )
      } catch (err) {
        toast(errorText(err, 'Не удалось переместить трек'), 'error')
      } finally {
        setPickerTrack(null)
      }
    },
    [pickerTrack, setData, toast],
  )

  /** Добавление трека в выбранный плейлист. */
  const handlePickPlaylist = useCallback(
    async (playlist) => {
      const track = pickerTrack
      setPlaylistPickerOpen(false)
      if (!track || !playlist) {
        setPickerTrack(null)
        return
      }
      try {
        await api.playlists.addTracks(playlist.id, [track.id])
        toast(`Трек добавлен в плейлист «${playlist.name}»`, 'success')
      } catch (err) {
        toast(errorText(err, 'Не удалось добавить трек в плейлист'), 'error')
      } finally {
        setPickerTrack(null)
      }
    },
    [pickerTrack, toast],
  )

  const trackActions = useMemo(
    () => [
      { key: 'playlist', icon: '➕', title: 'Добавить в плейлист', onClick: openPlaylistPicker },
      { key: 'folder', icon: '📁', title: 'Переместить в папку', onClick: openFolderPicker },
    ],
    [openFolderPicker, openPlaylistPicker],
  )

  /** Раздел показывается на вкладке «Всё» и на своей собственной. */
  const showSection = (key) => tab === 'all' || tab === key

  const renderAlbumTracks = (albumId) => {
    const entry = albumTracks[albumId]
    if (!entry) return null
    if (entry.loading) return <Loader label="Загружаем треки альбома…" />
    if (entry.error) {
      return (
        <p className="error" role="alert">
          {entry.error}
        </p>
      )
    }
    if (!entry.items || entry.items.length === 0) {
      return <p className="muted">В этом альбоме пока нет треков.</p>
    }
    // Фильтр действует и внутри альбома — иначе выдача противоречила бы правилу
    // «остаются только треки, у которых есть все выбранные исполнители».
    const items = entry.items.filter((track) => matchesArtists(track, selectedIds))
    if (items.length === 0) {
      return (
        <p className="muted">
          В этом альбоме нет треков, где встречаются все выбранные исполнители.
        </p>
      )
    }
    return (
      <>
        {items.length < entry.items.length ? (
          <p className="field__hint">
            Показаны только треки выбранных исполнителей: {items.length} из {entry.items.length}.
          </p>
        ) : null}
        <div className="list">
          {items.map((track, index) => (
            <TrackRow
              key={track.id}
              track={track}
              index={index}
              queue={items}
              actions={trackActions}
              onChanged={handleFavouriteChanged}
            />
          ))}
        </div>
      </>
    )
  }

  return (
    <div className="page">
      <div className="row row--between">
        <button
          type="button"
          className="chip"
          aria-selected={hasFilter}
          aria-haspopup="dialog"
          onClick={() => {
            hapticImpact('light')
            setArtistModalOpen(true)
          }}
        >
          <span aria-hidden="true">🎤</span>
          Исполнители
          {hasFilter ? <span className="badge">{selectedIds.length}</span> : null}
        </button>
        {hasFilter ? (
          <button type="button" className="btn btn--ghost" onClick={resetArtists}>
            ✕ Сбросить
          </button>
        ) : null}
      </div>

      {hasFilter ? (
        <div className="chips" aria-label="Выбранные исполнители">
          {selectedArtists.map((artist) => (
            <span key={artist.id} className="chip chip--selected is-active">
              <span aria-hidden="true">🎤</span>
              {artist.name}
              <button
                type="button"
                className="chip__remove"
                onClick={() => toggleArtist(artist)}
                aria-label={`Убрать «${artist.name}» из фильтра`}
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      ) : null}

      <p className="field__hint">
        {hasFilter
          ? `Показываем только треки, у которых есть ВСЕ выбранные исполнители (${joinNames(
              selectedArtists,
            )}) — это пересечение, а не объединение. Поиск по словам работает уже внутри этого набора, а пустая строка поиска покажет все такие треки. Папки при этом ищутся по всей библиотеке.`
          : 'Можно выбрать сразу нескольких исполнителей — тогда останутся только треки, где есть все они одновременно, а поиск по словам будет работать внутри этого набора.'}
      </p>

      <SearchBar
        value={query}
        onChange={setQuery}
        placeholder="Трек, исполнитель, альбом или папка…"
        autoFocus
      />

      <p className="field__hint">
        Опечатки и неверная раскладка не помеха: «Rbyj» найдёт «Кино», а «Мосвка» — «Москву».
      </p>

      <div className="chip-row" role="tablist" aria-label="Разделы результатов">
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            className="chip"
            aria-selected={tab === item.key}
            onClick={() => {
              hapticImpact('light')
              setTab(item.key)
            }}
          >
            <span aria-hidden="true">{item.icon}</span>
            {item.title}
            {active ? <span className="badge">{counts[item.key]}</span> : null}
          </button>
        ))}
      </div>

      <div className="row row--between">
        <span className="muted">
          {active && !busy && !error && counts.all > 0
            ? `Найдено совпадений: ${counts.all}`
            : 'Нет нужного трека в библиотеке?'}
        </span>
        <Link className="btn btn--ghost" to="/tgsearch">
          🔎 Искать в Telegram
        </Link>
      </div>

      {active && busy ? <Loader label="Ищем…" /> : null}

      {error ? (
        <p className="error" role="alert">
          {errorText(error, 'Не удалось выполнить поиск. Попробуйте ещё раз.')}
        </p>
      ) : null}

      {!active && !busy ? (
        <EmptyState
          icon="🔍"
          title="Что будем искать?"
          description="Введите название трека, исполнителя, альбома или папки — хватит нескольких букв, остальное поиск додумает сам. А кнопка «🎤 Исполнители» соберёт треки, над которыми работали сразу несколько выбранных артистов."
        />
      ) : null}

      {nothingFound ? (
        <EmptyState
          icon="🙃"
          title="Ничего не найдено"
          description={
            hasFilter
              ? `Похоже, у выбранных исполнителей (${joinNames(selectedArtists)}) нет общих треков${
                  hasQuery ? ' с таким запросом' : ''
                }. Уберите кого-нибудь из фильтра крестиком или проверьте, нет ли опечатки в запросе.`
              : 'Попробуйте запрос покороче или проверьте раскладку клавиатуры. А если трека ещё нет в библиотеке — поищите его в Telegram.'
          }
          action={
            hasFilter ? (
              <button type="button" className="btn btn--primary" onClick={resetArtists}>
                ✕ Сбросить фильтр
              </button>
            ) : (
              <Link className="btn btn--primary" to="/tgsearch">
                🔎 Искать в Telegram
              </Link>
            )
          }
        />
      ) : null}

      {activeTabEmpty ? (
        <EmptyState
          icon="🤔"
          title="В этом разделе пусто"
          description={`${EMPTY_TAB_TEXTS[tab]} Загляните на вкладку «Всё» — совпадения есть в других разделах.`}
        />
      ) : null}

      {showSection('tracks') && result.tracks.length > 0 ? (
        <section className="section">
          <div className="section__header">
            <h2 className="section__title">🎵 Треки</h2>
            <span className="muted">{formatCount(result.tracks.length, TRACK_FORMS)}</span>
          </div>
          {hasFilter ? (
            <p className="field__hint">
              {selectedIds.length === 1
                ? 'У каждого из этих треков есть выбранный исполнитель.'
                : `У каждого из этих треков есть все ${formatCount(
                    selectedIds.length,
                    ARTIST_FORMS,
                  )} из фильтра.`}
            </p>
          ) : null}
          <div className="list">
            {result.tracks.map((track, index) => (
              <TrackRow
                key={track.id}
                track={track}
                index={index}
                queue={result.tracks}
                actions={trackActions}
                onChanged={handleFavouriteChanged}
              />
            ))}
          </div>
        </section>
      ) : null}

      {showSection('albums') && albums.length > 0 ? (
        <section className="section">
          <div className="section__header">
            <h2 className="section__title">💿 Группы (альбомы)</h2>
            <span className="muted">{albums.length}</span>
          </div>
          <div className="list">
            {albums.map((album) => {
              const opened = expandedAlbum === album.id
              const meta = [album.artist_name, album.year ? String(album.year) : null]
                .filter(Boolean)
                .join(' · ')
              return (
                <div key={album.id} className="search-album">
                  <button
                    type="button"
                    className="list-item"
                    onClick={() => toggleAlbum(album)}
                    aria-expanded={opened}
                  >
                    <span className="list-item__icon" aria-hidden="true">
                      {opened ? '📂' : '💿'}
                    </span>
                    <span className="list-item__title">
                      {album.title || 'Без названия'}
                      {meta ? <span className="muted"> · {meta}</span> : null}
                    </span>
                    <span className="list-item__meta">
                      {formatCount(album.track_count || 0, TRACK_FORMS)}
                    </span>
                  </button>
                  {opened ? renderAlbumTracks(album.id) : null}
                </div>
              )
            })}
          </div>
        </section>
      ) : null}

      {showSection('artists') && result.artists.length > 0 ? (
        <section className="section">
          <div className="section__header">
            <h2 className="section__title">🎤 Исполнители</h2>
            <span className="muted">{result.artists.length}</span>
          </div>
          <div className="list">
            {result.artists.map((artist) => {
              const picked = selectedIds.includes(artist.id)
              return (
                <div key={artist.id} className="list-item list-item--row">
                  <button
                    type="button"
                    className="list-item__main"
                    onClick={() => {
                      hapticImpact('light')
                      navigate(`/artists/${artist.id}`)
                    }}
                  >
                    <span className="list-item__icon" aria-hidden="true">
                      {artist.is_listened ? '✅' : '🎤'}
                    </span>
                    <span className="list-item__title">{artist.name || 'Без имени'}</span>
                    <span className="list-item__meta">
                      {formatCount(artist.track_count || 0, TRACK_FORMS)}
                    </span>
                  </button>
                  <span className="list-item__actions">
                    <button
                      type="button"
                      className={picked ? 'btn btn--icon btn--primary' : 'btn btn--icon'}
                      aria-pressed={picked}
                      aria-label={
                        picked
                          ? `Убрать «${artist.name}» из фильтра`
                          : `Добавить «${artist.name}» в фильтр`
                      }
                      title={picked ? 'Убрать из фильтра' : 'Добавить в фильтр'}
                      onClick={() => toggleArtist(artist)}
                    >
                      {picked ? '✔️' : '➕'}
                    </button>
                  </span>
                </div>
              )
            })}
          </div>
        </section>
      ) : null}

      {showSection('folders') && result.folders.length > 0 ? (
        <section className="section">
          <div className="section__header">
            <h2 className="section__title">📁 Папки</h2>
            <span className="muted">{result.folders.length}</span>
          </div>
          <div className="list">
            {result.folders.map((folder) => (
              <button
                key={folder.id}
                type="button"
                className="list-item"
                onClick={() => {
                  hapticImpact('light')
                  navigate(`/folders/${folder.id}`)
                }}
              >
                <span className="list-item__icon" aria-hidden="true">
                  {folder.is_artist_folder ? '🎤' : '📁'}
                </span>
                <span className="list-item__title">{folder.name || 'Без названия'}</span>
                <span className="list-item__meta">
                  {formatCount(folder.track_count ?? folder.total_track_count ?? 0, TRACK_FORMS)}
                </span>
              </button>
            ))}
          </div>
        </section>
      ) : null}

      <ArtistFilterModal
        open={artistModalOpen}
        selected={selectedArtists}
        onToggle={toggleArtist}
        onReset={resetArtists}
        onClose={() => setArtistModalOpen(false)}
      />

      <FolderPickerModal
        open={folderPickerOpen}
        onClose={() => {
          setFolderPickerOpen(false)
          setPickerTrack(null)
        }}
        onPick={handlePickFolder}
        title="Переместить трек в папку"
      />

      <PlaylistPickerModal
        open={playlistPickerOpen}
        onClose={() => {
          setPlaylistPickerOpen(false)
          setPickerTrack(null)
        }}
        onPick={handlePickPlaylist}
      />
    </div>
  )
}

export default SearchPage
