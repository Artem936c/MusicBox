import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'

import EmptyState from '../components/EmptyState'
import Loader from '../components/Loader'
import SearchBar from '../components/SearchBar'
import { api } from '../api/client'
import { useToast } from '../context/ToastContext'
import { hapticImpact, hapticNotification, tg } from '../telegram'
import { formatDuration, formatFileSize } from '../utils/format'

/**
 * Страница поиска аудио в Telegram (маршрут /tgsearch).
 *
 * Поиск по публичным каналам через `api.search.telegram(q)`; каждый результат
 * можно импортировать в хранилище кнопкой «⬇️ Добавить» (`api.search.importRemote`).
 * Отдельно — импорт по прямой ссылке вида https://t.me/<канал>/<id>
 * (`api.search.importLink`).
 *
 * Если backend отвечает 503, поиск по Telegram не настроен: показываем понятное
 * объяснение и предлагаем переслать аудио боту — этот путь работает всегда.
 *
 * Запрос можно передать снаружи: `/tgsearch?q=<имя>` или state роутера
 * (`{ q }` / `{ query }`) — так сюда приходит кнопка «🔎 Найти в Telegram» из
 * рекомендаций. Имя подставляется в поле и поиск запускается сразу, без
 * повторного нажатия кнопки.
 */

/** Сколько результатов запрашивать. */
const SEARCH_LIMIT = 20

/** Текст на случай, когда поиск по Telegram не настроен (503). */
const UNAVAILABLE_FALLBACK =
  'Поиск по Telegram не настроен. Перешлите аудио боту — я сохраню его в хранилище и разложу по папкам.'

/** Проверка, похожа ли строка на ссылку Telegram. */
const TELEGRAM_LINK_RE = /^(https?:\/\/)?(t\.me|telegram\.me)\/[^\s]+$/i

/** Человеческий текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  const detail = error?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  const message = error?.message
  if (typeof message === 'string' && message.trim()) return message
  return fallback
}

/** Открывает ссылку на сообщение: внутри Telegram — системным способом. */
function openTelegramLink(url) {
  if (!url) return
  hapticImpact('light')
  if (tg && typeof tg.openTelegramLink === 'function') {
    tg.openTelegramLink(url)
    return
  }
  if (typeof window !== 'undefined') {
    window.open(url, '_blank', 'noopener,noreferrer')
  }
}

/** Куда импортирован трек: сообщение для тоста. */
function importedMessage(track) {
  const folder = track?.folder_name
  if (folder) return `Трек добавлен в папку «${folder}»`
  return 'Трек добавлен в библиотеку'
}

/** Разбирает список каналов «@one, two» в массив ['@one', 'two']. */
function parseChats(value) {
  return String(value ?? '')
    .split(/[,;\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

export function TelegramSearchPage() {
  const { toast } = useToast()
  const [searchParams] = useSearchParams()
  const location = useLocation()

  // Запрос, пришедший с другой страницы. HashRouter кладёт `?q=` внутрь хэша,
  // поэтому читаем его роутер-хуком; state роутера поддерживаем как запасной
  // вариант — какой бы способ ни выбрала вызывающая страница.
  const incomingQuery = String(
    searchParams.get('q') || location.state?.q || location.state?.query || '',
  ).trim()

  const [query, setQuery] = useState(incomingQuery)
  const [chats, setChats] = useState('')
  const [results, setResults] = useState([])
  const [searched, setSearched] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  // Текст объяснения, если поиск по Telegram выключен на сервере (ответ 503).
  const [unavailable, setUnavailable] = useState(null)

  // Состояние импорта по каждому результату: { [token]: { status, message } }.
  const [imports, setImports] = useState({})

  const [linkUrl, setLinkUrl] = useState('')
  const [linkBusy, setLinkBusy] = useState(false)
  const [linkError, setLinkError] = useState(null)

  const searchDisabled = loading || !query.trim()

  /** Выполняет поиск по готовой строке: и из формы, и при запросе из `?q=`. */
  const runSearch = useCallback(
    async (text) => {
      hapticImpact('light')
      setLoading(true)
      setError(null)
      setSearched(true)
      try {
        const chatList = parseChats(chats)
        const found = await api.search.telegram(text, {
          limit: SEARCH_LIMIT,
          chats: chatList.length ? chatList : undefined,
        })
        const items = Array.isArray(found) ? found.filter(Boolean) : []
        setResults(items)
        setImports({})
        setUnavailable(null)
        if (!items.length) hapticNotification('warning')
      } catch (err) {
        setResults([])
        if (err?.status === 503) {
          setUnavailable(errorText(err, UNAVAILABLE_FALLBACK))
          setError(null)
        } else {
          setError(errorText(err, 'Не удалось выполнить поиск в Telegram'))
        }
        hapticNotification('error')
      } finally {
        setLoading(false)
      }
    },
    [chats],
  )

  const handleSearch = useCallback(
    (event) => {
      if (event) event.preventDefault()
      const text = query.trim()
      if (!text) {
        toast('Введите, что искать в Telegram', 'error')
        return
      }
      runSearch(text)
    },
    [query, runSearch, toast],
  )

  // Запрос из `?q=` / state выполняем сразу: пользователь уже нажал «Найти в
  // Telegram» на другой странице, просить его нажать ещё раз незачем. Ref
  // держит последний автозапуск, чтобы не повторять его на каждом рендере.
  const autoSearchedRef = useRef(null)

  useEffect(() => {
    if (!incomingQuery) return
    if (autoSearchedRef.current === incomingQuery) return
    autoSearchedRef.current = incomingQuery
    setQuery(incomingQuery)
    runSearch(incomingQuery)
  }, [incomingQuery, runSearch])

  /** Импорт найденного трека по токену результата. */
  const handleImport = useCallback(
    async (item) => {
      const token = item?.token
      if (!token) return
      if (imports[token]?.status === 'loading' || imports[token]?.status === 'done') return

      hapticImpact('light')
      setImports((prev) => ({ ...prev, [token]: { status: 'loading', message: 'Добавляем…' } }))
      try {
        const track = await api.search.importRemote(token)
        const message = importedMessage(track)
        setImports((prev) => ({ ...prev, [token]: { status: 'done', message } }))
        hapticNotification('success')
        toast(message, 'success')
      } catch (err) {
        const message =
          err?.status === 503
            ? errorText(err, UNAVAILABLE_FALLBACK)
            : errorText(err, 'Не удалось добавить трек')
        if (err?.status === 503) setUnavailable(message)
        setImports((prev) => ({ ...prev, [token]: { status: 'error', message } }))
        hapticNotification('error')
        toast(message, 'error')
      }
    },
    [imports, toast],
  )

  /** Импорт трека по прямой ссылке t.me/... */
  const handleImportLink = useCallback(
    async (event) => {
      if (event) event.preventDefault()
      const url = linkUrl.trim()
      if (!url) {
        setLinkError('Вставьте ссылку на сообщение с аудио')
        return
      }
      if (!TELEGRAM_LINK_RE.test(url)) {
        setLinkError('Ссылка должна выглядеть так: https://t.me/канал/123')
        return
      }
      hapticImpact('light')
      setLinkBusy(true)
      setLinkError(null)
      try {
        const track = await api.search.importLink(url)
        const message = importedMessage(track)
        hapticNotification('success')
        toast(message, 'success')
        setLinkUrl('')
      } catch (err) {
        const message =
          err?.status === 503
            ? errorText(err, UNAVAILABLE_FALLBACK)
            : errorText(err, 'Не удалось импортировать трек по ссылке')
        if (err?.status === 503) setUnavailable(message)
        setLinkError(message)
        hapticNotification('error')
      } finally {
        setLinkBusy(false)
      }
    },
    [linkUrl, toast],
  )

  const importedCount = useMemo(
    () => Object.values(imports).filter((state) => state?.status === 'done').length,
    [imports],
  )

  const nothingFound = searched && !loading && !error && !unavailable && results.length === 0

  return (
    <div className="page">
      <form className="section" onSubmit={handleSearch}>
        <SearchBar
          value={query}
          onChange={setQuery}
          placeholder="Название трека или исполнителя…"
          autoFocus
        />

        <div className="field">
          <label className="field__label" htmlFor="tgsearch-chats">
            Каналы для поиска (необязательно)
          </label>
          <input
            id="tgsearch-chats"
            className="input"
            type="text"
            value={chats}
            onChange={(event) => setChats(event.target.value)}
            placeholder="@musicchannel, @another_channel"
            autoComplete="off"
            autoCorrect="off"
            spellCheck={false}
            disabled={loading}
          />
          <span className="field__hint">
            Пусто — ищем в каналах, заданных в настройках бота.
          </span>
        </div>

        <button type="submit" className="btn btn--primary is-block" disabled={searchDisabled}>
          {loading ? 'Ищем в Telegram…' : '🔎 Найти'}
        </button>
      </form>

      {unavailable ? (
        <div className="card">
          <h2 className="card__title">🔌 Поиск по Telegram недоступен</h2>
          <p>{unavailable}</p>
          <p className="muted">
            Что можно сделать прямо сейчас: откройте чат с ботом, перешлите ему любое аудио —
            трек попадёт в хранилище, а автосортировка разложит его по папкам исполнителей.
            Ещё один способ — вставить ниже прямую ссылку на сообщение с аудио.
          </p>
        </div>
      ) : null}

      {loading ? <Loader label="Ищем аудио в Telegram…" /> : null}

      {error ? (
        <p className="error" role="alert">
          {error}
        </p>
      ) : null}

      {nothingFound ? (
        <EmptyState
          icon="🙃"
          title="Ничего не нашлось"
          description="Попробуйте другой запрос или укажите конкретный канал. Если трек у вас уже есть — поищите его в библиотеке."
          action={
            <Link className="btn" to="/search">
              🔍 Искать в библиотеке
            </Link>
          }
        />
      ) : null}

      {results.length > 0 ? (
        <section className="section">
          <div className="section__header">
            <h2 className="section__title">Результаты в Telegram</h2>
            <span className="muted">
              {importedCount > 0 ? `Добавлено: ${importedCount} из ${results.length}` : results.length}
            </span>
          </div>

          <div className="list">
            {results.map((item) => {
              const state = imports[item.token] || null
              const status = state?.status || 'idle'
              const meta = [
                item.duration_label || formatDuration(item.duration),
                item.file_size ? formatFileSize(item.file_size) : null,
                item.chat_title || null,
              ]
                .filter(Boolean)
                .join(' · ')

              let buttonLabel = '⬇️ Добавить'
              if (status === 'loading') buttonLabel = '⏳ Добавляем…'
              else if (status === 'done') buttonLabel = '✅ Добавлено'
              else if (status === 'error') buttonLabel = '🔁 Повторить'

              return (
                <div className="card" key={item.token}>
                  <div className="card__title">{item.title || 'Без названия'}</div>
                  <div className="muted">{item.performer || 'Неизвестный исполнитель'}</div>
                  <div className="muted">{meta}</div>

                  <div className="row row--between">
                    {item.link ? (
                      <button
                        type="button"
                        className="btn btn--ghost"
                        onClick={() => openTelegramLink(item.link)}
                      >
                        ↗️ Открыть в Telegram
                      </button>
                    ) : (
                      <span className="muted">Ссылка недоступна</span>
                    )}

                    <button
                      type="button"
                      className={status === 'done' ? 'btn' : 'btn btn--primary'}
                      onClick={() => handleImport(item)}
                      disabled={status === 'loading' || status === 'done'}
                    >
                      {buttonLabel}
                    </button>
                  </div>

                  {state?.message && status !== 'idle' ? (
                    <p className={status === 'error' ? 'error' : 'muted'} role="status">
                      {state.message}
                    </p>
                  ) : null}
                </div>
              )
            })}
          </div>
        </section>
      ) : null}

      <section className="section">
        <div className="section__header">
          <h2 className="section__title">Импорт по ссылке</h2>
        </div>
        <form className="card" onSubmit={handleImportLink}>
          <div className="field">
            <label className="field__label" htmlFor="tgsearch-link">
              Ссылка на сообщение с аудио
            </label>
            <input
              id="tgsearch-link"
              className="input"
              type="url"
              inputMode="url"
              value={linkUrl}
              onChange={(event) => {
                setLinkUrl(event.target.value)
                if (linkError) setLinkError(null)
              }}
              placeholder="https://t.me/канал/123"
              autoComplete="off"
              autoCorrect="off"
              spellCheck={false}
              disabled={linkBusy}
            />
            <span className="field__hint">
              Скопируйте ссылку на сообщение с аудио в публичном канале и вставьте сюда.
            </span>
          </div>

          {linkError ? (
            <p className="error" role="alert">
              {linkError}
            </p>
          ) : null}

          <button type="submit" className="btn btn--primary is-block" disabled={linkBusy}>
            {linkBusy ? 'Импортируем…' : '⬇️ Импортировать по ссылке'}
          </button>
        </form>
      </section>

      <p className="field__hint">
        Самый надёжный способ пополнить библиотеку — переслать боту аудиофайл в чат: он сохранит
        трек в хранилище и предложит папку исполнителя.
      </p>
    </div>
  )
}

export default TelegramSearchPage
