import axios from 'axios'
import { getApiBaseUrl } from '@/utils/request'
import type { ApiTokenResponseData } from './authService'

// Public OAuth endpoints must also work when the browser has an expired platform token.
const client = axios.create({
  baseURL: getApiBaseUrl().replace(/\/$/, '') + '/accounts/feishu/',
  withCredentials: true,
  timeout: 30000,
})
const returnKey = 'feishu-login-return-path'

async function post<T>(path: string, payload?: Record<string, string>): Promise<T> {
  try {
    const { data } = await client.post(path, payload || {})
    return data.data
  } catch (error) {
    const body = axios.isAxiosError(error) && error.response?.data
    throw new Error(body?.errors?.error || body?.message || '飞书登录服务暂时不可用，请稍后重试。')
  }
}

export async function feishuLoginAvailable(): Promise<boolean> {
  try {
    const { data } = await client.get('config/')
    return data.data?.enabled === true
  } catch {
    return false
  }
}

function returnPath(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('/')) return '/dashboard'
  const url = new URL(value, window.location.origin)
  return url.origin === window.location.origin && !url.pathname.startsWith('//')
    ? url.pathname + url.search + url.hash : '/dashboard'
}

export async function authorizeFeishu(redirect: unknown) {
  const result = await post<{ authorize_url: string }>('authorize/')
  const url = new URL(result.authorize_url)
  if (url.origin !== 'https://accounts.feishu.cn' || url.pathname !== '/open-apis/authen/v1/authorize') {
    throw new Error('飞书授权地址无效。')
  }
  sessionStorage.setItem(returnKey, returnPath(redirect))
  window.location.assign(url.href)
}

export function finishFeishu(payload: Record<string, string>) {
  return post<ApiTokenResponseData>('callback/', payload)
}

export function takeReturnPath() {
  const value = sessionStorage.getItem(returnKey)
  sessionStorage.removeItem(returnKey)
  return returnPath(value)
}
