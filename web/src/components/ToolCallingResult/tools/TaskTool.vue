<template>
  <BaseToolCall :tool-call="toolCall">
    <template #header>
      <div class="sep-header">
        <span class="note">{{ subagentDisplayName }}</span>
        <span v-if="runStatusLabel" class="run-status" :class="runStatusClass">
          {{ runStatusLabel }}
        </span>
        <span class="separator" v-if="shortDescription">|</span>
        <span class="description" v-if="shortDescription">{{ shortDescription }}</span>
      </div>
    </template>

    <template #params>
      <div v-if="description" class="task-description">{{ description }}</div>
      <div v-if="childThreadId" class="task-thread-id">
        子智能体线程 ID：{{ childThreadId }}
      </div>
    </template>

    <template #result="{ resultContent }">
      <div class="task-result">
        <MarkdownPreview compact :content="String(resultContent)" class="md-preview-wrapper" />
      </div>
    </template>
  </BaseToolCall>
</template>

<script setup>
import { computed } from 'vue'
import BaseToolCall from '../BaseToolCall.vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'

const props = defineProps({
  toolCall: {
    type: Object,
    required: true
  }
})

const parsedArgs = computed(() => {
  const args = props.toolCall.args || props.toolCall.function?.arguments
  if (!args) return {}
  if (typeof args === 'object') return args
  try {
    return JSON.parse(args)
  } catch {
    return {}
  }
})

const subagentRun = computed(() => props.toolCall.subagent_run || null)
const subagentDisplayName = computed(
  () => subagentRun.value?.subagent_name || props.toolCall.display_label || '子智能体'
)
const description = computed(
  () => parsedArgs.value.description || subagentRun.value?.description || ''
)
const childThreadId = computed(() => subagentRun.value?.child_thread_id || parsedArgs.value.thread_id || '')
const hasToolResult = computed(() => Boolean(props.toolCall.tool_call_result || props.toolCall.result))
const runStatus = computed(() => {
  if (props.toolCall.status === 'error') return 'failed'
  if (props.toolCall.status !== 'success' && !hasToolResult.value) return 'running'
  if (subagentRun.value?.status) return subagentRun.value.status
  return 'completed'
})
const runStatusLabel = computed(() => {
  if (runStatus.value === 'completed') return '已完成'
  if (runStatus.value === 'failed') return '失败'
  if (runStatus.value === 'running') return '运行中'
  return ''
})
const runStatusClass = computed(() => ({
  'is-running': runStatus.value === 'running',
  'is-completed': runStatus.value === 'completed',
  'is-failed': runStatus.value === 'failed'
}))
const shortDescription = computed(() => {
  const desc = description.value
  if (!desc) return ''
  return desc.length > 50 ? desc.slice(0, 50) + '...' : desc
})
</script>

<style lang="less" scoped>
.sep-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  width: 100%;
  overflow: hidden;
}

.run-status {
  flex-shrink: 0;
  border-radius: 4px;
  padding: 0 4px;
  font-size: 11px;
  background: var(--gray-25);
  color: var(--gray-600);

  &.is-running {
    color: var(--color-primary-700);
    background: var(--color-primary-50);
  }

  &.is-completed {
    color: var(--color-success-700);
    background: var(--color-success-50);
  }

  &.is-failed {
    color: var(--color-error-700);
    background: var(--color-error-50);
  }
}

.task-description {
  border-radius: 8px;
  font-size: 13px;
  color: var(--gray-800);
  padding: 6px 8px;
  background: var(--gray-50);
}

.task-thread-id {
  margin-top: 6px;
  color: var(--gray-500);
  font-size: 12px;
  word-break: break-all;
}

.task-result {
  border-radius: 8px;

  .md-preview-wrapper {
    color: var(--gray-800);
  }
}
</style>
