import { HashRouter, Navigate, Route, Routes } from 'react-router-dom'

import Layout from './components/Layout.jsx'
import { PlayerProvider } from './context/PlayerContext.jsx'
import { ToastProvider } from './context/ToastContext.jsx'

import HomePage from './pages/HomePage.jsx'
import StatsPage from './pages/StatsPage.jsx'
import SectionPage from './pages/SectionPage.jsx'
import FoldersPage from './pages/FoldersPage.jsx'
import FolderPage from './pages/FolderPage.jsx'
import SearchPage from './pages/SearchPage.jsx'
import TelegramSearchPage from './pages/TelegramSearchPage.jsx'
import PlaylistsPage from './pages/PlaylistsPage.jsx'
import PlaylistPage from './pages/PlaylistPage.jsx'
import FavouritesPage from './pages/FavouritesPage.jsx'
import ArtistsPage from './pages/ArtistsPage.jsx'
import ArtistPage from './pages/ArtistPage.jsx'
import SettingsPage from './pages/SettingsPage.jsx'
import TracksPage from './pages/TracksPage.jsx'
import OtherPage from './pages/OtherPage.jsx'
import NotesPage from './pages/NotesPage.jsx'
import NotePage from './pages/NotePage.jsx'
import RecommendationsPage from './pages/RecommendationsPage.jsx'

/**
 * Корневой компонент Mini App.
 * HashRouter обязателен: Telegram открывает WebApp по ссылке с параметрами,
 * а сервер отдаёт единственный index.html без серверной маршрутизации.
 */
export default function App() {
  return (
    <HashRouter>
      <ToastProvider>
        <PlayerProvider>
          <Layout>
            <Routes>
              <Route path="/" element={<HomePage />} />
              <Route path="/stats" element={<StatsPage />} />
              <Route path="/section/:key" element={<SectionPage />} />
              <Route path="/folders" element={<FoldersPage />} />
              <Route path="/folders/:id" element={<FolderPage />} />
              <Route path="/search" element={<SearchPage />} />
              <Route path="/tgsearch" element={<TelegramSearchPage />} />
              <Route path="/playlists" element={<PlaylistsPage />} />
              <Route path="/playlists/:id" element={<PlaylistPage />} />
              <Route path="/favourites" element={<FavouritesPage />} />
              <Route path="/artists" element={<ArtistsPage />} />
              <Route path="/artists/:id" element={<ArtistPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              {/* Разделы V2 (контракт V2, раздел 6). */}
              <Route path="/tracks" element={<TracksPage />} />
              <Route path="/other" element={<OtherPage />} />
              <Route path="/notes" element={<NotesPage />} />
              <Route path="/notes/:id" element={<NotePage />} />
              <Route path="/recommendations" element={<RecommendationsPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Layout>
        </PlayerProvider>
      </ToastProvider>
    </HashRouter>
  )
}
