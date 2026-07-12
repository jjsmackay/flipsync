import { useTheme } from '../../hooks/useTheme'

const ICON: Record<string, string> = {
  light: '☀️',
  dark: '🌙',
  system: '🖥️',
}

const LABEL: Record<string, string> = {
  light: 'Light',
  dark: 'Dark',
  system: 'System',
}

export function ThemeToggle() {
  const { mode, cycle } = useTheme()

  return (
    <button
      type="button"
      onClick={cycle}
      title={`Theme: ${LABEL[mode]} (click to change)`}
      className="w-8 h-8 flex items-center justify-center rounded-lg text-sm border border-gray-200 bg-white hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:hover:bg-gray-700/50 transition-colors"
    >
      <span aria-hidden="true">{ICON[mode]}</span>
      <span className="sr-only">Theme: {LABEL[mode]}</span>
    </button>
  )
}
