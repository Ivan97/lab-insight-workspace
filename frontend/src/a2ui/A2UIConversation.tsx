import { useEffect, useRef, useState } from 'react'
import { A2uiSurface, type ReactComponentImplementation } from '@a2ui/react/v0_9'
import { MessageProcessor, type A2uiMessage, type SurfaceModel } from '@a2ui/web_core/v0_9'
import { analyticsCatalog, CATALOG_ID } from './catalog'
import { api } from '../api'
import { createUuid } from '../utils/id'

export function A2UIConversation({ conversationId, question, onStreamingChange, onCancelReady }: { conversationId: string; question: string; onStreamingChange?: (streaming: boolean) => void; onCancelReady?: (cancel: (() => Promise<void>) | null) => void }) {
  const processorRef = useRef(new MessageProcessor([analyticsCatalog], undefined, { version: 'v0.9.1' }))
  const [surfaces, setSurfaces] = useState<SurfaceModel<ReactComponentImplementation>[]>([])
  const [error, setError] = useState<string | null>(null)
  const messageIdRef = useRef<string | null>(null)
  const toolGroupRef = useRef<Record<string, unknown> | null>(null)
  const eventStreamRef = useRef<Record<string, unknown>[] | null>(null)
  const onStreamingChangeRef = useRef(onStreamingChange)
  const onCancelReadyRef = useRef(onCancelReady)

  useEffect(() => { onStreamingChangeRef.current = onStreamingChange }, [onStreamingChange])
  useEffect(() => { onCancelReadyRef.current = onCancelReady }, [onCancelReady])

  useEffect(() => {
    const processor = processorRef.current
    const sync = () => setSurfaces(Array.from(processor.model.surfacesMap.values()))
    const created = processor.onSurfaceCreated(sync)
    const deleted = processor.onSurfaceDeleted(sync)
    const controller = new AbortController()
    messageIdRef.current = null
    onStreamingChangeRef.current?.(true)
    const cancel = async () => {
      try {
        if (messageIdRef.current) await api.cancelMessage(conversationId, messageIdRef.current)
      } finally {
        if (messageIdRef.current) {
          const surfaceId = `message:${messageIdRef.current}`
          const cancelledGroup = toolGroupRef.current ? {
            ...toolGroupRef.current,
            calls: ((toolGroupRef.current.calls as Record<string, unknown>[] | undefined) ?? []).map((call) => call.status === 'RUNNING' ? { ...call, status: 'CANCELLED', summary: 'Stopped by user' } : call),
          } : null
          const cancelledEvents = eventStreamRef.current?.map((event) => {
            if (event.type === 'reasoning' && event.status === 'RUNNING') return { ...event, status: 'CANCELLED' }
            if (event.type !== 'tool_group') return event
            return {
              ...event,
              calls: ((event.calls as Record<string, unknown>[] | undefined) ?? []).map((call) => call.status === 'RUNNING' ? { ...call, status: 'CANCELLED', summary: 'Stopped by user' } : call),
            }
          }) ?? null
          processor.processMessages([
            { version: 'v0.9.1', updateDataModel: { surfaceId, path: '/reasoning/status', value: 'CANCELLED' } },
            ...(cancelledGroup ? [{ version: 'v0.9.1' as const, updateDataModel: { surfaceId, path: '/toolGroups/analysis', value: cancelledGroup } }] : []),
            ...(cancelledEvents ? [{ version: 'v0.9.1' as const, updateDataModel: { surfaceId, path: '/events', value: cancelledEvents } }] : []),
            { version: 'v0.9.1', updateDataModel: { surfaceId, path: '/status', value: 'CANCELLED' } },
          ])
          sync()
        }
        controller.abort()
        onStreamingChangeRef.current?.(false)
      }
    }
    onCancelReadyRef.current?.(cancel)
    void streamMessage(conversationId, question, controller.signal, (messageId) => { messageIdRef.current = messageId }, (message) => {
      const update = (message as { updateDataModel?: { path?: string; value?: unknown } }).updateDataModel
      if (update?.path === '/toolGroups/analysis') toolGroupRef.current = update.value as Record<string, unknown>
      if (update?.path === '/events') eventStreamRef.current = update.value as Record<string, unknown>[]
      processor.processMessages([message])
      sync()
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Unable to stream analysis') })
      .finally(() => { onCancelReadyRef.current?.(null); onStreamingChangeRef.current?.(false) })
    return () => { onCancelReadyRef.current?.(null); controller.abort(); created.unsubscribe(); deleted.unsubscribe() }
  }, [conversationId, question])

  if (error) return <div className="stream-error">{error}<button onClick={() => window.location.reload()}>Retry</button></div>
  if (!surfaces.length) return <div className="assistant-loading"><i /><span>Creating analysis surface…</span></div>
  return <div className="assistant-message">{surfaces.map((surface) => <A2uiSurface key={surface.id} surface={surface} />)}</div>
}

async function streamMessage(conversationId: string, question: string, signal: AbortSignal, onMessageId: (messageId: string) => void, onMessage: (message: A2uiMessage) => void) {
  const response = await fetch(`/api/v1/conversations/${conversationId}/messages/stream`, {
    method: 'POST', signal,
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': createUuid() },
    body: JSON.stringify({ question, filters: {}, a2uiClientCapabilities: { supportedCatalogIds: [CATALOG_ID] } }),
  })
  if (!response.ok || !response.body) throw new Error(await response.text() || 'Streaming endpoint unavailable')
  const responseMessageId = response.headers.get('X-Message-ID')
  if (responseMessageId) onMessageId(responseMessageId)
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replaceAll('\r\n', '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const event = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const id = event.split('\n').find((line) => line.startsWith('id:'))?.slice(3).trim()
      if (id) onMessageId(id.split(':')[0])
      const data = event.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n')
      if (data) onMessage(JSON.parse(data) as A2uiMessage)
      boundary = buffer.indexOf('\n\n')
    }
    if (done) break
  }
}
