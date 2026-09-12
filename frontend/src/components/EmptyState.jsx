/**
 * Дружелюбное пустое состояние: иконка, заголовок, пояснение и необязательное действие.
 * Пропсы (контракт): EmptyState({ icon = '🎧', title, description = null, action = null })
 *
 * `action` — готовый React-узел (например, кнопка), рендерится под описанием.
 */
export function EmptyState({ icon = '🎧', title, description = null, action = null }) {
  return (
    <div className="empty">
      {icon ? (
        <div className="empty__icon" aria-hidden="true">
          {icon}
        </div>
      ) : null}
      <div className="empty__title">{title || 'Пока пусто'}</div>
      {description ? <p className="empty__description">{description}</p> : null}
      {action ? <div className="empty__action">{action}</div> : null}
    </div>
  )
}

export default EmptyState
