<template>
  <a-image v-if="source" :src="source" v-bind="$attrs" />
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { loadArtifact } from '../api'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ src: string }>()
const source = ref('')

watch(() => props.src, async (value, _, onCleanup) => {
  source.value = ''
  if (/^data:image\/(png|jpeg|webp);base64,/.test(value)) {
    source.value = value
    return
  }
  let active = true
  let objectUrl = ''
  onCleanup(() => {
    active = false
    if (objectUrl) URL.revokeObjectURL(objectUrl)
  })
  try {
    const blob = await loadArtifact(value)
    if (active) source.value = objectUrl = URL.createObjectURL(blob)
  } catch {
    if (active) source.value = ''
  }
}, { immediate: true })
</script>
