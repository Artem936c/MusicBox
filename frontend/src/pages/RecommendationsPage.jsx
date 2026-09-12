/**
 * Страница «Рекомендации» (маршрут `/recommendations`, ТЗ п. 2).
 *
 * Данные: `GET /recommendations?limit=15` → `api.recommendations.get(limit)`:
 * {
 *   popular:     [Recommendation…],   // «🔥 Популярные»
 *   underground: [Recommendation…],   // «💎 Менее известные»
 *   shortfall:   { popular: int, underground: int },  // сколько позиций не набрали
 *   note:        string | null                        // честное пояснение по-русски
 * }
 * Recommendation = { name, normalized_name, total_plays, listeners, reason,
 *                    local_artist_id, sample_track_ids }.
 *
 * Две вкладки-сегмента переключают категории. Карточка исполнителя показывает имя,
 * число прослушиваний, число слушателей и причину рекомендации; если исполнитель уже
 * есть в библиотеке (`local_artist_id`) — кнопка «К трекам» на `/artists/{id}`,
 * иначе — «Найти в Telegram»: переход на `/tgsearch` с копированием имени в буфер
 * обмена (см. комментарий к `handleTelegramSearch`).
 *
 * ЧЕСТНАЯ ДЕГРАДАЦИЯ (обязательна по ТЗ): бэкенд не выдумывает исполнителей и не
 * ходит во внешние сервисы, поэтому списки бывают короче запрошенного. Тогда в
 * ответе приходят `note` и/или ненулевой `shortfall` — страница ОБЯЗАНА показать это
 * заметным блоком `reco-note`, а не молча вывести короткий список. Если пользователь
 * ещё ничего не слушал, обе категории пустые и в `note` лежит подсказка — она
 * становится описанием пустого состояния.
 *
 * `playVersion` из `usePlayer()` стоит в зависимостях загрузки: рекомендации строятся
 * по прослушиваниям, поэтому после проигрывания трека подборка пересчитывается сама.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '../api/client.js'
import EmptyState from '../components/EmptyState.jsx'
import Loader from '../components/Loader.jsx'
import { usePlayer } from '../context/PlayerContext.jsx'
import { useToast } from '../context/ToastContext.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { hapticImpact } from '../telegram.js'
import { formatCount } from '../utils/format.js'

/** Сколько исполнителей просим в каждой категории. */
const LIMIT = 15

/** Формы слов для счётчиков. */
const PLAY_FORMS = ['прослушивание', 'прослушивания', 'прослушиваний']
const LISTENER_FORMS = ['слушатель', 'слушателя', 'слушателей']
const ARTIST_FORMS = ['исполнитель', 'исполнителя', 'исполнителей']
const POSITION_FORMS = ['позиции', 'позиций', 'позиций']

/** Две вкладки-сегмента: ключ совпадает с ключом в ответе API. */
const TABS = [
  {
    key: 'popular',
    icon: '🔥',
    label: 'Популярные',
    emptyTitle: 'Популярных пока не набралось',
    emptyText:
      'Это не ошибка: подборка строится только по вашей библиотеке и прослушиваниям других ' +
      'пользователей, а их пока мало. Послушайте ещё несколько треков — список появится.',
  },
  {
    key: 'underground',
    icon: '💎',
    label: 'Менее известные',
    emptyTitle: 'Менее известных пока не набралось',
    emptyText:
      'Сюда попадают исполнители, которых уже слушают, но редко. Пока таких не нашлось — ' +
      'подборка обновится, когда в базе станет больше прослушиваний.',
  },
]

/** Текст пустого состояния, если пользователь ещё ничего не слушал, а note не пришёл. */
const NO_HISTORY_FALLBACK =
  'Послушайте несколько треков, и я подберу похожих исполнителей по жанрам и вкусам ' +
  'других слушателей.'

/** Текст пояснения внутри `.reco-note`: у <p> сбрасываем внешние поля. */
const noteTextStyle = { margin: 0 }

/** Человеческий текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  const detail = error?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  const message = error?.message
  if (typeof message === 'string' && message.trim()) return message
  return fallback
}

/**
 * Запасное копирование через скрытое поле (старый `document.execCommand`).
 * Работает только внутри пользовательского жеста; ошибки гасим — вернём false.
 */
function copyViaTextarea(text) {
  if (typeof document === 'undefined' || !document.body) return false
  const area = document.createElement('textarea')
  try {
    area.value = text
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.top = '0'
    area.style.opacity = '0'
    area.style.pointerEvents = 'none'
    document.body.appendChild(area)
    area.select()
    return Boolean(document.execCommand('copy'))
  } catch {
    return false
  } finally {
    if (area.parentNode) area.parentNode.removeChild(area)
  }
}

/**
 * Копирует текст в буфер обмена. Промис НИКОГДА не отклоняется: в Telegram
 * WebView clipboard-API может быть запрещён — тогда вернётся false, и вызывающий
 * код честно скажет пользователю, что текст нужно набрать вручную.
 */
function copyToClipboard(text) {
  try {
    const clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : null
    if (clipboard && typeof clipboard.writeText === 'function') {
      return clipboard.writeText(text).then(
        () => true,
        () => false,
      )
    }
  } catch {
    /* clipboard-API недоступен — пробуем запасной путь */
  }
  return Promise.resolve(copyViaTextarea(text))
}

/** Безопасное целое неотрицательное число. */
function toCount(value) {
  const number = Math.floor(Number(value) || 0)
  return number > 0 ? number : 0
}

/** Список рекомендаций из ответа: только записи с именем. */
function toItems(value) {
  if (!Array.isArray(value)) return []
  return value.filter((item) => item && String(item.name || '').trim())
}

/** «12 прослушиваний» / «пока никто не слушал». */
function playsText(count) {
  const plays = toCount(count)
  return plays > 0 ? formatCount(plays, PLAY_FORMS) : 'пока никто не слушал'
}

/** «3 слушателя» / «слушателей пока нет». */
function listenersText(count) {
  const listeners = toCount(count)
  return listeners > 0 ? formatCount(listeners, LISTENER_FORMS) : 'слушателей пока нет'
}

/**
 * Запасной текст блока «reco-note», если backend прислал shortfall без note.
 * Никогда не преуменьшает: перечисляет обе категории.
 */
function fallbackNoteText(shortfall, limit) {
  const parts = TABS.filter((tab) => toCount(shortfall[tab.key]) > 0).map(
    (tab) =>
      `«${tab.icon} ${tab.label}» — ${formatCount(toCount(shortfall[tab.key]), POSITION_FORMS)}`,
  )
  if (!parts.length) return ''
  return (
    `Набрать по ${limit} исполнителей в обе категории не удалось: в базе пока мало ` +
    `пользователей и прослушиваний. Не хватило: ${parts.join(', ')}. ` +
    'Исполнители не выдумываются и не берутся из внешних сервисов — только из ваших данных.'
  )
}

export function RecommendationsPage() {
  const { toast } = useToast()
  const { playVersion } = usePlayer()
  const navigate = useNavigate()

  const [tab, setTab] = useState(TABS[0].key)

  // playVersion в зависимостях: подборка считается по прослушиваниям и должна
  // обновляться сразу после проигрывания трека.
  const { data, error, loading, reload } = useAsync(
    () => api.recommendations.get(LIMIT),
    [playVersion],
  )

  useEffect(() => {
    if (error) toast(errorText(error, 'Не удалось загрузить рекомендации'), 'error')
  }, [error, toast])

  const popular = useMemo(() => toItems(data?.popular), [data])
  const underground = useMemo(() => toItems(data?.underground), [data])

  const lists = useMemo(
    () => ({ popular, underground }),
    [popular, underground],
  )

  const shortfall = useMemo(
    () => ({
      popular: toCount(data?.shortfall?.popular),
      underground: toCount(data?.shortfall?.underground),
    }),
    [data],
  )

  const shortfallTotal = shortfall.popular + shortfall.underground
  const note = typeof data?.note === 'string' && data.note.trim() ? data.note.trim() : ''
  const totalCount = popular.length + underground.length

  const initialLoading = loading && data === null
  const hasError = Boolean(error) && data === null
  const isEmpty = !initialLoading && !hasError && totalCount === 0

  const activeTab = TABS.find((item) => item.key === tab) || TABS[0]
  const activeItems = lists[activeTab.key] || []

  // Если активная вкладка пуста, а во второй кто-то есть — честно подсказываем, где смотреть.
  const otherTab = TABS.find((item) => item.key !== activeTab.key) || TABS[1]
  const otherCount = (lists[otherTab.key] || []).length
  const emptyTabDescription = [
    note || activeTab.emptyText,
    otherCount > 0
      ? `Загляните во вкладку «${otherTab.icon} ${otherTab.label}» — там ${formatCount(
          otherCount,
          ARTIST_FORMS,
        )}.`
      : '',
  ]
    .filter(Boolean)
    .join(' ')

  const handleTab = useCallback((key) => {
    hapticImpact('light')
    setTab(key)
  }, [])

  const handleReload = useCallback(() => {
    hapticImpact('light')
    reload()
  }, [reload])

  /**
   * «Найти в Telegram»: открываем /tgsearch и помогаем перенести туда имя.
   *
   * ВАЖНО: страница /tgsearch (TelegramSearchPage) пока НЕ читает ни `?q=`, ни
   * state роутера — её поле поиска всегда стартует пустым, а поиск сам собой не
   * запускается. Поэтому имя копируется в буфер обмена, а тост говорит ровно то,
   * что произошло на самом деле: вставьте (или наберите) имя и нажмите «Найти».
   * Обещать запущенный поиск здесь нельзя — это было бы ложью пользователю.
   *
   * `?q=` и state оставлены намеренно: когда TelegramSearchPage научится читать
   * начальный запрос, поиск подхватится сам, без правок этой страницы.
   */
  const handleTelegramSearch = useCallback(
    (item) => {
      const name = String(item?.name || '').trim()
      if (!name) return
      hapticImpact('light')
      // Копируем ДО перехода: пользовательский жест ещё активен.
      copyToClipboard(name).then((copied) => {
        toast(
          copied
            ? `Имя «${name}» скопировано — вставьте его в поиск и нажмите «Найти»`
            : `Введите «${name}» в поиск по Telegram и нажмите «Найти»`,
          'info',
        )
      })
      navigate(`/tgsearch?q=${encodeURIComponent(name)}`, {
        state: { q: name, query: name, from: 'recommendations' },
      })
    },
    [navigate, toast],
  )

  return (
    <div className="page">
      <div className="section__header">
        <h1 className="section__title">✨ Рекомендации</h1>
        {activeItems.length > 0 ? (
          <span className="muted">{formatCount(activeItems.length, ARTIST_FORMS)}</span>
        ) : null}
      </div>

      {initialLoading ? <Loader label="Подбираем исполнителей…" /> : null}

      {!initialLoading && hasError ? (
        <EmptyState
          icon="⚠️"
          title="Не удалось загрузить рекомендации"
          description={errorText(error, 'Проверьте подключение и попробуйте ещё раз.')}
          action={
            <button type="button" className="btn btn--primary" onClick={handleReload}>
              Повторить
            </button>
          }
        />
      ) : null}

      {isEmpty ? (
        <EmptyState
          icon="🎧"
          title="Пока нечего рекомендовать"
          description={note || NO_HISTORY_FALLBACK}
          action={
            <div className="card-actions">
              <Link
                className="btn btn--primary"
                to="/tracks"
                onClick={() => hapticImpact('light')}
              >
                🎵 Послушать треки
              </Link>
              <button type="button" className="btn btn--ghost" onClick={handleReload}>
                ↻ Обновить
              </button>
            </div>
          }
        />
      ) : null}

      {!initialLoading && !hasError && !isEmpty ? (
        <>
          <div className="segmented" role="tablist" aria-label="Категории рекомендаций">
            {TABS.map((item) => {
              const isActive = item.key === activeTab.key
              return (
                <button
                  key={item.key}
                  type="button"
                  id={`reco-tab-${item.key}`}
                  role="tab"
                  aria-selected={isActive}
                  aria-controls={`reco-panel-${item.key}`}
                  className={isActive ? 'segmented__item is-active' : 'segmented__item'}
                  onClick={() => handleTab(item.key)}
                >
                  <span aria-hidden="true">{item.icon}</span>
                  <span>{item.label}</span>
                </button>
              )
            })}
          </div>

          {note || shortfallTotal > 0 ? (
            <div className="reco-note" role="note">
              <span aria-hidden="true">ℹ️</span>
              <div className="section">
                <p style={noteTextStyle}>{note || fallbackNoteText(shortfall, LIMIT)}</p>
                {shortfallTotal > 0 ? (
                  <div className="chips">
                    {TABS.map((item) =>
                      shortfall[item.key] > 0 ? (
                        <span className="badge" key={item.key}>
                          {item.icon} {item.label}: не хватило {shortfall[item.key]} из {LIMIT}
                        </span>
                      ) : null,
                    )}
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}

          <div
            className="section"
            id={`reco-panel-${activeTab.key}`}
            role="tabpanel"
            aria-labelledby={`reco-tab-${activeTab.key}`}
          >
            {activeItems.length > 0 ? (
              activeItems.map((item, index) => {
                const name = String(item.name).trim()
                const localId = toCount(item.local_artist_id)
                const reason = String(item.reason || '').trim()
                return (
                  <article
                    className="reco-card"
                    key={`${activeTab.key}-${item.normalized_name || name}-${index}`}
                  >
                    <h2 className="reco-card__name">{name}</h2>
                    <div className="reco-card__meta">
                      <span>
                        <span aria-hidden="true">🎧 </span>
                        {playsText(item.total_plays)}
                      </span>
                      <span>
                        <span aria-hidden="true">👥 </span>
                        {listenersText(item.listeners)}
                      </span>
                    </div>
                    {reason ? (
                      <div className="reco-card__reason">
                        <span aria-hidden="true">💡</span>
                        <span>{reason}</span>
                      </div>
                    ) : null}
                    <div className="card-actions">
                      {localId ? (
                        <Link
                          className="btn btn--primary"
                          to={`/artists/${localId}`}
                          onClick={() => hapticImpact('light')}
                          aria-label={`Открыть треки исполнителя ${name}`}
                        >
                          🎵 К трекам
                        </Link>
                      ) : (
                        <button
                          type="button"
                          className="btn btn--ghost"
                          onClick={() => handleTelegramSearch(item)}
                          aria-label={`Найти ${name} в Telegram`}
                        >
                          🔎 Найти в Telegram
                        </button>
                      )}
                    </div>
                  </article>
                )
              })
            ) : (
              <EmptyState
                icon={activeTab.icon}
                title={activeTab.emptyTitle}
                description={emptyTabDescription}
                action={
                  <button type="button" className="btn btn--ghost" onClick={handleReload}>
                    ↻ Обновить
                  </button>
                }
              />
            )}
          </div>
        </>
      ) : null}
    </div>
  )
}

export default RecommendationsPage
