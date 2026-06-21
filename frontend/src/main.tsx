import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { applyTheme, readStoredTheme } from './state/useTheme'

// Apply the saved theme before first paint to avoid a flash of the wrong theme.
applyTheme(readStoredTheme())

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
