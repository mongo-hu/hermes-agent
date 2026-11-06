import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { Box3, Group, Vector3 } from 'three'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { directionFromPose, directionOrigin, directionPose } from './dfm-direction-axis'
import { DfmDirectionEditor, type DirectionDraft } from './dfm-direction-editor'
import { parseDirectionVector, type ReviewScope } from './dfm-review'

afterEach(cleanup)

describe('forming direction editing', () => {
  it('accepts only a finite, nonzero JSON vector; never guesses a local direction', () => {
    for (const text of ['', '[]', '[0,0,0]', '[true,0,1]', '["1",0,0]', '[1,2]', '[NaN,0,1]', '[1e309,0,0]']) {
      expect(parseDirectionVector(text)).toBeNull()
    }

    expect(parseDirectionVector('[0,0,1]')).toEqual([0, 0, 1])
    expect(parseDirectionVector('[1, -2, 0.5]')).toEqual([1, -2, 0.5].map(value => value / Math.hypot(1, -2, 0.5)))
    expect(parseDirectionVector('[0,0,8]')).toEqual([0, 0, 1])
  })

  it('keeps numeric input, axis modes and explicit presets in one draft', () => {
    const save = vi.fn()
    const cancel = vi.fn()

    function Harness() {
      const [draft, setDraft] = useState<DirectionDraft>({
        scope: { forming_scope_id: 'SIDE', kind: 'undercut', mode: 'local', draw_direction: null, geometry_refs: [],
          mechanism_kind: 'undetermined', undercut_geometry_refs: [], candidate_solutions: [], coordinate_system: 'model', direction_source: null },
        revision: 7, text: 'invalid', mode: 'rotate', mechanismKind: 'undetermined'
      })

      return <DfmDirectionEditor busy={false} draft={draft} error="" onCancel={cancel} onChange={setDraft} onSave={() => save(parseDirectionVector(draft.text), draft.mode)} />
    }

    render(<Harness />)
    expect((screen.getByRole('button', { name: '暂存，稍后处理' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByRole('textbox', { name: '方向向量' }), { target: { value: '[0,0,1]' } })
    fireEvent.click(screen.getByRole('button', { name: '平移轴线' }))
    fireEvent.click(screen.getByRole('button', { name: '暂存，稍后处理' }))
    expect(save).toHaveBeenLastCalledWith([0, 0, 1], 'translate')
    fireEvent.click(screen.getByRole('button', { name: '沿 +X' }))
    expect((screen.getByRole('textbox', { name: '方向向量' }) as HTMLInputElement).value).toBe('[1,0,0]')
    fireEvent.click(screen.getByRole('button', { name: '取消修改' }))
    expect(cancel).toHaveBeenCalledOnce()
  })

  it('saves a partial mechanism draft without inventing a direction', () => {
    const save = vi.fn()

    function Harness() {
      const [draft, setDraft] = useState<DirectionDraft>({
        scope: { forming_scope_id: 'SIDE', kind: 'undercut', mode: 'local', draw_direction: null, geometry_refs: [],
          mechanism_kind: 'undetermined', undercut_geometry_refs: [], candidate_solutions: [],
          coordinate_system: 'model', direction_source: null },
        revision: 1, text: '', mechanismKind: 'undetermined', mode: 'rotate'
      })

      return <DfmDirectionEditor busy={false} draft={draft} error="" onCancel={() => {}} onChange={setDraft}
        onSave={() => save(draft.mechanismKind, parseDirectionVector(draft.text))} />
    }

    render(<Harness />)
    fireEvent.change(screen.getByRole('combobox', { name: '成型机构' }), { target: { value: 'lifter' } })
    fireEvent.click(screen.getByRole('button', { name: '暂存，稍后处理' }))
    expect(save).toHaveBeenCalledWith('lifter', null)
    expect(screen.getByText(/补全后才能确认方案/)).toBeTruthy()
  })

  it('loads only the selected candidate surfaces while preserving undercut faces', () => {
    const save = vi.fn()
    const face = (index: number) => ({ kind: 'face' as const, index })

    const scope: ReviewScope = {
      forming_scope_id: 'SIDE', kind: 'undercut', mode: 'local',
      mechanism_kind: 'undetermined', draw_direction: null, coordinate_system: 'model', direction_source: null,
      geometry_refs: [face(1)], undercut_geometry_refs: [face(1)],
      candidate_solutions: [
        { mechanism_kind: 'slider', geometry_refs: [face(2)], draw_direction: [1, 0, 0] },
        { mechanism_kind: 'lifter', geometry_refs: [face(3)], draw_direction: [0, 1, 0] }
      ]
    }

    function Harness() {
      const [draft, setDraft] = useState<DirectionDraft>({
        scope, revision: 1, text: '', mechanismKind: 'undetermined', mode: 'rotate'
      })

      return <DfmDirectionEditor busy={false} draft={draft} error="" onCancel={() => {}} onChange={setDraft}
        onSave={() => save(draft)} />
    }

    render(<Harness />)
    fireEvent.change(screen.getByRole('combobox', { name: '备选成型方案' }), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: '暂存，稍后处理' }))
    expect(save).toHaveBeenCalledWith(expect.objectContaining({
      text: '[1,0,0]', mechanismKind: 'slider',
      scope: expect.objectContaining({ geometry_refs: [face(2)], undercut_geometry_refs: [face(1)] })
    }))
  })

  it('rotates the model-coordinate vector and translates only its display anchor', () => {
    const object = new Group()
    const pose = directionPose([0, 0, 2], new Vector3(3, 4, 5))
    object.position.copy(pose.position)
    object.quaternion.copy(pose.quaternion)
    object.rotateX(Math.PI / 2)
    expect(directionFromPose(object)).toEqual([0, -1, 0])
    object.position.add(new Vector3(100, 200, 300))
    expect(directionFromPose(object)).toEqual([0, -1, 0])
    const index = { bounds: new Box3(new Vector3(-10, -10, -10), new Vector3(10, 10, 10)), faces: new Map() }
    expect(directionOrigin(index as never, []).toArray()).toEqual([0, 0, 0])
  })

  it('confirms a complete edited plan directly while keeping staging a separate action', () => {
    const confirm = vi.fn()
    const save = vi.fn()
    const draft: DirectionDraft = {
      scope: { forming_scope_id: 'SIDE', kind: 'undercut', mode: 'local', draw_direction: [1, 0, 0],
        geometry_refs: [{ kind: 'face', index: 2 }], mechanism_kind: 'slider', undercut_geometry_refs: [],
        candidate_solutions: [], coordinate_system: 'model', direction_source: 'inferred' },
      revision: 7, text: '[1,0,0]', mode: 'rotate', mechanismKind: 'slider'
    }
    const { rerender } = render(<DfmDirectionEditor busy={false} draft={draft} error="" onCancel={vi.fn()}
      onChange={vi.fn()} onConfirm={confirm} onSave={save} />)
    fireEvent.click(screen.getByRole('button', { name: '确认方案' }))
    expect(confirm).toHaveBeenCalledOnce()
    expect(save).not.toHaveBeenCalled()
    rerender(<DfmDirectionEditor busy={false} draft={{ ...draft, text: '' }} error="" onCancel={vi.fn()}
      onChange={vi.fn()} onConfirm={confirm} onSave={save} />)
    expect((screen.getByRole('button', { name: '确认方案' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '暂存，稍后处理' }))
    expect(save).toHaveBeenCalledOnce()
    expect(confirm).toHaveBeenCalledOnce()
  })

  it('edits each face set inside the draft and finishes face selection without submitting', () => {
    const edit = vi.fn()
    const finish = vi.fn()
    const save = vi.fn()
    const confirm = vi.fn()
    const draft: DirectionDraft = {
      scope: { forming_scope_id: 'SIDE', kind: 'undercut', mode: 'local', draw_direction: [1, 0, 0],
        geometry_refs: [{ kind: 'face', index: 2 }], mechanism_kind: 'slider', undercut_geometry_refs: [{ kind: 'face', index: 1 }],
        candidate_solutions: [], coordinate_system: 'model', direction_source: 'inferred' },
      revision: 7, text: '[1,0,0]', mode: 'rotate', mechanismKind: 'slider'
    }
    const props = { busy: false, draft, error: '', onCancel: vi.fn(), onChange: vi.fn(), onConfirm: confirm, onSave: save,
      onEditFaces: edit, onFinishFaces: finish }
    const { rerender } = render(<DfmDirectionEditor {...props} />)
    fireEvent.click(screen.getByRole('button', { name: '调整倒扣面' }))
    expect(edit).toHaveBeenCalledWith('undercut')
    fireEvent.click(screen.getByRole('button', { name: '调整成型面' }))
    expect(edit).toHaveBeenCalledWith('forming')
    rerender(<DfmDirectionEditor {...props} faceRole="forming" />)
    for (const name of ['确认方案', '暂存，稍后处理', '旋转轴线', '调整倒扣面']) {
      expect((screen.getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true)
    }
    fireEvent.click(screen.getByRole('button', { name: '结束选面' }))
    expect(finish).toHaveBeenCalledOnce()
    expect(save).not.toHaveBeenCalled()
    expect(confirm).not.toHaveBeenCalled()
  })

  it('offers all signed model axes explicitly', () => {
    const onChange = vi.fn()
    const draft: DirectionDraft = {
      scope: { forming_scope_id: 'SIDE', kind: 'undercut', mode: 'local', draw_direction: null,
        geometry_refs: [], mechanism_kind: 'undetermined', undercut_geometry_refs: [], candidate_solutions: [],
        coordinate_system: 'model', direction_source: null },
      revision: 1, text: '', mode: 'rotate', mechanismKind: 'undetermined'
    }
    render(<DfmDirectionEditor busy={false} draft={draft} error="" onCancel={vi.fn()} onChange={onChange} onSave={vi.fn()} />)
    for (const [label, vector] of [['+X', [1, 0, 0]], ['−X', [-1, 0, 0]], ['+Y', [0, 1, 0]],
      ['−Y', [0, -1, 0]], ['+Z', [0, 0, 1]], ['−Z', [0, 0, -1]]] as const) {
      fireEvent.click(screen.getByRole('button', { name: `沿 ${label}` }))
      expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ text: JSON.stringify(vector) }))
    }
  })
})
