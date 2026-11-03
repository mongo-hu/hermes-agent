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
    expect((screen.getByRole('button', { name: '保存方案草稿' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByRole('textbox', { name: '方向向量' }), { target: { value: '[0,0,1]' } })
    fireEvent.click(screen.getByRole('button', { name: '平移轴线' }))
    fireEvent.click(screen.getByRole('button', { name: '保存方案草稿' }))
    expect(save).toHaveBeenLastCalledWith([0, 0, 1], 'translate')
    fireEvent.click(screen.getByRole('button', { name: '沿 X' }))
    expect((screen.getByRole('textbox', { name: '方向向量' }) as HTMLInputElement).value).toBe('[1,0,0]')
    fireEvent.click(screen.getByRole('button', { name: '取消方向编辑' }))
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
    fireEvent.click(screen.getByRole('button', { name: '保存方案草稿' }))
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
    fireEvent.click(screen.getByRole('button', { name: '保存方案草稿' }))
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
})
