/* oxlint-disable react/only-export-components */
import { useEffect, useMemo, useRef, useState } from 'react'
import { BrainCircuit, Check, ChevronDown, ChevronRight, CircleAlert, LoaderCircle, Wrench } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'
import { z } from 'zod'
import { Catalog, CommonSchemas } from '@a2ui/web_core/v0_9'
import { Column, createComponentImplementation, type ReactComponentImplementation } from '@a2ui/react/v0_9'

export const CATALOG_ID = 'https://mini-hackathon.local/a2ui/catalogs/analytics-chat/v1'

type ReasoningSegment = { id: string; text: string; createdAt: string }
type ToolCall = { toolCallId: string; displayName: string; status: string; sequence: number; summary?: string }
type ToolGroup = { groupId: string; calls: ToolCall[] }
type Kpi = { key: string; label: string; value: number; format: string }
type Visualization = { status: string; tool_name: string; title: string; rationale: string; asset_url?: string | null; error?: string; x_field: string; y_field: string; data: Record<string, string | number>[] }
type Analysis = { answer: string; kpis: Kpi[]; table: { columns: string[]; rows: Record<string, string | number>[] }; insights: { kind: string; title: string; description: string }[]; sql: string; visualization: Visualization }

const ReasoningApi = { name: 'ReasoningPanel', schema: z.object({ segments: CommonSchemas.DynamicValue, status: CommonSchemas.DynamicValue }) }
const ToolApi = { name: 'ToolCallGroup', schema: z.object({ groups: CommonSchemas.DynamicValue }) }
const MarkdownApi = { name: 'RichMarkdown', schema: z.object({ markdown: CommonSchemas.DynamicValue }) }
const AnalysisApi = { name: 'AnalysisResult', schema: z.object({ analysis: CommonSchemas.DynamicValue, artifacts: CommonSchemas.DynamicValue }) }

const ReasoningPanel = createComponentImplementation(ReasoningApi, ({ props }) => {
  const segments = (props.segments ?? []) as ReasoningSegment[]
  const status = String(props.status ?? 'IDLE')
  const [expanded, setExpanded] = useState(false)
  const [manuallyExpanded, setManuallyExpanded] = useState(false)
  const previousStatus = useRef(status)

  useEffect(() => {
    if (status === 'RUNNING' && previousStatus.current !== 'RUNNING') setExpanded(true)
    if (status === 'COMPLETED' && !manuallyExpanded) {
      const timer = window.setTimeout(() => setExpanded(false), 300)
      return () => window.clearTimeout(timer)
    }
    previousStatus.current = status
  }, [status, manuallyExpanded])

  if (!segments.length) return null
  return <section className={`reasoning-panel ${expanded ? 'expanded' : ''}`}>
    <button className="reasoning-summary" onClick={() => { setExpanded((value) => !value); setManuallyExpanded(!expanded) }}>
      <span className="reasoning-title"><BrainCircuit size={16} /> 思考 {status === 'RUNNING' && <LoaderCircle size={14} className="spin" />}</span>
      <span className="reasoning-meta">{status === 'COMPLETED' ? '已完成' : '正在分析'} · {segments.length} 步</span>
      <ChevronDown size={15} />
    </button>
    <div className="reasoning-content">{segments.map((segment) => <p key={segment.id}>{segment.text}</p>)}</div>
  </section>
})

const ToolCallGroup = createComponentImplementation(ToolApi, ({ props }) => {
  const groups = props.groups as unknown as Record<string, ToolGroup> | undefined
  const group = groups ? Object.values(groups)[0] : undefined
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState<string | null>(null)
  if (!group?.calls.length) return null
  const calls = [...group.calls].sort((a, b) => a.sequence - b.sequence)
  const running = calls.some((call) => call.status === 'RUNNING')
  const label = calls.length === 1 ? calls[0].displayName : `已调用 ${calls.length} 个工具`
  return <section className="tool-group">
    <button className="tool-summary" onClick={() => setOpen((value) => !value)}>
      <span>{running ? <LoaderCircle size={15} className="spin" /> : <Wrench size={15} />}{label}</span>
      <span className="tool-state">{running ? '运行中' : '完成'} <ChevronRight size={14} /></span>
    </button>
    {open && <div className="tool-list">{calls.map((call) => <div className="tool-item" key={call.toolCallId}>
      <button onClick={() => setActive(active === call.toolCallId ? null : call.toolCallId)}>
        {call.status === 'RUNNING' ? <LoaderCircle size={14} className="spin" /> : call.status === 'FAILED' ? <CircleAlert size={14} /> : <Check size={14} />}
        <span>{call.displayName}</span><small>{call.status.toLowerCase()}</small><ChevronDown size={13} />
      </button>
      {active === call.toolCallId && <div className="tool-detail"><span>Tool call</span><code>{call.toolCallId}</code><span>Output</span><p>{call.summary ?? 'Waiting for tool output…'}</p></div>}
    </div>)}</div>}
  </section>
})

const RichMarkdown = createComponentImplementation(MarkdownApi, ({ props }) => {
  const markdown = String(props.markdown ?? '')
  if (!markdown) return null
  return <div className="rich-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]} components={{
    code({ className, children, ...rest }) {
      const language = /language-(\w+)/.exec(className ?? '')?.[1]
      if (language === 'mermaid') return <MermaidDiagram source={String(children).trim()} />
      return <code className={className} {...rest}>{children}</code>
    },
    img({ src, alt }) { return <SafeImage src={src} alt={alt ?? 'Generated artifact'} /> },
  }}>{markdown}</ReactMarkdown></div>
})

const AnalysisResult = createComponentImplementation(AnalysisApi, ({ props }) => {
  const analysis = props.analysis as unknown as Analysis | undefined
  if (!analysis?.kpis) return null
  return <div className="analysis-result">
    <div className="kpi-grid">{analysis.kpis.map((kpi) => <div className="kpi-card" key={kpi.key}><span>{kpi.label}</span><strong>{formatKpi(kpi)}</strong></div>)}</div>
    <VisualizationPanel visualization={analysis.visualization} />
    <div className="insight-grid">{analysis.insights.map((insight) => <article key={insight.title}><span>{insight.kind}</span><h3>{insight.title}</h3><p>{insight.description}</p></article>)}</div>
    <div className="result-table-wrap"><div className="result-title"><h3>Query result</h3><span>{analysis.table.rows.length} rows</span></div><div className="result-scroll"><table><thead><tr>{analysis.table.columns.map((column) => <th key={column}>{humanize(column)}</th>)}</tr></thead><tbody>{analysis.table.rows.map((row, index) => <tr key={index}>{analysis.table.columns.map((column) => <td key={column}>{formatCell(row[column], column)}</td>)}</tr>)}</tbody></table></div></div>
    <details className="sql-disclosure"><summary>View SQL & metric definitions <ChevronDown size={14} /></summary><pre><code>{analysis.sql}</code></pre></details>
  </div>
})

function VisualizationPanel({ visualization }: { visualization: Visualization }) {
  const safeUrl = visualization.asset_url && isSafeImageUrl(visualization.asset_url) ? visualization.asset_url : null
  const max = Math.max(...(visualization.data ?? []).map((row) => Number(row[visualization.y_field]) || 0), 1)
  return <section className="visualization-panel"><div className="visualization-head"><div><span>VISUALIZATION AGENT</span><h3>{visualization.title}</h3><p>{visualization.rationale}</p></div><span className="mcp-chip">AntV MCP · {visualization.tool_name.replace('generate_', '').replace('_chart', '')}</span></div>
    {safeUrl ? <SafeImage src={safeUrl} alt={visualization.title} /> : <div className="fallback-chart" aria-label={visualization.title}>{visualization.data.map((row) => <div className="bar-item" key={String(row[visualization.x_field])}><div className="bar-value">{row[visualization.y_field]}</div><div className="bar-track"><i style={{ height: `${(Number(row[visualization.y_field]) / max) * 100}%` }} /></div><span>{row[visualization.x_field]}</span></div>)}</div>}
  </section>
}

function SafeImage({ src, alt }: { src?: string; alt: string }) {
  const [failed, setFailed] = useState(false)
  if (!src || !isSafeImageUrl(src) || failed) return <div className="image-fallback"><CircleAlert size={18} /> Visualization preview unavailable</div>
  return <img className="artifact-image" src={src} alt={alt} loading="lazy" onError={() => setFailed(true)} />
}

function MermaidDiagram({ source }: { source: string }) {
  const [svg, setSvg] = useState('')
  const id = useMemo(() => `mermaid-${crypto.randomUUID()}`, [])
  useEffect(() => {
    void import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral' })
      return mermaid.render(id, source)
    }).then((result) => setSvg(result.svg)).catch(() => setSvg(''))
  }, [id, source])
  return svg ? <div className="mermaid-diagram" dangerouslySetInnerHTML={{ __html: svg }} /> : <pre><code>{source}</code></pre>
}

function isSafeImageUrl(value: string) {
  try { const url = new URL(value, window.location.origin); return url.origin === window.location.origin || (url.protocol === 'https:' && url.hostname === 'mdn.alipayobjects.com') } catch { return false }
}
function formatKpi(kpi: Kpi) { if (kpi.format === 'CURRENCY_USD') return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(kpi.value); if (kpi.format === 'PERCENT') return `${kpi.value}%`; if (kpi.format === 'DAYS') return `${kpi.value} days`; return kpi.value.toLocaleString() }
function humanize(value: string) { return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()) }
function formatCell(value: string | number, column: string) { if (typeof value === 'number' && column.includes('cost')) return `$${value.toFixed(2)}`; if (typeof value === 'number' && column.includes('pct')) return `${value}%`; return value }

export const analyticsCatalog: Catalog<ReactComponentImplementation> = new Catalog(CATALOG_ID, [Column, ReasoningPanel, ToolCallGroup, RichMarkdown, AnalysisResult] as ReactComponentImplementation[])
