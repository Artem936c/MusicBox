/** Утилиты форматирования значений для интерфейса (все подписи — русские). */

/** Сокращённые названия месяцев в родительном падеже. */
const MONTHS_SHORT = [
  'янв.',
  'февр.',
  'мар.',
  'апр.',
  'мая',
  'июн.',
  'июл.',
  'авг.',
  'сент.',
  'окт.',
  'нояб.',
  'дек.',
]

/**
 * Длительность в человеческом виде: 187 -> "3:07", 3723 -> "1:02:03".
 * Пусто / 0 / некорректное значение -> "—".
 */
export function formatDuration(seconds) {
  const total = Math.floor(Number(seconds))
  if (!Number.isFinite(total) || total <= 0) return '—'
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  const pad = (value) => String(value).padStart(2, '0')
  if (hours > 0) return `${hours}:${pad(minutes)}:${pad(secs)}`
  return `${minutes}:${pad(secs)}`
}

/**
 * Разбирает дату из формата БД ("2026-09-06 10:00:00", UTC) или ISO-строки.
 * @returns {Date|null}
 */
export function parseDate(value) {
  if (!value) return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  if (typeof value === 'number') {
    const fromNumber = new Date(value)
    return Number.isNaN(fromNumber.getTime()) ? null : fromNumber
  }
  const text = String(value).trim()
  if (!text) return null
  // Формат SQLite: "YYYY-MM-DD HH:MM:SS" в UTC — приводим к ISO с суффиксом Z.
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(text)
    ? `${text.replace(' ', 'T')}Z`
    : text
  const date = new Date(normalized)
  return Number.isNaN(date.getTime()) ? null : date
}

/** Дата в виде "6 сент. 2026". Пустое/битое значение -> "—". */
export function formatDate(value) {
  const date = parseDate(value)
  if (!date) return '—'
  return `${date.getDate()} ${MONTHS_SHORT[date.getMonth()]} ${date.getFullYear()}`
}

/** Дата и время: "6 сент. 2026, 13:05". */
export function formatDateTime(value) {
  const date = parseDate(value)
  if (!date) return '—'
  const hours = String(date.getHours()).padStart(2, '0')
  const minutes = String(date.getMinutes()).padStart(2, '0')
  return `${formatDate(value)}, ${hours}:${minutes}`
}

/**
 * Подбирает форму слова по числу.
 * @param {number} n число
 * @param {string[]} forms ['трек', 'трека', 'треков']
 */
export function pluralize(n, forms) {
  const safeForms = Array.isArray(forms) && forms.length === 3 ? forms : ['', '', '']
  const count = Math.abs(Math.floor(Number(n) || 0))
  const mod10 = count % 10
  const mod100 = count % 100
  if (mod10 === 1 && mod100 !== 11) return safeForms[0]
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return safeForms[1]
  return safeForms[2]
}

/**
 * Число + правильная форма слова: formatCount(5, ['трек','трека','треков']) -> "5 треков".
 */
export function formatCount(n, forms) {
  const count = Math.floor(Number(n) || 0)
  const word = pluralize(count, forms)
  return word ? `${count} ${word}` : String(count)
}

/** Размер файла: 4914534 -> "4,7 МБ". */
export function formatFileSize(bytes) {
  const size = Number(bytes)
  if (!Number.isFinite(size) || size <= 0) return '—'
  const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ']
  let value = size
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  const digits = unitIndex === 0 ? 0 : value < 10 ? 1 : 0
  return `${value.toFixed(digits).replace('.', ',')} ${units[unitIndex]}`
}

/** Количество прослушиваний: 0 -> "ни разу", иначе "5 прослушиваний". */
export function formatPlays(count) {
  const plays = Math.floor(Number(count) || 0)
  if (plays <= 0) return 'ни разу'
  return formatCount(plays, ['прослушивание', 'прослушивания', 'прослушиваний'])
}
