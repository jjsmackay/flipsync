import { describe, it, expect, beforeEach } from 'vitest'
import { createRef } from 'react'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CollapsibleSection, type CollapsibleSectionHandle } from './CollapsibleSection'

const key = (k: string) => `flipsync:section:${k}`

describe('CollapsibleSection', () => {
  beforeEach(() => localStorage.clear())

  it('honours defaultOpen when there is no stored override', () => {
    render(
      <CollapsibleSection title="Sources" sectionKey="sources" defaultOpen>
        <p>body</p>
      </CollapsibleSection>,
    )
    expect(screen.getByText('body')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /sources/i })).toHaveAttribute('aria-expanded', 'true')
  })

  it('collapses by default when defaultOpen is false', () => {
    render(
      <CollapsibleSection title="Settings" sectionKey="settings" defaultOpen={false}>
        <p>body</p>
      </CollapsibleSection>,
    )
    expect(screen.queryByText('body')).not.toBeInTheDocument()
  })

  it('a stored override wins over the smart default', () => {
    localStorage.setItem(key('sources'), 'closed')
    render(
      <CollapsibleSection title="Sources" sectionKey="sources" defaultOpen>
        <p>body</p>
      </CollapsibleSection>,
    )
    expect(screen.queryByText('body')).not.toBeInTheDocument()
  })

  it('toggling writes the explicit choice through to localStorage', async () => {
    const user = userEvent.setup()
    render(
      <CollapsibleSection title="Sources" sectionKey="sources" defaultOpen>
        <p>body</p>
      </CollapsibleSection>,
    )
    await user.click(screen.getByRole('button', { name: /sources/i }))
    expect(localStorage.getItem(key('sources'))).toBe('closed')
    expect(screen.queryByText('body')).not.toBeInTheDocument()
  })

  it('imperative open() expands a collapsed section', async () => {
    const ref = createRef<CollapsibleSectionHandle>()
    render(
      <CollapsibleSection ref={ref} title="Voice" sectionKey="voice" defaultOpen={false}>
        <p>body</p>
      </CollapsibleSection>,
    )
    expect(screen.queryByText('body')).not.toBeInTheDocument()
    act(() => ref.current!.open())
    expect(screen.getByText('body')).toBeInTheDocument()
  })
})
