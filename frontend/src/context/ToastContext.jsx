/**
 * Всплывающие уведомления (тосты) MusicBox.
 *
 * Провайдер рендерит фиксированный контейнер поверх приложения; каждый тост
 * автоматически исчезает через 3 секунды, по клику закрывается сразу.
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

const ToastContext = createContext(null)

/** Время жизни тоста, мс. */
const TOAST_TTL = 3000

/** Значок по типу уведомления. */
const TOAST_ICONS = {
  info: 'ℹ️',
  success: '✅',
  error: '⚠️',
}

/** Заглушка для использования вне провайдера (стабильная ссылка). */
const FALLBACK_TOAST = {
  toast: (message, type = 'info') => {
    // eslint-disable-next-line no-console
    console.warn(`MusicBox [${type}]: ${message}`)
    return null
  },
  dismiss: () => {},
  toasts: [],
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const timers = useRef(new Map())
  const counter = useRef(0)

  const dismiss = useCallback((id) => {
    const timer = timers.current.get(id)
    if (timer) {
      window.clearTimeout(timer)
      timers.current.delete(id)
    }
    setToasts((items) => items.filter((item) => item.id !== id))
  }, [])

  /** Показать уведомление. type: info | success | error. */
  const toast = useCallback(
    (message, type = 'info') => {
      const text = typeof message === 'string' ? message : String(message ?? '')
      if (!text.trim()) return null
      counter.current += 1
      const id = `toast-${counter.current}`
      const kind = TOAST_ICONS[type] ? type : 'info'
      setToasts((items) => [...items, { id, text, type: kind }])
      const timer = window.setTimeout(() => {
        timers.current.delete(id)
        setToasts((items) => items.filter((item) => item.id !== id))
      }, TOAST_TTL)
      timers.current.set(id, timer)
      return id
    },
    [],
  )

  // Снимаем таймеры при размонтировании провайдера.
  useEffect(() => {
    const store = timers.current
    return () => {
      store.forEach((timer) => window.clearTimeout(timer))
      store.clear()
    }
  }, [])

  const value = useMemo(() => ({ toast, dismiss, toasts }), [toast, dismiss, toasts])

  return (
    <ToastContext.Provider value={value}>
      {children}
      {/* pointer-events отключены у контейнера, чтобы пустая область не ловила клики. */}
      <div
        className="toast-container"
        aria-live="polite"
        aria-atomic="false"
        style={{ pointerEvents: 'none' }}
      >
        {toasts.map((item) => (
          <button
            key={item.id}
            type="button"
            className={`toast toast--${item.type}`}
            onClick={() => dismiss(item.id)}
            title="Закрыть уведомление"
            style={{ pointerEvents: 'auto' }}
          >
            <span className="toast__icon" aria-hidden="true">
              {TOAST_ICONS[item.type]}
            </span>
            <span className="toast__text">{item.text}</span>
          </button>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

/**
 * Доступ к тостам: { toast(message, type = 'info'), dismiss(id), toasts }.
 * Вне провайдера возвращает безопасную заглушку — приложение не падает.
 */
export function useToast() {
  const context = useContext(ToastContext)
  return context || FALLBACK_TOAST
}

export { ToastContext }
export default ToastProvider
