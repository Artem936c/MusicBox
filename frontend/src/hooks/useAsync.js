import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Универсальный хук асинхронной загрузки данных.
 *
 * @param {Function} fn    асинхронная функция-загрузчик (может принимать AbortSignal)
 * @param {Array} deps     зависимости: при изменении данные перезагружаются
 * @param {object} options { immediate } — грузить ли сразу при монтировании
 * @returns {{ data: any, error: Error|null, loading: boolean, reload: Function, setData: Function }}
 */
export function useAsync(fn, deps = [], { immediate = true } = {}) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(Boolean(immediate))

  // Всегда держим свежую версию загрузчика, не перезапуская эффект из-за неё.
  const fnRef = useRef(fn)
  fnRef.current = fn

  // Флаг «компонент ещё смонтирован» — чтобы не писать в state после размонтирования.
  const mountedRef = useRef(true)
  // Номер последнего запроса — защита от гонок: ответ старого запроса игнорируется.
  const requestIdRef = useRef(0)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const run = useCallback(async () => {
    const requestId = requestIdRef.current + 1
    requestIdRef.current = requestId
    setLoading(true)
    setError(null)
    try {
      const result = await fnRef.current()
      if (!mountedRef.current || requestIdRef.current !== requestId) return undefined
      setData(result)
      setLoading(false)
      return result
    } catch (err) {
      if (err?.name === 'AbortError') return undefined
      if (!mountedRef.current || requestIdRef.current !== requestId) return undefined
      setError(err instanceof Error ? err : new Error(String(err)))
      setLoading(false)
      return undefined
    }
  }, [])

  useEffect(() => {
    if (!immediate) {
      setLoading(false)
      return undefined
    }
    run()
    return undefined
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, error, loading, reload: run, setData }
}

export default useAsync
