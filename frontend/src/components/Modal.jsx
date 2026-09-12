import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

/**
 * Универсальное модальное окно.
 * Пропсы (контракт): Modal({ open, title, onClose, children, footer = null })
 *
 * Возможности:
 *  - закрытие по клику на подложку (.modal__backdrop) и по клавише Esc;
 *  - блокировка скролла body, пока открыто хотя бы одно окно;
 *  - корректная работа вложенных окон (Esc закрывает только верхнее).
 */

/** Стек открытых окон: верхний элемент реагирует на Esc. */
const modalStack = []

/** Счётчик и исходное значение overflow — чтобы не сломать скролл вложенными окнами. */
let lockCount = 0
let previousBodyOverflow = ''
let previousBodyPaddingRight = ''

function lockBodyScroll() {
  if (typeof document === 'undefined') return
  lockCount += 1
  if (lockCount > 1) return
  const body = document.body
  previousBodyOverflow = body.style.overflow
  previousBodyPaddingRight = body.style.paddingRight
  // Компенсируем ширину системного скроллбара на десктопе, чтобы вёрстка не «прыгала».
  const scrollbarWidth = window.innerWidth - document.documentElement.clientWidth
  if (scrollbarWidth > 0) {
    body.style.paddingRight = `${scrollbarWidth}px`
  }
  body.style.overflow = 'hidden'
}

function unlockBodyScroll() {
  if (typeof document === 'undefined') return
  lockCount = Math.max(0, lockCount - 1)
  if (lockCount > 0) return
  const body = document.body
  body.style.overflow = previousBodyOverflow
  body.style.paddingRight = previousBodyPaddingRight
}

export function Modal({ open, title, onClose, children, footer = null }) {
  // Держим onClose в ref: так эффект не переподписывается на каждый рендер родителя.
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    if (!open || typeof document === 'undefined') return undefined

    const token = {}
    modalStack.push(token)
    lockBodyScroll()

    const handleKeyDown = (event) => {
      if (event.key !== 'Escape' && event.key !== 'Esc') return
      if (modalStack[modalStack.length - 1] !== token) return
      event.stopPropagation()
      onCloseRef.current?.()
    }

    document.addEventListener('keydown', handleKeyDown)

    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      const index = modalStack.indexOf(token)
      if (index >= 0) modalStack.splice(index, 1)
      unlockBodyScroll()
    }
  }, [open])

  if (!open || typeof document === 'undefined') return null

  const handleBackdropClick = () => {
    onCloseRef.current?.()
  }

  const content = (
    <div className="modal" role="dialog" aria-modal="true" aria-label={typeof title === 'string' ? title : undefined}>
      <div className="modal__backdrop" onClick={handleBackdropClick} />
      <div className="modal__body" role="document">
        <div className="modal__header">
          <div className="modal__title">{title}</div>
          <button type="button" className="modal__close" onClick={handleBackdropClick} aria-label="Закрыть">
            ✕
          </button>
        </div>
        <div className="modal__content">{children}</div>
        {footer ? <div className="modal__footer">{footer}</div> : null}
      </div>
    </div>
  )

  return createPortal(content, document.body)
}

export default Modal
