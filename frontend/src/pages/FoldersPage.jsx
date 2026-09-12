/**
 * Страница «Папки» (маршрут `/folders`) — ДРЕВОВИДНАЯ структура (контракт V2, раздел 6).
 *
 * Возможности:
 *  - дерево папок раздела «Музыка» (`GET /folders/tree?section=music`) с раскрытием
 *    и сворачиванием узлов, отступом по глубине и счётчиками
 *    `track_count` (собственные треки) / `total_track_count` (вместе с подпапками);
 *  - создание папки с выбором родителя (`POST /folders`);
 *  - переименование (`PATCH /folders/{id}`);
 *  - перемещение (`POST /folders/{id}/move`) — 400 «цикл» показывается понятным тостом;
 *  - удаление (`DELETE /folders/{id}`) с выбором «удалять ли треки»;
 *  - поиск по названию папки: совпадения показываются вместе с родителями,
 *    ветки до совпадений раскрываются автоматически.
 *
 * Узел дерева (схема FolderTreeOut):
 * { id, name, parent_folder_id, section, is_artist_folder, has_children,
 *   track_count, total_track_count, created_at, children: [...] }.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client.js'
import EmptyState from '../components/EmptyState.jsx'
import Loader from '../components/Loader.jsx'
import Modal from '../components/Modal.jsx'
import SearchBar from '../components/SearchBar.jsx'
import { useToast } from '../context/ToastContext.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { hapticImpact, hapticNotification } from '../telegram.js'
import { formatCount } from '../utils/format.js'

/** Раздел страницы: «Папки» — это музыка; файлы из «Другого» живут на своей странице. */
const SECTION = 'music'

const TRACK_FORMS = ['трек', 'трека', 'треков']
const FOLDER_FORMS = ['папка', 'папки', 'папок']
const SUBFOLDER_FORMS = ['вложенная папка', 'вложенные папки', 'вложенных папок']

/** Максимальная длина названия папки (совпадает с ограничением backend). */
const NAME_MAX_LENGTH = 100

/** Значение «в корень раздела» для выпадающих списков (в <option> нельзя null). */
const ROOT_VALUE = ''

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
  const detail = error?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  const message = error?.message
  if (typeof message === 'string' && message.trim()) return message
  return fallback
}

/**
 * Текст ошибки перемещения папки.
 * Backend отвечает 400 с русским пояснением (цикл, чужой раздел, тёзка на уровне),
 * но если пояснения нет — объясняем сами.
 */
function moveErrorText(error) {
  const text = errorText(error, '')
  if (text) return text
  if (error?.status === 400) {
    return 'Папку нельзя перенести в саму себя или в свою подпапку.'
  }
  return 'Не удалось перенести папку'
}

/** Алфавитная сортировка узлов одного уровня с учётом кириллицы. */
function sortNodes(nodes) {
  return [...nodes].sort((a, b) =>
    String(a?.name || '').localeCompare(String(b?.name || ''), 'ru', { sensitivity: 'base' }),
  )
}

/** Рекурсивная сортировка всего дерева (backend порядок не гарантирует). */
function sortTree(nodes) {
  const list = Array.isArray(nodes) ? nodes.filter(Boolean) : []
  return sortNodes(list).map((node) => ({
    ...node,
    children: sortTree(node.children),
  }))
}

/** Плоский список узлов дерева с глубиной: [{ node, depth }, …]. */
function flattenTree(nodes, depth = 0, out = []) {
  for (const node of Array.isArray(nodes) ? nodes : []) {
    out.push({ node, depth })
    flattenTree(node.children, depth + 1, out)
  }
  return out
}

/** Идентификаторы самой папки и всех её потомков — их нельзя выбрать новым родителем. */
function subtreeIds(node, acc = new Set()) {
  if (!node) return acc
  acc.add(node.id)
  for (const child of Array.isArray(node.children) ? node.children : []) {
    subtreeIds(child, acc)
  }
  return acc
}

/** Сколько вложенных папок внутри узла (без него самого). */
function countDescendants(node) {
  const children = Array.isArray(node?.children) ? node.children : []
  return children.reduce((sum, child) => sum + 1 + countDescendants(child), 0)
}

/**
 * Фильтрация дерева по подстроке.
 * Совпавший узел показывается со всем содержимым; узел без совпадения остаётся,
 * только если совпадение есть внутри — и тогда попадает в `expand` (авто-раскрытие).
 */
function filterTree(nodes, needle, expand) {
  const out = []
  for (const node of Array.isArray(nodes) ? nodes : []) {
    const children = Array.isArray(node.children) ? node.children : []
    const selfMatch = normalize(node.name).includes(needle)
    const matchedChildren = filterTree(children, needle, expand)
    if (selfMatch) {
      out.push(node)
      if (matchedChildren.length) expand.add(node.id)
    } else if (matchedChildren.length) {
      out.push({ ...node, children: matchedChildren })
      expand.add(node.id)
    }
  }
  return out
}

/** Видимые строки дерева с учётом раскрытых узлов. */
function visibleRows(nodes, expanded, depth = 0, out = []) {
  for (const node of Array.isArray(nodes) ? nodes : []) {
    const children = Array.isArray(node.children) ? node.children : []
    const hasChildren = children.length > 0 || Boolean(node.has_children)
    const isOpen = hasChildren && expanded.has(node.id)
    out.push({ node, depth, hasChildren, isOpen })
    if (isOpen) visibleRows(children, expanded, depth + 1, out)
  }
  return out
}

/** Все идентификаторы дерева — для кнопки «Развернуть всё». */
function allIds(nodes, acc = new Set()) {
  for (const node of Array.isArray(nodes) ? nodes : []) {
    acc.add(node.id)
    allIds(node.children, acc)
  }
  return acc
}

/**
 * Выпадающий список папок с отступами по глубине.
 * Используется и при создании (родитель), и при перемещении (новый родитель).
 */
function FolderSelect({ id, label, value, onChange, options, disabled = false, rootLabel }) {
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      <select
        id={id}
        className="input"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value={ROOT_VALUE}>{rootLabel}</option>
        {options.map(({ node, depth }) => (
          <option key={node.id} value={String(node.id)}>
            {`${'  '.repeat(depth)}${depth > 0 ? '└ ' : ''}${node.name}`}
          </option>
        ))}
      </select>
    </div>
  )
}

export function FoldersPage() {
  const { toast } = useToast()
  const { data, error, loading, reload } = useAsync(() => api.folders.tree(SECTION), [])

  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState(() => new Set())

  // Диалог действий над папкой (компактная замена ряду иконок в строке).
  const [actionsTarget, setActionsTarget] = useState(null)

  const [createOpen, setCreateOpen] = useState(false)
  const [createName, setCreateName] = useState('')
  const [createParent, setCreateParent] = useState(ROOT_VALUE)
  const [creating, setCreating] = useState(false)

  const [renameTarget, setRenameTarget] = useState(null)
  const [renameValue, setRenameValue] = useState('')
  const [renaming, setRenaming] = useState(false)

  const [moveTarget, setMoveTarget] = useState(null)
  const [moveParent, setMoveParent] = useState(ROOT_VALUE)
  const [moving, setMoving] = useState(false)

  const [deleteTarget, setDeleteTarget] = useState(null)
  const [deleteTracks, setDeleteTracks] = useState(false)
  const [deleting, setDeleting] = useState(false)

  // Ошибку загрузки дублируем тостом — так требует контракт (ApiError → useToast).
  useEffect(() => {
    if (error) toast(errorText(error, 'Не удалось загрузить папки'), 'error')
  }, [error, toast])

  const tree = useMemo(() => sortTree(data), [data])
  const flat = useMemo(() => flattenTree(tree), [tree])

  const totalFolders = flat.length
  const totalTracks = useMemo(
    () => tree.reduce((sum, node) => sum + (Number(node.total_track_count) || 0), 0),
    [tree],
  )

  // Поиск: дерево фильтруется, ветки до совпадений раскрываются сами.
  const needle = normalize(query)
  const { visibleTree, autoExpanded } = useMemo(() => {
    if (!needle) return { visibleTree: tree, autoExpanded: null }
    const expand = new Set()
    return { visibleTree: filterTree(tree, needle, expand), autoExpanded: expand }
  }, [needle, tree])

  const effectiveExpanded = useMemo(() => {
    if (!autoExpanded) return expanded
    return new Set([...expanded, ...autoExpanded])
  }, [autoExpanded, expanded])

  const rows = useMemo(
    () => visibleRows(visibleTree, effectiveExpanded),
    [effectiveExpanded, visibleTree],
  )

  const toggleNode = useCallback((folderId) => {
    hapticImpact('light')
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(folderId)) next.delete(folderId)
      else next.add(folderId)
      return next
    })
  }, [])

  const anyExpanded = expanded.size > 0
  const handleExpandAll = () => {
    hapticImpact('light')
    setExpanded(anyExpanded ? new Set() : allIds(tree))
  }

  // Пока не пришёл первый ответ — Loader; при перезагрузке дерево остаётся на месте.
  const initialLoading = loading && data === null

  const openActions = (node) => {
    hapticImpact('light')
    setActionsTarget(node)
  }

  const closeActions = () => setActionsTarget(null)

  // --- Создание ------------------------------------------------------------

  const openCreate = (parentNode = null) => {
    hapticImpact('light')
    setActionsTarget(null)
    setCreateName('')
    setCreateParent(parentNode ? String(parentNode.id) : ROOT_VALUE)
    setCreateOpen(true)
  }

  const closeCreate = () => {
    if (creating) return
    setCreateOpen(false)
    setCreateName('')
  }

  const handleCreate = async () => {
    if (creating) return
    const name = createName.trim()
    if (!name) {
      toast('Введите название папки', 'error')
      return
    }
    const parentId = createParent === ROOT_VALUE ? null : Number(createParent)
    setCreating(true)
    try {
      const folder = await api.folders.create(name, {
        parentFolderId: parentId,
        section: SECTION,
      })
      hapticNotification('success')
      toast(`Папка «${folder?.name || name}» создана`, 'success')
      // Раскрываем родителя, чтобы новая папка сразу была видна.
      if (parentId) {
        setExpanded((prev) => new Set(prev).add(parentId))
      }
      setCreateOpen(false)
      setCreateName('')
      await reload()
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось создать папку'), 'error')
    } finally {
      setCreating(false)
    }
  }

  // --- Переименование ------------------------------------------------------

  const openRename = (node) => {
    hapticImpact('light')
    setActionsTarget(null)
    setRenameTarget(node)
    setRenameValue(node?.name || '')
  }

  const closeRename = () => {
    if (renaming) return
    setRenameTarget(null)
    setRenameValue('')
  }

  const handleRename = async () => {
    if (!renameTarget || renaming) return
    const name = renameValue.trim()
    if (!name) {
      toast('Введите новое название папки', 'error')
      return
    }
    if (name === renameTarget.name) {
      closeRename()
      return
    }
    setRenaming(true)
    try {
      await api.folders.rename(renameTarget.id, name)
      hapticNotification('success')
      toast(`Папка переименована в «${name}»`, 'success')
      setRenameTarget(null)
      setRenameValue('')
      await reload()
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось переименовать папку'), 'error')
    } finally {
      setRenaming(false)
    }
  }

  const handleRenameKeyDown = (event) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      handleRename()
    }
  }

  // --- Перемещение ---------------------------------------------------------

  const openMove = (node) => {
    hapticImpact('light')
    setActionsTarget(null)
    setMoveTarget(node)
    setMoveParent(node?.parent_folder_id ? String(node.parent_folder_id) : ROOT_VALUE)
  }

  const closeMove = () => {
    if (moving) return
    setMoveTarget(null)
    setMoveParent(ROOT_VALUE)
  }

  // Сама папка и её потомки родителем быть не могут — убираем их из списка.
  const moveOptions = useMemo(() => {
    if (!moveTarget) return []
    const forbidden = subtreeIds(moveTarget)
    return flat.filter(({ node }) => !forbidden.has(node.id))
  }, [flat, moveTarget])

  const handleMove = async () => {
    if (!moveTarget || moving) return
    const parentId = moveParent === ROOT_VALUE ? null : Number(moveParent)
    const currentParent = moveTarget.parent_folder_id ?? null
    if ((parentId ?? null) === currentParent) {
      toast('Папка уже находится здесь', 'info')
      return
    }
    setMoving(true)
    try {
      await api.folders.move(moveTarget.id, parentId)
      hapticNotification('success')
      const parentNode = parentId
        ? flat.find(({ node }) => node.id === parentId)?.node
        : null
      toast(
        parentNode
          ? `Папка «${moveTarget.name}» перенесена в «${parentNode.name}»`
          : `Папка «${moveTarget.name}» перенесена в корень`,
        'success',
      )
      if (parentId) setExpanded((prev) => new Set(prev).add(parentId))
      setMoveTarget(null)
      setMoveParent(ROOT_VALUE)
      await reload()
    } catch (err) {
      hapticNotification('error')
      toast(moveErrorText(err), 'error')
    } finally {
      setMoving(false)
    }
  }

  // --- Удаление ------------------------------------------------------------

  const openDelete = (node) => {
    hapticImpact('light')
    setActionsTarget(null)
    setDeleteTarget(node)
    setDeleteTracks(false)
  }

  const closeDelete = () => {
    if (deleting) return
    setDeleteTarget(null)
    setDeleteTracks(false)
  }

  const handleDelete = async () => {
    if (!deleteTarget || deleting) return
    setDeleting(true)
    try {
      // recursive=true: подпапки удаляются вместе с родителем (иначе backend вернёт 400).
      await api.folders.remove(deleteTarget.id, {
        delete_tracks: deleteTracks,
        recursive: true,
      })
      hapticNotification('success')
      toast(
        deleteTracks
          ? `Папка «${deleteTarget.name}» удалена вместе с треками`
          : `Папка «${deleteTarget.name}» удалена, треки остались в библиотеке`,
        'success',
      )
      setExpanded((prev) => {
        const next = new Set(prev)
        for (const removed of subtreeIds(deleteTarget)) next.delete(removed)
        return next
      })
      setDeleteTarget(null)
      setDeleteTracks(false)
      await reload()
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось удалить папку'), 'error')
    } finally {
      setDeleting(false)
    }
  }

  // --- Разметка ------------------------------------------------------------

  const deleteSubfolders = deleteTarget ? countDescendants(deleteTarget) : 0
  const deleteTracksTotal = Number(deleteTarget?.total_track_count) || 0

  const createFooter = (
    <div className="modal__actions">
      <button type="button" className="btn" onClick={closeCreate} disabled={creating}>
        Отмена
      </button>
      <button
        type="button"
        className="btn btn--primary"
        onClick={handleCreate}
        disabled={creating || !createName.trim()}
      >
        {creating ? 'Создаём…' : 'Создать'}
      </button>
    </div>
  )

  const renameFooter = (
    <div className="modal__actions">
      <button type="button" className="btn" onClick={closeRename} disabled={renaming}>
        Отмена
      </button>
      <button
        type="button"
        className="btn btn--primary"
        onClick={handleRename}
        disabled={renaming || !renameValue.trim()}
      >
        {renaming ? 'Сохранение…' : 'Сохранить'}
      </button>
    </div>
  )

  const moveFooter = (
    <div className="modal__actions">
      <button type="button" className="btn" onClick={closeMove} disabled={moving}>
        Отмена
      </button>
      <button type="button" className="btn btn--primary" onClick={handleMove} disabled={moving}>
        {moving ? 'Переносим…' : 'Перенести'}
      </button>
    </div>
  )

  const deleteFooter = (
    <div className="modal__actions">
      <button type="button" className="btn" onClick={closeDelete} disabled={deleting}>
        Отмена
      </button>
      <button
        type="button"
        className="btn btn--primary btn--danger"
        onClick={handleDelete}
        disabled={deleting}
      >
        {deleting ? 'Удаляем…' : 'Удалить'}
      </button>
    </div>
  )

  return (
    <div className="page">
      <div className="section__header">
        <h1 className="section__title">📁 Папки</h1>
        {totalFolders > 0 ? (
          <span className="muted">
            {formatCount(totalFolders, FOLDER_FORMS)} · {formatCount(totalTracks, TRACK_FORMS)}
          </span>
        ) : null}
      </div>

      <div className="row">
        <button type="button" className="btn btn--primary" onClick={() => openCreate(null)}>
          ➕ Новая папка
        </button>
        {totalFolders > 1 ? (
          <button type="button" className="btn btn--ghost" onClick={handleExpandAll}>
            {anyExpanded ? '⬆️ Свернуть всё' : '⬇️ Развернуть всё'}
          </button>
        ) : null}
      </div>

      {totalFolders > 0 ? (
        <SearchBar value={query} onChange={setQuery} placeholder="Поиск по папкам…" />
      ) : null}

      {initialLoading ? <Loader label="Загружаем папки…" /> : null}

      {!initialLoading && rows.length > 0 ? (
        <div className="tree" aria-label="Дерево папок">
          {rows.map(({ node, depth, hasChildren, isOpen }) => {
            const own = Number(node.track_count) || 0
            const total = Number(node.total_track_count) || 0
            return (
              // Отступ уровня задаёт переменная --depth (см. `.tree__node` в styles.css).
              <div className="tree__node" key={node.id} style={{ '--depth': String(depth) }}>
                <button
                  type="button"
                  className="tree__toggle"
                  disabled={!hasChildren}
                  aria-expanded={hasChildren ? isOpen : undefined}
                  onClick={() => toggleNode(node.id)}
                  title={hasChildren ? (isOpen ? 'Свернуть' : 'Развернуть') : ''}
                  aria-label={
                    hasChildren
                      ? isOpen
                        ? `Свернуть папку ${node.name}`
                        : `Развернуть папку ${node.name}`
                      : `Папка ${node.name} без подпапок`
                  }
                >
                  {/* Треугольник поворачивается на 90° при aria-expanded="true". */}
                  <span aria-hidden="true">▸</span>
                </button>

                <Link
                  className="tree__label"
                  to={`/folders/${node.id}`}
                  onClick={() => hapticImpact('light')}
                  aria-label={`Открыть папку ${node.name}`}
                >
                  <span aria-hidden="true">{node.is_artist_folder ? '🎤' : '📁'}</span>{' '}
                  {node.name}
                  <span className="muted">{formatCount(own, TRACK_FORMS)}</span>
                  {total > own ? (
                    <span className="badge" title="Всего вместе с подпапками">
                      Σ {total}
                    </span>
                  ) : null}
                </Link>

                <button
                  type="button"
                  className="btn-icon"
                  onClick={() => openActions(node)}
                  title="Действия с папкой"
                  aria-label={`Действия с папкой ${node.name}`}
                >
                  ⋯
                </button>
              </div>
            )
          })}
        </div>
      ) : null}

      {!initialLoading && rows.length === 0 ? (
        <EmptyState
          icon={error ? '⚠️' : '📁'}
          title={
            error ? 'Не удалось загрузить папки' : query ? 'Ничего не нашлось' : 'Папок пока нет'
          }
          description={
            error
              ? errorText(error, 'Проверьте подключение и попробуйте ещё раз.')
              : query
                ? 'Попробуйте изменить запрос — поиск ищет по названиям папок на всех уровнях.'
                : 'Создайте первую папку — библиотека станет аккуратнее. Папки можно вкладывать друг в друга, а бот сам раскладывает загруженные треки по папкам исполнителей.'
          }
          action={
            error ? (
              <button type="button" className="btn btn--primary" onClick={reload}>
                Повторить
              </button>
            ) : query ? (
              <button type="button" className="btn" onClick={() => setQuery('')}>
                Сбросить поиск
              </button>
            ) : (
              <button type="button" className="btn btn--primary" onClick={() => openCreate(null)}>
                Создать папку
              </button>
            )
          }
        />
      ) : null}

      {/* Действия над папкой */}
      <Modal
        open={Boolean(actionsTarget)}
        title={actionsTarget ? `📁 ${actionsTarget.name}` : 'Папка'}
        onClose={closeActions}
      >
        <ul className="list">
          <li>
            <Link
              className="list-item list-item--action"
              to={actionsTarget ? `/folders/${actionsTarget.id}` : '/folders'}
              onClick={closeActions}
            >
              <span className="list-item__icon" aria-hidden="true">
                📂
              </span>
              <span className="list-item__title">Открыть папку</span>
            </Link>
          </li>
          <li>
            <button
              type="button"
              className="list-item list-item--action"
              onClick={() => openCreate(actionsTarget)}
            >
              <span className="list-item__icon" aria-hidden="true">
                ➕
              </span>
              <span className="list-item__title">Создать подпапку</span>
            </button>
          </li>
          <li>
            <button
              type="button"
              className="list-item"
              onClick={() => openRename(actionsTarget)}
            >
              <span className="list-item__icon" aria-hidden="true">
                ✏️
              </span>
              <span className="list-item__title">Переименовать</span>
            </button>
          </li>
          <li>
            <button type="button" className="list-item" onClick={() => openMove(actionsTarget)}>
              <span className="list-item__icon" aria-hidden="true">
                📦
              </span>
              <span className="list-item__title">Переместить</span>
            </button>
          </li>
          <li>
            <button type="button" className="list-item" onClick={() => openDelete(actionsTarget)}>
              <span className="list-item__icon" aria-hidden="true">
                🗑
              </span>
              <span className="list-item__title">Удалить</span>
            </button>
          </li>
        </ul>
      </Modal>

      {/* Создание папки */}
      <Modal open={createOpen} title="Новая папка" onClose={closeCreate} footer={createFooter}>
        <div className="field">
          <label className="field__label" htmlFor="folder-create-name">
            Название
          </label>
          <input
            id="folder-create-name"
            className="input"
            type="text"
            value={createName}
            maxLength={NAME_MAX_LENGTH}
            placeholder="Например: Любимое"
            onChange={(event) => setCreateName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                handleCreate()
              }
            }}
            disabled={creating}
            autoComplete="off"
          />
        </div>

        <FolderSelect
          id="folder-create-parent"
          label="Где создать"
          value={createParent}
          onChange={setCreateParent}
          options={flat}
          disabled={creating}
          rootLabel="🏠 В корне раздела"
        />

        <p className="field__hint">
          Папки можно вкладывать друг в друга — например «Рок» → «Русский рок».
        </p>
      </Modal>

      {/* Переименование */}
      <Modal
        open={Boolean(renameTarget)}
        title="Переименовать папку"
        onClose={closeRename}
        footer={renameFooter}
      >
        <div className="field">
          <label className="field__label" htmlFor="folder-rename">
            Новое название
          </label>
          <input
            id="folder-rename"
            className="input"
            type="text"
            value={renameValue}
            maxLength={NAME_MAX_LENGTH}
            placeholder="Например: Любимое"
            onChange={(event) => setRenameValue(event.target.value)}
            onKeyDown={handleRenameKeyDown}
            disabled={renaming}
            autoComplete="off"
          />
        </div>
      </Modal>

      {/* Перемещение */}
      <Modal
        open={Boolean(moveTarget)}
        title="Переместить папку"
        onClose={closeMove}
        footer={moveFooter}
      >
        <p className="modal__message">
          Папка «{moveTarget?.name}» переедет вместе со всем содержимым.
        </p>

        <FolderSelect
          id="folder-move-parent"
          label="Новое расположение"
          value={moveParent}
          onChange={setMoveParent}
          options={moveOptions}
          disabled={moving}
          rootLabel="🏠 В корень раздела"
        />

        <p className="field__hint">
          Папку нельзя перенести в саму себя или в свою подпапку — такие варианты в списке не
          показаны.
        </p>
      </Modal>

      {/* Удаление */}
      <Modal
        open={Boolean(deleteTarget)}
        title="Удалить папку?"
        onClose={closeDelete}
        footer={deleteFooter}
      >
        <p className="modal__message">
          Папка «{deleteTarget?.name}» будет удалена. Внутри{' '}
          {formatCount(deleteTracksTotal, TRACK_FORMS)}
          {deleteSubfolders > 0
            ? ` и ${formatCount(deleteSubfolders, SUBFOLDER_FORMS)} — они удалятся вместе с ней.`
            : '.'}
        </p>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={deleteTracks}
            onChange={(event) => setDeleteTracks(event.target.checked)}
            disabled={deleting}
          />
          <span className="checkbox__label">Удалить треки вместе с папкой</span>
        </label>

        <p className="muted">
          {deleteTracks
            ? 'Треки будут удалены из библиотеки безвозвратно.'
            : 'Треки останутся в библиотеке — они просто станут «без папки».'}
        </p>
      </Modal>
    </div>
  )
}

export default FoldersPage
