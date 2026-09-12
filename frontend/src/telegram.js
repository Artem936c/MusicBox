/**
 * Интеграция с Telegram WebApp SDK.
 *
 * Модуль обязан безопасно работать вне Telegram (обычный браузер, тесты):
 * в этом случае `tg === null`, а все функции превращаются в no-op.
 */

/** Экземпляр Telegram.WebApp или null, если приложение открыто вне Telegram. */
export const tg = typeof window !== 'undefined' ? window.Telegram?.WebApp ?? null : null

/** Признак «мы внутри Telegram». */
export const isTelegram = Boolean(tg)

/** Текущий обработчик кнопки «Назад» — нужен, чтобы корректно его снимать. */
let backButtonHandler = null

/**
 * Преобразует ключ темы Telegram в имя CSS-переменной.
 * bg_color -> --tg-theme-bg-color
 */
function themeVarName(key) {
  return `--tg-theme-${String(key).replace(/_/g, '-')}`
}

/**
 * Проставляет CSS-переменные темы Telegram в document.documentElement.
 * Вне Telegram выставляет только атрибут схемы, чтобы styles.css применил дефолты.
 */
export function applyTheme() {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  const scheme = tg?.colorScheme === 'dark' ? 'dark' : 'light'
  root.dataset.theme = scheme
  root.style.setProperty('color-scheme', scheme)

  const params = tg?.themeParams
  if (params && typeof params === 'object') {
    for (const [key, value] of Object.entries(params)) {
      if (typeof value === 'string' && value) {
        root.style.setProperty(themeVarName(key), value)
      }
    }
    if (params.bg_color) {
      root.style.setProperty('--tg-viewport-bg-color', params.bg_color)
    }
  }

  // Высота вьюпорта — удобна для вёрстки полноэкранных списков.
  if (tg?.viewportStableHeight) {
    root.style.setProperty('--tg-viewport-stable-height', `${tg.viewportStableHeight}px`)
  }
}

/**
 * Инициализация Mini App: сообщаем Telegram о готовности, разворачиваем окно,
 * подписываемся на смену темы и изменение размеров вьюпорта.
 */
export function initTelegram() {
  if (!tg) return
  try {
    tg.ready()
    tg.expand()
    if (typeof tg.disableVerticalSwipes === 'function') {
      // Отключаем «сворачивание свайпом», иначе мешает скроллу списков.
      tg.disableVerticalSwipes()
    }
    if (typeof tg.setHeaderColor === 'function' && tg.themeParams?.secondary_bg_color) {
      tg.setHeaderColor(tg.themeParams.secondary_bg_color)
    }
    tg.onEvent('themeChanged', applyTheme)
    tg.onEvent('viewportChanged', applyTheme)
  } catch (error) {
    console.warn('Не удалось инициализировать Telegram WebApp:', error)
  }
}

/**
 * Строка initData для авторизации запросов к API.
 * Вне Telegram берётся значение из VITE_DEV_INIT_DATA (режим разработки).
 */
export function getInitData() {
  return tg?.initData || import.meta.env.VITE_DEV_INIT_DATA || ''
}

/** Тактильный отклик при действии: light | medium | heavy | rigid | soft. */
export function hapticImpact(style = 'light') {
  try {
    tg?.HapticFeedback?.impactOccurred?.(style)
  } catch {
    /* тактильный отклик не критичен */
  }
}

/** Тактильный отклик о результате: success | warning | error. */
export function hapticNotification(type = 'success') {
  try {
    tg?.HapticFeedback?.notificationOccurred?.(type)
  } catch {
    /* тактильный отклик не критичен */
  }
}

/** Показывает системную кнопку «Назад» и вешает обработчик. */
export function showBackButton(onClick) {
  if (!tg?.BackButton) return
  hideBackButton()
  backButtonHandler = typeof onClick === 'function' ? onClick : null
  if (backButtonHandler) {
    tg.BackButton.onClick(backButtonHandler)
  }
  tg.BackButton.show()
}

/** Прячет системную кнопку «Назад» и снимает обработчик. */
export function hideBackButton() {
  if (!tg?.BackButton) return
  if (backButtonHandler) {
    tg.BackButton.offClick(backButtonHandler)
    backButtonHandler = null
  }
  tg.BackButton.hide()
}
