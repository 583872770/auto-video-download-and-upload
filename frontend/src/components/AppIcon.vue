<script setup lang="ts">
defineProps<{ name: string }>();
const paths: Record<string, string> = {
  grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  download: 'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',
  media: 'M3 5h18v14H3z M9 9l6 3-6 3z',
  chart: 'M4 3v18h17M9 16v-5m5 5V6m5 10V9',
  users:
    'M16 21v-3a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v3m14-14a4 4 0 0 1 0 8m6 6v-3a4 4 0 0 0-3-3M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  server: 'M3 3h18v7H3z M3 14h18v7H3z M7 6.5h.01M7 17.5h.01',
  logs: 'M5 3h14v18H5z M9 7h6M9 12h6M9 17h3',
  network: 'M12 8v5M5 16v-3h14v3M9 2h6v6H9z M2 16h6v6H2z M16 16h6v6h-6z',
  settings: 'M4 7h16M4 17h16M8 4v6m8 4v6',
  deploy: 'M8 8 4 12l4 4m8-8 4 4-4 4M14 4l-4 16',
  folder: 'M3 6h7l2 2h9v12H3z',
  arrow: 'M5 12h14m-5-5 5 5-5 5',
  plus: 'M12 5v14M5 12h14',
  logout: 'M9 4H4v16h5m5-4 4-4-4-4m-6 4h12',
  menu: 'M4 6h16M4 12h16M4 18h16',
  check: 'M5 12l4 4L19 6',
};
</script>
<template>
  <svg
    class="app-icon"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[name] || paths.grid" />
  </svg>
</template>
