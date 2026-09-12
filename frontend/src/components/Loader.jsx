/**
 * Индикатор загрузки.
 * Пропсы (контракт): Loader({ label = 'Загрузка…' })
 */
export function Loader({ label = 'Загрузка…' }) {
  return (
    <div className="loader" role="status" aria-live="polite">
      <span className="loader__spinner" aria-hidden="true" />
      {label ? <span className="loader__label">{label}</span> : null}
    </div>
  )
}

export default Loader
