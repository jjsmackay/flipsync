import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { UploadArea } from './UploadArea'
import { uploadSource } from '../../api/client'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    uploadSource: vi.fn(),
  }
})

const mockUploadSource = vi.mocked(uploadSource)

function getFileInput(container: HTMLElement): HTMLInputElement {
  return container.querySelector('input[type="file"]') as HTMLInputElement
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('UploadArea — multi-file queue', () => {
  it('uploads multiple files sequentially in order and fires onUploaded per file', async () => {
    const user = userEvent.setup()
    const onUploaded = vi.fn()
    const order: string[] = []
    mockUploadSource.mockImplementation(async (_projectId, file) => {
      order.push(file.name)
      return { id: file.name, filename: file.name, status: 'extracting' }
    })

    const { container } = render(<UploadArea projectId="proj-1" onUploaded={onUploaded} />)

    const files = [
      new File(['a'], 'a.mp4', { type: 'video/mp4' }),
      new File(['b'], 'b.mp4', { type: 'video/mp4' }),
      new File(['c'], 'c.mp4', { type: 'video/mp4' }),
    ]
    await user.upload(getFileInput(container), files)

    await waitFor(() => expect(mockUploadSource).toHaveBeenCalledTimes(3))
    expect(order).toEqual(['a.mp4', 'b.mp4', 'c.mp4'])
    expect(onUploaded).toHaveBeenCalledTimes(3)
  })

  it('continues past a failed file and reports its name and error', async () => {
    const user = userEvent.setup()
    const onUploaded = vi.fn()
    mockUploadSource.mockImplementation(async (_projectId, file) => {
      if (file.name === 'b.mp4') throw new Error('server exploded')
      return { id: file.name, filename: file.name, status: 'extracting' }
    })

    const { container } = render(<UploadArea projectId="proj-1" onUploaded={onUploaded} />)

    const files = [
      new File(['a'], 'a.mp4', { type: 'video/mp4' }),
      new File(['b'], 'b.mp4', { type: 'video/mp4' }),
      new File(['c'], 'c.mp4', { type: 'video/mp4' }),
    ]
    await user.upload(getFileInput(container), files)

    await waitFor(() => expect(mockUploadSource).toHaveBeenCalledTimes(3))
    expect(onUploaded).toHaveBeenCalledTimes(2)
    expect(screen.getByText(/b\.mp4/)).toBeInTheDocument()
    expect(screen.getByText(/server exploded/)).toBeInTheDocument()
  })

  it('clears previous failures when a new upload run starts', async () => {
    const user = userEvent.setup()
    const onUploaded = vi.fn()
    mockUploadSource
      .mockImplementationOnce(async () => {
        throw new Error('first run failure')
      })
      .mockImplementationOnce(async (_projectId, file) => ({ id: file.name, filename: file.name, status: 'extracting' }))

    const { container } = render(<UploadArea projectId="proj-1" onUploaded={onUploaded} />)

    await user.upload(getFileInput(container), new File(['a'], 'a.mp4', { type: 'video/mp4' }))
    await waitFor(() => expect(screen.getByText(/first run failure/)).toBeInTheDocument())

    await user.upload(getFileInput(container), new File(['b'], 'b.mp4', { type: 'video/mp4' }))
    await waitFor(() => expect(mockUploadSource).toHaveBeenCalledTimes(2))
    expect(screen.queryByText(/first run failure/)).not.toBeInTheDocument()
  })

  it('does not show queue count clutter for a single file', async () => {
    const user = userEvent.setup()
    const onUploaded = vi.fn()
    let resolveUpload: () => void = () => {}
    mockUploadSource.mockImplementation(
      (_projectId, file, onProgress) =>
        new Promise((resolve) => {
          onProgress?.(0.5)
          resolveUpload = () => resolve({ id: file.name, filename: file.name, status: 'extracting' })
        }),
    )

    const { container } = render(<UploadArea projectId="proj-1" onUploaded={onUploaded} />)
    await user.upload(getFileInput(container), new File(['a'], 'a.mp4', { type: 'video/mp4' }))

    await waitFor(() => expect(screen.getByText(/uploading/i)).toBeInTheDocument())
    expect(screen.queryByText(/1 of 1/)).not.toBeInTheDocument()

    resolveUpload()
    await waitFor(() => expect(onUploaded).toHaveBeenCalledTimes(1))
  })
})

describe('UploadArea — compact mode', () => {
  it('shows terse queue position and percent while uploading multiple files', async () => {
    const user = userEvent.setup()
    const onUploaded = vi.fn()
    let resolveFirst: () => void = () => {}
    mockUploadSource
      .mockImplementationOnce(
        (_projectId, file, onProgress) =>
          new Promise((resolve) => {
            onProgress?.(0.43)
            resolveFirst = () => resolve({ id: file.name, filename: file.name, status: 'extracting' })
          }),
      )
      .mockImplementationOnce(async (_projectId, file) => ({ id: file.name, filename: file.name, status: 'extracting' }))

    const { container } = render(<UploadArea projectId="proj-1" onUploaded={onUploaded} compact />)

    const files = [
      new File(['a'], 'a.mp4', { type: 'video/mp4' }),
      new File(['b'], 'b.mp4', { type: 'video/mp4' }),
    ]
    await user.upload(getFileInput(container), files)

    await waitFor(() => expect(screen.getByText(/Uploading 1\/2 43%/)).toBeInTheDocument())

    resolveFirst()
    await waitFor(() => expect(onUploaded).toHaveBeenCalledTimes(2))
  })
})
