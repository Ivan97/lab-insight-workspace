import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  ArrowRight,
  ChartNoAxesCombined,
  CheckCircle2,
  ChevronRight,
  CircleHelp,
  Database,
  Download,
  FileSpreadsheet,
  GitMerge,
  LayoutDashboard,
  Menu,
  Plus,
  Search,
  Square,
  Sparkles,
  Trash2,
  Upload,
} from 'lucide-react'
import { A2UIConversation } from './a2ui/A2UIConversation'
import { api, type DataPreview, type DataProfile, type Ingestion, type JoinRuleInput, type MappingDraft, type SemanticLayer } from './api'
import './App.css'

type Page = 'sources' | 'schema' | 'analyze'
type Session = { id: string; title: string; turns: string[]; running: boolean }

const navigation = [
  { id: 'sources' as const, label: 'Sources', icon: Database },
  { id: 'schema' as const, label: 'Schema', icon: FileSpreadsheet },
  { id: 'analyze' as const, label: 'Analyze', icon: ChartNoAxesCombined },
]

const canonicalFields = [
  'sample_id', 'vendor', 'project', 'material', 'test_name', 'result', 'cost_amount',
  'completed_date', 'lab_vendor', 'contract_tier', 'region', 'contracted_cost_usd',
  'sla_days', 'quality_target_pct', 'owner', 'priority', 'approved_budget_usd',
  'start_date', 'end_date',
  'material_family', 'target_failure_pct', 'max_avg_cost_usd', 'risk_tier',
]

export default function App() {
  const [page, setPage] = useState<Page>('sources')
  const [sources, setSources] = useState<Ingestion[]>([])
  const [selectedBatch, setSelectedBatch] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [creatingSession, setCreatingSession] = useState(false)
  const runningRefs = useRef(new Map<string, boolean>())
  const initializedSessionRef = useRef(false)

  const refreshSources = async () => {
    const response = await api.listIngestions()
    setSources(response.items)
    setSelectedBatch((current) => current ?? response.items[0]?.batch_id ?? null)
  }

  useEffect(() => {
    void refreshSources()
  }, [])

  useEffect(() => {
    if (initializedSessionRef.current) return
    initializedSessionRef.current = true
    void api.createConversation().then((value) => {
      const session = { id: value.conversation_id, title: 'New analysis', turns: [], running: false }
      setSessions([session])
      setActiveSessionId(session.id)
    })
  }, [])

  const createSession = async () => {
    if (creatingSession) return
    setCreatingSession(true)
    try {
      const value = await api.createConversation()
      const session = { id: value.conversation_id, title: 'New analysis', turns: [], running: false }
      setSessions((current) => [...current, session])
      setActiveSessionId(session.id)
      setPage('analyze')
    } finally {
      setCreatingSession(false)
    }
  }

  const setRunning = (sessionId: string, running: boolean) => {
    runningRefs.current.set(sessionId, running)
    setSessions((current) => current.map((session) => session.id === sessionId ? { ...session, running } : session))
  }

  const ask = (sessionId: string, value: string) => {
    const question = value.trim()
    if (!question || runningRefs.current.get(sessionId)) return
    runningRefs.current.set(sessionId, true)
    setSessions((current) => current.map((session) => session.id === sessionId ? {
      ...session,
      title: session.turns.length ? session.title : question.slice(0, 38),
      turns: [...session.turns, question],
      running: true,
    } : session))
  }

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
              <div className="nav-group" key={item.id}>
                <button className={page === item.id ? 'nav-item active' : 'nav-item'} onClick={() => setPage(item.id)}>
                  <Icon size={18} /><span>{item.label}</span>
                </button>
                {item.id === 'analyze' && page === 'analyze' && <section className="conversation-subnav" aria-label="Analysis conversations">
                  <div className="conversation-subnav-head"><span>Conversations</span><button onClick={() => void createSession()} disabled={creatingSession} aria-label="New conversation" title="New conversation"><Plus size={14} /></button></div>
                  <div className="conversation-subtabs">
                    {sessions.map((session, index) => <button key={session.id} className={activeSessionId === session.id ? 'active' : ''} onClick={() => setActiveSessionId(session.id)}>
                      <span className={session.running ? 'session-state running' : 'session-state'} />
                      <strong>{session.title}</strong>
                      <small>{session.running ? 'Running' : session.turns.length ? `${session.turns.length} question${session.turns.length === 1 ? '' : 's'}` : `Conversation ${index + 1}`}</small>
                    </button>)}
                    {!sessions.length && <span className="conversation-subnav-empty">Preparing conversation…</span>}
                  </div>
                </section>}
              </div>
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
        {page === 'schema' && <SchemaPage sources={sources} batchId={selectedBatch} onSelectBatch={setSelectedBatch} onCommitted={refreshSources} onAnalyze={() => setPage('analyze')} />}
        <div hidden={page !== 'analyze'}><AnalyzePage sessions={sessions} activeSessionId={activeSessionId} ask={ask} setRunning={setRunning} /></div>
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
    <div className="row-actions">
      {source.download_url && <a className="download-action" href={source.download_url} download aria-label={`Download ${source.source_name}`} title="Download original file"><Download size={14} /></a>}
      <button className="row-action" onClick={() => openReview(source.batch_id)}>{isReady ? 'View' : 'Review'}<ArrowRight size={14} /></button>
    </div>
  </div>
}

function SchemaPage({ sources, batchId, onSelectBatch, onCommitted, onAnalyze }: { sources: Ingestion[]; batchId: string | null; onSelectBatch: (batchId: string) => void; onCommitted: () => Promise<void>; onAnalyze: () => void }) {
  const [mapping, setMapping] = useState<MappingDraft | null>(null)
  const [profile, setProfile] = useState<DataProfile | null>(null)
  const [preview, setPreview] = useState<DataPreview | null>(null)
  const [reviewTab, setReviewTab] = useState<'profile' | 'preview'>('profile')
  const [schemaMode, setSchemaMode] = useState<'fields' | 'relationships'>('fields')
  const [semantic, setSemantic] = useState<SemanticLayer | null>(null)
  const source = sources.find((item) => item.batch_id === batchId) ?? sources[0]
  const sourceId = source?.batch_id
  useEffect(() => {
    if (!sourceId) return
    setMapping(null); setProfile(null); setPreview(null)
    void Promise.all([api.getMapping(sourceId), api.getProfile(sourceId), api.getPreview(sourceId)]).then(([nextMapping, nextProfile, nextPreview]) => {
      setMapping(nextMapping); setProfile(nextProfile); setPreview(nextPreview)
    })
  }, [sourceId])
  useEffect(() => { void api.getRelationships().then(setSemantic) }, [])

  const updateTarget = (index: number, target: string) => setMapping((current) => current && ({ ...current, mappings: current.mappings.map((item, itemIndex) => itemIndex === index ? { ...item, target_field: target, status: 'MODIFIED' } : item) }))
  const commit = async () => {
    if (!source || !mapping) return
    await api.updateMapping(source.batch_id, mapping)
    await api.commitIngestion(source.batch_id)
    await onCommitted()
    onAnalyze()
  }

  if (!source || !mapping) return <div className="page loading-state">Loading mapping…</div>
  const attentionCount = mapping.mappings.filter((item) => item.confidence < .8).length
  return <div className="page schema-page">
    <section className="page-heading"><span className="eyebrow">SCHEMA REVIEW</span><h1>Make the meaning explicit.</h1><p>Review AI suggestions before anything enters the trusted analysis layer.</p></section>
    <div className="schema-mode-tabs" role="tablist"><button className={schemaMode === 'fields' ? 'active' : ''} onClick={() => setSchemaMode('fields')}><FileSpreadsheet size={15} /> Fields & mapping</button><button className={schemaMode === 'relationships' ? 'active' : ''} onClick={() => setSchemaMode('relationships')}><GitMerge size={15} /> Relationships</button></div>
    {schemaMode === 'relationships' ? <RelationshipsPanel semantic={semantic} onPublished={setSemantic} /> : <>
    <section className="schema-source-switcher section-card" aria-label="Schema sources">
      <div className="schema-source-switcher-head"><div><strong>Source schemas</strong><span>{sources.length} connected files</span></div><small>Select a file to inspect its profile and field mapping.</small></div>
      <div className="schema-source-tabs" role="tablist">{sources.map((item) => <button role="tab" aria-selected={item.batch_id === source.batch_id} className={item.batch_id === source.batch_id ? 'active' : ''} key={item.batch_id} onClick={() => onSelectBatch(item.batch_id)}><span className={`mini-file-icon ${item.source_type.toLowerCase()}`}>{item.source_type === 'TEXT' ? 'TXT' : item.source_type}</span><span><strong>{item.source_name}</strong><small>{item.record_count.toLocaleString()} records</small></span></button>)}</div>
    </section>
    <section className="review-summary section-card"><div className="source-name"><div className={`file-icon ${source.source_type.toLowerCase()}`}>{source.source_type === 'TEXT' ? 'TXT' : source.source_type}</div><div><strong>{source.source_name}</strong><small>{source.record_count} records · mapping version {mapping.version}</small></div></div><div className="review-progress"><span>{mapping.mappings.filter((item) => item.confidence >= .8).length} auto-mapped</span><span>{attentionCount} need attention</span></div></section>
    {profile && preview && <section className="section-card data-review-card">
      <div className="review-tabs" role="tablist"><button className={reviewTab === 'profile' ? 'active' : ''} onClick={() => setReviewTab('profile')}>Data profile</button><button className={reviewTab === 'preview' ? 'active' : ''} onClick={() => setReviewTab('preview')}>Source preview</button><span>{profile.row_count.toLocaleString()} rows · {profile.column_count} fields</span></div>
      {reviewTab === 'profile' ? <div className="profile-grid">{profile.columns.map((column) => { const missing = Math.round(column.null_rate * 100); return <article key={column.name}><div><code>{column.name}</code><span>{column.inferred_type}</span></div><strong>{100 - missing}% <small>complete</small></strong><p>{missing}% missing · {column.distinct_count.toLocaleString()} distinct</p><p>{column.sample_values.map(String).join(', ') || 'No sample values'}</p>{column.warnings[0] && <em>{column.warnings[0]}</em>}</article> })}</div> : <div className="preview-scroll"><table><thead><tr>{Object.keys(preview.rows[0] ?? {}).map((key) => <th key={key}>{key}</th>)}</tr></thead><tbody>{preview.rows.map((row, index) => <tr key={index}>{Object.keys(preview.rows[0] ?? {}).map((key) => <td key={key}>{String(row[key] ?? '—')}</td>)}</tr>)}</tbody></table></div>}
    </section>}
    <section className="section-card mapping-card">
      <div className="section-header"><div><h2>Field mapping</h2><p>Original fields remain unchanged. These rules create the canonical view.</p></div><span className={`status-badge ${attentionCount ? 'review' : 'ready'}`}>{attentionCount ? <Activity size={13} /> : <CheckCircle2 size={13} />}{attentionCount ? 'Review required' : 'Ready to publish'}</span></div>
      <div className="mapping-table">
        <div className="mapping-row mapping-head"><span>Source field</span><span>Sample value</span><span>Canonical field</span><span>Confidence</span><span>Transform</span></div>
        {mapping.mappings.map((item, index) => <div className={item.confidence < .8 ? 'mapping-row attention' : 'mapping-row'} key={item.source_field}>
          <code>{item.source_field}</code><span>{String(item.sample_before[0] ?? '—')}</span>
          <select value={item.target_field ?? ''} onChange={(event) => updateTarget(index, event.target.value)}><option value="">Ignore field</option>{canonicalFields.map((field) => <option key={field}>{field}</option>)}</select>
          <span className="confidence"><i style={{ width: `${item.confidence * 100}%` }} /><strong>{Math.round(item.confidence * 100)}%</strong></span>
          <span><code className="transform">{item.transform}</code>{item.warnings[0] && <small className="warning-copy">{item.warnings[0]}</small>}</span>
        </div>)}
      </div>
      <div className="mapping-footer"><div><CheckCircle2 size={17} /><span>All required canonical fields are covered</span></div><button className="primary-button" onClick={() => void commit()}>Confirm & publish <ArrowRight size={16} /></button></div>
    </section>
    </>}
  </div>
}

function RelationshipsPanel({ semantic, onPublished }: { semantic: SemanticLayer | null; onPublished: (semantic: SemanticLayer) => void }) {
  const [rules, setRules] = useState<JoinRuleInput[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    if (semantic) setRules(semantic.rules.map(({ name, left_table, left_field, right_table, right_field, join_type, relationship }) => ({ name, left_table, left_field, right_table, right_field, join_type, relationship })))
  }, [semantic])
  if (!semantic) return <section className="section-card relationship-loading">Loading relationships…</section>
  const table = (name: string) => semantic.tables.find((item) => item.name === name)
  const dimensionTables = semantic.tables.filter((item) => item.name !== semantic.base_table)
  const updateRule = (index: number, patch: Partial<JoinRuleInput>) => setRules((current) => current.map((rule, ruleIndex) => ruleIndex === index ? { ...rule, ...patch } : rule))
  const addRule = () => {
    const nextTable = dimensionTables.find((item) => !rules.some((rule) => rule.right_table === item.name)) ?? dimensionTables[0]
    if (!nextTable) return
    const leftColumns = table(semantic.base_table)?.columns ?? []
    const shared = nextTable.columns.find((column) => leftColumns.includes(column)) ?? nextTable.columns[0]
    setRules((current) => [...current, { name: `Join ${nextTable.name}`, left_table: semantic.base_table, left_field: shared, right_table: nextTable.name, right_field: shared, join_type: 'LEFT', relationship: 'MANY_TO_ONE' }])
  }
  const publish = async () => {
    setSaving(true); setError('')
    try { onPublished(await api.publishRelationships(rules)) } catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to publish relationships') } finally { setSaving(false) }
  }
  const previewColumns = Object.keys(semantic.view.preview[0] ?? {})
  return <div className="relationships-workspace">
    <section className="relationship-overview section-card">
      <div><span className="relationship-icon"><GitMerge size={19} /></span><div><strong>Published analytical view</strong><code>{semantic.view_name}</code></div></div>
      <div className="relationship-stats"><span><strong>{semantic.rules.length}</strong> relationships</span><span><strong>{semantic.view.column_count}</strong> fields</span><span className="status-badge ready"><CheckCircle2 size={13} /> Published</span></div>
    </section>
    <section className="section-card relationship-editor">
      <div className="section-header"><div><h2>Join rules</h2><p>Define reviewed paths from the fact table to trusted dimensions. Keys are validated before publishing.</p></div><button className="secondary-button" onClick={addRule} disabled={rules.length >= dimensionTables.length}><Plus size={15} /> Add relationship</button></div>
      <div className="relationship-list">
        {rules.map((rule, index) => {
          const published = semantic.rules.find((item) => item.right_table === rule.right_table && item.left_field === rule.left_field && item.right_field === rule.right_field)
          const rightColumns = table(rule.right_table)?.columns ?? []
          return <article className="relationship-rule" key={`${index}-${rule.right_table}`}>
            <div className="relationship-rule-head"><input value={rule.name} aria-label="Relationship name" onChange={(event) => updateRule(index, { name: event.target.value })} /><div>{published && <><span className="match-chip">{published.matched_pct}% matched</span><span className={published.right_key_unique ? 'unique-chip' : 'warning-chip'}>{published.right_key_unique ? 'Unique key' : 'Duplicate key'}</span></>}<button className="icon-button remove-rule" onClick={() => setRules((current) => current.filter((_, ruleIndex) => ruleIndex !== index))} aria-label="Remove relationship"><Trash2 size={15} /></button></div></div>
            <div className="join-builder">
              <label><span>From table</span><select value={rule.left_table} disabled><option>{semantic.base_table}</option></select></label>
              <label><span>Join key</span><select value={rule.left_field} onChange={(event) => updateRule(index, { left_field: event.target.value })}>{table(semantic.base_table)?.columns.map((column) => <option key={column}>{column}</option>)}</select></label>
              <span className="join-arrow"><GitMerge size={18} /><small>{rule.join_type}</small></span>
              <label><span>To table</span><select value={rule.right_table} onChange={(event) => { const nextTable = table(event.target.value); const shared = nextTable?.columns.find((column) => table(semantic.base_table)?.columns.includes(column)) ?? nextTable?.columns[0] ?? ''; updateRule(index, { right_table: event.target.value, right_field: shared }) }}>{dimensionTables.map((item) => <option key={item.name}>{item.name}</option>)}</select></label>
              <label><span>Join key</span><select value={rule.right_field} onChange={(event) => updateRule(index, { right_field: event.target.value })}>{rightColumns.map((column) => <option key={column}>{column}</option>)}</select></label>
            </div>
            <div className="join-options"><label>Join type <select value={rule.join_type} onChange={(event) => updateRule(index, { join_type: event.target.value as JoinRuleInput['join_type'] })}><option value="LEFT">Keep all fact rows</option><option value="INNER">Only matched rows</option></select></label><label>Cardinality <select value={rule.relationship} onChange={(event) => updateRule(index, { relationship: event.target.value as JoinRuleInput['relationship'] })}><option value="MANY_TO_ONE">Many to one</option><option value="ONE_TO_ONE">One to one</option></select></label></div>
          </article>
        })}
      </div>
      <div className="mapping-footer"><div>{error ? <span className="relationship-error">{error}</span> : <><CheckCircle2 size={17} /><span>Publishing rebuilds the read-only view atomically</span></>}</div><button className="primary-button" disabled={saving || !rules.length} onClick={() => void publish()}>{saving ? 'Validating…' : 'Validate & publish'} <ArrowRight size={16} /></button></div>
    </section>
    <section className="section-card relationship-preview">
      <div className="section-header"><div><h2>Joined view preview</h2><p>Sample rows from <code>{semantic.view_name}</code>. Text-to-SQL uses this view for cross-source questions.</p></div><span>{semantic.view.preview.length} rows</span></div>
      <div className="preview-scroll"><table><thead><tr>{previewColumns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{semantic.view.preview.map((row, index) => <tr key={index}>{previewColumns.map((column) => <td key={column}>{String(row[column] ?? '—')}</td>)}</tr>)}</tbody></table></div>
    </section>
  </div>
}

function AnalyzePage({ sessions, activeSessionId, ask, setRunning }: { sessions: Session[]; activeSessionId: string | null; ask: (sessionId: string, question: string) => void; setRunning: (sessionId: string, running: boolean) => void }) {
  const cancelRefs = useRef(new Map<string, () => Promise<void>>())
  const suggestions = useMemo(() => [
    'Compare cost, pass rate and turnaround time by vendor',
    'What cost trends or anomalies appeared in recent months?',
    'Which materials have the highest failure rate?',
  ], [])

  return <div className="page analyze-page">
    <section className="analyze-hero"><div><span className="eyebrow">ASK & ANALYZE</span><h1>What would you like to understand?</h1><p>Ask across every trusted source. Prism will show its work, use the right tools, and surface the decision—not just the numbers.</p></div></section>
    {sessions.map((session) => <section className="session-workspace" key={session.id} hidden={activeSessionId !== session.id}>
      {!session.turns.length && <div className="suggestion-grid">{suggestions.map((item, index) => <button key={item} onClick={() => ask(session.id, item)}><span className={`suggestion-icon s${index}`}><LayoutDashboard size={18} /></span><strong>{['Compare vendor performance','Find recent anomalies','Inspect quality risk'][index]}</strong><small>{item}</small><ArrowRight size={16} /></button>)}</div>}
      {session.turns.map((turn, index) => <div className="conversation" key={`${index}-${turn}`}><div className="user-message">{turn}</div><A2UIConversation conversationId={session.id} question={turn} onStreamingChange={index === session.turns.length - 1 ? (running) => setRunning(session.id, running) : undefined} onCancelReady={index === session.turns.length - 1 ? (cancel) => { if (cancel) cancelRefs.current.set(session.id, cancel); else cancelRefs.current.delete(session.id) } : undefined} /></div>)}
      <QuestionComposer running={session.running} onSubmit={(question) => ask(session.id, question)} onStop={() => void cancelRefs.current.get(session.id)?.()} />
    </section>)}
    {!sessions.length && <div className="assistant-loading"><i /><span>Preparing your first conversation…</span></div>}
  </div>
}

function QuestionComposer({ running, onSubmit, onStop }: { running: boolean; onSubmit: (question: string) => void; onStop: () => void }) {
  const [draft, setDraft] = useState('')
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const composingRef = useRef(false)
  const submit = () => {
    const value = draft.trim()
    if (running || !value) return
    onSubmit(value)
    setDraft('')
    window.requestAnimationFrame(() => inputRef.current?.focus())
  }
  useEffect(() => { if (!running) inputRef.current?.focus() }, [running])
  return <div className="composer-wrap"><form className={`composer ${running ? 'running stop-mode' : ''}`} onSubmit={(event) => { event.preventDefault(); submit() }}><Sparkles size={18} /><textarea ref={inputRef} rows={1} value={draft} placeholder={running ? 'Prepare your next question while this response runs…' : 'Ask a question about the published data…'} onChange={(event) => setDraft(event.target.value)} onCompositionStart={() => { composingRef.current = true }} onCompositionEnd={() => { composingRef.current = false }} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && !composingRef.current) { event.preventDefault(); submit() } }} aria-label="Ask a question" /><button type={running ? 'button' : 'submit'} disabled={!running && !draft.trim()} onClick={running ? onStop : undefined} aria-label={running ? 'Stop response' : 'Send question'} title={running ? 'Stop response' : 'Send question'}>{running ? <Square size={13} fill="currentColor" /> : <ArrowRight size={18} />}</button></form><span>{running ? 'You can keep typing. Stop the current response or wait for it to finish before sending.' : 'Answers use published data only. SQL and metric definitions remain available for review.'}</span></div>
}
