import { useEffect, useMemo, useState } from 'react'
import { Link, NavLink, useLocation } from 'react-router-dom'

import Modal from './Modal.jsx'
import { hapticImpact } from '../telegram'
import { appSectionsExcept } from '../utils/sections.js'

/**
 * Нижняя навигация Mini App.
 * Пропсы (контракт): TabBar() — без пропсов.
 *
 * Пять вкладок остаются прежними (/ , /stats , /folders , /search , /playlists),
 * а шестая кнопка «Ещё» открывает список остальных разделов приложения
 * (контракт V2, раздел 6): Треки, Исполнители, Избранное, Рекомендации,
 * Заметки, Другое, Настройки и поиск в Telegram. Так все страницы достижимы,
 * хотя в самой панели помещается только пять постоянных вкладок.
 */
const TABS = [
  { to: '/', icon: '🏠', label: 'Главная', end: true },
  { to: '/stats', icon: '📊', label: 'Статистика', end: false },
  { to: '/folders', icon: '📁', label: 'Папки', end: false },
  { to: '/search', icon: '🔍', label: 'Поиск', end: false },
  { to: '/playlists', icon: '🎵', label: 'Плейлисты', end: false },
]

/** Маршруты вкладок — они не дублируются в списке «Ещё». */
const TAB_PATHS = TABS.map((tab) => tab.to)

/**
 * Разделы, которых нет в APP_SECTIONS, но которые тоже нужно чем-то открывать.
 * Поиск по Telegram — отдельная страница со своей логикой импорта.
 */
const EXTRA_ITEMS = [
  {
    key: 'tgsearch',
    title: 'Поиск в Telegram',
    icon: '📥',
    path: '/tgsearch',
    description: 'Найти и импортировать музыку',
  },
]

export function TabBar() {
  const location = useLocation()
  const [open, setOpen] = useState(false)

  /** Разделы для листа «Ещё»: всё, чего нет во вкладках. */
  const moreItems = useMemo(() => [...appSectionsExcept(TAB_PATHS), ...EXTRA_ITEMS], [])

  // После перехода лист закрывается сам — иначе он остался бы поверх новой страницы.
  useEffect(() => {
    setOpen(false)
  }, [location.pathname])

  const isMoreActive = moreItems.some(
    (item) =>
      location.pathname === item.path || location.pathname.startsWith(`${item.path}/`),
  )

  const moreClassName = [
    'tabbar__item',
    'tabbar__item--more',
    isMoreActive ? 'tabbar__item--active' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <>
      <nav className="tabbar" aria-label="Основная навигация">
        {TABS.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.end}
            onClick={() => hapticImpact('light')}
            className={({ isActive }) =>
              isActive ? 'tabbar__item tabbar__item--active' : 'tabbar__item'
            }
          >
            <span className="tabbar__icon" aria-hidden="true">
              {tab.icon}
            </span>
            <span className="tabbar__label">{tab.label}</span>
          </NavLink>
        ))}

        <button
          type="button"
          className={moreClassName}
          onClick={() => {
            hapticImpact('light')
            setOpen(true)
          }}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-label="Ещё разделы"
        >
          <span className="tabbar__icon" aria-hidden="true">
            ⋯
          </span>
          <span className="tabbar__label">Ещё</span>
        </button>
      </nav>

      <Modal open={open} title="Ещё разделы" onClose={() => setOpen(false)}>
        <ul className="list">
          {moreItems.map((item) => {
            const active =
              location.pathname === item.path || location.pathname.startsWith(`${item.path}/`)
            return (
              <li key={item.key}>
                <Link
                  className={active ? 'list-item list-item--action' : 'list-item'}
                  to={item.path}
                  title={item.description || undefined}
                  aria-current={active ? 'page' : undefined}
                  onClick={() => {
                    hapticImpact('light')
                    setOpen(false)
                  }}
                >
                  <span className="list-item__icon" aria-hidden="true">
                    {item.icon}
                  </span>
                  <span className="list-item__title">{item.title}</span>
                  <span className="list-item__meta" aria-hidden="true">
                    →
                  </span>
                </Link>
              </li>
            )
          })}
        </ul>
      </Modal>
    </>
  )
}

export default TabBar
