import { useEffect, useMemo, useState } from 'react'

import EmptyState from './EmptyState'
import Loader from './Loader'
import Modal from './Modal'
import SearchBar from './SearchBar'
import { api } from '../api/client'
import { hapticImpact, hapticNotification } from '../telegram'

/**
 * Выбор папки: дерево папок с раскрытием узлов, поиск по названию,
 * пункты «🚫 Без папки» и «🆕 Новая папка».
 *
 * Пропсы V1 (контракт, раздел 16) сохранены дословно:
 * FolderPickerModal({ open, onClose, onPick, allowNone = true, allowCreate = true,
 *                     title = 'Выберите папку' })
 * onPick(folder | null) — null означает «без папки».
 * После выбора окно закрывается само (вызывается onClose).
 *
 * Добавлено в V2 (контракт V2, раздел 6):
 *  - section ∈ music | other — какой раздел показывать (по умолчанию «music»);
 *  - parentId — папка, внутрь которой создаются новые папки; путь до неё
 *    раскрывается сразу при открытии окна.
 *
 * Дерево берём из GET /folders/tree; если эндпоинт недоступен (старый backend),
 * молча откатываемся на плоский GET /folders — окно продолжает работать.
 */

const TRACK_FORMS = ['трек', 'трека', 'треков']

/** Русское склонение количества треков: 1 трек / 2 трека / 5 треков. */
function formatTrackCount(count) {
  const value = Math.abs(Number(count) || 0)
  const mod10 = value % 10
  const mod100 = value % 100
  let form = TRACK_FORMS[2]
  if (mod10 === 1 && mod100 !== 11) form = TRACK_FORMS[0]
  else if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) form = TRACK_FORMS[1]
  return `${value} ${form}`
}

/** Нормализация для поиска: регистр, «ё» и лишние пробелы не мешают. */
function normalize(value) {
  return String(value ?? '')
    .toLowerCase()
    .replace(/ё/g, 'е')
    .replace(/\s+/g, ' ')
    .trim()
}

/** Человеческий текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  return error?.detail || error?.message || fallback
}

/**
 * Сколько треков показать у папки: вместе с подпапками, если backend это посчитал,
 * иначе — треки самой папки (плоский ответ V1).
 */
function folderCount(folder) {
  const total = Number(folder?.total_track_count)
  if (Number.isFinite(total) && total > 0) return total
  return Number(folder?.track_count) || 0
}

/** Значок папки: у папки исполнителя — микрофон, у раздела «Другое» — коробка. */
function folderIcon(folder) {
  if (folder?.is_artist_folder) return '🎤'
  if (folder?.section === 'other') return '📦'
  return '📁'
}

/** Ответ может прийти списком или объектом со списком — приводим к массиву. */
function asList(payload) {
  if (Array.isArray(payload)) return payload
  if (Array.isArray(payload?.items)) return payload.items
  if (Array.isArray(payload?.folders)) return payload.folders
  return []
}

/** Рекурсивно приводим дерево к виду {…folder, children: []}. */
function normalizeTree(payload) {
  return asList(payload)
    .filter(Boolean)
    .map((node) => ({ ...node, children: normalizeTree(node.children) }))
}

/**
 * Разворачивает дерево в плоский список для поиска и для подсказок о пути.
 * @returns {Array<{folder: object, depth: number, trail: string[], parents: number[]}>}
 */
function flattenTree(nodes, depth = 0, trail = [], parents = []) {
  const result = []
  for (const node of nodes) {
    result.push({ folder: node, depth, trail, parents })
    if (node.children.length) {
      result.push(
        ...flattenTree(node.children, depth + 1, [...trail, node.name], [...parents, node.id]),
      )
    }
  }
  return result
}

export function FolderPickerModal({
  open,
  onClose,
  onPick,
  allowNone = true,
  allowCreate = true,
  title = 'Выберите папку',
  section = 'music',
  parentId = null,
}) {
  const [nodes, setNodes] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState(() => new Set())
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [saving, setSaving] = useState(false)

  // Загружаем дерево при каждом открытии — состав папок мог измениться.
  useEffect(() => {
    if (!open) return undefined
    let cancelled = false
    setLoading(true)
    setError(null)
    api.folders
      .tree(section)
      // Старый backend без /folders/tree — показываем плоский список.
      .catch(() => api.folders.list({ section }))
      .then((payload) => {
        if (cancelled) return
        setNodes(normalizeTree(payload))
      })
      .catch((err) => {
        if (cancelled) return
        setNodes([])
        setError(errorText(err, 'Не удалось загрузить папки'))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, section])

  // Сбрасываем поиск и форму создания после закрытия.
  useEffect(() => {
    if (open) return
    setQuery('')
    setCreating(false)
    setNewName('')
    setSaving(false)
    setError(null)
  }, [open])

  const flat = useMemo(() => flattenTree(nodes), [nodes])

  /** Папка, внутрь которой создаются новые (пропс parentId). */
  const parentEntry = useMemo(() => {
    const id = Number(parentId)
    if (!Number.isInteger(id) || id <= 0) return null
    return flat.find((entry) => Number(entry.folder.id) === id) ?? null
  }, [flat, parentId])

  // Путь до parentId раскрываем сразу: пользователь видит, куда попадёт папка.
  useEffect(() => {
    if (!open) return
    if (!parentEntry) return
    setExpanded((prev) => {
      const next = new Set(prev)
      for (const id of parentEntry.parents) next.add(id)
      next.add(parentEntry.folder.id)
      return next
    })
  }, [open, parentEntry])

  const needle = normalize(query)

  /** При поиске показываем плоский результат по всему дереву, а не только по корню. */
  const matches = useMemo(() => {
    if (!needle) return []
    return flat.filter((entry) => normalize(entry.folder.name).includes(needle))
  }, [flat, needle])

  const toggle = (id) => {
    hapticImpact('light')
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const pick = (folder) => {
    hapticImpact('light')
    onPick?.(folder)
    onClose?.()
  }

  const handleCreate = async (event) => {
    event.preventDefault()
    const name = newName.trim()
    if (!name) {
      setError('Введите название папки')
      return
    }
    setSaving(true)
    setError(null)
    try {
      const folder = await api.folders.create(name, {
        parentFolderId: parentEntry ? parentEntry.folder.id : null,
        section,
      })
      hapticNotification('success')
      setNewName('')
      setCreating(false)
      onPick?.(folder)
      onClose?.()
    } catch (err) {
      hapticNotification('error')
      setError(errorText(err, 'Не удалось создать папку'))
    } finally {
      setSaving(false)
    }
  }

  const nothingFound = !loading && (needle ? matches.length === 0 : nodes.length === 0)

  /** Строка одной папки: отступ по уровню, стрелка раскрытия, кнопка выбора. */
  const renderRow = (folder, depth, { expandable = false, isOpen = false, meta = null } = {}) => (
    <div
      className="row folder-tree__row"
      style={depth ? { paddingLeft: `${depth * 14}px` } : undefined}
    >
      {expandable ? (
        <button
          type="button"
          className="btn-icon folder-tree__toggle"
          onClick={() => toggle(folder.id)}
          aria-expanded={isOpen}
          title={isOpen ? 'Свернуть' : 'Раскрыть'}
          aria-label={isOpen ? `Свернуть «${folder.name}»` : `Раскрыть «${folder.name}»`}
        >
          {isOpen ? '▾' : '▸'}
        </button>
      ) : (
        // Пустышка той же ширины, что и стрелка: названия папок стоят ровно.
        <span
          className="btn-icon folder-tree__toggle folder-tree__toggle--empty"
          aria-hidden="true"
        />
      )}

      <button type="button" className="list-item" onClick={() => pick(folder)}>
        <span className="list-item__icon" aria-hidden="true">
          {folderIcon(folder)}
        </span>
        <span className="list-item__title">{folder.name}</span>
        <span className="list-item__meta">{meta ?? formatTrackCount(folderCount(folder))}</span>
      </button>
    </div>
  )

  /** Рекурсивная отрисовка узлов дерева. */
  const renderNodes = (list, depth = 0) =>
    list.map((node) => {
      const children = node.children ?? []
      const isOpen = expanded.has(node.id)
      return (
        <li key={node.id} className="folder-tree__node">
          {renderRow(node, depth, { expandable: children.length > 0, isOpen })}
          {children.length > 0 && isOpen ? (
            <ul className="list folder-tree__children">{renderNodes(children, depth + 1)}</ul>
          ) : null}
        </li>
      )
    })

  return (
    <Modal open={open} title={title} onClose={onClose}>
      <SearchBar value={query} onChange={setQuery} placeholder="Название папки…" />

      {error ? (
        <p className="modal__error" role="alert">
          {error}
        </p>
      ) : null}

      {loading ? (
        <Loader label="Загружаем папки…" />
      ) : (
        <>
          <ul className="list folder-tree">
            {allowNone ? (
              <li>
                <button type="button" className="list-item list-item--action" onClick={() => pick(null)}>
                  <span className="list-item__icon" aria-hidden="true">
                    🚫
                  </span>
                  <span className="list-item__title">Без папки</span>
                </button>
              </li>
            ) : null}

            {allowCreate ? (
              <li>
                {creating ? (
                  <form className="list-item list-item--form" onSubmit={handleCreate}>
                    <input
                      className="input"
                      type="text"
                      value={newName}
                      onChange={(event) => setNewName(event.target.value)}
                      placeholder="Название новой папки"
                      autoFocus
                      maxLength={100}
                      disabled={saving}
                      aria-label="Название новой папки"
                    />
                    {parentEntry ? (
                      <p className="modal__hint">Появится внутри «{parentEntry.folder.name}».</p>
                    ) : null}
                    <div className="modal__actions">
                      <button
                        type="button"
                        className="btn"
                        onClick={() => {
                          setCreating(false)
                          setNewName('')
                          setError(null)
                        }}
                        disabled={saving}
                      >
                        Отмена
                      </button>
                      <button type="submit" className="btn btn--primary" disabled={saving || !newName.trim()}>
                        {saving ? 'Создаём…' : 'Создать'}
                      </button>
                    </div>
                  </form>
                ) : (
                  <button
                    type="button"
                    className="list-item list-item--action"
                    onClick={() => {
                      hapticImpact('light')
                      setError(null)
                      setNewName(query.trim())
                      setCreating(true)
                    }}
                  >
                    <span className="list-item__icon" aria-hidden="true">
                      🆕
                    </span>
                    <span className="list-item__title">Новая папка</span>
                  </button>
                )}
              </li>
            ) : null}

            {needle
              ? matches.map((entry) => (
                  <li key={entry.folder.id} className="folder-tree__node">
                    {renderRow(entry.folder, 0, {
                      meta: entry.trail.length ? entry.trail.join(' / ') : null,
                    })}
                  </li>
                ))
              : renderNodes(nodes)}
          </ul>

          {nothingFound ? (
            <EmptyState
              icon="📁"
              title={query ? 'Ничего не нашлось' : 'Папок пока нет'}
              description={
                query
                  ? 'Попробуйте изменить запрос или создайте новую папку.'
                  : 'Создайте первую папку — библиотека станет аккуратнее.'
              }
            />
          ) : null}
        </>
      )}
    </Modal>
  )
}

export default FolderPickerModal
