import { useEffect, useRef } from 'react'
import { hapticImpact } from '../telegram'

/**
 * Поле поиска с кнопкой очистки.
 * Пропсы (контракт): SearchBar({ value, onChange, placeholder = 'Поиск…', autoFocus = false })
 *
 * onChange получает НОВУЮ СТРОКУ (не событие) — так же вызывается при очистке крестиком.
 */
export function SearchBar({ value, onChange, placeholder = 'Поиск…', autoFocus = false }) {
  const inputRef = useRef(null)

  useEffect(() => {
    if (!autoFocus) return
    // Небольшая задержка нужна, чтобы фокус не сбивался анимацией открытия окна.
    const timer = setTimeout(() => inputRef.current?.focus(), 60)
    return () => clearTimeout(timer)
  }, [autoFocus])

  const text = value ?? ''

  const handleChange = (event) => {
    onChange?.(event.target.value)
  }

  const handleClear = () => {
    hapticImpact('light')
    onChange?.('')
    inputRef.current?.focus()
  }

  return (
    <div className="searchbar">
      <span className="searchbar__icon" aria-hidden="true">
        🔎
      </span>
      <input
        ref={inputRef}
        className="searchbar__input"
        type="text"
        inputMode="search"
        enterKeyHint="search"
        value={text}
        onChange={handleChange}
        placeholder={placeholder}
        autoComplete="off"
        autoCorrect="off"
        spellCheck={false}
        aria-label={placeholder}
      />
      {text ? (
        <button type="button" className="searchbar__clear" onClick={handleClear} aria-label="Очистить поиск">
          ✕
        </button>
      ) : null}
    </div>
  )
}

export default SearchBar
