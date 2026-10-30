import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { Box3, Group, Vector3 } from 'three'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { directionFromPose, directionOrigin, directionPose } from './dfm-direction-axis'
import { DfmDirectionEditor, type DirectionDraft } from './dfm-direction-editor'
import { parseDirectionVector } from './dfm-review'

afterEach(cleanup)

describe('forming direction editing', () => {
  it('accepts only a finite, nonzero JSON vector; never guesses a local direction', () => {
    for (const text of ['', '[]', '[0,0,0]', '[true,0,1]', '["1",0,0]', '[1,2]', '[NaN,0,1]', '[1e309,0,0]']) {
      expect(parseDirectionVector(text)).toBeNull()
    }

    expect(parseDirectionVector('[0,0,1]')).toEqual([0, 0, 1])
    expect(parseDirectionVector('[1, -2, 0.5]')).toEqual([1, -2, 0.5])
  })

  it('keeps numeric input, axis modes and explicit presets in one draft', () => {
    const save = vi.fn()
    const cancel = vi.fn()

    function Harness() {
      const [draft, setDraft] = useState<DirectionDraft>({
        scope: { forming_scope_id: 'SIDE', kind: 'undercut', mode: 'local', draw_direction: null, geometry_refs: [] },
        revision: 7, text: '', mode: 'rotate'
      })

      return <DfmDirectionEditor busy={false} draft={draft} error="" onCancel={cancel} onChange={setDraft} onSave={() => save(parseDirectionVector(draft.text), draft.mode)} />
    }

    render(<Harness />)
    expect((screen.getByRole('button', { name: '保存方向' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByRole('textbox', { name: '方向向量' }), { target: { value: '[0,0,1]' } })
    fireEvent.click(screen.getByRole('button', { name: '平移轴线' }))
    fireEvent.click(screen.getByRole('button', { name: '保存方向' }))
    expect(save).toHaveBeenLastCalledWith([0, 0, 1], 'translate')
    fireEvent.click(screen.getByRole('button', { name: '沿 X' }))
    expect((screen.getByRole('textbox', { name: '方向向量' }) as HTMLInputElement).value).toBe('[1,0,0]')
    fireEvent.click(screen.getByRole('button', { name: '取消方向编辑' }))
    expect(cancel).toHaveBeenCalledOnce()
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
