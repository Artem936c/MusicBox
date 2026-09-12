import Modal from './Modal'
import { hapticImpact } from '../telegram'

/**
 * Диалог подтверждения действия.
 * Пропсы (контракт):
 * ConfirmDialog({ open, title, message, confirmText = 'Удалить',
 *                 cancelText = 'Отмена', onConfirm, onCancel })
 *
 * Закрытие по подложке/Esc трактуется как отмена (onCancel).
 */
export function ConfirmDialog({
  open,
  title,
  message,
  confirmText = 'Удалить',
  cancelText = 'Отмена',
  onConfirm,
  onCancel,
}) {
  const handleCancel = () => {
    hapticImpact('light')
    onCancel?.()
  }

  const handleConfirm = () => {
    hapticImpact('medium')
    onConfirm?.()
  }

  const footer = (
    <div className="modal__actions">
      <button type="button" className="btn" onClick={handleCancel}>
        {cancelText}
      </button>
      <button type="button" className="btn btn--primary btn--danger" onClick={handleConfirm}>
        {confirmText}
      </button>
    </div>
  )

  return (
    <Modal open={open} title={title || 'Подтвердите действие'} onClose={handleCancel} footer={footer}>
      <p className="modal__message">{message || 'Вы уверены? Действие нельзя отменить.'}</p>
    </Modal>
  )
}

export default ConfirmDialog
