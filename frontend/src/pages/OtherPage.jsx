/**
 * Страница «Другое» (маршрут `/other`, контракт V2 раздел 6, ТЗ п. 13).
 *
 * Здесь живут файлы, у которых `file_type !== 'audio'`: документы, видео,
 * кружочки (`video_note`) и голосовые (`voice`). Данные приходят из:
 *   GET  /other?folder_id=&q=&file_type=&limit=&offset=  — файлы раздела,
 *   GET  /other/folders?parent_id=                       — папки раздела,
 *   POST /other/folders                                  — создание папки,
 *   GET  /folders/{id}/path                              — хлебные крошки.
 *
 * Навигация по вложенным папкам хранится в query-параметре `?folder=<id>`
 * (HashRouter): так системная кнопка «Назад» Telegram возвращает на уровень
 * выше, а ссылку на папку можно просто переслать.
 *
 * Воспроизведение: видео, кружочки и голосовые проигрываются прямо в списке
 * тегами <video>/<audio> с `src = downloadUrl(file, { inline: true })` —
 * backend отдаёт файл по тому же подписанному stream-токену, что и аудио.
 * Документы скачиваются ссылкой на тот же адрес без `inline`.
 *
 * ВАЖНО про скачивание: WebView Telegram нередко блокирует загрузку файлов.
 * Поэтому внутри Telegram ссылка открывается через `tg.openLink()` (внешний
 * браузер), а под строкой файла показывается подсказка: если ничего не
 * произошло — файл всегда можно получить сообщением от бота (команда /other).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api } from '../api/client.js'
import EmptyState from '../components/EmptyState.jsx'
import Loader from '../components/Loader.jsx'
import SearchBar from '../components/SearchBar.jsx'
import { usePlayer } from '../context/PlayerContext.jsx'
import { useToast } from '../context/ToastContext.jsx'
import { hapticImpact, hapticNotification, isTelegram, tg } from '../telegram.js'
import { formatCount, formatDate, formatFileSize } from '../utils/format.js'

/** Сколько файлов запрашиваем за раз (backend разрешает максимум 200). */
const PAGE_SIZE = 50

/** Задержка перед отправкой поискового запроса, мс. */
const SEARCH_DEBOUNCE = 300

/** Максимальная длина названия папки (совпадает с проверкой backend). */
const NAME_MAX_LENGTH = 100

const FILE_FORMS = ['файл', 'файла', 'файлов']
const FOLDER_FORMS = ['папка', 'папки', 'папок']

/**
 * Типы файлов раздела. Значки и подписи совпадают с теми, что бот показывает
 * в чате (`backend/services/media.py`), чтобы один и тот же файл выглядел
 * одинаково и в боте, и в Mini App.
 */
const FILE_TYPES = {
  document: { icon: '📄', label: 'Документ', plural: 'Документы' },
  video: { icon: '🎬', label: 'Видео', plural: 'Видео' },
  video_note: { icon: '⭕', label: 'Кружочек', plural: 'Кружочки' },
  voice: { icon: '🎤', label: 'Голосовое', plural: 'Голосовые' },
  audio: { icon: '🎵', label: 'Аудио', plural: 'Аудио' },
}

/** Запасной вид для неизвестного типа (на случай будущих типов файлов). */
const UNKNOWN_TYPE = { icon: '📎', label: 'Файл', plural: 'Файлы' }

/** Фильтры-чипы над списком. Пустой ключ — «Все». */
const TYPE_FILTERS = [
  { key: '', label: 'Все' },
  { key: 'document', label: FILE_TYPES.document.plural },
  { key: 'video', label: FILE_TYPES.video.plural },
  { key: 'video_note', label: FILE_TYPES.video_note.plural },
  { key: 'voice', label: FILE_TYPES.voice.plural },
]

/** Типы, которые умеем проигрывать прямо в списке. */
const PLAYABLE_TYPES = new Set(['video', 'video_note', 'voice'])

/** Описание типа файла со значком и подписью. */
function typeInfo(fileType) {
  return FILE_TYPES[String(fileType || '')] || UNKNOWN_TYPE
}

/**
 * Модификатор класса для типа файла: `video_note` -> `video-note`,
 * неизвестный тип -> `other` (в именах классов не должно быть подчёркиваний).
 */
function typeModifier(fileType) {
  const key = String(fileType || '')
  if (!FILE_TYPES[key]) return 'other'
  return key.replace(/_/g, '-')
}

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

/**
 * Адрес файла: сначала пробуем собрать подписанную ссылку из stream_url
 * (`api.other.downloadUrl`), затем — готовое поле download_url, если backend
 * когда-нибудь начнёт отдавать его напрямую.
 */
function fileUrl(file, { inline = false } = {}) {
  const signed = api.other.downloadUrl(file, { inline })
  if (signed) return signed
  const direct = typeof file?.download_url === 'string' ? file.download_url : ''
  if (!direct) return ''
  return (import.meta.env.VITE_API_ORIGIN || '') + direct
}

/** Положительный идентификатор папки из строки запроса или null. */
function toFolderId(value) {
  const id = Number(value)
  return Number.isInteger(id) && id > 0 ? id : null
}

// Локальная вёрстка, для которой в styles.css нет отдельных классов.
const columnStyle = { flex: '1 1 auto', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }
const metaRowStyle = { display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 6, minWidth: 0 }
const growStyle = { flex: '1 1 auto', minWidth: 0 }
const previewStyle = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  gap: 'var(--gap-sm)',
  margin: '2px 0 var(--gap-sm)',
  padding: 'var(--gap-sm)',
  borderRadius: 'var(--radius-sm)',
  background: 'var(--secondary-bg)',
}
const videoStyle = {
  width: '100%',
  maxHeight: '58vh',
  borderRadius: 'var(--radius-sm)',
  background: '#000',
}
const videoNoteStyle = {
  width: 'min(240px, 70vw)',
  height: 'min(240px, 70vw)',
  borderRadius: '50%',
  objectFit: 'cover',
  background: '#000',
}
const audioStyle = { width: '100%' }
const hintStyle = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--gap-sm)',
  margin: '2px 0 var(--gap-sm)',
  padding: 'var(--gap-sm) var(--gap)',
  borderRadius: 'var(--radius-sm)',
  background: 'var(--secondary-bg)',
}
const hintActionsStyle = { display: 'flex', flexWrap: 'wrap', gap: 'var(--gap-sm)' }

export function OtherPage() {
  const { toast } = useToast()
  // playVersion держим в зависимостях загрузки: если где-то в приложении учли
  // прослушивание, список раздела перечитывается вместе со статистикой.
  const { playVersion, isPlaying, stop } = usePlayer()

  const [searchParams, setSearchParams] = useSearchParams()
  const folderId = useMemo(() => toFolderId(searchParams.get('folder')), [searchParams])

  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [reloadKey, setReloadKey] = useState(0)

  const [folders, setFolders] = useState([])
  const [crumbs, setCrumbs] = useState([])
  const [foldersLoading, setFoldersLoading] = useState(true)
  const [foldersError, setFoldersError] = useState(null)

  const [files, setFiles] = useState([])
  const [filesLoading, setFilesLoading] = useState(true)
  const [filesError, setFilesError] = useState(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [hasMore, setHasMore] = useState(false)

  const [openFileId, setOpenFileId] = useState(null)
  const [hintFileId, setHintFileId] = useState(null)

  const [formOpen, setFormOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)

  // Защита от гонок: ответ устаревшего запроса игнорируется.
  const filesRequestRef = useRef(0)
  const foldersRequestRef = useRef(0)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  // Поиск с задержкой: не дёргаем backend на каждую букву.
  useEffect(() => {
    const text = query.trim()
    if (text === search) return undefined
    const timer = setTimeout(() => setSearch(text), SEARCH_DEBOUNCE)
    return () => clearTimeout(timer)
  }, [query, search])

  const searching = search.length > 0

  /** Возврат в корень раздела (например, когда папку удалили из бота). */
  const resetToRoot = useCallback(() => {
    setSearchParams({}, { replace: true })
  }, [setSearchParams])

  const loadFolders = useCallback(async () => {
    const requestId = foldersRequestRef.current + 1
    foldersRequestRef.current = requestId
    setFoldersLoading(true)
    setFoldersError(null)
    // В корне крошек нет — гасим их сразу, чтобы подписи не «отставали».
    if (!folderId) setCrumbs([])
    try {
      const [list, path] = await Promise.all([
        api.other.folders({ parent_id: folderId ?? 0 }),
        folderId ? api.folders.path(folderId) : Promise.resolve(null),
      ])
      if (!mountedRef.current || foldersRequestRef.current !== requestId) return
      setFolders(sortByName(Array.isArray(list) ? list.filter(Boolean) : []))
      const items = Array.isArray(path?.items) ? path.items.filter(Boolean) : []
      setCrumbs(folderId ? items : [])
    } catch (err) {
      if (!mountedRef.current || foldersRequestRef.current !== requestId) return
      if (err?.status === 404 && folderId) {
        // Папку удалили (например, из бота) — мягко возвращаемся в корень.
        toast('Папка не найдена — вернулись в начало раздела', 'info')
        setFolders([])
        setCrumbs([])
        resetToRoot()
        return
      }
      setFoldersError(err)
      setFolders([])
      setCrumbs([])
      toast(errorText(err, 'Не удалось загрузить папки'), 'error')
    } finally {
      if (mountedRef.current && foldersRequestRef.current === requestId) setFoldersLoading(false)
    }
  }, [folderId, resetToRoot, toast])

  const loadFiles = useCallback(
    async (offset = 0) => {
      const requestId = filesRequestRef.current + 1
      filesRequestRef.current = requestId
      if (offset === 0) {
        setFilesLoading(true)
        setFilesError(null)
      } else {
        setLoadingMore(true)
      }
      try {
        const params = { limit: PAGE_SIZE, offset }
        if (typeFilter) params.file_type = typeFilter
        // При поиске ищем по всему разделу, иначе — по текущей папке
        // (folder_id=0 означает «файлы без папки», то есть корень раздела).
        if (search) params.q = search
        else params.folder_id = folderId ?? 0

        const items = await api.other.list(params)
        if (!mountedRef.current || filesRequestRef.current !== requestId) return
        const list = Array.isArray(items) ? items.filter(Boolean) : []
        setFiles((prev) => (offset === 0 ? list : [...prev, ...list]))
        setHasMore(list.length >= PAGE_SIZE)
      } catch (err) {
        if (!mountedRef.current || filesRequestRef.current !== requestId) return
        setFilesError(err)
        setHasMore(false)
        if (offset === 0) setFiles([])
        toast(errorText(err, 'Не удалось загрузить файлы'), 'error')
      } finally {
        if (mountedRef.current && filesRequestRef.current === requestId) {
          setFilesLoading(false)
          setLoadingMore(false)
        }
      }
    },
    [folderId, search, typeFilter, toast],
  )

  // Папки нужны только в режиме навигации: при поиске список папок скрыт.
  useEffect(() => {
    if (searching) {
      setFoldersLoading(false)
      return
    }
    loadFolders()
  }, [loadFolders, searching, reloadKey])

  useEffect(() => {
    // playVersion — по контракту: страницы со списками треков перечитываются
    // после каждого учтённого прослушивания.
    loadFiles(0)
  }, [loadFiles, playVersion, reloadKey])

  // Смена папки, запроса или фильтра закрывает открытый проигрыватель.
  useEffect(() => {
    setOpenFileId(null)
    setHintFileId(null)
  }, [folderId, search, typeFilter])

  const currentFolder = crumbs.length ? crumbs[crumbs.length - 1] : null

  const openFolder = useCallback(
    (id) => {
      hapticImpact('light')
      const next = new URLSearchParams(searchParams)
      if (id) next.set('folder', String(id))
      else next.delete('folder')
      setSearchParams(next)
      setFormOpen(false)
      setNewName('')
    },
    [searchParams, setSearchParams],
  )

  const handleRetry = useCallback(() => {
    setReloadKey((value) => value + 1)
  }, [])

  const handleCreateFolder = async (event) => {
    event.preventDefault()
    const name = newName.trim()
    if (!name) {
      toast('Введите название папки', 'error')
      return
    }
    setCreating(true)
    try {
      const folder = await api.other.createFolder(name, folderId)
      hapticNotification('success')
      toast(`Папка «${folder?.name || name}» создана`, 'success')
      setNewName('')
      setFormOpen(false)
      await loadFolders()
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось создать папку'), 'error')
    } finally {
      setCreating(false)
    }
  }

  /** Открывает/закрывает встроенный проигрыватель файла. */
  const togglePreview = (file) => {
    hapticImpact('light')
    setHintFileId(null)
    const opening = openFileId !== file.id
    // Два звука одновременно — плохо: останавливаем общий плеер.
    if (opening && isPlaying) stop()
    setOpenFileId(opening ? file.id : null)
  }

  /**
   * Скачивание документа. Внутри Telegram открываем ссылку во внешнем браузере
   * (`tg.openLink`) — так файл сохраняется надёжнее, чем через <a download>,
   * который WebView часто игнорирует. В любом случае показываем подсказку.
   */
  const handleDownload = (file) => (event) => {
    const url = fileUrl(file)
    if (!url) {
      event.preventDefault()
      hapticNotification('error')
      toast('Ссылка на файл недоступна. Обновите страницу и попробуйте ещё раз.', 'error')
      return
    }
    hapticImpact('light')
    setHintFileId(file.id)
    if (isTelegram && typeof tg?.openLink === 'function') {
      event.preventDefault()
      try {
        tg.openLink(url)
      } catch {
        // Если WebView не дал открыть ссылку — остаётся подсказка про бота.
        window.open(url, '_blank', 'noopener')
      }
    }
  }

  /** Свернуть Mini App, чтобы пользователь оказался в чате с ботом. */
  const openBotChat = () => {
    hapticImpact('light')
    setHintFileId(null)
    try {
      tg?.close?.()
    } catch {
      /* вне Telegram закрывать нечего */
    }
  }

  const initialLoading = (filesLoading && files.length === 0) || (!searching && foldersLoading)
  // Пустое состояние показываем, когда файлов нет и подсказать больше нечем:
  // при поиске и активном фильтре — всегда, в обычном режиме — если нет и папок.
  const isEmpty =
    !initialLoading && files.length === 0 && (searching || Boolean(typeFilter) || folders.length === 0)
  const loadError = filesError || foldersError

  const renderPreview = (file) => {
    const url = fileUrl(file, { inline: true })
    const info = typeInfo(file.file_type)
    if (!url) {
      return (
        <div style={hintStyle} key={`preview-${file.id}`}>
          <span className="muted">
            Файл недоступен для воспроизведения — попросите бота прислать его командой /other.
          </span>
        </div>
      )
    }
    return (
      <div style={previewStyle} key={`preview-${file.id}`}>
        {file.file_type === 'voice' ? (
          <audio
            style={audioStyle}
            src={url}
            controls
            autoPlay
            preload="metadata"
            onError={() => setHintFileId(file.id)}
          />
        ) : (
          <video
            style={file.file_type === 'video_note' ? videoNoteStyle : videoStyle}
            src={url}
            controls
            autoPlay
            playsInline
            preload="metadata"
            onError={() => setHintFileId(file.id)}
          />
        )}
        <div style={hintActionsStyle}>
          <a
            className="btn btn--ghost"
            href={fileUrl(file)}
            download
            onClick={handleDownload(file)}
            aria-label={`Скачать ${info.label.toLowerCase()} ${file.title}`}
          >
            ⬇️ Скачать
          </a>
          <button type="button" className="btn btn--ghost" onClick={() => setOpenFileId(null)}>
            ✕ Закрыть
          </button>
        </div>
      </div>
    )
  }

  const renderHint = (file) => (
    <div style={hintStyle} key={`hint-${file.id}`}>
      <span className="muted">
        Если файл не открылся, Telegram мог заблокировать скачивание внутри мини-приложения.
        Отправьте боту команду /other — он пришлёт файл сообщением, оттуда его можно сохранить.
      </span>
      <div style={hintActionsStyle}>
        {isTelegram ? (
          <button type="button" className="btn btn--ghost" onClick={openBotChat}>
            💬 Открыть чат с ботом
          </button>
        ) : null}
        <button type="button" className="btn btn--ghost" onClick={() => setHintFileId(null)}>
          Понятно
        </button>
      </div>
    </div>
  )

  const renderFile = (file) => {
    const info = typeInfo(file.file_type)
    const playable = PLAYABLE_TYPES.has(String(file.file_type))
    const opened = openFileId === file.id
    const duration = file.duration_label && file.duration_label !== '—' ? file.duration_label : ''
    const size = Number(file.file_size) > 0 ? formatFileSize(file.file_size) : ''
    const created = formatDate(file.created_at)
    const meta = [size, duration, created !== '—' ? created : ''].filter(Boolean).join(' · ')
    const downloadHref = fileUrl(file)

    return (
      <div key={file.id}>
        <div className="list-item list-item--row">
          <button
            type="button"
            className="list-item__main"
            onClick={playable ? () => togglePreview(file) : handleDownload(file)}
            aria-label={
              playable
                ? `${opened ? 'Закрыть' : 'Воспроизвести'}: ${file.title}`
                : `Скачать: ${file.title}`
            }
          >
            <span className="list-item__icon" aria-hidden="true">
              {info.icon}
            </span>
            <span style={columnStyle}>
              <span className="list-item__title">{file.title}</span>
              <span style={metaRowStyle}>
                <span className={`badge file-type-badge file-type-badge--${typeModifier(file.file_type)}`}>
                  {info.label}
                </span>
                {meta ? <span className="muted">{meta}</span> : null}
                {searching && file.folder_name ? (
                  <span className="muted">📁 {file.folder_name}</span>
                ) : null}
              </span>
            </span>
          </button>

          <div className="list-item__actions">
            {playable ? (
              <button
                type="button"
                className="btn-icon"
                aria-pressed={opened}
                title={opened ? 'Остановить' : 'Воспроизвести'}
                aria-label={opened ? `Остановить ${file.title}` : `Воспроизвести ${file.title}`}
                onClick={() => togglePreview(file)}
              >
                {opened ? '⏹' : '▶️'}
              </button>
            ) : null}
            <a
              className="btn-icon"
              href={downloadHref}
              download
              title="Скачать"
              aria-label={`Скачать ${file.title}`}
              onClick={handleDownload(file)}
            >
              ⬇️
            </a>
          </div>
        </div>

        {opened ? renderPreview(file) : null}
        {hintFileId === file.id ? renderHint(file) : null}
      </div>
    )
  }

  return (
    <div className="page">
      <div className="section__header">
        <h1 className="section__title">📦 Другое</h1>
        {files.length > 0 || folders.length > 0 ? (
          <span className="muted">
            {folders.length > 0 ? `${formatCount(folders.length, FOLDER_FORMS)} · ` : ''}
            {formatCount(files.length, FILE_FORMS)}
            {hasMore ? '…' : ''}
          </span>
        ) : null}
      </div>

      <SearchBar
        value={query}
        onChange={setQuery}
        placeholder="Поиск файлов раздела «Другое»…"
      />

      <div className="chips" role="group" aria-label="Тип файла">
        {TYPE_FILTERS.map((filter) => (
          <button
            key={filter.key || 'all'}
            type="button"
            className={`chip${typeFilter === filter.key ? ' is-active' : ''}`}
            aria-pressed={typeFilter === filter.key}
            onClick={() => {
              hapticImpact('light')
              setTypeFilter(filter.key)
            }}
          >
            {filter.key ? `${typeInfo(filter.key).icon} ` : ''}
            {filter.label}
          </button>
        ))}
      </div>

      {searching ? (
        <p className="muted">Ищем по всему разделу «Другое» — папки временно скрыты.</p>
      ) : (
        <>
          <nav className="chips" aria-label="Хлебные крошки">
            <button
              type="button"
              className={`chip${folderId ? '' : ' is-active'}`}
              onClick={() => openFolder(null)}
            >
              📦 Все файлы
            </button>
            {crumbs.map((crumb, position) => (
              <button
                key={crumb.id}
                type="button"
                className={`chip${position === crumbs.length - 1 ? ' is-active' : ''}`}
                onClick={() => openFolder(crumb.id)}
              >
                📁 {crumb.name}
              </button>
            ))}
          </nav>

          {formOpen ? (
            <form className="form-row" onSubmit={handleCreateFolder}>
              <input
                className="input"
                style={growStyle}
                type="text"
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                placeholder={
                  currentFolder ? `Папка внутри «${currentFolder.name}»` : 'Название новой папки'
                }
                maxLength={NAME_MAX_LENGTH}
                disabled={creating}
                autoComplete="off"
                aria-label="Название новой папки"
              />
              <button
                type="submit"
                className="btn btn--primary"
                disabled={creating || !newName.trim()}
              >
                {creating ? 'Создаём…' : 'Создать'}
              </button>
              <button
                type="button"
                className="btn btn--ghost"
                disabled={creating}
                onClick={() => {
                  setFormOpen(false)
                  setNewName('')
                }}
              >
                Отмена
              </button>
            </form>
          ) : (
            <button
              type="button"
              className="btn btn--ghost btn--block"
              onClick={() => {
                hapticImpact('light')
                setFormOpen(true)
              }}
            >
              ➕ Новая папка{currentFolder ? ` в «${currentFolder.name}»` : ''}
            </button>
          )}
        </>
      )}

      {initialLoading ? <Loader label="Загружаем раздел…" /> : null}

      {!initialLoading && !searching && folders.length > 0 ? (
        <div className="list">
          {folders.map((folder) => (
            <button
              type="button"
              className="list-item"
              key={folder.id}
              onClick={() => openFolder(folder.id)}
              aria-label={`Открыть папку ${folder.name}`}
            >
              <span className="list-item__icon" aria-hidden="true">
                📁
              </span>
              <span className="list-item__title">{folder.name}</span>
              <span className="list-item__meta">
                {formatCount(folder.total_track_count ?? folder.track_count, FILE_FORMS)}
              </span>
            </button>
          ))}
        </div>
      ) : null}

      {!initialLoading && files.length > 0 ? (
        <div className="list">{files.map(renderFile)}</div>
      ) : null}

      {!initialLoading && hasMore ? (
        <div className="list-footer">
          <button
            type="button"
            className="btn btn--ghost btn--block"
            disabled={loadingMore}
            onClick={() => loadFiles(files.length)}
          >
            {loadingMore ? 'Загружаем…' : 'Показать ещё'}
          </button>
        </div>
      ) : null}

      {isEmpty ? (
        <EmptyState
          icon={loadError ? '⚠️' : searching ? '🔎' : typeFilter ? '🗂' : '📦'}
          title={
            loadError
              ? 'Не удалось загрузить раздел'
              : searching
                ? 'Ничего не нашлось'
                : typeFilter
                  ? `${typeInfo(typeFilter).plural}: пока пусто`
                  : currentFolder
                    ? `В папке «${currentFolder.name}» пусто`
                    : 'Здесь пока пусто'
          }
          description={
            loadError
              ? errorText(loadError, 'Проверьте подключение и попробуйте ещё раз.')
              : searching
                ? 'Попробуйте изменить запрос — поиск понимает опечатки, но не читает мысли.'
                : typeFilter
                  ? 'Выберите другой тип файлов или пришлите боту что-нибудь новое.'
                  : currentFolder
                    ? 'Создайте подпапку или перенесите сюда файлы командой /other в чате с ботом.'
                    : 'Пришлите боту документ, видео, кружочек или голосовое сообщение — файл сохранится в канале-хранилище и появится здесь.'
          }
          action={
            loadError ? (
              <button type="button" className="btn btn--primary" onClick={handleRetry}>
                Повторить
              </button>
            ) : searching ? (
              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => {
                  setQuery('')
                  setSearch('')
                }}
              >
                Очистить поиск
              </button>
            ) : typeFilter ? (
              <button type="button" className="btn btn--ghost" onClick={() => setTypeFilter('')}>
                Показать все файлы
              </button>
            ) : null
          }
        />
      ) : null}
    </div>
  )
}

export default OtherPage
