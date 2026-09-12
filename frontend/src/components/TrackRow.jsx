/**
 * Строка трека (или файла раздела «Другое») в списке.
 *
 * Пропсы V1 зафиксированы контрактом (раздел 16) и сохранены дословно:
 * TrackRow({ track, index = null, queue = null, showStats = true, actions = [],
 *            onChanged = null, dragHandle = null, selected = false })
 *  - клик по строке → player.playTrack(track, queue ?? [track], index ?? 0)
 *  - встроенная кнопка ⭐ → api.tracks.toggleFavourite(track.id) → onChanged?.()
 *  - actions: [{ key, icon, title, onClick(track) }]
 *  - dragHandle: ReactNode, вставляется слева (ручка перетаскивания в плейлистах)
 *
 * Добавлено в V2 (ТЗ п. 10, 15):
 *  - встроенное «✏️ Переименовать» — инлайн-поле прямо в строке (api.tracks.rename);
 *  - встроенное «🗑 Удалить» — ConfirmDialog с необязательным удалением из канала
 *    (api.tracks.remove); удалённая строка сразу исчезает, даже если страница
 *    не перезагружает список;
 *  - несколько исполнителей из track.artists выводятся через запятую;
 *  - у файлов не-аудио (file_type ≠ audio) вместо номера показывается значок типа,
 *    а вместо длительности — размер файла; такие строки не запускают плеер.
 *
 * Необязательные пропсы V2 (значения по умолчанию сохраняют поведение контракта):
 *  - allowRename / allowDelete — можно выключить встроенные действия;
 *    если страница уже передала собственное действие с ключом 'rename' или
 *    'delete', встроенная кнопка не дублируется автоматически.
 *
 * Ширина строки на телефоне (ТЗ: экран 375 px):
 *  - рядом со звёздочкой остаётся не больше MAX_INLINE_ACTIONS иконок;
 *    всё остальное (действия страницы вместе со встроенными «✏️» и «🗑»)
 *    схлопывается в одну кнопку «⋯» с меню, где у каждого пункта есть подпись.
 *    Иначе четыре-пять иконок по 38 px съедали всю ширину названия трека:
 *    .track-row__meta и .track-row__actions объявлены flex: none, сжимается
 *    только .track-row__main, и от названия оставалось несколько букв.
 *
 * onChanged вызывается тремя способами и всегда совместим с V1:
 *   onChanged(track, isFavourite)                          — переключено избранное
 *   onChanged(updatedTrack, isFavourite, { type: 'renamed' })
 *   onChanged(track, isFavourite, { type: 'deleted' })
 * Второй аргумент — всегда актуальное значение is_favourite, поэтому страницы,
 * которые точечно обновляют звёздочку, продолжают работать без изменений.
 */

import React, { useEffect, useMemo, useState } from 'react'

import ConfirmDialog from './ConfirmDialog.jsx'
import Modal from './Modal.jsx'
import { api } from '../api/client.js'
import { usePlayer } from '../context/PlayerContext.jsx'
import { useToast } from '../context/ToastContext.jsx'
import { hapticImpact, hapticNotification } from '../telegram.js'
import { formatFileSize } from '../utils/format.js'

/** Значки типов файлов — совпадают с backend/services/media.py::FILE_TYPE_ICONS. */
const FILE_TYPE_ICONS = {
  audio: '🎵',
  document: '📄',
  video: '🎬',
  video_note: '⭕',
  voice: '🎤',
}

/** Русские названия типов файлов — как в боте (media.FILE_TYPE_LABELS). */
const FILE_TYPE_LABELS = {
  audio: 'Аудио',
  document: 'Документ',
  video: 'Видео',
  video_note: 'Видеосообщение',
  voice: 'Голосовое сообщение',
}

/** Ограничение длины названия — столько же принимает backend. */
const TITLE_MAX_LENGTH = 300

/**
 * Сколько кнопок-действий (кроме звёздочки) остаётся прямо в строке.
 * Кнопка занимает 38 px, а на экране 375 px строке достаётся 335 px, из которых
 * номер и длительность забирают около 100 px. Две иконки рядом со звёздочкой уже
 * не оставляют места названию, поэтому лишнее уходит в меню «⋯».
 */
const MAX_INLINE_ACTIONS = 1

/** Секунды → «3:07» / «1:02:03»; 0 и пустое значение → «—». */
function formatDuration(seconds) {
  const total = Number(seconds)
  if (!Number.isFinite(total) || total <= 0) return '—'
  const value = Math.floor(total)
  const hours = Math.floor(value / 3600)
  const minutes = Math.floor((value % 3600) / 60)
  const secs = value % 60
  const pad = (num) => String(num).padStart(2, '0')
  if (hours > 0) return `${hours}:${pad(minutes)}:${pad(secs)}`
  return `${minutes}:${pad(secs)}`
}

/** Нормализованный тип файла; всё неизвестное считаем аудио (совместимость с V1). */
function fileTypeOf(track) {
  const value = String(track?.file_type || 'audio').toLowerCase()
  return FILE_TYPE_ICONS[value] ? value : 'audio'
}

/**
 * Подпись под названием: все исполнители через запятую.
 * track.artists — список {id, name} (V2); если его нет, берём строковое track.artist.
 */
function artistsLabel(track) {
  const list = Array.isArray(track?.artists) ? track.artists : []
  const names = list
    .map((item) => (typeof item === 'string' ? item : item?.name))
    .map((name) => (typeof name === 'string' ? name.trim() : ''))
    .filter(Boolean)
  if (names.length) return names.join(', ')
  const single = typeof track?.artist === 'string' ? track.artist.trim() : ''
  return single
}

/** Человекочитаемый текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  if (!error) return fallback
  if (typeof error.detail === 'string' && error.detail) return error.detail
  return error.message || fallback
}

export function TrackRow({
  track,
  index = null,
  queue = null,
  showStats = true,
  actions = [],
  onChanged = null,
  dragHandle = null,
  selected = false,
  allowRename = true,
  allowDelete = true,
}) {
  const player = usePlayer()
  const { toast } = useToast()

  const [favourite, setFavourite] = useState(Boolean(track && track.is_favourite))
  const [busy, setBusy] = useState(false)

  // Название держим локально: после переименования строка обновляется сразу,
  // даже если страница не перезагружает список.
  const [title, setTitle] = useState(track ? track.title || '' : '')
  const [renaming, setRenaming] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)

  const [confirmOpen, setConfirmOpen] = useState(false)
  const [alsoChannel, setAlsoChannel] = useState(false)
  const [removed, setRemoved] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  const trackId = track ? track.id : null
  const trackFavourite = track ? Boolean(track.is_favourite) : false
  const trackTitle = track ? track.title || '' : ''

  useEffect(() => {
    setFavourite(trackFavourite)
  }, [trackId, trackFavourite])

  // Эффект срабатывает только при смене трека в строке или при новом названии
  // снаружи, поэтому локальное переименование не откатывается перерисовкой.
  useEffect(() => {
    setTitle(trackTitle)
    setRenaming(false)
    setRemoved(false)
    // Открытое меню относится к прежнему треку — закрываем, чтобы действие
    // не сработало по новой строке, если список переехал под тем же компонентом.
    setMenuOpen(false)
  }, [trackId, trackTitle])

  const actionList = useMemo(
    () => (Array.isArray(actions) ? actions.filter(Boolean) : []),
    [actions],
  )

  if (!track || removed) return null

  const fileType = fileTypeOf(track)
  const isAudio = fileType === 'audio'
  const displayTitle = title || 'Без названия'

  const isCurrent = Boolean(player.current && player.current.id === track.id)
  const isPlayingThis = isCurrent && player.isPlaying
  const durationLabel = track.duration_label || formatDuration(track.duration)
  const sizeLabel = Number(track.file_size) > 0 ? formatFileSize(track.file_size) : ''
  const metaLabel = isAudio ? durationLabel : sizeLabel
  const playCount = Number(track.play_count) || 0

  // Кнопки не дублируем: страница могла передать своё «переименовать»/«удалить».
  const hasAction = (key) => actionList.some((action) => action.key === key)
  const showRename = allowRename && !hasAction('rename')
  const showDelete = allowDelete && !hasAction('delete')

  // Плеер запускаем только для аудио и только когда строка не в режиме правки.
  const playable = isAudio && !renaming

  const artists = artistsLabel(track)
  const subtitleParts = []
  if (artists) subtitleParts.push(artists)
  else if (isAudio) subtitleParts.push('Неизвестный исполнитель')
  else subtitleParts.push(FILE_TYPE_LABELS[fileType] || 'Файл')
  if (track.folder_name) subtitleParts.push(track.folder_name)

  const handlePlay = () => {
    if (!playable) return
    hapticImpact('light')
    player.playTrack(track, queue || [track], index === null || index === undefined ? 0 : index)
  }

  const handleRowKeyDown = (event) => {
    if (!playable) return
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      handlePlay()
    }
  }

  const handleFavourite = async (event) => {
    event.stopPropagation()
    if (busy) return
    setBusy(true)
    try {
      const result = await api.tracks.toggleFavourite(track.id)
      const value = Boolean(result && result.is_favourite)
      setFavourite(value)
      hapticImpact('light')
      toast(value ? 'Добавлено в избранное' : 'Удалено из избранного', 'success')
      if (typeof onChanged === 'function') onChanged(track, value)
    } catch (error) {
      toast(errorText(error, 'Не удалось изменить избранное'), 'error')
    } finally {
      setBusy(false)
    }
  }

  const startRename = () => {
    hapticImpact('light')
    setDraft(title)
    setRenaming(true)
  }

  const cancelRename = () => {
    setRenaming(false)
    setDraft('')
  }

  const submitRename = async (event) => {
    event.preventDefault()
    event.stopPropagation()
    const name = draft.trim()
    if (!name) {
      toast('Название не может быть пустым', 'error')
      return
    }
    if (name === title) {
      cancelRename()
      return
    }
    setSaving(true)
    try {
      const updated = await api.tracks.rename(track.id, name)
      const nextTitle = (updated && updated.title) || name
      setTitle(nextTitle)
      setRenaming(false)
      setDraft('')
      hapticNotification('success')
      toast('Название обновлено', 'success')
      if (typeof onChanged === 'function') {
        onChanged(updated || { ...track, title: nextTitle }, favourite, { type: 'renamed' })
      }
    } catch (error) {
      hapticNotification('error')
      toast(errorText(error, 'Не удалось переименовать трек'), 'error')
    } finally {
      setSaving(false)
    }
  }

  const askDelete = () => {
    hapticImpact('medium')
    setAlsoChannel(false)
    setConfirmOpen(true)
  }

  const handleDelete = async () => {
    if (busy) return
    setBusy(true)
    try {
      await api.tracks.remove(track.id, { delete_from_channel: alsoChannel })
      // Если удалили то, что играет прямо сейчас, — останавливаем плеер:
      // поток по этой ссылке всё равно больше не откроется.
      if (isCurrent && typeof player.stop === 'function') player.stop()
      hapticNotification('success')
      toast(alsoChannel ? 'Удалено вместе с файлом' : 'Удалено из библиотеки', 'success')
      setConfirmOpen(false)
      setRemoved(true)
      if (typeof onChanged === 'function') onChanged(track, favourite, { type: 'deleted' })
    } catch (error) {
      hapticNotification('error')
      toast(errorText(error, 'Не удалось удалить'), 'error')
    } finally {
      setBusy(false)
    }
  }

  // Единый список действий строки: встроенное «✏️», действия страницы и «🗑».
  // Порядок тот же, что был у иконок, — меняется только способ показа.
  const rowActions = []
  if (showRename && !renaming) {
    rowActions.push({ key: 'rename', icon: '✏️', title: 'Переименовать', run: startRename })
  }
  actionList.forEach((action) => {
    rowActions.push({
      key: action.key,
      icon: action.icon,
      title: action.title || '',
      run: () => {
        if (typeof action.onClick === 'function') action.onClick(track)
      },
    })
  })
  if (showDelete) {
    rowActions.push({
      key: 'delete',
      icon: '🗑',
      title: 'Удалить',
      disabled: busy,
      run: askDelete,
    })
  }

  // Помещается — показываем иконками, не помещается — прячем всё в меню «⋯».
  const collapsed = rowActions.length > MAX_INLINE_ACTIONS
  const inlineActions = collapsed ? [] : rowActions
  const menuActions = collapsed ? rowActions : []

  const runAction = (action, event) => {
    if (event) event.stopPropagation()
    if (action.disabled) return
    setMenuOpen(false)
    action.run()
  }

  const openMenu = (event) => {
    event.stopPropagation()
    hapticImpact('light')
    setMenuOpen(true)
  }

  const rowClassName = [
    'track-row',
    isCurrent ? 'track-row--active' : '',
    selected ? 'track-row--selected' : '',
    playable ? '' : 'track-row--static',
  ]
    .filter(Boolean)
    .join(' ')

  let indexLabel = FILE_TYPE_ICONS[fileType] || '🎵'
  if (isCurrent) indexLabel = isPlayingThis ? '▶' : '⏸'
  else if (isAudio && index !== null && index !== undefined) indexLabel = String(Number(index) + 1)

  const rowProps = playable
    ? {
        role: 'button',
        tabIndex: 0,
        onClick: handlePlay,
        onKeyDown: handleRowKeyDown,
        'aria-label': `Воспроизвести: ${displayTitle}`,
      }
    : {}

  return (
    <>
      <div className={rowClassName} {...rowProps}>
        {dragHandle ? <div className="track-row__drag">{dragHandle}</div> : null}

        <div
          className="track-row__index muted"
          aria-hidden={isAudio ? 'true' : undefined}
          title={isAudio ? undefined : FILE_TYPE_LABELS[fileType] || 'Файл'}
        >
          {indexLabel}
        </div>

        <div className="track-row__main">
          {renaming ? (
            <form
              className="row track-row__rename"
              onSubmit={submitRename}
              onClick={(event) => event.stopPropagation()}
            >
              <input
                className="input track-row__rename-input"
                type="text"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Escape' || event.key === 'Esc') {
                    event.stopPropagation()
                    cancelRename()
                  }
                }}
                placeholder="Новое название"
                maxLength={TITLE_MAX_LENGTH}
                disabled={saving}
                autoFocus
                aria-label="Новое название"
              />
              <button
                type="submit"
                className="btn-icon"
                disabled={saving || !draft.trim()}
                title="Сохранить название"
                aria-label="Сохранить название"
              >
                ✔
              </button>
              <button
                type="button"
                className="btn-icon"
                onClick={cancelRename}
                disabled={saving}
                title="Отменить переименование"
                aria-label="Отменить переименование"
              >
                ✕
              </button>
            </form>
          ) : (
            <>
              <div className="track-row__title">{displayTitle}</div>
              <div className="track-row__artist muted">{subtitleParts.join(' · ')}</div>
            </>
          )}
        </div>

        {renaming ? null : (
          <div className="track-row__meta muted">
            {metaLabel ? <span className="track-row__duration">{metaLabel}</span> : null}
            {showStats && isAudio ? (
              <span className="badge track-row__plays" title="Прослушиваний">
                ▶ {playCount}
              </span>
            ) : null}
          </div>
        )}

        <div className="track-row__actions">
          <button
            type="button"
            className="btn-icon track-row__fav"
            onClick={handleFavourite}
            disabled={busy}
            aria-pressed={favourite}
            title={favourite ? 'Убрать из избранного' : 'В избранное'}
            aria-label={favourite ? 'Убрать из избранного' : 'В избранное'}
          >
            {favourite ? '⭐' : '☆'}
          </button>

          {inlineActions.map((action) => (
            <button
              key={action.key}
              type="button"
              className="btn-icon"
              title={action.title}
              aria-label={action.title}
              disabled={Boolean(action.disabled)}
              onClick={(event) => runAction(action, event)}
            >
              {action.icon}
            </button>
          ))}

          {menuActions.length ? (
            <button
              type="button"
              className="btn-icon track-row__menu"
              onClick={openMenu}
              aria-haspopup="dialog"
              aria-expanded={menuOpen}
              title="Действия с треком"
              aria-label="Действия с треком"
            >
              ⋯
            </button>
          ) : null}
        </div>
      </div>

      <Modal
        open={menuOpen && menuActions.length > 0}
        title={displayTitle}
        onClose={() => setMenuOpen(false)}
      >
        <div className="list">
          {menuActions.map((action) => (
            <button
              key={action.key}
              type="button"
              className="list-item"
              disabled={Boolean(action.disabled)}
              onClick={() => runAction(action)}
            >
              <span className="list-item__icon" aria-hidden="true">
                {action.icon}
              </span>
              <span className="list-item__title">{action.title || 'Действие'}</span>
            </button>
          ))}
        </div>
      </Modal>

      <ConfirmDialog
        open={confirmOpen}
        title="Удалить безвозвратно?"
        message={
          <>
            «{displayTitle}» исчезнет из библиотеки, статистики и плейлистов. Отменить это нельзя.
            <label className="checkbox track-row__confirm-option">
              <input
                type="checkbox"
                checked={alsoChannel}
                onChange={(event) => setAlsoChannel(event.target.checked)}
              />
              <span className="checkbox__label">Удалить файл и из канала-хранилища</span>
            </label>
          </>
        }
        confirmText={busy ? 'Удаляем…' : 'Удалить'}
        onConfirm={handleDelete}
        onCancel={() => setConfirmOpen(false)}
      />
    </>
  )
}

export default TrackRow
