const API_BASE = '/api/v1'

export interface Ingestion {
  batch_id: string; source_type: 'CSV' | 'XLSX' | 'TEXT'; source_name: string; vendor_hint: string | null
  status: string; record_count: number; current_stage: string | null; created_at: string; updated_at: string; download_url: string | null
}
export interface FieldMapping { source_field: string; target_field: string | null; confidence: number; transform: string; reason: string; status: string; sample_before: unknown[]; sample_after: unknown[]; warnings: string[] }
export interface MappingDraft { batch_id: string; version: number; mappings: FieldMapping[]; missing_required_fields: string[]; can_commit: boolean }
export interface ColumnProfile { name: string; inferred_type: string; null_rate: number; distinct_count: number; sample_values: unknown[]; warnings: string[] }
export interface DataProfile { row_count: number; column_count: number; columns: ColumnProfile[]; warnings: string[] }
export interface DataPreview { rows: Record<string, unknown>[]; row_count: number; limit: number }
export interface JoinRuleInput { name: string; left_table: string; left_field: string; right_table: string; right_field: string; join_type: 'LEFT' | 'INNER'; relationship: 'MANY_TO_ONE' | 'ONE_TO_ONE' }
export interface JoinRule extends JoinRuleInput { rule_id: string; status: string; view_name: string; matched_pct: number; right_key_unique: boolean; created_at: string; updated_at: string }
export interface SemanticLayer { base_table: string; view_name: string; tables: { name: string; columns: string[] }[]; rules: JoinRule[]; view: { status: string; column_count: number; preview: Record<string, unknown>[] } }

export interface Conversation { conversation_id: string; title: string; created_at: string; updated_at: string; question_count: number }
export interface StoredMessage { message_id: string; role: 'USER' | 'ASSISTANT'; content: string; status: string; a2ui_replay: unknown[] | null; created_at: string; completed_at: string | null }

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${url}`, init)
  if (!response.ok) throw new Error(await response.text())
  return response.json() as Promise<T>
}

async function empty(url: string, init?: RequestInit): Promise<void> {
  const response = await fetch(`${API_BASE}${url}`, init)
  if (!response.ok) throw new Error(await response.text())
}

export interface CanonicalField { entity: string; field: string; display_name: string; description: string; data_type: string; result_format: string; unit: string | null; enum_values: string[]; is_key: boolean; nullable: boolean }
export interface SchemaEntity { entity: string; display_name: string; description: string; role: string; fields: CanonicalField[] }

export const api = {
  getCanonicalFields: () => json<{ items: string[] }>('/schema/canonical-fields'),
  getRegistry: () => json<{ entities: SchemaEntity[]; field_count: number }>('/schema/registry'),
  listIngestions: () => json<{ items: Ingestion[]; total: number }>('/ingestions'),
  getMapping: (batchId: string) => json<MappingDraft>(`/ingestions/${batchId}/mapping`),
  getProfile: (batchId: string) => json<DataProfile>(`/ingestions/${batchId}/profile`),
  getPreview: (batchId: string) => json<DataPreview>(`/ingestions/${batchId}/preview`),
  getRelationships: () => json<SemanticLayer>('/schema/relationships'),
  publishRelationships: (rules: JoinRuleInput[]) => json<SemanticLayer>('/schema/relationships', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rules }) }),
  updateMapping: (batchId: string, draft: MappingDraft) => json<MappingDraft>(`/ingestions/${batchId}/mapping`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft) }),
  commitIngestion: (batchId: string) => json<Ingestion>(`/ingestions/${batchId}/commit`, { method: 'POST' }),
  createConversation: () => json<{ conversation_id: string }>('/conversations', { method: 'POST' }),
  listConversations: () => json<{ items: Conversation[]; total: number }>('/conversations'),
  deleteConversation: (conversationId: string) => empty(`/conversations/${conversationId}`, { method: 'DELETE' }),
  listMessages: (conversationId: string) => json<{ items: StoredMessage[]; total: number }>(`/conversations/${conversationId}/messages`),
  cancelMessage: (conversationId: string, messageId: string) => json<{ message_id: string; status: string }>(`/conversations/${conversationId}/messages/${messageId}/cancel`, { method: 'POST' }),
  ingestText: (sourceName: string, content: string) => json<Ingestion>('/ingestions/text', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_name: sourceName, content }) }),
  uploadFile: async (file: File) => { const form = new FormData(); form.append('file', file); return json<Ingestion>('/ingestions/files', { method: 'POST', body: form }) },
}
