interface KeyboardHelpProps {
  onClose: () => void
}

const SHORTCUTS = [
  { key: 'A', description: 'Approve segment' },
  { key: 'M', description: 'Mark as maybe' },
  { key: 'X', description: 'Reject segment' },
  { key: 'J', description: 'Next segment' },
  { key: 'K', description: 'Previous segment' },
  { key: 'Space', description: 'Play / pause audio' },
  { key: 'R', description: 'Restart audio' },
  { key: 'E', description: 'Edit transcript' },
  { key: '[', description: 'Slower playback' },
  { key: ']', description: 'Faster playback' },
  { key: '?', description: 'Show / hide shortcuts' },
]

export function KeyboardHelp({ onClose }: KeyboardHelpProps) {
  return (
    /* Overlay */
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={onClose}
    >
      {/* Panel */}
      <div
        className="bg-white rounded-lg shadow-xl p-6 w-80 max-w-full"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold text-gray-800">Keyboard shortcuts</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 text-xl leading-none focus:outline-none"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <table className="w-full text-sm">
          <tbody>
            {SHORTCUTS.map(({ key, description }) => (
              <tr key={key} className="border-b border-gray-100 last:border-0">
                <td className="py-1.5 pr-4 w-10">
                  <kbd className="inline-block px-1.5 py-0.5 rounded bg-gray-100 text-gray-700 font-mono text-xs border border-gray-300">
                    {key}
                  </kbd>
                </td>
                <td className="py-1.5 text-gray-600">{description}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <p className="mt-4 text-xs text-gray-400 text-center">
          Shortcuts active when detail panel has focus. Esc / Enter work inside transcript editor.
        </p>
      </div>
    </div>
  )
}
