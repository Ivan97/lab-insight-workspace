const API_BASE = '/api/v1'

export interface Ingestion {
  batch_id: string; source_type: 'CSV' | 'XLSX' | 'TEXT'; source_name: string; vendor_hint: string | null
  status: string; record_count: number; quality_score: number; current_stage: string | null; created_at: string; updated_at: string
}
export interface FieldMapping { source_field: string; target_field: string | null; confidence: number; transform: string; reason: string; status: string; sample_before: unknown[]; sample_after: unknown[]; warnings: string[] }
export interface MappingDraft { batch_id: string; version: number; mappings: FieldMapping[]; missing_required_fields: string[]; can_commit: boolean }
export interface ColumnProfile { name: string; inferred_type: string; null_rate: number; distinct_count: number; sample_values: unknown[]; warnings: string[] }
export interface DataProfile { row_count: number; column_count: number; columns: ColumnProfile[]; warnings: string[] }
export interface DataPreview { rows: Record<string, unknown>[]; row_count: number; limit: number }

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${url}`, init)
  if (!response.ok) throw new Error(await response.text())
  return response.json() as Promise<T>
}

export const api = {
  listIngestions: () => json<{ items: Ingestion[]; total: number }>('/ingestions'),
  getMapping: (batchId: string) => json<MappingDraft>(`/ingestions/${batchId}/mapping`),
  getProfile: (batchId: string) => json<DataProfile>(`/ingestions/${batchId}/profile`),
  getPreview: (batchId: string) => json<DataPreview>(`/ingestions/${batchId}/preview`),
  updateMapping: (batchId: string, draft: MappingDraft) => json<MappingDraft>(`/ingestions/${batchId}/mapping`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft) }),
  commitIngestion: (batchId: string) => json<Ingestion>(`/ingestions/${batchId}/commit`, { method: 'POST' }),
  createConversation: () => json<{ conversation_id: string }>('/conversations', { method: 'POST' }),
  cancelMessage: (conversationId: string, messageId: string) => json<{ message_id: string; status: string }>(`/conversations/${conversationId}/messages/${messageId}/cancel`, { method: 'POST' }),
  ingestText: (sourceName: string, content: string) => json<Ingestion>('/ingestions/text', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_name: sourceName, content }) }),
  uploadFile: async (file: File) => { const form = new FormData(); form.append('file', file); return json<Ingestion>('/ingestions/files', { method: 'POST', body: form }) },
}
