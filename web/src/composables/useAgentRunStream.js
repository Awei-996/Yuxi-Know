import { unref } from 'vue'
import { agentApi } from '@/apis'
import { handleChatError } from '@/utils/errorHandler'

const RUN_TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'interrupted'])
const ACTIVE_RUN_STORAGE_TTL_MS = 60 * 60 * 1000
const ACTIVE_RUN_CLIENT_ID = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
const RUN_SEQ_PATTERN = /^\d+-\d+$/

const getActiveRunStorageKey = (threadId) => `active_run:${threadId}`

const normalizeRunSeq = (value) => {
  if (value === undefined || value === null) return '0-0'
  const text = String(value).trim()
  return RUN_SEQ_PATTERN.test(text) ? text : '0-0'
}

const parseRunSeq = (value) => {
  const text = normalizeRunSeq(value)
  if (!text.includes('-')) {
    return { major: 0n, minor: 0n }
  }
  const [majorRaw, minorRaw] = text.split('-', 2)

  try {
    const major = BigInt(majorRaw || '0')
    const minor = BigInt(minorRaw || '0')
    return { major, minor }
  } catch {
    return { major: 0n, minor: 0n }
  }
}

const compareRunSeq = (incoming, current) => {
  const left = parseRunSeq(incoming)
  const right = parseRunSeq(current)

  if (left.major > right.major) return 1
  if (left.major < right.major) return -1
  if (left.minor > right.minor) return 1
  if (left.minor < right.minor) return -1
  return 0
}

const getThreadIdFromObject = (value) => {
  if (!value || typeof value !== 'object') return ''
  if (typeof value.thread_id === 'string' && value.thread_id.trim()) return value.thread_id.trim()
  const nestedSources = [value.meta, value.metadata, value.configurable, value.stream_event]
  for (const source of nestedSources) {
    const nestedThreadId = getThreadIdFromObject(source)
    if (nestedThreadId) return nestedThreadId
  }
  return ''
}

const resolveChunkThreadId = ({ envelope, payload, chunk, fallbackThreadId }) => {
  return (
    getThreadIdFromObject(envelope) ||
    getThreadIdFromObject(payload) ||
    getThreadIdFromObject(chunk) ||
    fallbackThreadId
  )
}

const processRunSseResponse = async (response, onEvent) => {
  if (!response || !response.body) return
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let eventType = 'message'
  let eventId = null
  let dataLines = []

  const dispatch = () => {
    if (dataLines.length === 0) return
    const dataText = dataLines.join('\n')
    try {
      const parsed = JSON.parse(dataText)
      onEvent(eventType, parsed, eventId)
    } catch (e) {
      console.warn('Failed to parse run SSE data:', e, dataText)
    }
  }

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const rawLine of lines) {
        const line = rawLine.replace(/\r$/, '')
        if (!line) {
          dispatch()
          eventType = 'message'
          eventId = null
          dataLines = []
          continue
        }

        if (line.startsWith(':')) {
          continue
        }
        if (line.startsWith('event:')) {
          eventType = line.slice(6).trim() || 'message'
        } else if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).trimStart())
        } else if (line.startsWith('id:')) {
          eventId = line.slice(3).trim()
        }
      }
    }

    dispatch()
  } finally {
    try {
      reader.releaseLock()
    } catch {
      // ignore
    }
  }
}

export function useAgentRunStream({
  getThreadState,
  currentAgentId,
  handleStreamChunk,
  fetchThreadMessages,
  fetchAgentState,
  resetOnGoingConv,
  onScrollToBottom,
  streamSmoother
}) {
  const saveActiveRunSnapshot = (threadId, runId, lastSeq = '0-0') => {
    if (!threadId || !runId) return
    localStorage.setItem(
      getActiveRunStorageKey(threadId),
      JSON.stringify({
        run_id: runId,
        last_seq: normalizeRunSeq(lastSeq),
        created_at: Date.now(),
        client_id: ACTIVE_RUN_CLIENT_ID
      })
    )
  }

  const loadActiveRunSnapshot = (threadId) => {
    if (!threadId) return null
    try {
      const raw = localStorage.getItem(getActiveRunStorageKey(threadId))
      return raw ? JSON.parse(raw) : null
    } catch {
      return null
    }
  }

  const clearActiveRunSnapshot = (threadId) => {
    if (!threadId) return
    localStorage.removeItem(getActiveRunStorageKey(threadId))
  }

  const stopRunStreamSubscription = (threadId) => {
    const ts = getThreadState(threadId)
    if (!ts) return
    streamSmoother?.flushThread(threadId)
    if (ts.runStreamAbortController) {
      ts.runStreamAbortController.abort()
      ts.runStreamAbortController = null
    }
  }

  const finalizeRunStream = (threadId, runId, touchedThreadIds, { delay = 200, scroll = false } = {}) => {
    const ts = getThreadState(threadId)
    if (!ts || ts.activeRunId !== runId) return
    touchedThreadIds.forEach((id) => streamSmoother?.flushThread(id))
    ts.isStreaming = false
    ts.activeRunId = null
    ts.lastRetryableJobTry = null
    ts.replyLoadingVisible = false
    ts.pendingRequestId = null
    clearActiveRunSnapshot(threadId)
    fetchThreadMessages({ agentId: unref(currentAgentId), threadId, delay }).finally(() => {
      resetOnGoingConv(threadId)
      fetchAgentState(unref(currentAgentId), threadId)
      if (scroll) onScrollToBottom()
    })
  }

  const scheduleRunReconnect = (threadId, runId, delay = 500) => {
    const ts = getThreadState(threadId)
    if (!ts || ts.activeRunId !== runId) return
    setTimeout(() => {
      const latest = getThreadState(threadId)
      if (latest?.activeRunId === runId && !latest.runStreamAbortController) {
        void startRunStream(threadId, runId, latest.runLastSeq)
      }
    }, delay)
  }

  const startRunStream = async (threadId, runId, afterSeq = '0-0') => {
    if (!threadId || !runId) return
    const ts = getThreadState(threadId)
    if (!ts) return

    stopRunStreamSubscription(threadId)
    const runController = new AbortController()
    ts.runStreamAbortController = runController
    ts.activeRunId = runId
    ts.runLastSeq = normalizeRunSeq(afterSeq)
    ts.lastRetryableJobTry = null
    ts.isStreaming = true
    saveActiveRunSnapshot(threadId, runId, ts.runLastSeq)
    const touchedThreadIds = new Set([threadId])
    let sawTerminalEvent = false

    try {
      const response = await agentApi.streamAgentRunEvents(runId, ts.runLastSeq, {
        signal: runController.signal
      })
      if (!response.ok) {
        throw new Error(`SSE response not ok: ${response.status}`)
      }

      await processRunSseResponse(response, (event, data, eventId) => {
        if (!data || ts.activeRunId !== runId) return

        if (eventId) {
          const incomingSeq = normalizeRunSeq(eventId)
          if (compareRunSeq(incomingSeq, ts.runLastSeq) <= 0) return
          ts.runLastSeq = incomingSeq
          saveActiveRunSnapshot(threadId, runId, incomingSeq)
        }

        const payload = data.payload || {}
        const terminalStatus = event === 'end' ? payload.status : data.status
        const isRetryableError =
          event === 'error' && (payload?.retryable === true || payload?.chunk?.retryable === true)
        if (isRetryableError) {
          const parsedJobTry = Number.parseInt(payload?.chunk?.job_try, 10)
          const retryJobTry = Number.isNaN(parsedJobTry) ? null : parsedJobTry
          if (retryJobTry !== null && ts.lastRetryableJobTry === retryJobTry) {
            return
          }
          ts.lastRetryableJobTry = retryJobTry
          console.warn('Run encountered retryable error, waiting for worker retry', {
            threadId,
            runId,
            retryJobTry,
            errorType: payload?.chunk?.error_type
          })
          return
        }

        if (Array.isArray(payload.items)) {
          payload.items.forEach((chunk) => {
            const routeThreadId = resolveChunkThreadId({
              envelope: data,
              payload,
              chunk,
              fallbackThreadId: threadId
            })
            touchedThreadIds.add(routeThreadId)
            handleStreamChunk(
              { ...chunk, run_id: chunk.run_id || data.run_id || runId, thread_id: routeThreadId },
              routeThreadId
            )
          })
        } else if (payload.chunk) {
          const routeThreadId = resolveChunkThreadId({
            envelope: data,
            payload,
            chunk: payload.chunk,
            fallbackThreadId: threadId
          })
          touchedThreadIds.add(routeThreadId)
          handleStreamChunk(
            { ...payload.chunk, run_id: payload.chunk.run_id || data.run_id || runId, thread_id: routeThreadId },
            routeThreadId
          )
        }

        if (event === 'end') {
          sawTerminalEvent = true
          if (RUN_TERMINAL_STATUSES.has(terminalStatus)) {
            finalizeRunStream(threadId, runId, touchedThreadIds)
          } else {
            touchedThreadIds.forEach((id) => streamSmoother?.flushThread(id))
            ts.isStreaming = false
          }
        }

        if (event === 'error') {
          sawTerminalEvent = true
          finalizeRunStream(threadId, runId, touchedThreadIds, { delay: 300, scroll: true })
        }
      })

      if (!sawTerminalEvent && !runController.signal.aborted && ts.activeRunId === runId) {
        try {
          const runRes = await agentApi.getAgentRun(runId)
          const run = runRes?.run
          if (run && RUN_TERMINAL_STATUSES.has(run.status)) {
            finalizeRunStream(threadId, runId, touchedThreadIds)
          } else {
            scheduleRunReconnect(threadId, runId)
          }
        } catch (e) {
          console.warn('Run SSE closed before terminal event; reconnecting after status check failed:', e)
          scheduleRunReconnect(threadId, runId)
        }
      }
    } catch (error) {
      if (error?.name !== 'AbortError') {
        streamSmoother?.flushThread(threadId)
        console.error('Run SSE stream error:', error)
        handleChatError(error, 'stream')
        scheduleRunReconnect(threadId, runId)
      } else if (ts.activeRunId !== runId) {
        ts.replyLoadingVisible = false
        ts.pendingRequestId = null
      }
    } finally {
      if (ts.runStreamAbortController === runController) {
        ts.runStreamAbortController = null
      }
      if (!ts.activeRunId) {
        ts.isStreaming = false
        ts.replyLoadingVisible = false
        ts.pendingRequestId = null
      }
    }
  }

  const resumeActiveRunForThread = async (threadId) => {
    if (!threadId) return
    const ts = getThreadState(threadId)
    if (!ts || ts.runStreamAbortController) return

    const snapshot = loadActiveRunSnapshot(threadId)
    if (snapshot?.run_id) {
      if (Date.now() - Number(snapshot.created_at || 0) > ACTIVE_RUN_STORAGE_TTL_MS) {
        clearActiveRunSnapshot(threadId)
      } else {
        try {
          const runRes = await agentApi.getAgentRun(snapshot.run_id)
          const run = runRes?.run
          if (run && !RUN_TERMINAL_STATUSES.has(run.status)) {
            await startRunStream(threadId, run.id, snapshot.last_seq || '0-0')
            return
          }
        } catch {
          // ignore
        }
        clearActiveRunSnapshot(threadId)
      }
    }

    try {
      const active = await agentApi.getThreadActiveRun(threadId)
      const run = active?.run
      if (run && !RUN_TERMINAL_STATUSES.has(run.status)) {
        await startRunStream(threadId, run.id, '0-0')
        return
      }
    } catch (e) {
      console.warn('Failed to load active run for thread:', threadId, e)
    }

    ts.activeRunId = null
    ts.runLastSeq = '0-0'
    ts.isStreaming = false
    ts.replyLoadingVisible = false
    ts.pendingRequestId = null
    clearActiveRunSnapshot(threadId)
  }

  return {
    startRunStream,
    resumeActiveRunForThread,
    stopRunStreamSubscription
  }
}
