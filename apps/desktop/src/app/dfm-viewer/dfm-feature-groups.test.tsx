import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { DfmFeatureGroups } from './dfm-feature-groups'

afterEach(cleanup)

const features = [
  { feature_id: 'boss-1', kind: 'hollow_boss' },
  { feature_id: 'blend-1', kind: 'blend' },
  { feature_id: 'boss-2', kind: 'hollow_boss' },
  { feature_id: 'blend-2', kind: 'blend' },
  { feature_id: 'other-1', kind: 'new_kind' }
]

const labels = { blend: '圆角链', hollow_boss: '带孔 Boss 柱' }

function openCategory(label: string) {
  const details = screen.getByText(label).closest('details')!
  details.open = true
  fireEvent(details, new Event('toggle'))

  return details
}

describe('DFM feature detail categories', () => {
  it('groups every result, including blends without confirmation groups, and keeps selection identities', () => {
    const onSelect = vi.fn()
    render(
      <DfmFeatureGroups
        activeFeatureElementRef={createRef()}
        activeFeatureId={null}
        editingFeatureId={null}
        features={features}
        labels={labels}
        renderFeature={(feature, index) => (
          <button onClick={() => onSelect(feature.feature_id)}>
            {feature.feature_id} · {index + 1}
          </button>
        )}
      />
    )
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.getByText('带孔 Boss 柱 · 2 个实例').closest('details')?.open).toBe(false)
    const blends = openCategory('圆角链 · 2 个实例')
    expect(within(blends).getAllByRole('button')).toHaveLength(2)
    fireEvent.click(within(blends).getByRole('button', { name: 'blend-2 · 4' }))
    expect(onSelect).toHaveBeenCalledWith('blend-2')
    expect(screen.queryByRole('button', { name: /boss-1/ })).toBeNull()
    const other = openCategory('new_kind · 1 个实例')
    expect(within(other).getByRole('button', { name: 'other-1 · 5' })).toBeTruthy()
    blends.open = false
    fireEvent(blends, new Event('toggle'))
    expect(screen.queryByRole('button', { name: /blend-2/ })).toBeNull()
  })

  it('reveals and scrolls a model selection, keeping unrelated categories closed across refreshes', () => {
    const activeRef = createRef<HTMLDivElement>()
    const scroll = vi.fn()

    const display = (activeFeatureId: string | null, currentFeatures = features) => (
      <DfmFeatureGroups
        activeFeatureElementRef={activeRef}
        activeFeatureId={activeFeatureId}
        editingFeatureId={null}
        features={currentFeatures}
        labels={labels}
        renderFeature={feature => (
          <div
            ref={element => {
              if (feature.feature_id === activeFeatureId && element) {
                element.scrollIntoView = scroll
                activeRef.current = element
              }
            }}
          >
            {feature.feature_id}
          </div>
        )}
      />
    )

    const { rerender } = render(display(null))
    rerender(display('blend-2'))
    expect(screen.getByText('圆角链 · 2 个实例').closest('details')?.open).toBe(true)
    expect(screen.getByText('blend-2')).toBeTruthy()
    expect(scroll).toHaveBeenCalledTimes(1)
    rerender(display('blend-2', [...features, { feature_id: 'blend-3', kind: 'blend' }]))
    expect(screen.getByText('圆角链 · 3 个实例').closest('details')?.open).toBe(true)
    expect(screen.getByText('带孔 Boss 柱 · 2 个实例').closest('details')?.open).toBe(false)
    expect(screen.getByText('blend-3')).toBeTruthy()
  })

  it('reveals an edited feature even when no model selection is active', () => {
    render(
      <DfmFeatureGroups
        activeFeatureElementRef={createRef()}
        activeFeatureId={null}
        editingFeatureId="boss-2"
        features={features}
        labels={labels}
        renderFeature={feature => <button>{feature.feature_id}</button>}
      />
    )
    expect(screen.getByText('带孔 Boss 柱 · 2 个实例').closest('details')?.open).toBe(true)
    expect(screen.getByRole('button', { name: 'boss-2' })).toBeTruthy()
    expect(screen.getByText('圆角链 · 2 个实例').closest('details')?.open).toBe(false)
  })
})
