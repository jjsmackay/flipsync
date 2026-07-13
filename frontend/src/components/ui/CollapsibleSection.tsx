import { forwardRef, useImperativeHandle, useRef, useState } from 'react'

export interface CollapsibleSectionHandle {
  /** Expand the section (used by the gear button and the Train chip). */
  open: () => void
  /** The section element, for scrollIntoView. */
  el: HTMLElement | null
}

interface CollapsibleSectionProps {
  title: string
  /** Persistence key; the open/closed override is stored per key, global across projects. */
  sectionKey: string
  /** Smart default when the user has no stored override for this key. */
  defaultOpen: boolean
  children: React.ReactNode
}

const STORAGE_PREFIX = 'flipsync:section:'

// localStorage can throw (private mode, disabled storage) — degrade to in-memory.
function readOverride(sectionKey: string): boolean | null {
  try {
    const v = localStorage.getItem(STORAGE_PREFIX + sectionKey)
    if (v === 'open') return true
    if (v === 'closed') return false
  } catch {
    /* ignore */
  }
  return null
}

function writeOverride(sectionKey: string, open: boolean): void {
  try {
    localStorage.setItem(STORAGE_PREFIX + sectionKey, open ? 'open' : 'closed')
  } catch {
    /* ignore */
  }
}

export const CollapsibleSection = forwardRef<CollapsibleSectionHandle, CollapsibleSectionProps>(
  function CollapsibleSection({ title, sectionKey, defaultOpen, children }, ref) {
    // A stored explicit toggle wins over the smart default. Seeded once at mount:
    // the smart default is a starting point, not reactive to later stage changes.
    const [open, setOpen] = useState<boolean>(() => readOverride(sectionKey) ?? defaultOpen)
    const sectionRef = useRef<HTMLElement>(null)

    useImperativeHandle(ref, () => ({
      open: () => {
        setOpen(true)
        writeOverride(sectionKey, true)
      },
      el: sectionRef.current,
    }))

    function toggle() {
      setOpen((prev) => {
        const next = !prev
        writeOverride(sectionKey, next)
        return next
      })
    }

    return (
      <section ref={sectionRef}>
        <button
          onClick={toggle}
          aria-expanded={open}
          className="flex items-center gap-2 text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-3 hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
        >
          <span className={`inline-block transition-transform ${open ? 'rotate-90' : ''}`}>▸</span>
          {title}
        </button>
        {open && children}
      </section>
    )
  },
)
