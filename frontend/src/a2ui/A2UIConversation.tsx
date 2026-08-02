import { useEffect, useRef, useState } from 'react'
import { A2uiSurface, type ReactComponentImplementation } from '@a2ui/react/v0_9'
import { MessageProcessor, type A2uiMessage, type SurfaceModel } from '@a2ui/web_core/v0_9'
import { analyticsCatalog, CATALOG_ID } from './catalog'

export function A2UIConversation({ conversationId, question }: { conversationId: string; question: string }) {
  const processorRef = useRef(new MessageProcessor([analyticsCatalog], undefined, { version: 'v0.9.1' }))
  const [surfaces, setSurfaces] = useState<SurfaceModel<ReactComponentImplementation>[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const processor = processorRef.current
    const sync = () => setSurfaces(Array.from(processor.model.surfacesMap.values()))
    const created = processor.onSurfaceCreated(sync)
    const deleted = processor.onSurfaceDeleted(sync)
    const controller = new AbortController()
    void streamMessage(conversationId, question, controller.signal, (message) => {
      processor.processMessages([message])
      sync()
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Unable to stream analysis')
    })
    return () => { controller.abort(); created.unsubscribe(); deleted.unsubscribe() }
  }, [conversationId, question])

  if (error) return <div className="stream-error">{error}<button onClick={() => window.location.reload()}>Retry</button></div>
  if (!surfaces.length) return <div className="assistant-loading"><i /><span>Creating analysis surface…</span></div>
  return <div className="assistant-message">{surfaces.map((surface) => <A2uiSurface key={surface.id} surface={surface} />)}</div>
}

async function streamMessage(conversationId: string, question: string, signal: AbortSignal, onMessage: (message: A2uiMessage) => void) {
  const response = await fetch(`/api/v1/conversations/${conversationId}/messages/stream`, {
    method: 'POST', signal,
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
    body: JSON.stringify({ question, filters: {}, a2uiClientCapabilities: { supportedCatalogIds: [CATALOG_ID] } }),
  })
  if (!response.ok || !response.body) throw new Error(await response.text() || 'Streaming endpoint unavailable')
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
      const data = event.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n')
      if (data) onMessage(JSON.parse(data) as A2uiMessage)
      boundary = buffer.indexOf('\n\n')
    }
    if (done) break
  }
}
