<script setup lang="ts">
import { computed, ref } from 'vue';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import { useInstances } from '../../stores/instances';
import { useResource } from '../../composables/resource';
import { usePolling } from '../../composables/polling';
import { useLeaveGuard, confirmNavigation } from '../../composables/leaveGuard';
import InstancePicker from '../../components/InstancePicker.vue';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import TaskLog from '../../components/TaskLog.vue';
import MediaPreview from './MediaPreview.vue';
import { mediaAddress, type MediaItem, type MediaPage } from './contracts';
import { formatGB } from '../jobs/contracts';
const instances = useInstances(),
  editable = bootstrap().role !== 'viewer';
const resource = useResource<MediaPage>(),
  ready = ref(false),
  busy = ref(false),
  query = ref('');
const cursor = ref<string | null>(null),
  history = ref<Array<string | null>>([]);
const preview = ref<MediaItem>(),
  dialog = ref<'import' | 'clear'>(),
  confirmation = ref('');
const video = ref<File>(),
  cover = ref<File>(),
  error = ref(''),
  message = ref('');
const task = ref<{ id: string; instance_id: string }>();
useLeaveGuard(() => ({
  dirty: dialog.value === 'import' && !!(video.value || cover.value),
  busy: busy.value,
}));
async function refresh(signal?: AbortSignal) {
  if (!ready.value || busy.value) return;
  const scope = instances.scope(),
    params = new URLSearchParams({ q: query.value, limit: '24' });
  if (cursor.value) params.set('cursor', cursor.value);
  await resource.load((signal) => api.request('/api/media?' + params, { scope, signal }), signal);
}
function filter() {
  cursor.value = null;
  history.value = [];
  void refresh();
}
function page(next: boolean) {
  if (next && resource.data.value?.next_cursor) {
    history.value.push(cursor.value);
    cursor.value = resource.data.value.next_cursor;
  } else if (!next) cursor.value = history.value.pop() || null;
  void refresh();
}
function instanceChanged() {
  resource.clear();
  preview.value = undefined;
  task.value = undefined;
  dialog.value = undefined;
  video.value = undefined;
  cover.value = undefined;
  error.value = '';
  message.value = '';
  ready.value = !bootstrap().fleetEnabled || !!instances.selectedId;
  filter();
}
function file(event: Event, kind: 'video' | 'cover') {
  const picked = (event.target as HTMLInputElement).files?.[0];
  if (kind === 'video') video.value = picked;
  else cover.value = picked;
}
async function closeDialog() {
  if (await confirmNavigation()) {
    dialog.value = undefined;
    video.value = undefined;
    cover.value = undefined;
  }
}
async function write(action: 'import' | 'clear' | 'reindex') {
  if (!editable || busy.value || !ready.value || resource.error.value || !resource.data.value)
    return;
  if (action === 'clear' && confirmation.value !== '清空') {
    error.value = '请输入“清空”确认。';
    return;
  }
  if (action === 'import' && (!video.value || !cover.value)) {
    error.value = '请选择视频文件和对应封面。';
    return;
  }
  const scope = instances.scope(),
    directory = resource.data.value.directory;
  busy.value = true;
  error.value = '';
  message.value = '';
  try {
    const form = new FormData();
    if (video.value) form.set('video', video.value);
    if (cover.value) form.set('cover', cover.value);
    const result = await api.request<{ id?: string; task?: { id: string }; message?: string }>(
      '/api/media' + (action === 'import' ? '' : '/' + action),
      {
        method: 'POST',
        scope,
        body:
          action === 'import'
            ? form
            : action === 'clear'
              ? { confirmation: confirmation.value, directory }
              : {},
      },
    );
    const id = result.task?.id || result.id;
    if (id) task.value = { id, instance_id: scope.id };
    message.value =
      result.message || (action === 'import' ? '视频已导入。' : '操作已提交，可查看执行日志。');
    dialog.value = undefined;
    video.value = undefined;
    cover.value = undefined;
    confirmation.value = '';
    cursor.value = null;
    history.value = [];
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : String(failure);
  } finally {
    busy.value = false;
    await refresh();
  }
}
usePolling(
  computed(() => ready.value && !busy.value && !dialog.value),
  refresh,
  10000,
);
</script>
<template>
  <main class="content">
    <div class="page-heading">
      <div>
        <span class="eyebrow">MEDIA LIBRARY</span>
        <h1>素材库</h1>
        <p>查看当前实例的本地视频和封面。</p>
      </div>
      <InstancePicker :disabled="busy || !!dialog" @change="instanceChanged" />
    </div>
    <p v-if="message" class="notice" role="status">{{ message }}</p>
    <p v-if="error && !dialog" class="form-error" role="alert">{{ error }}</p>
    <section class="card">
      <div class="section-heading">
        <div>
          <h2>本地视频 · {{ resource.data.value?.total ?? '—' }}</h2>
          <p class="cell-note">{{ resource.data.value?.directory }}</p>
        </div>
        <div class="actions">
          <button
            :disabled="!editable || busy || !resource.data.value || !!resource.error.value"
            @click="
              dialog = 'import';
              error = '';
            "
          >
            导入视频</button
          ><button
            :disabled="!editable || busy || !resource.data.value || !!resource.error.value"
            @click="write('reindex')"
          >
            同步本地文件</button
          ><button
            class="danger"
            :disabled="!editable || busy || !resource.data.value || !!resource.error.value"
            @click="
              dialog = 'clear';
              error = '';
              confirmation = '';
            "
          >
            清空素材库
          </button>
        </div>
      </div>
      <form class="toolbar" @submit.prevent="filter">
        <input
          v-model="query"
          aria-label="搜索素材"
          placeholder="标题前缀或视频 ID"
          maxlength="200"
        /><button :disabled="busy || resource.loading.value">查询</button>
      </form>
      <p v-if="resource.error.value" class="form-error" role="alert">
        {{ resource.error.value }} <button @click="refresh()">重试</button>
      </p>
      <p v-if="resource.loading.value && !resource.data.value" role="status">正在读取素材…</p>
      <div class="media-grid">
        <button
          v-for="item in resource.data.value?.items"
          :key="item.id"
          class="media-card"
          data-media
          :aria-label="(item.playable ? '播放：' : '查看：') + item.title"
          @click="preview = { ...item, instance_id: instances.selectedId || item.instance_id }"
        >
          <span class="media-image"
            ><img
              v-if="item.cover"
              :src="mediaAddress('covers', item.cover, instances.selectedId || item.instance_id)"
              :alt="item.title + '的封面'"
              loading="lazy"
            /><span v-else>暂无封面</span
            ><span class="media-type">{{ item.playable ? '视频' : '待合并' }}</span></span
          ><span class="media-info"
            ><strong>{{ item.title || item.name }}</strong
            ><small class="cell-note"
              >{{ formatGB(item.size) }} ·
              {{ item.playable ? '点击播放' : '音视频尚未合并' }}</small
            ></span
          >
        </button>
      </div>
      <p v-if="resource.data.value && !resource.data.value.items.length" class="empty">
        暂无符合条件的素材。下载完成后可同步本地文件。
      </p>
      <div class="pager">
        <button
          id="media-prev"
          :disabled="!history.length || resource.loading.value || busy"
          @click="page(false)"
        >
          上一页</button
        ><button
          id="media-next"
          :disabled="!resource.data.value?.next_cursor || resource.loading.value || busy"
          @click="page(true)"
        >
          下一页
        </button>
      </div>
    </section>
    <MediaPreview v-if="preview" :item="preview" @close="preview = undefined" />
    <TaskLog
      v-if="task"
      :key="task.id"
      :task="task"
      @close="
        task = undefined;
        refresh();
      "
    />
    <WorkspaceDialog
      v-if="dialog"
      :open="true"
      :title="dialog === 'import' ? '导入视频' : '清空当前实例素材库'"
      :busy="busy"
      @close="closeDialog"
      ><form @submit.prevent="write(dialog!)">
        <template v-if="dialog === 'import'"
          ><label
            >视频文件<input
              type="file"
              accept=".mp4,.mkv,.webm,.mov"
              :disabled="busy"
              @change="file($event, 'video')" /></label
          ><label
            >封面图片<input
              type="file"
              accept=".jpg,.jpeg,.png,.webp"
              :disabled="busy"
              @change="file($event, 'cover')" /></label></template
        ><template v-else
          ><p>
            将删除 {{ instances.name
            }}<template v-if="instances.selectedId">（{{ instances.selectedId }}）</template>
            素材目录中的文件，无法撤销。
          </p>
          <p class="cell-note">{{ resource.data.value?.directory }}</p>
          <label
            >输入“清空”确认<input
              v-model="confirmation"
              aria-label="确认清空素材"
              :disabled="busy"
              autocomplete="off" /></label
        ></template>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        <button type="submit" :disabled="busy">
          {{ busy ? '正在提交…' : dialog === 'import' ? '确认导入' : '确认清空' }}
        </button>
      </form></WorkspaceDialog
    >
  </main>
</template>
