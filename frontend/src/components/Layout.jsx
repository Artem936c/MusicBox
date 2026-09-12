import { useCallback, useEffect, useMemo } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import Player from './Player'
import TabBar from './TabBar'
import { hapticImpact, hideBackButton, isTelegram, showBackButton } from '../telegram'
import { sectionByKey } from '../utils/sections'

/**
 * Каркас приложения.
 * Пропсы (контракт): Layout({ children })
 *
 * Шапка с названием текущего раздела (определяется по useLocation), кнопка «назад»
 * через системный Telegram BackButton (вне Telegram — обычная кнопка в шапке),
 * затем <main>, мини-плеер и нижняя навигация.
 */

/** Разделы нижней навигации — на них кнопка «назад» не нужна. */
const ROOT_PATHS = new Set(['/', '/stats', '/folders', '/search', '/playlists'])

/** Разбирает путь на сегменты: '/folders/12' -> ['folders', '12']. */
function pathSegments(pathname) {
  return String(pathname || '/')
    .split('/')
    .filter(Boolean)
}

/** Нормализует путь: убирает хвостовой слэш ('/folders/' -> '/folders'). */
function normalizePath(pathname) {
  const path = String(pathname || '/')
  if (path.length > 1 && path.endsWith('/')) return path.slice(0, -1)
  return path
}

/** Заголовок текущего раздела для шапки. */
export function resolveTitle(pathname) {
  const segments = pathSegments(pathname)
  const [root, param] = segments

  switch (root) {
    case undefined:
      return 'Главная'
    case 'stats':
      return 'Статистика'
    case 'section':
      return sectionByKey?.(param)?.title || 'Раздел'
    case 'folders':
      return param ? 'Папка' : 'Папки'
    case 'search':
      return 'Поиск'
    case 'tgsearch':
      return 'Поиск в Telegram'
    case 'playlists':
      return param ? 'Плейлист' : 'Плейлисты'
    case 'favourites':
      return 'Избранное'
    case 'artists':
      return param ? 'Исполнитель' : 'Исполнители'
    case 'settings':
      return 'Настройки'
    case 'tracks':
      return 'Треки'
    case 'other':
      return 'Другое'
    case 'notes':
      return param ? 'Заметка' : 'Заметки'
    case 'recommendations':
      return 'Рекомендации'
    default:
      return 'MusicBox'
  }
}

/**
 * Куда возвращаться, если истории переходов нет (открыли ссылку сразу вглубь).
 * null — значит это корневой раздел и кнопка «назад» не нужна.
 */
export function resolveParentPath(pathname) {
  const path = normalizePath(pathname)
  if (ROOT_PATHS.has(path)) return null

  const [root] = pathSegments(path)
  switch (root) {
    case 'section':
      return '/stats'
    case 'folders':
      return '/folders'
    case 'playlists':
      return '/playlists'
    case 'artists':
      return pathSegments(path).length > 1 ? '/artists' : '/'
    case 'notes':
      return pathSegments(path).length > 1 ? '/notes' : '/'
    case 'tgsearch':
      return '/search'
    default:
      return '/'
  }
}

export function Layout({ children }) {
  const location = useLocation()
  const navigate = useNavigate()

  const title = useMemo(() => resolveTitle(location.pathname), [location.pathname])
  const parentPath = useMemo(() => resolveParentPath(location.pathname), [location.pathname])
  const canGoBack = parentPath !== null

  const goBack = useCallback(() => {
    hapticImpact('light')
    const historyIndex = typeof window !== 'undefined' ? window.history.state?.idx : undefined
    if (typeof historyIndex === 'number' && historyIndex > 0) {
      navigate(-1)
      return
    }
    navigate(parentPath || '/', { replace: true })
  }, [navigate, parentPath])

  // Системная кнопка «Назад» Telegram: показываем только вне корневых разделов.
  useEffect(() => {
    if (canGoBack) {
      showBackButton(goBack)
    } else {
      hideBackButton()
    }
    return () => hideBackButton()
  }, [canGoBack, goBack])

  // Заголовок вкладки браузера — полезно при отладке вне Telegram.
  useEffect(() => {
    if (typeof document === 'undefined') return
    document.title = title === 'Главная' ? 'MusicBox' : `${title} · MusicBox`
  }, [title])

  const showFallbackBack = canGoBack && !isTelegram

  return (
    <div className="app">
      <header className="app-header">
        {showFallbackBack ? (
          <button type="button" className="app-header__back" onClick={goBack} aria-label="Назад">
            ←
          </button>
        ) : null}
        <h1 className="app-header__title">{title}</h1>
      </header>

      <main className="app-main">{children}</main>

      <Player />
      <TabBar />
    </div>
  )
}

export default Layout
