import { ref } from 'vue';
import { defineStore } from 'pinia';

// Returning from a task detail must keep the customer's search and archived view.
export const useJobList = defineStore('job-list', () => {
  const query = ref(''),
    lifecycle = ref('active');
  const cursor = ref<string | null>(null),
    history = ref<Array<string | null>>([]);
  return { query, lifecycle, cursor, history };
});
