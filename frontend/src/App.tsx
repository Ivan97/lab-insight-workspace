import { useEffect, useMemo, useState } from 'react'
import {
  Activity,
  ArrowRight,
  ChartNoAxesCombined,
  CheckCircle2,
  ChevronRight,
  CircleHelp,
  Database,
  FileSpreadsheet,
  LayoutDashboard,
  Menu,
  Plus,
  Search,
  Sparkles,
  Upload,
} from 'lucide-react'
import { A2UIConversation } from './a2ui/A2UIConversation'
import { api, type Ingestion, type MappingDraft } from './api'
import './App.css'

type Page = 'sources' | 'schema' | 'analyze'

const navigation = [
  { id: 'sources' as const, label: 'Sources', icon: Database },
  { id: 'schema' as const, label: 'Schema', icon: FileSpreadsheet },
  { id: 'analyze' as const, label: 'Analyze', icon: ChartNoAxesCombined },
]

export default function App() {
  const [page, setPage] = useState<Page>('sources')
  const [sources, setSources] = useState<Ingestion[]>([])
  const [selectedBatch, setSelectedBatch] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(true)

  const refreshSources = async () => {
    const response = await api.listIngestions()
    setSources(response.items)
    setSelectedBatch((current) => current ?? response.items[0]?.batch_id ?? null)
  }

  useEffect(() => {
    void refreshSources()
  }, [])

  const openReview = (batchId: string) => {
    setSelectedBatch(batchId)
    setPage('schema')
  }

  return (
    <div className={`app-shell ${sidebarOpen ? '' : 'sidebar-collapsed'}`}>
      <aside className="sidebar">
        <div className="brand-mark"><Sparkles size={18} /><span>Prism</span></div>
        <nav aria-label="Primary navigation">
          {navigation.map((item) => {
            const Icon = item.icon
            return (
              <button key={item.id} className={page === item.id ? 'nav-item active' : 'nav-item'} onClick={() => setPage(item.id)}>
                <Icon size={18} /><span>{item.label}</span>
              </button>
            )
          })}
        </nav>
        <div className="sidebar-footer">
          <div className="status-dot" />
          <div><strong>System ready</strong><span>1,000 records indexed</span></div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <button className="icon-button" aria-label="Toggle sidebar" onClick={() => setSidebarOpen((value) => !value)}><Menu size={18} /></button>
          <div className="scope-pill"><Database size={14} /><span>All laboratory data</span><ChevronRight size={14} /></div>
          <div className="topbar-actions"><button className="icon-button" aria-label="Search"><Search size={18} /></button><button className="icon-button" aria-label="Help"><CircleHelp size={18} /></button><div className="avatar">IL</div></div>
        </header>

        {page === 'sources' && <SourcesPage sources={sources} refresh={refreshSources} openReview={openReview} />}
        {page === 'schema' && <SchemaPage sources={sources} batchId={selectedBatch} onCommitted={refreshSources} onAnalyze={() => setPage('analyze')} />}
        {page === 'analyze' && <AnalyzePage />}
      </main>
    </div>
  )
}

function SourcesPage({ sources, refresh, openReview }: { sources: Ingestion[]; refresh: () => Promise<void>; openReview: (id: string) => void }) {
  const [showText, setShowText] = useState(false)
  const [text, setText] = useState('BluePeak reported sample S-1042 completed on 28 Jul. Thermal Cycling result was NG; invoice amount was USD 89.50.')
  const [uploading, setUploading] = useState(false)

  const upload = async (file: File) => {
    setUploading(true)
    try { await api.uploadFile(file); await refresh() } finally { setUploading(false) }
  }

  const submitText = async () => {
    setUploading(true)
    try { await api.ingestText('Slack lab update', text); setShowText(false); await refresh() } finally { setUploading(false) }
  }

  const ready = sources.filter((item) => item.status === 'READY').length
  return (
    <div className="page sources-page">
      <section className="page-heading split-heading">
        <div><span className="eyebrow">DATA FOUNDATION</span><h1>Bring every source into focus.</h1><p>Connect messy supplier files and conversations, then turn them into analysis-ready data with a reviewable schema.</p></div>
        <div className="heading-actions">
          <button className="secondary-button" onClick={() => setShowText((value) => !value)}><Plus size={16} /> Paste text</button>
          <label className="primary-button"><Upload size={16} /> {uploading ? 'Processing…' : 'Upload data'}<input type="file" accept=".csv,.xlsx" hidden onChange={(event) => event.target.files?.[0] && void upload(event.target.files[0])} /></label>
        </div>
      </section>

      {showText && <section className="text-ingest-card"><div><strong>Paste an email or Slack update</strong><span>We’ll extract candidate fields and ask you to confirm them.</span></div><textarea value={text} onChange={(event) => setText(event.target.value)} /><div className="inline-actions"><button className="ghost-button" onClick={() => setShowText(false)}>Cancel</button><button className="primary-button" onClick={() => void submitText()}>Extract records</button></div></section>}

      <section className="overview-grid">
        <div className="metric-panel"><span>Connected sources</span><strong>{sources.length}</strong><small>CSV, Excel and text</small></div>
        <div className="metric-panel"><span>Ready for analysis</span><strong>{ready}</strong><small>{sources.length - ready} source needs review</small></div>
        <div className="metric-panel"><span>Canonical records</span><strong>1,000</strong><small>Updated moments ago</small></div>
      </section>

      <section className="section-card source-card">
        <div className="section-header"><div><h2>Data sources</h2><p>Every source keeps its original lineage and mapping history.</p></div><button className="more-button">•••</button></div>
        <div className="source-table" role="table">
          <div className="source-row source-head" role="row"><span>Source</span><span>Type</span><span>Records</span><span>Quality</span><span>Status</span><span /></div>
          {sources.map((source) => <SourceRow key={source.batch_id} source={source} openReview={openReview} />)}
        </div>
      </section>
    </div>
  )
}

function SourceRow({ source, openReview }: { source: Ingestion; openReview: (id: string) => void }) {
  const isReady = source.status === 'READY'
  return <div className="source-row" role="row">
    <div className="source-name"><div className={`file-icon ${source.source_type.toLowerCase()}`}>{source.source_type === 'TEXT' ? 'TXT' : source.source_type}</div><div><strong>{source.source_name}</strong><small>{source.vendor_hint ?? 'Unstructured source'}</small></div></div>
    <span>{source.source_type}</span><span>{source.record_count.toLocaleString()}</span>
    <span className="quality-score"><i style={{ width: `${source.quality_score}%` }} />{source.quality_score}%</span>
    <span><span className={`status-badge ${isReady ? 'ready' : 'review'}`}>{isReady ? <CheckCircle2 size={13} /> : <Activity size={13} />}{isReady ? 'Ready' : 'Needs review'}</span></span>
    <button className="row-action" onClick={() => openReview(source.batch_id)}>{isReady ? 'View' : 'Review'}<ArrowRight size={14} /></button>
  </div>
}

function SchemaPage({ sources, batchId, onCommitted, onAnalyze }: { sources: Ingestion[]; batchId: string | null; onCommitted: () => Promise<void>; onAnalyze: () => void }) {
  const [mapping, setMapping] = useState<MappingDraft | null>(null)
  const source = sources.find((item) => item.batch_id === batchId) ?? sources[0]
  const sourceId = source?.batch_id
  useEffect(() => { if (sourceId) void api.getMapping(sourceId).then(setMapping) }, [sourceId])

  const updateTarget = (index: number, target: string) => setMapping((current) => current && ({ ...current, mappings: current.mappings.map((item, itemIndex) => itemIndex === index ? { ...item, target_field: target, status: 'MODIFIED' } : item) }))
  const commit = async () => {
    if (!source || !mapping) return
    await api.updateMapping(source.batch_id, mapping)
    await api.commitIngestion(source.batch_id)
    await onCommitted()
    onAnalyze()
  }

  if (!source || !mapping) return <div className="page loading-state">Loading mapping…</div>
  return <div className="page schema-page">
    <section className="page-heading"><span className="eyebrow">SCHEMA REVIEW</span><h1>Make the meaning explicit.</h1><p>Review AI suggestions before anything enters the trusted analysis layer.</p></section>
    <section className="review-summary section-card"><div className="source-name"><div className="file-icon xlsx">XLSX</div><div><strong>{source.source_name}</strong><small>{source.record_count} records · mapping version {mapping.version}</small></div></div><div className="review-progress"><span>{mapping.mappings.filter((item) => item.confidence >= .8).length} auto-mapped</span><span>{mapping.mappings.filter((item) => item.confidence < .8).length} need attention</span></div></section>
    <section className="section-card mapping-card">
      <div className="section-header"><div><h2>Field mapping</h2><p>Original fields remain unchanged. These rules create the canonical view.</p></div><span className="status-badge review"><Activity size={13} /> Review required</span></div>
      <div className="mapping-table">
        <div className="mapping-row mapping-head"><span>Source field</span><span>Sample value</span><span>Canonical field</span><span>Confidence</span><span>Transform</span></div>
        {mapping.mappings.map((item, index) => <div className={item.confidence < .8 ? 'mapping-row attention' : 'mapping-row'} key={item.source_field}>
          <code>{item.source_field}</code><span>{String(item.sample_before[0] ?? '—')}</span>
          <select value={item.target_field ?? ''} onChange={(event) => updateTarget(index, event.target.value)}><option value="">Ignore field</option>{['sample_id','test_name','result','cost_amount','completed_date','lab_vendor'].map((field) => <option key={field}>{field}</option>)}</select>
          <span className="confidence"><i style={{ width: `${item.confidence * 100}%` }} /><strong>{Math.round(item.confidence * 100)}%</strong></span>
          <span><code className="transform">{item.transform}</code>{item.warnings[0] && <small className="warning-copy">{item.warnings[0]}</small>}</span>
        </div>)}
      </div>
      <div className="mapping-footer"><div><CheckCircle2 size={17} /><span>All required canonical fields are covered</span></div><button className="primary-button" onClick={() => void commit()}>Confirm & publish <ArrowRight size={16} /></button></div>
    </section>
  </div>
}

function AnalyzePage() {
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [question, setQuestion] = useState('Compare cost, pass rate and turnaround time by vendor')
  const [submitted, setSubmitted] = useState<string | null>(null)
  const suggestions = useMemo(() => [
    'Compare cost, pass rate and turnaround time by vendor',
    'What cost trends or anomalies appeared in recent months?',
    'Which materials have the highest failure rate?',
  ], [])
  useEffect(() => { void api.createConversation().then((value) => setConversationId(value.conversation_id)) }, [])
  const ask = (value = question) => { if (value.trim() && conversationId) { setQuestion(value); setSubmitted(value) } }
  return <div className="page analyze-page">
    <section className="analyze-hero"><div><span className="eyebrow">ASK & ANALYZE</span><h1>What would you like to understand?</h1><p>Ask across every trusted source. Prism will show its work, use the right tools, and surface the decision—not just the numbers.</p></div></section>
    {!submitted && <div className="suggestion-grid">{suggestions.map((item, index) => <button key={item} onClick={() => ask(item)}><span className={`suggestion-icon s${index}`}><LayoutDashboard size={18} /></span><strong>{['Compare vendor performance','Find recent anomalies','Inspect quality risk'][index]}</strong><small>{item}</small><ArrowRight size={16} /></button>)}</div>}
    {submitted && conversationId && <div className="conversation"><div className="user-message">{submitted}</div><A2UIConversation key={submitted} conversationId={conversationId} question={submitted} /></div>}
    <div className="composer-wrap"><div className="composer"><Sparkles size={18} /><textarea rows={1} value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); ask() } }} aria-label="Ask a question" /><button onClick={() => ask()} aria-label="Send question"><ArrowRight size={18} /></button></div><span>Answers use published data only. SQL and metric definitions remain available for review.</span></div>
  </div>
}
