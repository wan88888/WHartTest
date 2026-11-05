import { request } from '@/utils/request'
import { useAuthStore } from '@/store/authStore'
import { RequirementDocumentService } from './requirementService'
import type { CreateDocumentRequest } from '../types'

interface ImportDraft {
  project: string
  title: string
  description?: string
  feishu_url: string
}

function draftKey() {
  return 'requirement-feishu-import-' + useAuthStore().currentUser?.id
}

export function getImportDraft(): (ImportDraft & { state: string }) | null {
  return JSON.parse(sessionStorage.getItem(draftKey()) || 'null')
}

export function clearImportDraft() {
  sessionStorage.removeItem(draftKey())
}

export async function startImport(draft: ImportDraft) {
  const result = await request<{ authorize_url: string }>({
    method: 'POST', url: '/requirements/documents/feishu-authorize/',
    data: { project: draft.project, feishu_url: draft.feishu_url }, withCredentials: true,
  })
  if (!result.success || !result.data) throw new Error(result.error || '无法发起飞书文档授权。')
  const url = new URL(result.data.authorize_url)
  const state = url.searchParams.get('state')
  if (url.origin !== 'https://accounts.feishu.cn' || url.pathname !== '/open-apis/authen/v1/authorize' || !state?.startsWith('doc.')) {
    throw new Error('飞书授权地址无效。')
  }
  sessionStorage.setItem(draftKey(), JSON.stringify({ ...draft, state }))
  window.location.assign(url.href)
}

export async function completeImport(state: string, code: string) {
  const draft = getImportDraft()
  if (!draft || draft.state !== state) throw new Error('导入请求已失效，请重新上传。')
  const data: CreateDocumentRequest = {
    ...draft, document_type: 'txt', feishu_code: code, feishu_state: state,
  }
  const result = await RequirementDocumentService.uploadDocument(data)
  if (result.status !== 'success') throw new Error(result.message)
  clearImportDraft()
}
