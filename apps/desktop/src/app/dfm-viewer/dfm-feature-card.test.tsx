import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { DfmFeatureCard } from './dfm-feature-card'
import { featurePresentation, formatFeatureConfidence } from './dfm-feature-presentation'
import type { GeometryReference } from './dfm-viewer-geometry'
import { formatViewerValue } from './dfm-viewer-issues'

afterEach(cleanup)

const refs: GeometryReference[] = [{ kind: 'face', index: 12 }, { kind: 'face', index: 3 }]
const feature = {
  feature_id: 'feature_blend_a_reproducible_full_identity', kind: 'blend', confidence: 0.83666,
  properties: { geometry_refs: refs, subtype: 'edge_blend_chain', parameters: { radius_mm: 2.125 } }
}

describe('feature presentation', () => {
  it.each([[0, '0%'], [1, '100%'], [0.83666, '83.7%'], [0.83, '83%']])(
    'preserves valid confidence %s while formatting at most one decimal', (value, label) => {
      expect(formatFeatureConfidence(value)).toEqual({ label, value })
    })

  it('distinguishes unavailable scores from missing scores without inventing a zero', () => {
    for (const value of [null, undefined]) {
      expect(formatFeatureConfidence(value)).toEqual({ label: '未提供', value: null })
    }
    expect(formatFeatureConfidence(0.98, { confidence_available: false })).toEqual({ label: '未提供', value: null })
    for (const value of [-0.01, 1.01, NaN, Infinity, -Infinity, '0.8', false, {}]) {
      expect(formatFeatureConfidence(value)).toEqual({ label: '不可用', value: null })
    }
  })

  it('presents nested review and flat viewer inputs alike, keeping source scores and refs intact', () => {
    const before = structuredClone(feature)
    const flat = {
      feature_id: feature.feature_id, kind: feature.kind, confidence: feature.confidence,
      geometry_refs: refs, subtype: 'edge_blend_chain', parameters: { radius_mm: 2.125 }
    }
    expect(featurePresentation(feature)).toEqual(featurePresentation(flat))
    expect(featurePresentation({ ...flat, parameters: feature.properties })).toEqual(featurePresentation(flat))
    expect(featurePresentation(feature)).toMatchObject({
      label: '圆角链', subtype: '边圆角链', parameters: '半径 2.125 mm',
      confidence: { label: '83.7%', value: 0.83666 }
    })
    expect(feature).toEqual(before)
    expect(featurePresentation({ ...feature, kind: 'rib' }).label).toBe('加强筋')
    expect(featurePresentation({ ...feature, kind: 'future_kind' }).label).toBe('future_kind')
  })

  it('counts unique valid faces, never treating edge references as faces', () => {
    const input = {
      ...feature, geometry_refs: [...refs, refs[0], { kind: 'edge', index: 22 }, { kind: 'face', index: 0 }] as GeometryReference[]
    }
    expect(featurePresentation(input).faceRefs.map(ref => ref.index)).toEqual([3, 12])
    expect(featurePresentation({ ...feature, properties: {} }).faceRefs).toEqual([])
  })

  it('uses the existing viewer precision for dimension parameters', () => {
    const radius = 0.000012345678912345
    const display = featurePresentation({ ...feature, parameters: { radius_mm: radius } })
    expect(display.parameters).toBe(`半径 ${formatViewerValue(radius)} mm`)
    expect(display.parameters).not.toBe('半径 0 mm')
  })

  it('honors score availability metadata in both source shapes', () => {
    expect(featurePresentation({ ...feature, properties: { ...feature.properties, quality: { confidence_available: false } } })
      .confidence).toEqual({ label: '未提供', value: null })
    expect(featurePresentation({ ...feature, parameters: { quality: { confidence_available: false } } })
      .confidence).toEqual({ label: '未提供', value: null })
  })
})

describe('compact feature card', () => {
  it('keeps selection, location, editing and confirmation independent', () => {
    const onSelect = vi.fn(), onLocate = vi.fn(), onEdit = vi.fn(), onConfirm = vi.fn(), onCheck = vi.fn()
    render(<DfmFeatureCard confirmed={false} feature={feature} featureNumber={8}
      onConfirm={onConfirm} onEdit={onEdit} onLocate={onLocate} onSelect={onSelect}
      selection={<input aria-label={`选择 ${feature.feature_id}`} onChange={onCheck} type="checkbox" />} />)
    expect(screen.getByText('待确认')).toBeTruthy()
    expect(screen.getByText('83.7%')).toBeTruthy()
    expect(screen.getByText('关联面 2 个')).toBeTruthy()
    const title = screen.getByRole('button', { name: '圆角链 #8' })
    expect(title.getAttribute('title')).toBe(feature.feature_id)
    expect(screen.getByTitle(/不代表 DFM 合格率/)).toBeTruthy()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: '定位' }))
    fireEvent.click(screen.getByRole('button', { name: '编辑选面' }))
    fireEvent.click(screen.getByRole('button', { name: '确认' }))
    expect(onCheck).toHaveBeenCalledTimes(1)
    expect(onLocate).toHaveBeenCalledTimes(1)
    expect(onEdit).toHaveBeenCalledTimes(1)
    expect(onConfirm).toHaveBeenCalledTimes(1)
    expect(onSelect).not.toHaveBeenCalled()
    fireEvent.click(title)
    expect(onSelect).toHaveBeenCalledTimes(1)
  })

  it('keeps the original score and stable number after confirmation, with details separate from selection', () => {
    const onSelect = vi.fn(), onDelete = vi.fn(), onConfirm = vi.fn()
    const display = (confirmed: boolean) => <DfmFeatureCard confirmed={confirmed} feature={feature} featureNumber={8}
      onConfirm={onConfirm} onDelete={onDelete} onSelect={onSelect} />
    const { rerender } = render(display(false))
    const details = screen.getByText('详情').closest('details')!
    expect(details.open).toBe(false)
    fireEvent.click(screen.getByText('详情'))
    expect(details.open).toBe(true)
    expect(screen.getByText('边圆角链')).toBeTruthy()
    expect(screen.getByText('半径 2.125 mm')).toBeTruthy()
    expect(screen.getByText(feature.feature_id)).toBeTruthy()
    expect(screen.getByText('face #3、face #12')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '删除特征' }))
    expect(onDelete).toHaveBeenCalledTimes(1)
    expect(onSelect).not.toHaveBeenCalled()
    rerender(display(true))
    expect(screen.getByText('已确认')).toBeTruthy()
    expect(screen.getByText('83.7%')).toBeTruthy()
    expect(screen.getByRole('button', { name: '圆角链 #8' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: '确认' })).toBeNull()
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('disables all supplied actions during parent editing without hiding details', () => {
    const action = vi.fn()
    render(<DfmFeatureCard confirmed={false} disabled feature={feature} featureNumber={8}
      notice="上方编辑中" onConfirm={action} onDelete={action} onEdit={action} onLocate={action} onSelect={action} />)
    fireEvent.click(screen.getByText('详情'))
    for (const button of screen.getAllByRole('button')) {
      expect((button as HTMLButtonElement).disabled).toBe(true)
      fireEvent.click(button)
    }
    expect(action).not.toHaveBeenCalled()
    expect(screen.getByText('上方编辑中')).toBeTruthy()
    expect(screen.getByText(feature.feature_id)).toBeTruthy()
  })
})
