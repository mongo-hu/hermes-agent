import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { DfmAnnotationEditor } from './dfm-annotation-editor'
import { resolveReviewDisplayRefs } from './dfm-viewer-geometry'

afterEach(cleanup)

it('requires selected faces and a supported value before saving', () => {
  const onValue = vi.fn(),
    onSave = vi.fn(),
    onCancel = vi.fn()
  const props = {
    identity: 'a',
    value: 'appearance',
    values: ['appearance', 'non_appearance'],
    busy: false,
    onValue,
    onSave,
    onCancel
  }
  const view = render(<DfmAnnotationEditor {...props} faceCount={0} />)
  expect((screen.getByRole('button', { name: '保存属性' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('面属性值'), { target: { value: 'non_appearance' } })
  expect(onValue).toHaveBeenCalledWith('non_appearance')
  view.rerender(<DfmAnnotationEditor {...props} faceCount={2} />)
  fireEvent.click(screen.getByRole('button', { name: '保存属性' }))
  expect(onSave).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  expect(onCancel).toHaveBeenCalledOnce()
})

it('locates edge attributes through adjacent faces, and refuses another topology', () => {
  const identity = { input_sha256: 'a'.repeat(64), topology_snapshot_id: 'topology' }
  const face = { ...identity, kind: 'face' as const, index: 2 }
  const edge = { ...identity, kind: 'edge' as const, index: 7 }
  const faces = [{ geometry_ref: face, triangle_refs: [] }]
  const edges = [{ geometry_ref: edge, adjacent_face_indices: [2] }]
  expect(resolveReviewDisplayRefs([edge], faces, edges)).toEqual([face])
  expect(resolveReviewDisplayRefs([{ ...edge, topology_snapshot_id: 'other' }], faces, edges)).toEqual([])
  expect(resolveReviewDisplayRefs([{ ...face, input_sha256: 'other' }], faces, edges)).toEqual([])
})
