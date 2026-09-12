import { createRoot } from 'react-dom/client'

import App from './App.jsx'
import { applyTheme, initTelegram } from './telegram.js'
import './styles.css'

// Сообщаем Telegram о готовности и подтягиваем цвета темы ДО первого рендера,
// чтобы приложение сразу отрисовалось в нужной цветовой схеме.
initTelegram()
applyTheme()

const container = document.getElementById('root')
if (!container) {
  throw new Error('Не найден контейнер #root в index.html')
}

// StrictMode намеренно не используется: он дважды монтирует эффекты в dev-режиме,
// из-за чего плеер успевал бы дважды засчитать прослушивание трека.
createRoot(container).render(<App />)
