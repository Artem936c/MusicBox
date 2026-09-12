import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import Loader from '../components/Loader'
import { api } from '../api/client'
import { useToast } from '../context/ToastContext'
import { useAsync } from '../hooks/useAsync'
import { hapticImpact, hapticNotification } from '../telegram'
import { SECTIONS } from '../utils/sections'

/**
 * Страница настроек (маршрут /settings).
 *
 * Здесь живут персональные настройки пользователя (`GET/PATCH /settings`):
 * автосортировка загрузок по исполнителю, границы разделов статистики
 * («часто» и «редко прослушиваемые») и порог нечёткого поиска.
 * Ниже — блок «О приложении» с версией и краткой справкой по разделам.
 */

/** Версия Mini App; при отсутствии переменной сборки — версия из package.json. */
const APP_VERSION = import.meta.env.VITE_APP_VERSION || '1.0.0'

/** Верхняя граница числовых настроек (совпадает с проверкой backend). */
const MAX_COUNT_VALUE = 1000000

/** Значения по умолчанию — на случай пустого ответа сервера. */
const DEFAULT_SETTINGS = {
  auto_sort_enabled: true,
  frequent_threshold: 10,
  rare_min: 1,
  rare_max: 5,
  fuzzy_threshold: 60,
}

/** Числовые поля формы. */
const NUMBER_FIELDS = ['frequent_threshold', 'rare_min', 'rare_max', 'fuzzy_threshold']

/** Человеческий текст ошибки из ApiError или обычной ошибки. */
function errorText(error, fallback) {
  const detail = error?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  const message = error?.message
  if (typeof message === 'string' && message.trim()) return message
  return fallback
}

/** Приводит ответ сервера к полному набору настроек. */
function normalizeSettings(payload) {
  const source = payload && typeof payload === 'object' ? payload : {}
  const int = (value, fallback) => {
    const parsed = Number.parseInt(value, 10)
    return Number.isFinite(parsed) ? parsed : fallback
  }
  return {
    auto_sort_enabled:
      source.auto_sort_enabled === undefined
        ? DEFAULT_SETTINGS.auto_sort_enabled
        : Boolean(source.auto_sort_enabled),
    frequent_threshold: int(source.frequent_threshold, DEFAULT_SETTINGS.frequent_threshold),
    rare_min: int(source.rare_min, DEFAULT_SETTINGS.rare_min),
    rare_max: int(source.rare_max, DEFAULT_SETTINGS.rare_max),
    fuzzy_threshold: int(source.fuzzy_threshold, DEFAULT_SETTINGS.fuzzy_threshold),
  }
}

/** Числа в форме держим строками — иначе поле нельзя очистить при вводе. */
function toForm(settings) {
  return {
    auto_sort_enabled: settings.auto_sort_enabled,
    frequent_threshold: String(settings.frequent_threshold),
    rare_min: String(settings.rare_min),
    rare_max: String(settings.rare_max),
    fuzzy_threshold: String(settings.fuzzy_threshold),
  }
}

/**
 * Проверяет числовые поля формы теми же правилами, что и backend.
 * @returns {{ values: object|null, errors: object }}
 */
function validate(form) {
  const errors = {}
  const values = {}

  for (const field of NUMBER_FIELDS) {
    const raw = String(form[field] ?? '').trim()
    if (!raw) {
      errors[field] = 'Укажите значение'
      continue
    }
    if (!/^\d+$/.test(raw)) {
      errors[field] = 'Нужно целое число'
      continue
    }
    values[field] = Number.parseInt(raw, 10)
  }

  if (values.frequent_threshold !== undefined) {
    if (values.frequent_threshold < 1) {
      errors.frequent_threshold = 'Порог «часто прослушиваемых» — не меньше 1'
    } else if (values.frequent_threshold > MAX_COUNT_VALUE) {
      errors.frequent_threshold = `Слишком большое значение (максимум ${MAX_COUNT_VALUE})`
    }
  }
  if (values.rare_min !== undefined && values.rare_min < 1) {
    errors.rare_min = 'Минимум для «редко прослушиваемых» — не меньше 1'
  }
  if (values.rare_max !== undefined) {
    if (values.rare_max > MAX_COUNT_VALUE) {
      errors.rare_max = `Слишком большое значение (максимум ${MAX_COUNT_VALUE})`
    } else if (values.rare_min !== undefined && values.rare_max < values.rare_min) {
      errors.rare_max = 'Максимум не может быть меньше минимума'
    }
  }
  if (values.fuzzy_threshold !== undefined) {
    if (values.fuzzy_threshold > 100) {
      errors.fuzzy_threshold = 'Порог нечёткого поиска — число от 0 до 100'
    }
  }

  const ok = Object.keys(errors).length === 0
  return { values: ok ? values : null, errors }
}

export function SettingsPage() {
  const { toast } = useToast()
  const { data, error, loading } = useAsync(() => api.settings.get(), [])

  // Последние сохранённые настройки и текущее состояние формы.
  const [saved, setSaved] = useState(null)
  const [form, setForm] = useState(null)
  const [saving, setSaving] = useState(false)
  const [togglingAutoSort, setTogglingAutoSort] = useState(false)
  const [touched, setTouched] = useState(false)

  useEffect(() => {
    if (!data) return
    const settings = normalizeSettings(data)
    setSaved(settings)
    setForm(toForm(settings))
    setTouched(false)
  }, [data])

  useEffect(() => {
    if (error) toast(errorText(error, 'Не удалось загрузить настройки'), 'error')
  }, [error, toast])

  const validation = useMemo(() => (form ? validate(form) : { values: null, errors: {} }), [form])

  const dirty = useMemo(() => {
    if (!form || !saved) return false
    return NUMBER_FIELDS.some((field) => String(form[field]).trim() !== String(saved[field]))
  }, [form, saved])

  const setField = (field, value) => {
    setTouched(true)
    setForm((prev) => (prev ? { ...prev, [field]: value } : prev))
  }

  /** Переключатель автосортировки сохраняется сразу — так понятнее. */
  const handleToggleAutoSort = async (event) => {
    const value = event.target.checked
    if (!form || togglingAutoSort) return
    hapticImpact('light')
    setForm((prev) => (prev ? { ...prev, auto_sort_enabled: value } : prev))
    setTogglingAutoSort(true)
    try {
      const updated = normalizeSettings(await api.settings.update({ auto_sort_enabled: value }))
      setSaved((prev) => ({ ...(prev || updated), auto_sort_enabled: updated.auto_sort_enabled }))
      setForm((prev) => (prev ? { ...prev, auto_sort_enabled: updated.auto_sort_enabled } : prev))
      hapticNotification('success')
      toast(
        updated.auto_sort_enabled
          ? 'Автосортировка включена: новые треки попадут в папку исполнителя'
          : 'Автосортировка выключена: бот будет спрашивать папку',
        'success',
      )
    } catch (err) {
      setForm((prev) => (prev ? { ...prev, auto_sort_enabled: !value } : prev))
      hapticNotification('error')
      toast(errorText(err, 'Не удалось изменить автосортировку'), 'error')
    } finally {
      setTogglingAutoSort(false)
    }
  }

  const handleSubmit = async (event) => {
    event.preventDefault()
    if (!form || saving) return
    setTouched(true)
    const { values } = validate(form)
    if (!values) {
      hapticNotification('error')
      toast('Проверьте значения: они выделены под полями', 'error')
      return
    }
    setSaving(true)
    try {
      const updated = normalizeSettings(await api.settings.update(values))
      setSaved(updated)
      setForm(toForm(updated))
      setTouched(false)
      hapticNotification('success')
      toast('Настройки сохранены', 'success')
    } catch (err) {
      hapticNotification('error')
      toast(errorText(err, 'Не удалось сохранить настройки'), 'error')
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => {
    if (!saved) return
    hapticImpact('light')
    setForm(toForm(saved))
    setTouched(false)
  }

  /** Краткая справка по разделам статистики — с учётом текущих порогов. */
  const sectionHints = useMemo(() => {
    const current = saved || DEFAULT_SETTINGS
    return {
      top: 'Треки, которые вы включали чаще всего. Порядок — по числу прослушиваний.',
      recent: 'Последние добавленные в библиотеку треки — новинки сверху.',
      unplayed: 'Треки, которые вы ещё ни разу не включали.',
      frequent: `Треки с ${current.frequent_threshold} и более прослушиваниями.`,
      rare: `Треки, которые вы включали от ${current.rare_min} до ${current.rare_max} раз.`,
    }
  }, [saved])

  const showErrors = touched
  const fieldError = (field) => (showErrors ? validation.errors[field] : null)

  if (loading && !form) return <Loader label="Загружаем настройки…" />

  return (
    <div className="page">
      {!form ? (
        <p className="error" role="alert">
          {errorText(error, 'Настройки недоступны. Попробуйте открыть приложение ещё раз.')}
        </p>
      ) : (
        <>
          <section className="section">
            <div className="section__header">
              <h2 className="section__title">🗂 Загрузка треков</h2>
            </div>
            <div className="card">
              <div className="row row--between">
                <label htmlFor="settings-autosort">
                  <strong>Автосортировка по исполнителю</strong>
                </label>
                <input
                  id="settings-autosort"
                  type="checkbox"
                  checked={Boolean(form.auto_sort_enabled)}
                  onChange={handleToggleAutoSort}
                  disabled={togglingAutoSort}
                  aria-label="Автосортировка по исполнителю"
                />
              </div>
              <p className="field__hint">
                Новый трек сразу попадает в папку с именем исполнителя — папка создаётся
                автоматически. Если выключить, бот будет спрашивать папку для каждого трека.
              </p>
              <p className="field__hint">
                {togglingAutoSort
                  ? 'Сохраняем…'
                  : 'Переключатель сохраняется сразу, без кнопки «Сохранить».'}
              </p>
            </div>
          </section>

          <form className="section" onSubmit={handleSubmit}>
            <div className="section__header">
              <h2 className="section__title">📊 Границы разделов статистики</h2>
            </div>

            <div className="card">
              <div className="field">
                <label className="field__label" htmlFor="settings-frequent">
                  «Часто прослушиваемые»: от скольких прослушиваний
                </label>
                <input
                  id="settings-frequent"
                  className="input"
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={MAX_COUNT_VALUE}
                  step={1}
                  value={form.frequent_threshold}
                  onChange={(event) => setField('frequent_threshold', event.target.value)}
                  disabled={saving}
                />
                <span className="field__hint">
                  Трек попадёт в раздел «Часто прослушиваемые», если его включали столько раз
                  или больше.
                </span>
                {fieldError('frequent_threshold') ? (
                  <span className="error" role="alert">
                    {fieldError('frequent_threshold')}
                  </span>
                ) : null}
              </div>

              <div className="field">
                <label className="field__label" htmlFor="settings-rare-min">
                  «Редко прослушиваемые»: от
                </label>
                <input
                  id="settings-rare-min"
                  className="input"
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={MAX_COUNT_VALUE}
                  step={1}
                  value={form.rare_min}
                  onChange={(event) => setField('rare_min', event.target.value)}
                  disabled={saving}
                />
                {fieldError('rare_min') ? (
                  <span className="error" role="alert">
                    {fieldError('rare_min')}
                  </span>
                ) : null}
              </div>

              <div className="field">
                <label className="field__label" htmlFor="settings-rare-max">
                  «Редко прослушиваемые»: до
                </label>
                <input
                  id="settings-rare-max"
                  className="input"
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={MAX_COUNT_VALUE}
                  step={1}
                  value={form.rare_max}
                  onChange={(event) => setField('rare_max', event.target.value)}
                  disabled={saving}
                />
                <span className="field__hint">
                  В раздел попадут треки, число прослушиваний которых укладывается в этот
                  диапазон включительно.
                </span>
                {fieldError('rare_max') ? (
                  <span className="error" role="alert">
                    {fieldError('rare_max')}
                  </span>
                ) : null}
              </div>
            </div>

            <div className="section__header">
              <h2 className="section__title">🔍 Поиск</h2>
            </div>

            <div className="card">
              <div className="field">
                <label className="field__label" htmlFor="settings-fuzzy">
                  Порог нечёткого поиска: {form.fuzzy_threshold || 0}
                </label>
                <input
                  id="settings-fuzzy"
                  type="range"
                  min={0}
                  max={100}
                  step={1}
                  value={Number.parseInt(form.fuzzy_threshold, 10) || 0}
                  onChange={(event) => setField('fuzzy_threshold', event.target.value)}
                  disabled={saving}
                  aria-label="Порог нечёткого поиска"
                />
                <input
                  className="input"
                  type="number"
                  inputMode="numeric"
                  min={0}
                  max={100}
                  step={1}
                  value={form.fuzzy_threshold}
                  onChange={(event) => setField('fuzzy_threshold', event.target.value)}
                  disabled={saving}
                  aria-label="Порог нечёткого поиска, число"
                />
                <span className="field__hint">
                  Насколько запрос должен совпасть с названием: 0 — покажет почти всё,
                  100 — только точные совпадения. Рекомендуем 60.
                </span>
                {fieldError('fuzzy_threshold') ? (
                  <span className="error" role="alert">
                    {fieldError('fuzzy_threshold')}
                  </span>
                ) : null}
              </div>
            </div>

            <div className="row row--between">
              <button
                type="button"
                className="btn btn--ghost"
                onClick={handleReset}
                disabled={saving || !dirty}
              >
                Отменить изменения
              </button>
              <button type="submit" className="btn btn--primary" disabled={saving || !dirty}>
                {saving ? 'Сохраняем…' : 'Сохранить'}
              </button>
            </div>
            {!dirty && !saving ? (
              <span className="field__hint">Все изменения сохранены.</span>
            ) : null}
          </form>

          <section className="section">
            <div className="section__header">
              <h2 className="section__title">ℹ️ О приложении</h2>
              <span className="muted">версия {APP_VERSION}</span>
            </div>

            <div className="card">
              <p>
                <strong>MusicBox</strong> — ваша музыкальная библиотека в Telegram. Треки лежат
                в приватном канале-хранилище, а приложение показывает их, раскладывает по папкам
                и считает прослушивания.
              </p>
              <p className="muted">
                Пришлите боту аудиофайл — он сохранит его в хранилище. Треки из публичных каналов
                можно найти и импортировать на странице поиска в Telegram.
              </p>
              <div className="row">
                <Link className="btn btn--ghost" to="/artists">
                  🎤 Исполнители
                </Link>
                <Link className="btn btn--ghost" to="/favourites">
                  ⭐ Избранное
                </Link>
                <Link className="btn btn--ghost" to="/tgsearch">
                  🔎 Поиск в Telegram
                </Link>
              </div>
            </div>

            <div className="card">
              <h3 className="card__title">Разделы статистики</h3>
              {SECTIONS.map((section) => (
                <div className="section" key={section.key}>
                  <Link className="list-item" to={`/section/${section.key}`}>
                    <span className="list-item__icon" aria-hidden="true">
                      {section.icon}
                    </span>
                    <span className="list-item__title">{section.title}</span>
                    <span className="list-item__meta">Открыть</span>
                  </Link>
                  <p className="field__hint">{sectionHints[section.key]}</p>
                </div>
              ))}
              <span className="field__hint">
                Счётчик прослушиваний растёт и в приложении, и в боте — разделы обновляются сразу.
              </span>
            </div>
          </section>
        </>
      )}
    </div>
  )
}

export default SettingsPage
