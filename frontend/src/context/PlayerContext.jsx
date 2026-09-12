/**
 * Глобальный плеер MusicBox.
 *
 * В приложении существует ровно один тег <audio>: он рендерится компонентом
 * `components/Player.jsx`, но полностью управляется этим контекстом — Player
 * лишь привязывает полученный отсюда `audioRef` к своему <audio ref={audioRef}>.
 *
 * Ключевое правило учёта статистики: ПЕРЕД стартом воспроизведения каждого трека
 * (клик по треку, кнопки ⏮/⏭, автопереход по событию 'ended') вызывается
 * `api.tracks.play(track.id, 'web')`. Ошибка запроса только логируется и не мешает
 * воспроизведению; после успешного ответа `playVersion` увеличивается на 1 —
 * страницы статистики держат его в зависимостях useEffect и обновляются сами.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'

import { api, streamUrl } from '../api/client.js'
import { useToast } from './ToastContext.jsx'

const PlayerContext = createContext(null)

/** Абсолютный адрес обложки (backend отдаёт относительный путь с токеном). */
function coverUrl(track) {
  if (!track || !track.cover_url) return null
  return (import.meta.env.VITE_API_ORIGIN || '') + track.cover_url
}

/** Безопасное число секунд (NaN/Infinity/отрицательные → 0). */
function safeSeconds(value) {
  const num = Number(value)
  if (!Number.isFinite(num) || num < 0) return 0
  return num
}

const noop = () => {}

/** Заглушка для использования вне провайдера (стабильная ссылка). */
const FALLBACK_PLAYER = {
  audioRef: { current: null },
  current: null,
  queue: [],
  index: 0,
  isPlaying: false,
  progress: 0,
  duration: 0,
  repeat: false,
  shuffle: false,
  playVersion: 0,
  playTrack: noop,
  playQueue: noop,
  toggle: noop,
  next: noop,
  prev: noop,
  seek: noop,
  stop: noop,
  setRepeat: noop,
  setShuffle: noop,
}

export function PlayerProvider({ children }) {
  const { toast } = useToast()

  // Единственный <audio> приложения; тег создаётся в Player.
  const audioRef = useRef(null)

  const [current, setCurrent] = useState(null)
  const [queue, setQueue] = useState([])
  const [index, setIndex] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)
  const [progress, setProgress] = useState(0)
  const [duration, setDuration] = useState(0)
  const [repeat, setRepeat] = useState(false)
  const [shuffle, setShuffle] = useState(false)
  const [playVersion, setPlayVersion] = useState(0)

  // Зеркала состояния для обработчиков событий <audio> (без устаревших замыканий).
  const currentRef = useRef(null)
  const queueRef = useRef([])
  const indexRef = useRef(0)
  const durationRef = useRef(0)
  const repeatRef = useRef(false)
  const shuffleRef = useRef(false)
  // true — текущий трек доиграл до конца (очередь остановлена): следующий пуск
  // считается НОВЫМ прослушиванием, а не продолжением после паузы.
  const finishedRef = useRef(false)

  // Player монтируется ниже провайдера — ждём появления тега <audio>.
  const [audioReady, setAudioReady] = useState(false)

  useEffect(() => {
    repeatRef.current = repeat
  }, [repeat])

  useEffect(() => {
    shuffleRef.current = shuffle
  }, [shuffle])

  useEffect(() => {
    if (audioRef.current) {
      setAudioReady(true)
      return undefined
    }
    let frame = 0
    const check = () => {
      if (audioRef.current) {
        setAudioReady(true)
        return
      }
      frame = window.requestAnimationFrame(check)
    }
    frame = window.requestAnimationFrame(check)
    return () => window.cancelAnimationFrame(frame)
  }, [])

  /** Учёт прослушивания. Не блокирует воспроизведение; ошибку только логируем. */
  const registerPlay = useCallback((track) => {
    if (!track || track.id === undefined || track.id === null) return
    Promise.resolve()
      .then(() => api.tracks.play(track.id, 'web'))
      .then(() => {
        setPlayVersion((value) => value + 1)
      })
      .catch((error) => {
        // eslint-disable-next-line no-console
        console.warn('MusicBox: не удалось учесть прослушивание', error)
      })
  }, [])

  /**
   * Запускает трек: сначала регистрирует прослушивание, затем стартует <audio>.
   * Вызов api.tracks.play намеренно не ожидается — иначе браузер потеряет
   * пользовательский жест и заблокирует автозапуск.
   */
  const startTrack = useCallback(
    (track, list, startIndex) => {
      if (!track) return
      const nextQueue = Array.isArray(list) && list.length ? list : [track]
      let nextIndex = Number.isInteger(startIndex) ? startIndex : 0
      if (nextIndex < 0 || nextIndex >= nextQueue.length) nextIndex = 0

      currentRef.current = track
      queueRef.current = nextQueue
      indexRef.current = nextIndex
      durationRef.current = safeSeconds(track.duration)
      finishedRef.current = false

      setCurrent(track)
      setQueue(nextQueue)
      setIndex(nextIndex)
      setProgress(0)
      setDuration(safeSeconds(track.duration))

      registerPlay(track)

      const audio = audioRef.current
      if (!audio) {
        setIsPlaying(false)
        return
      }
      const src = streamUrl(track)
      if (!src) {
        setIsPlaying(false)
        toast('У трека нет ссылки на аудиопоток', 'error')
        return
      }
      try {
        audio.src = src
        audio.load()
        const promise = audio.play()
        if (promise && typeof promise.catch === 'function') {
          promise.catch((error) => {
            // Смена src во время воспроизведения даёт AbortError — это не ошибка.
            if (error && error.name === 'AbortError') return
            setIsPlaying(false)
            // eslint-disable-next-line no-console
            console.warn('MusicBox: воспроизведение не началось', error)
            toast('Не удалось начать воспроизведение', 'error')
          })
        }
      } catch (error) {
        setIsPlaying(false)
        // eslint-disable-next-line no-console
        console.warn('MusicBox: ошибка запуска аудио', error)
        toast('Не удалось начать воспроизведение', 'error')
      }
    },
    [registerPlay, toast],
  )

  const startTrackRef = useRef(startTrack)
  useEffect(() => {
    startTrackRef.current = startTrack
  }, [startTrack])

  /** Пауза и сброс позиции — конец очереди без повтора. */
  const haltPlayback = useCallback(() => {
    const audio = audioRef.current
    if (audio) {
      try {
        audio.pause()
        audio.currentTime = 0
      } catch (error) {
        // eslint-disable-next-line no-console
        console.warn('MusicBox: не удалось остановить аудио', error)
      }
    }
    // Трек доигран, а не поставлен на паузу: currentRef/audio.src сохраняются,
    // поэтому помечаем состояние, чтобы повторный пуск учли как прослушивание.
    finishedRef.current = true
    setIsPlaying(false)
    setProgress(0)
  }, [])

  /**
   * Следующий индекс очереди.
   * @param {boolean} auto true — автопереход после 'ended' (в конце очереди
   *   без repeat воспроизведение останавливается), false — нажатие «Вперёд»
   *   (очередь закольцована всегда).
   */
  const pickNextIndex = useCallback((auto) => {
    const list = queueRef.current
    if (!list.length) return -1
    if (list.length === 1) {
      if (repeatRef.current || !auto) return 0
      return -1
    }
    if (shuffleRef.current) {
      let candidate = indexRef.current
      for (let attempt = 0; attempt < 20 && candidate === indexRef.current; attempt += 1) {
        candidate = Math.floor(Math.random() * list.length)
      }
      return candidate
    }
    const next = indexRef.current + 1
    if (next < list.length) return next
    if (repeatRef.current || !auto) return 0
    return -1
  }, [])

  const advance = useCallback(
    (auto) => {
      const list = queueRef.current
      const nextIndex = pickNextIndex(auto)
      if (nextIndex < 0 || !list[nextIndex]) {
        haltPlayback()
        return
      }
      startTrackRef.current(list[nextIndex], list, nextIndex)
    },
    [haltPlayback, pickNextIndex],
  )

  const advanceRef = useRef(advance)
  useEffect(() => {
    advanceRef.current = advance
  }, [advance])

  const seek = useCallback((seconds) => {
    const audio = audioRef.current
    if (!audio) return
    const total =
      Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : durationRef.current
    const target = Math.max(0, Math.min(safeSeconds(seconds), total > 0 ? total : safeSeconds(seconds)))
    try {
      audio.currentTime = target
      // Перемотка внутрь доигранного трека — пользователь хочет продолжить
      // с выбранной позиции, а не слушать трек заново.
      if (target > 0) finishedRef.current = false
      setProgress(target)
    } catch (error) {
      // eslint-disable-next-line no-console
      console.warn('MusicBox: перемотка недоступна', error)
    }
  }, [])

  const toggle = useCallback(() => {
    const audio = audioRef.current
    if (!audio || !currentRef.current) return
    // Трек уже доиграл до конца — это новый старт, а не продолжение после паузы:
    // идём через startTrack, чтобы прослушивание попало в статистику.
    if (finishedRef.current) {
      startTrackRef.current(currentRef.current, queueRef.current, indexRef.current)
      return
    }
    if (audio.paused) {
      const promise = audio.play()
      if (promise && typeof promise.catch === 'function') {
        promise.catch((error) => {
          if (error && error.name === 'AbortError') return
          setIsPlaying(false)
          // eslint-disable-next-line no-console
          console.warn('MusicBox: воспроизведение не возобновилось', error)
          toast('Не удалось продолжить воспроизведение', 'error')
        })
      }
    } else {
      audio.pause()
    }
  }, [toast])

  const next = useCallback(() => {
    if (!currentRef.current) return
    advance(false)
  }, [advance])

  const prev = useCallback(() => {
    const audio = audioRef.current
    if (!currentRef.current) return
    // Первые 3 секунды — «в начало трека», дальше — предыдущий трек очереди.
    if (audio && audio.currentTime > 3) {
      seek(0)
      return
    }
    const list = queueRef.current
    if (list.length <= 1) {
      seek(0)
      return
    }
    let target = indexRef.current - 1
    if (target < 0) target = list.length - 1
    if (!list[target]) {
      seek(0)
      return
    }
    startTrack(list[target], list, target)
  }, [seek, startTrack])

  const stop = useCallback(() => {
    const audio = audioRef.current
    if (audio) {
      try {
        audio.pause()
        audio.removeAttribute('src')
        audio.load()
      } catch (error) {
        // eslint-disable-next-line no-console
        console.warn('MusicBox: не удалось освободить аудио', error)
      }
    }
    currentRef.current = null
    queueRef.current = []
    indexRef.current = 0
    durationRef.current = 0
    finishedRef.current = false
    setCurrent(null)
    setQueue([])
    setIndex(0)
    setIsPlaying(false)
    setProgress(0)
    setDuration(0)
  }, [])

  const playTrack = useCallback(
    (track, nextQueue = null, startIndex = 0) => {
      if (!track) return
      const list =
        Array.isArray(nextQueue) && nextQueue.length ? nextQueue.filter(Boolean) : [track]
      let position = Number.isInteger(startIndex) ? startIndex : 0
      if (!list[position] || list[position].id !== track.id) {
        const found = list.findIndex((item) => item && item.id === track.id)
        position = found >= 0 ? found : 0
      }
      // Повторный клик по играющему треку — пауза/продолжение, без нового учёта.
      // Но если трек уже доиграл до конца, это новый старт — со своим учётом.
      const audio = audioRef.current
      if (
        currentRef.current &&
        currentRef.current.id === track.id &&
        audio &&
        audio.src &&
        !finishedRef.current
      ) {
        queueRef.current = list
        indexRef.current = position
        setQueue(list)
        setIndex(position)
        toggle()
        return
      }
      startTrack(track, list, position)
    },
    [startTrack, toggle],
  )

  const playQueue = useCallback(
    (tracks, startIndex = 0) => {
      const list = Array.isArray(tracks) ? tracks.filter(Boolean) : []
      if (!list.length) {
        toast('Список пуст — нечего воспроизводить', 'info')
        return
      }
      let position = Number.isInteger(startIndex) ? startIndex : 0
      if (position < 0 || position >= list.length) position = 0
      startTrack(list[position], list, position)
    },
    [startTrack, toast],
  )

  // Обработчики событий <audio>: timeupdate, loadedmetadata, ended, error и др.
  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return undefined

    const handleTimeUpdate = () => {
      setProgress(safeSeconds(audio.currentTime))
    }
    const handleLoadedMetadata = () => {
      const value =
        Number.isFinite(audio.duration) && audio.duration > 0
          ? audio.duration
          : safeSeconds(currentRef.current && currentRef.current.duration)
      durationRef.current = value
      setDuration(value)
    }
    const handlePlay = () => setIsPlaying(true)
    const handlePause = () => setIsPlaying(false)
    const handleEnded = () => {
      setIsPlaying(false)
      setProgress(0)
      advanceRef.current(true)
    }
    const handleError = () => {
      // Пустой src после stop() ошибкой не считаем.
      if (!audio.getAttribute('src')) return
      setIsPlaying(false)
      // eslint-disable-next-line no-console
      console.warn('MusicBox: ошибка загрузки аудио', audio.error)
      toast('Не удалось загрузить аудио. Попробуйте ещё раз', 'error')
    }

    audio.addEventListener('timeupdate', handleTimeUpdate)
    audio.addEventListener('loadedmetadata', handleLoadedMetadata)
    audio.addEventListener('durationchange', handleLoadedMetadata)
    audio.addEventListener('play', handlePlay)
    audio.addEventListener('playing', handlePlay)
    audio.addEventListener('pause', handlePause)
    audio.addEventListener('ended', handleEnded)
    audio.addEventListener('error', handleError)

    return () => {
      audio.removeEventListener('timeupdate', handleTimeUpdate)
      audio.removeEventListener('loadedmetadata', handleLoadedMetadata)
      audio.removeEventListener('durationchange', handleLoadedMetadata)
      audio.removeEventListener('play', handlePlay)
      audio.removeEventListener('playing', handlePlay)
      audio.removeEventListener('pause', handlePause)
      audio.removeEventListener('ended', handleEnded)
      audio.removeEventListener('error', handleError)
    }
  }, [audioReady, toast])

  // Media Session API: название/исполнитель/обложка на экране блокировки.
  useEffect(() => {
    if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return
    const session = navigator.mediaSession
    try {
      if (!current) {
        session.metadata = null
        session.playbackState = 'none'
        return
      }
      if (typeof window.MediaMetadata === 'function') {
        const cover = coverUrl(current)
        session.metadata = new window.MediaMetadata({
          title: current.title || 'Без названия',
          artist: current.artist || 'Неизвестный исполнитель',
          album: current.album || 'MusicBox',
          artwork: cover ? [{ src: cover, sizes: '320x320', type: 'image/jpeg' }] : [],
        })
      }
      session.playbackState = isPlaying ? 'playing' : 'paused'
    } catch (error) {
      // eslint-disable-next-line no-console
      console.warn('MusicBox: Media Session недоступна', error)
    }
  }, [current, isPlaying])

  // Кнопки системного плеера (наушники, экран блокировки).
  useEffect(() => {
    if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return undefined
    const session = navigator.mediaSession
    const handlers = [
      ['play', () => toggle()],
      ['pause', () => toggle()],
      ['previoustrack', () => prev()],
      ['nexttrack', () => next()],
      [
        'seekto',
        (details) => {
          if (details && typeof details.seekTime === 'number') seek(details.seekTime)
        },
      ],
      ['stop', () => stop()],
    ]
    handlers.forEach(([action, handler]) => {
      try {
        session.setActionHandler(action, handler)
      } catch (error) {
        // Часть действий не поддерживается браузером — это нормально.
      }
    })
    return () => {
      handlers.forEach(([action]) => {
        try {
          session.setActionHandler(action, null)
        } catch (error) {
          // Игнорируем: обработчик уже снят.
        }
      })
    }
  }, [next, prev, seek, stop, toggle])

  const value = useMemo(
    () => ({
      audioRef,
      current,
      queue,
      index,
      isPlaying,
      progress,
      duration,
      repeat,
      shuffle,
      playVersion,
      playTrack,
      playQueue,
      toggle,
      next,
      prev,
      seek,
      stop,
      setRepeat,
      setShuffle,
    }),
    [
      current,
      queue,
      index,
      isPlaying,
      progress,
      duration,
      repeat,
      shuffle,
      playVersion,
      playTrack,
      playQueue,
      toggle,
      next,
      prev,
      seek,
      stop,
    ],
  )

  return <PlayerContext.Provider value={value}>{children}</PlayerContext.Provider>
}

/** Доступ к плееру. Вне провайдера возвращает безопасную «пустышку». */
export function usePlayer() {
  const context = useContext(PlayerContext)
  return context || FALLBACK_PLAYER
}

export { PlayerContext }
export default PlayerProvider
