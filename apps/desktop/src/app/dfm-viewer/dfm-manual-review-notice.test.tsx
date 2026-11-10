import { fireEvent, render, screen, cleanup } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { DfmManualReviewNotice } from './dfm-manual-review-notice'

afterEach(cleanup)
it('shows unresolved geometry as a review alert and locates its wall', () => {
  const select = vi.fn()
  render(<DfmManualReviewNotice results={[{ result_id: 'r', feature_id: 'wall',
    geometric_id: 'G_WALL_TRANS_LEN', reason: '存在未解析区域' }]} onSelectFeature={select} />)
  expect(screen.getByRole('alert', { name: '需人工复核' })).toBeTruthy()
  expect(screen.getByText('无法判定 · 需人工复核 1 项')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /G_WALL_TRANS_LEN/ }))
  expect(select).toHaveBeenCalledWith('wall')
})
it('does not label ordinary unsupported results as a review alert', () => {
  render(<DfmManualReviewNotice results={[]} onSelectFeature={vi.fn()} />)
  expect(screen.queryByRole('alert')).toBeNull()
})
