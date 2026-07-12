import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

export type ThemeMode = 'light' | 'dark' | 'system'
type Resolved = 'light' | 'dark'

const STORAGE_KEY = 'flipsync-theme'
const ORDER: ThemeMode[] = ['light', 'dark', 'system']

function readStoredMode(): ThemeMode {
  const stored = localStorage.getItem(STORAGE_KEY)
  return stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'system'
}

function resolve(mode: ThemeMode): Resolved {
  if (mode === 'system') {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  }
  return mode
}

function applyClass(resolved: Resolved) {
  document.documentElement.classList.toggle('dark', resolved === 'dark')
}

interface ThemeContextValue {
  mode: ThemeMode
  resolved: Resolved
  cycle: () => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ThemeMode>(readStoredMode)
  const [resolved, setResolved] = useState<Resolved>(() => resolve(readStoredMode()))

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, mode)
    const next = resolve(mode)
    setResolved(next)
    applyClass(next)
  }, [mode])

  // Live-update when mode is 'system' and the OS theme changes while the tab is open.
  useEffect(() => {
    if (mode !== 'system') return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    function onChange() {
      const next = resolve('system')
      setResolved(next)
      applyClass(next)
    }
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [mode])

  function cycle() {
    setMode((current) => ORDER[(ORDER.indexOf(current) + 1) % ORDER.length])
  }

  return (
    <ThemeContext.Provider value={{ mode, resolved, cycle }}>
      {children}
    </ThemeContext.Provider>
  )
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used within a ThemeProvider')
  return ctx
}
