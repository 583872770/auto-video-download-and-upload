<script setup lang="ts">
import { computed, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { api } from '../../api';
import { useResource } from '../../composables/resource';
import { usePolling } from '../../composables/polling';
import VideoResults from './VideoResults.vue';
import { fileStates } from './results';
import { videoStates, type Page, type VideoTask } from './contracts';
const list = useResource<Page<VideoTask>>();
const query = ref(''),
  status = ref(''),
  samplesOnly = ref(false),
  viewing = ref<VideoTask>();
const cursor = ref<string | null>(null),
  history = ref<Array<string | null>>([]);
const applied = ref({ q: '', status: '', samples_only: '' });
async function refresh(signal?: AbortSignal) {
  const params = new URLSearchParams(applied.value);
  if (!applied.value.status) params.delete('status');
  if (cursor.value) params.set('cursor', cursor.value);
  await list.load((signal) => api.request('/api/jobs/videos?' + params, { signal }), signal);
}
usePolling(
  computed(() => !viewing.value),
  refresh,
  15000,
);
function filter() {
  applied.value = {
    q: query.value.trim(),
    status: status.value,
    samples_only: samplesOnly.value ? '1' : '',
  };
  cursor.value = null;
  history.value = [];
  void refresh();
}
function page(next: boolean) {
  if (next && list.data.value?.next_cursor) {
    history.value.push(cursor.value);
    cursor.value = list.data.value.next_cursor;
  } else if (!next) cursor.value = history.value.pop() || null;
  void refresh();
}
</script>
<template>
  <section class="card">
    <h2>全部视频与文件</h2>
    <p>跨任务查看执行结果、正式文件和保留的测试样本。处理成功与文件是否还保留分别显示。</p>
    <form class="toolbar" @submit.prevent="filter">
      <input
        v-model="query"
        aria-label="全部视频 ID 查询"
        placeholder="完整视频 ID / YouTube 链接"
        maxlength="2048"
      />
      <select v-model="status" aria-label="执行状态">
        <option value="">全部有效视频</option>
        <option v-for="(label, key) in videoStates" :key="key" :value="key">{{ label }}</option>
      </select>
      <label><input v-model="samplesOnly" type="checkbox" />只看保留测试样本</label>
      <button>查询 / 刷新</button>
    </form>
    <p v-if="list.error.value" class="form-error" role="alert">{{ list.error.value }}</p>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>视频 ID / 标题</th>
            <th>所属任务</th>
            <th>执行状态</th>
            <th>文件状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="video in list.data.value?.items" :key="video.id">
            <td>
              {{ video.video_id }}<small class="cell-note">{{ video.title }}</small>
            </td>
            <td>
              <RouterLink
                class="text-link"
                :to="{ name: 'job', params: { id: video.batch_id }, query: { tab: 'videos' } }"
                >{{ video.job_name || video.batch_id }}</RouterLink
              >
            </td>
            <td>
              <span class="badge">{{
                video.result?.test_mode && video.status === 'succeeded'
                  ? '测试完成'
                  : videoStates[video.status] || video.status
              }}</span>
            </td>
            <td>{{ fileStates[video.file_state || 'none'] }}</td>
            <td><button @click="viewing = video">查看结果 / 文件</button></td>
          </tr>
          <tr v-if="!list.data.value?.items.length">
            <td colspan="5" class="empty">
              {{ list.loading.value ? '正在读取…' : '没有符合条件的视频。' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="pager">
      <button :disabled="!history.length || list.loading.value" @click="page(false)">上一页</button
      ><span>第 {{ history.length + 1 }} 页 · 每页 50 条</span
      ><button :disabled="!list.data.value?.next_cursor || list.loading.value" @click="page(true)">
        下一页
      </button>
    </div>
    <VideoResults
      v-if="viewing"
      :key="viewing.id"
      :job-id="viewing.batch_id"
      :task-id="viewing.id"
      @close="viewing = undefined"
      @changed="refresh()"
    />
  </section>
</template>
