/**
 * HTTP-клиент REST API MusicBox.
 *
 * Каждый запрос уходит с заголовком X-Telegram-Init-Data — по нему backend
 * (`backend/api/deps.py::get_current_user`) определяет пользователя.
 * Любой ответ со статусом >= 400 превращается в ApiError с полями status и detail.
 *
 * Сигнатуры V1 (контракт V1, раздел 16) сохранены дословно; методы V2 (контракт
 * V2, раздел 6) добавлены рядом и помечены комментарием `--- V2 ---`. Там, где
 * V2 расширяет старый вызов (`folders.create`, `search.all`), второй аргумент
 * остаётся необязательным и принимает прежний тип — старые страницы не ломаются.
 */

import { getInitData } from '../telegram.js'

/** Базовый путь API; по умолчанию относительный '/api'. */
const BASE = import.meta.env.VITE_API_BASE || '/api'

/** Ошибка обращения к API. */
export class ApiError extends Error {
  constructor(message, status = 0, detail = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/**
 * Собирает query-строку, отбрасывая пустые значения.
 * Массивы склеиваются через запятую (так их ждёт backend, например chats).
 */
function buildQuery(params) {
  if (!params) return ''
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) {
      const joined = value
        .filter((item) => item !== undefined && item !== null && item !== '')
        .join(',')
      if (joined) search.append(key, joined)
      continue
    }
    if (typeof value === 'boolean') {
      search.append(key, value ? 'true' : 'false')
      continue
    }
    search.append(key, String(value))
  }
  const query = search.toString()
  return query ? `?${query}` : ''
}

/** Достаёт человекочитаемый текст ошибки из ответа FastAPI. */
function extractDetail(payload, fallback) {
  if (payload === undefined || payload === null) return fallback
  if (typeof payload === 'string') return payload || fallback
  const detail = payload.detail ?? payload.message ?? payload.error
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) {
    // Ошибки валидации pydantic: [{loc, msg, type}, ...]
    const messages = detail
      .map((item) => (typeof item === 'string' ? item : item?.msg))
      .filter(Boolean)
    if (messages.length) return messages.join('; ')
  }
  if (detail && typeof detail === 'object') {
    const nested = detail.msg ?? detail.message
    if (typeof nested === 'string' && nested) return nested
  }
  return fallback
}

/**
 * Базовый запрос к API.
 *
 * @param {string} path путь относительно BASE, например '/tracks/12'
 * @param {object} options { method, params, body, signal, headers }
 * @returns {Promise<any>} распарсенный JSON (или null для пустого тела)
 */
async function request(
  path,
  { method = 'GET', params = null, body = undefined, signal = null, headers = {} } = {},
) {
  const url = `${BASE}${path}${buildQuery(params)}`
  const requestHeaders = {
    Accept: 'application/json',
    'X-Telegram-Init-Data': getInitData(),
    ...headers,
  }
  const init = { method, headers: requestHeaders }
  if (signal) init.signal = signal
  if (body !== undefined) {
    requestHeaders['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }

  let response
  try {
    response = await fetch(url, init)
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new ApiError('Нет связи с сервером. Проверьте подключение к интернету.', 0, null)
  }

  // Ответы без тела (204/205) — возвращаем null.
  if (response.status === 204 || response.status === 205) {
    if (!response.ok) throw new ApiError(`Ошибка ${response.status}`, response.status, null)
    return null
  }

  const rawText = await response.text()
  let payload = null
  if (rawText) {
    try {
      payload = JSON.parse(rawText)
    } catch {
      payload = rawText
    }
  }

  if (!response.ok) {
    const fallback = `Ошибка ${response.status}`
    const message = extractDetail(payload, fallback)
    const detail = payload && typeof payload === 'object' ? payload.detail ?? message : message
    throw new ApiError(message, response.status, detail)
  }

  return payload
}

/**
 * Приводит список идентификаторов к массиву целых чисел.
 * Пустой/некорректный ввод превращается в пустой массив — backend ждёт список.
 */
function toIdList(value) {
  if (value === undefined || value === null) return []
  const source = Array.isArray(value) ? value : [value]
  const ids = []
  for (const item of source) {
    const id = Number(item)
    if (Number.isInteger(id) && id > 0 && !ids.includes(id)) ids.push(id)
  }
  return ids
}

/** Необязательный числовой идентификатор: '' и NaN превращаются в null. */
function toId(value) {
  if (value === undefined || value === null || value === '') return null
  const id = Number(value)
  return Number.isInteger(id) && id > 0 ? id : null
}

const get = (path, params = null, options = {}) => request(path, { ...options, method: 'GET', params })
const post = (path, body = null, params = null) => request(path, { method: 'POST', body: body ?? {}, params })
const patch = (path, body = null) => request(path, { method: 'PATCH', body: body ?? {} })
const put = (path, body = null) => request(path, { method: 'PUT', body: body ?? {} })
const del = (path, params = null) => request(path, { method: 'DELETE', params })

/** Публичный API — сигнатуры зафиксированы контрактом (раздел 16). */
export const api = {
  stats: {
    overview: (params = {}) => get('/stats/overview', params),
    recent: (params = {}) => get('/stats/recent', params),
    unplayed: (params = {}) => get('/stats/unplayed', params),
    frequent: (params = {}) => get('/stats/frequent', params),
    rare: (params = {}) => get('/stats/rare', params),
    top: (params = {}) => get('/stats/top', params),
    counts: () => get('/stats/counts'),
    /** Диспетчер по ключу раздела: top | recent | unplayed | frequent | rare. */
    section: (key, params = {}) => {
      switch (key) {
        case 'top':
          return api.stats.top(params)
        case 'recent':
          return api.stats.recent(params)
        case 'unplayed':
          return api.stats.unplayed(params)
        case 'frequent':
          return api.stats.frequent(params)
        case 'rare':
          return api.stats.rare(params)
        default:
          return Promise.reject(new ApiError(`Неизвестный раздел: ${key}`, 400, null))
      }
    },
  },

  tracks: {
    list: (params = {}) => get('/tracks', params),
    get: (id) => get(`/tracks/${id}`),
    play: (id, source = 'web') => post(`/tracks/${id}/play`, { source }),
    update: (id, data) => patch(`/tracks/${id}`, data),
    remove: (id, { delete_from_channel = false } = {}) =>
      del(`/tracks/${id}`, { delete_from_channel }),
    move: (id, folderId) => post(`/tracks/${id}/move`, { folder_id: folderId ?? null }),
    assignFolder: (id, folderName) => post(`/tracks/${id}/folder`, { folder_name: folderName }),
    toggleFavourite: (id) => post(`/tracks/${id}/favourite`),

    // --- V2 ---------------------------------------------------------------
    /** Очередь всех аудио пользователя для плеера: { order, limit, offset }. */
    playAll: (params = {}) => get('/tracks/play_all', params),
    /** Переименование трека — частный случай update. */
    rename: (id, title) => patch(`/tracks/${id}`, { title }),
    /** Полная замена списка исполнителей трека (первый — основной). */
    setArtists: (id, artistIds) => patch(`/tracks/${id}`, { artist_ids: toIdList(artistIds) }),
  },

  folders: {
    /**
     * Список папок. Без параметров — весь раздел «Музыка» (поведение V1).
     * params: { parent_id, section } — parent_id=0 только корневые, N — дети N;
     * section ∈ music | other | all.
     */
    list: (params = {}) => get('/folders', params),
    /**
     * Создание папки. V1: create(name). V2: create(name, { parentFolderId, section }).
     * Второй аргумент можно передать и числом — это будет родительская папка.
     */
    create: (name, options = {}) => {
      const config =
        options === null || typeof options !== 'object' ? { parentFolderId: options } : options
      const { parentFolderId = null, section = 'music' } = config
      return post('/folders', {
        name,
        parent_folder_id: toId(parentFolderId),
        section,
      })
    },
    get: (id) => get(`/folders/${id}`),
    tracks: (id, params = {}) => get(`/folders/${id}/tracks`, params),
    rename: (id, name) => patch(`/folders/${id}`, { name }),
    remove: (id, { delete_tracks = false, recursive = true } = {}) =>
      del(`/folders/${id}`, { delete_tracks, recursive }),
    /** Переносит указанные треки В папку с идентификатором id. */
    moveTracks: (id, trackIds) =>
      post(`/folders/${id}/tracks`, { track_ids: trackIds, folder_id: id }),

    // --- V2 ---------------------------------------------------------------
    /** Дерево папок раздела: у каждого узла есть children[]. */
    tree: (section = 'music') => get('/folders/tree', { section }),
    /** Хлебные крошки от корня: { folder_id, section, items: [...] }. */
    path: (id) => get(`/folders/${id}/path`),
    /** Перенос папки; parentId = null — в корень. 400, если получился бы цикл. */
    move: (id, parentId) => post(`/folders/${id}/move`, { parent_folder_id: toId(parentId) }),
    /** Очередь треков папки для плеера. */
    play: (id, recursive = true) => get(`/folders/${id}/play`, { recursive }),
  },

  playlists: {
    list: () => get('/playlists'),
    create: (name, description = null) => post('/playlists', { name, description }),
    get: (id) => get(`/playlists/${id}`),
    update: (id, data) => patch(`/playlists/${id}`, data),
    remove: (id) => del(`/playlists/${id}`),
    addTracks: (id, trackIds) => post(`/playlists/${id}/tracks`, { track_ids: trackIds }),
    removeTrack: (id, trackId) => del(`/playlists/${id}/tracks/${trackId}`),
    reorder: (id, trackIds) => put(`/playlists/${id}/order`, { track_ids: trackIds }),
  },

  favourites: {
    /** params: { q, limit, offset } — q включает нечёткий поиск по избранному (V2). */
    list: (params = {}) => get('/favourites', params),
    add: (id) => post(`/favourites/${id}`),
    remove: (id) => del(`/favourites/${id}`),
  },

  artists: {
    /** params: { q, only_listened, limit, offset } — q добавлен в V2. */
    list: (params = {}) => get('/artists', params),
    get: (id) => get(`/artists/${id}`),
    tracks: (id, params = {}) => get(`/artists/${id}/tracks`, params),
    /** isListened === null — backend переключает отметку сам. */
    setListened: (id, isListened = null) =>
      post(`/artists/${id}/listened`, { is_listened: isListened }),

    // --- V2 ---------------------------------------------------------------
    /** Ручное создание исполнителя; повторный вызов вернёт существующего. */
    create: (name, folderId = null) => post('/artists', { name, folder_id: toId(folderId) }),
    /** Переименование; при совпадении имён backend сливает исполнителей. */
    rename: (id, name) => patch(`/artists/${id}`, { name }),
    /** Привязка исполнителя к папке; folderId = null — отвязать. */
    setFolder: (id, folderId) => post(`/artists/${id}/folder`, { folder_id: toId(folderId) }),
    /** Ещё не прослушанные треки исполнителя; params: { limit, offset }. */
    unplayed: (id, params = {}) => get(`/artists/${id}/unplayed`, params),
  },

  albums: {
    list: () => get('/albums'),
    tracks: (id) => get(`/albums/${id}/tracks`),
  },

  search: {
    /**
     * Общий поиск. V1: all(q, 20). V2: all(q, { artistIds, section, limit }).
     * Второй аргумент числом по-прежнему означает limit — старые вызовы живы.
     * artistIds — пересечение: отбираются треки, у которых есть ВСЕ исполнители.
     */
    all: (q, options = {}) => {
      const config = typeof options === 'number' ? { limit: options } : options ?? {}
      const { limit = 20, artistIds = null, section = null } = config
      const ids = toIdList(artistIds)
      return get('/search', {
        q,
        limit,
        artist_ids: ids.length ? ids : null,
        section,
      })
    },
    tracks: (q, limit = 30) => get('/search/tracks', { q, limit }),
    albums: (q, limit = 30) => get('/search/albums', { q, limit }),
    telegram: (q, { limit, chats } = {}) => get('/search/telegram', { q, limit, chats }),
    importRemote: (token) => post('/search/telegram/import', { token }),
    importLink: (url) => post('/search/telegram/link', { url }),
  },

  settings: {
    get: () => get('/settings'),
    update: (data) => patch('/settings', data),
  },

  // --- V2 -----------------------------------------------------------------

  /**
   * Заметки со списками пунктов.
   * Все методы, кроме list/remove/removeItem, возвращают заметку целиком
   * (с массивом items) — страница может просто заменить состояние ответом.
   */
  notes: {
    list: () => get('/notes'),
    create: (title) => post('/notes', { title }),
    get: (id) => get(`/notes/${id}`),
    /** update(id, 'Новое название') или update(id, { title }). */
    update: (id, data) => patch(`/notes/${id}`, typeof data === 'string' ? { title: data } : data),
    remove: (id) => del(`/notes/${id}`),
    addItem: (id, text) => post(`/notes/${id}/items`, { text }),
    /**
     * updateItem(id, itemId, { text, is_done }).
     * Строка — только текст, булево — только отметка «сделано».
     */
    updateItem: (id, itemId, data) => {
      let body = data
      if (typeof data === 'string') body = { text: data }
      else if (typeof data === 'boolean') body = { is_done: data }
      return patch(`/notes/${id}/items/${itemId}`, body)
    },
    removeItem: (id, itemId) => del(`/notes/${id}/items/${itemId}`),
    /** Новый порядок пунктов целиком; дубликаты идентификаторов → 422. */
    reorder: (id, itemIds) => put(`/notes/${id}/order`, { item_ids: toIdList(itemIds) }),
  },

  /** Рекомендации: { popular, underground, shortfall, note }. */
  recommendations: {
    get: (limit = 15) => {
      const value = typeof limit === 'object' && limit !== null ? limit.limit : limit
      return get('/recommendations', { limit: value ?? 15 })
    },
  },

  /** Раздел «Другое»: документы, видео, кружочки и голосовые. */
  other: {
    /** params: { folder_id, q, file_type, recursive, limit, offset }. */
    list: (params = {}) => get('/other', params),
    /** Папки раздела; params: { parent_id } — 0 только корневые, N — дети N. */
    folders: (params = {}) => get('/other/folders', params),
    createFolder: (name, parentId = null) =>
      post('/other/folders', { name, parent_folder_id: toId(parentId) }),
    /** Прямая ссылка на файл для <a download> / <video>; '' — токена нет. */
    downloadUrl: (track, options = {}) => downloadUrl(track, options),
  },
}

/**
 * Ссылка на поток аудио для тега <audio>.
 * track.stream_url приходит от backend уже с подписанным токеном.
 */
export function streamUrl(track) {
  if (!track?.stream_url) return ''
  return (import.meta.env.VITE_API_ORIGIN || '') + track.stream_url
}

/**
 * Ссылка на файл раздела «Другое» (`GET /other/{id}/download?token=…`).
 *
 * Тег <a download> и <video> не умеют отправлять заголовок X-Telegram-Init-Data,
 * поэтому backend авторизует скачивание тем же подписанным токеном, что и
 * аудиопоток. Токен берём из stream_url карточки файла — отдельного поля нет.
 * Префикс пути тоже берём оттуда, чтобы ссылка жила при нестандартном BASE.
 *
 * @param {object} track карточка файла (нужны id и stream_url)
 * @param {{inline?: boolean}} options inline — открыть в браузере, а не скачать
 * @returns {string} готовый URL или '' если ссылку построить нельзя
 */
export function downloadUrl(track, { inline = false } = {}) {
  const id = Number(track?.id)
  if (!Number.isInteger(id) || id <= 0) return ''

  const raw = typeof track?.stream_url === 'string' ? track.stream_url : ''
  const separator = raw.indexOf('?')
  const streamPath = separator === -1 ? raw : raw.slice(0, separator)
  const streamQuery = separator === -1 ? '' : raw.slice(separator + 1)
  const token = new URLSearchParams(streamQuery).get('token') || ''
  if (!token) return ''

  const marker = streamPath.indexOf('/tracks/')
  const prefix = marker === -1 ? BASE : streamPath.slice(0, marker)
  const query = buildQuery({ token, inline: inline || null })
  return `${import.meta.env.VITE_API_ORIGIN || ''}${prefix}/other/${id}/download${query}`
}
