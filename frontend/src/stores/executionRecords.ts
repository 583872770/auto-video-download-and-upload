import { ref } from 'vue';
import { defineStore } from 'pinia';
import type { Page } from '../features/jobs/contracts';

export interface ExecutionRecord {
  id: string;
  title: string;
  video_id: string;
  instance_id: string;
  action: string;
  status: string;
  error?: string;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  progress?: { stage?: string };
}

/** Bounded, memory-only pages belong to this signed-in app session. */
export const useExecutionRecords = defineStore('execution-records', () => {
  const query = ref(''),
    status = ref(''),
    view = ref('history');
  const cursor = ref<string | null>(null),
    history = ref<(string | null)[]>([]);
  const pages = new Map<string, { value: Page<ExecutionRecord>; savedAt: number }>();
  function key() {
    return JSON.stringify([query.value, status.value, view.value, cursor.value]);
  }
  function cached() {
    const entry = pages.get(key());
    if (!entry || Date.now() - entry.savedAt > 120000) {
      pages.delete(key());
      return undefined;
    }
    return entry.value;
  }
  function remember(identity: string, value: Page<ExecutionRecord>) {
    pages.delete(identity);
    pages.set(identity, { value, savedAt: Date.now() });
    while (pages.size > 8) pages.delete(pages.keys().next().value!);
  }
  return { query, status, view, cursor, history, key, cached, remember };
});
