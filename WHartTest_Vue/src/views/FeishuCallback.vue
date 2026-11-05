<template>
  <main class="callback">
    <section aria-live="polite">
      <h1>{{ importing ? (isEnglish ? 'Import Feishu document' : '导入飞书文档') : (isEnglish ? 'Feishu sign-in' : '飞书登录') }}</h1>
      <p>{{ error || (importing ? (isEnglish ? 'Importing…' : '正在导入正文…') : (isEnglish ? 'Signing in…' : '正在完成登录…')) }}</p>
      <router-link v-if="error && importing" to="/requirements?retryFeishu=1">{{ isEnglish ? 'Back to requirements' : '返回需求管理' }}</router-link>
      <router-link v-else-if="error" to="/login">{{ isEnglish ? 'Back to sign-in' : '返回登录页' }}</router-link>
    </section>
  </main>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAppI18n } from '@/composables/useAppI18n'
import { Message } from '@arco-design/web-vue'
import { completeImport } from '@/features/requirements/services/feishuImport'
import { finishFeishu, takeReturnPath } from '@/services/feishuAuth'
import { useAuthStore } from '@/store/authStore'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const { isEnglish } = useAppI18n()
const error = ref('')
const importing = typeof route.query.state === 'string' && route.query.state.startsWith('doc.')
onMounted(async () => {
  const { code, state, error: denied } = route.query
  if (importing) {
    window.history.replaceState(window.history.state, '', window.location.pathname)
    if (denied || typeof code !== 'string' || typeof state !== 'string') {
      error.value = isEnglish.value ? 'Document authorization was cancelled.' : '飞书文档授权未完成，请返回重试。'
      return
    }
    try {
      await completeImport(state, code)
      Message.success(isEnglish.value ? 'Document imported' : '飞书文档已导入')
      await router.replace('/requirements')
    } catch (err) {
      error.value = err instanceof Error ? err.message : '飞书文档导入失败。'
    }
    return
  }
  await router.replace({ name: 'FeishuCallback' })
  if (typeof state !== 'string' || !(typeof code === 'string' || typeof denied === 'string')) {
    error.value = isEnglish.value ? 'Invalid sign-in request. Please try again.' : '登录请求无效，请返回登录页重试。'
    return
  }
  try {
    const payload: Record<string, string> = typeof denied === 'string' ? { state, error: denied } : { state, code: code as string }
    const session = await finishFeishu(payload)
    authStore.acceptSession(session)
    await router.replace(takeReturnPath())
  } catch (err) {
    error.value = err instanceof Error ? err.message : '飞书登录失败，请重新授权。'
  }
})
</script>

<style scoped>
.callback { min-height: 100vh; display: grid; place-items: center; padding: 24px; background: var(--color-bg-1); color: var(--color-text-1); }
section { max-width: 32rem; text-align: center; }
h1 { font-size: 24px; }
p { margin: 16px 0; }
a { color: rgb(var(--primary-6)); }
</style>
