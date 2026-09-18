import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  AssistantRuntimeProvider,
  type ThreadMessage,
  ThreadPrimitive,
  useExternalStoreRuntime
} from '@assistant-ui/react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'
import { clearClarifyRequest, setClarifyRequest } from '@/store/clarify'
import { $gateway } from '@/store/gateway'
import { $activeSessionId } from '@/store/session'
import { clearDismissedToolRows } from '@/store/tool-dismiss'
import { $toolDisclosureStates } from '@/store/tool-view'

import { AssistantMessage } from '../thread/assistant-message'
import { UserMessage } from '../thread/user-message'

// jsdom has no layout engine. Apply the production tool-window rules to the
// real message DOM to cover the CSS/React contract; pixel clipping is verified
// separately in a browser with the complete stylesheet.
const stylesheet = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../../../styles.css'), 'utf8')
const toolWindowRules = stylesheet.match(/^\.tool-group-scroll[^{}]*\{[^}]*\}/gm)?.join('\n') ?? ''
let style: HTMLStyleElement
const question = '注塑模具的脱模方向是什么？'
const choices = ['+Z 方向', '−Z 方向', '需要工程师确认']

vi.stubGlobal(
  'ResizeObserver',
  class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
)
Element.prototype.animate = () => ({ cancel() {}, finished: Promise.resolve() }) as unknown as Animation

function message({ completed = false, count = 3, freeform = false } = {}): ThreadMessage {
  return {
    id: 'assistant-clarify-group',
    role: 'assistant',
    createdAt: new Date('2026-08-30T00:00:00Z'),
    content: Array.from({ length: count }, (_, index) => {
      const last = index === count - 1
      const args = { question: last ? question : `已回答的问题 ${index}`, choices: freeform ? null : choices }

      return {
        type: 'tool-call',
        toolCallId: `clarify-${index}`,
        toolName: 'clarify',
        args,
        argsText: JSON.stringify(args),
        ...(!last || completed ? { result: { answer: '+Z 方向' } } : {})
      }
    }),
    status: completed ? { type: 'complete', reason: 'stop' } : { type: 'running' },
    metadata: { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }
  } as ThreadMessage
}

function Harness({ value }: { value: ThreadMessage }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [value],
    isRunning: value.status?.type === 'running',
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root>
        <ThreadPrimitive.Messages components={{ AssistantMessage, UserMessage }} />
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  )
}

function ready(freeform = false) {
  setClarifyRequest({
    requestId: 'clarify-request',
    sessionId: 'clarify-session',
    question,
    choices: freeform ? null : choices
  })
}

beforeEach(() => {
  $activeSessionId.set('clarify-session')
  $toolDisclosureStates.set({})
  clearDismissedToolRows()
  clearClarifyRequest()
  style = document.createElement('style')
  // Stand in for Tailwind's generated max-height utility with an arbitrary
  // finite cap. Assert the production exception, not a particular pixel size.
  style.textContent = '.tool-group-scroll { max-height: 1px; }\n' + toolWindowRules
  document.head.append(style)
})

afterEach(() => {
  cleanup()
  style.remove()
  clearClarifyRequest()
  clearDismissedToolRows()
  $activeSessionId.set(null)
  $gateway.set(null)
})

describe('clarify controls in a bounded tool group (Taiga #85)', () => {
  it('releases the height cap for the third pending question and restores it after completion', async () => {
    ready()
    const view = render(<Harness value={message()} />)
    const group = view.container.querySelector('.tool-group-scroll')!

    expect(group).not.toBeNull()
    expect(screen.getByText(question)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Continue' })).toBeTruthy()
    expect(getComputedStyle(group).maxHeight).toBe('none')

    view.rerender(<Harness value={message({ completed: true })} />)

    await waitFor(() => expect(view.container.querySelector('[data-slot="clarify-inline"]')).toBeNull())
    expect(getComputedStyle(view.container.querySelector('.tool-group-scroll')!).maxHeight).not.toBe('none')
  })

  it('also releases the cap while the request id is arriving', async () => {
    const view = render(<Harness value={message()} />)

    expect(screen.getByRole('status')).toBeTruthy()
    expect(getComputedStyle(view.container.querySelector('.tool-group-scroll')!).maxHeight).toBe('none')

    act(() => ready())

    expect(await screen.findByRole('button', { name: 'Continue' })).toBeTruthy()
    expect(getComputedStyle(view.container.querySelector('.tool-group-scroll')!).maxHeight).toBe('none')
  })

  it.each([false, true])('keeps the actual answer submission working (freeform=%s)', async freeform => {
    ready(freeform)
    const request = vi.fn().mockResolvedValue({ ok: true })
    $gateway.set({ request } as unknown as HermesGateway)
    const view = render(<Harness value={message({ freeform })} />)

    expect(getComputedStyle(view.container.querySelector('.tool-group-scroll')!).maxHeight).toBe('none')

    if (freeform) {
      fireEvent.change(screen.getByRole('textbox'), { target: { value: '沿 +Z 出模，待工程确认' } })
    } else {
      fireEvent.click(screen.getByRole('button', { name: /A\s*\+Z 方向/ }))
    }

    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

    await waitFor(() =>
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        request_id: 'clarify-request',
        answer: freeform ? '沿 +Z 出模，待工程确认' : '+Z 方向'
      })
    )
  })

  it('keeps a short tool group outside the bounded window', () => {
    ready()
    const view = render(<Harness value={message({ count: 2 })} />)

    expect(view.container.querySelector('.tool-group-scroll')).toBeNull()
    expect(screen.getByRole('button', { name: 'Continue' })).toBeTruthy()
  })
})
