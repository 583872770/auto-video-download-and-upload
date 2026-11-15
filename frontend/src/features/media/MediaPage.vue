<script setup lang="ts">
import { computed, ref } from 'vue';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import { useInstances } from '../../stores/instances';
import { useResource } from '../../composables/resource';
import { usePolling } from '../../composables/polling';
import { confirmNavigation } from '../../composables/leaveGuard';
import InstancePicker from '../../components/InstancePicker.vue';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import TaskLog from '../../components/TaskLog.vue';
import MediaGroupFiles from './MediaGroupFiles.vue';
import {
  libraryTypeLabels,
  formatDownloadTime,
  formatFileSize,
  type LibraryKind,
  type MediaGroupsPage,
} from './contracts';

type AssetFilter = 'all' | LibraryKind;

const instances = useInstances(),
  editable = bootstrap().role !== 'viewer';
const resource = useResource<MediaGroupsPage>(),
  ready = ref(false),
  busy = ref(false),
  query = ref(''),
  activeQuery = ref(''),
  assetFilter = ref<AssetFilter>('all');
const position = ref<string | null>(null),
  history = ref<(string | null)[]>([]),
  expanded = ref<string>();
const dialog = ref<'import' | 'clear'>(),
  confirmation = ref('');
const video = ref<File>(),
  cover = ref<File>(),
  error = ref(''),
  message = ref('');
const task = ref<{ id: string; instance_id: string }>();
const kinds = Object.keys(libraryTypeLabels) as LibraryKind[];
const filters = [
  { kind: 'all' as const, label: '全部' },
  ...kinds.map((kind) => ({ kind, label: libraryTypeLabels[kind] })),
];
const groups = computed(() => resource.data.value?.items || []);

async function refresh(signal?: AbortSignal) {
  if (!ready.value || busy.value) return;
  const scope = instances.scope();
  const params = new URLSearchParams({
    q: activeQuery.value,
    limit: '24',
    type: assetFilter.value,
  });
  if (position.value) params.set('cursor', position.value);
  if (instances.selectedId) params.set('instance_id', instances.selectedId);
  await resource.load(
    (requestSignal) => api.request('/api/media/groups?' + params, { scope, signal: requestSignal }),
    signal,
  );
}
function filter() {
  activeQuery.value = query.value;
  position.value = null;
  history.value = [];
  expanded.value = undefined;
  void refresh();
}
function page(next: boolean) {
  expanded.value = undefined;
  if (next && resource.data.value?.next_cursor) {
    history.value.push(position.value);
    position.value = resource.data.value.next_cursor;
  } else if (!next) {
    position.value = history.value.pop() || null;
  }
  void refresh();
}
function instanceChanged() {
  resource.clear();
  expanded.value = undefined;
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
    position.value = null;
    history.value = [];
    expanded.value = undefined;
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
        <p>按视频标题归集已下载内容，展开查看视频、音频和各语言字幕。</p>
      </div>
    </div>
    <InstancePicker purpose="view" description="查看所选实例的全部任务素材；导入与清空也仅作用于此实例。" :disabled="busy || !!dialog" @change="instanceChanged" />
    <p v-if="message" class="notice" role="status">{{ message }}</p>
    <p v-if="error && !dialog" class="form-error" role="alert">{{ error }}</p>
    <div class="summary-grid media-summary">
      <div class="card">
        <small>视频标题组</small><strong>{{ resource.data.value?.counts.all ?? '—' }}</strong
        ><small>同一视频的文件归为一组</small>
      </div>
      <div class="card">
        <small>文件总数</small><strong>{{ resource.data.value?.file_count ?? '—' }}</strong
        ><small>每个交付文件分别计数</small>
      </div>
      <div class="card">
        <small>字幕文件</small><strong>{{ resource.data.value?.subtitle_files ?? '—' }}</strong
        ><small>包含各语言和字幕来源</small>
      </div>
      <div class="card">
        <small>素材总大小</small
        ><strong>{{ resource.data.value ? formatFileSize(resource.data.value.size) : '—' }}</strong
        ><small>当前搜索范围内的全部文件</small>
      </div>
    </div>
    <section class="card library-card">
      <div class="section-heading">
        <div>
          <h2>已下载内容</h2>
          <p class="cell-note">一行一个视频，可同时包含多种类型；按标题排序。</p>
        </div>
        <div class="actions">
          <button
            :disabled="!editable || busy || !resource.data.value || !!resource.error.value"
            @click="
              dialog = 'import';
              error = '';
            "
          >
            导入视频
          </button>
          <button
            :disabled="!editable || busy || !resource.data.value || !!resource.error.value"
            @click="write('reindex')"
          >
            同步本地文件
          </button>
          <button
            class="danger"
            :disabled="!editable || busy || !resource.data.value || !!resource.error.value"
            @click="
              dialog = 'clear';
              error = '';
              confirmation = '';
            "
          >
            清空本地素材
          </button>
        </div>
      </div>
      <form class="toolbar" @submit.prevent="filter">
        <input
          v-model="query"
          aria-label="搜索素材"
          placeholder="标题、来源 ID 或文件名"
          maxlength="200"
        />
        <button :disabled="busy || resource.loading.value">查询</button>
      </form>
      <div class="asset-filters" aria-label="素材类型筛选">
        <button
          v-for="item in filters"
          :key="item.kind"
          type="button"
          :class="{ active: assetFilter === item.kind }"
          :aria-pressed="assetFilter === item.kind"
          :disabled="busy || resource.loading.value"
          @click="
            assetFilter = item.kind;
            filter();
          "
        >
          {{ item.label }} <span>{{ resource.data.value?.counts[item.kind] ?? '—' }}</span>
        </button>
      </div>
      <p class="cell-note filter-hint">
        标签数量为包含该类型的视频组数；展开后展示该视频的全部文件。
      </p>
      <p v-if="resource.error.value" class="form-error" role="alert">
        {{ resource.error.value }}
        <button @click="refresh()">重试</button>
      </p>
      <p v-if="resource.loading.value && !resource.data.value" role="status">正在读取素材…</p>
      <div class="table-scroll" id="asset-table">
        <table>
          <thead>
            <tr>
              <th>视频标题</th>
              <th>已下载类型</th>
              <th>文件 / 大小</th>
              <th>最近下载</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="group in groups" :key="group.group_key">
              <tr class="group-row" :class="{ 'is-expanded': expanded === group.group_key }">
                <td class="group-title-cell">
                  <div class="asset-title">
                    <span class="asset-mark">{{ group.file_count }}</span
                    ><strong>{{ group.title }}</strong>
                  </div>
                  <small class="cell-note source-id">{{ group.video_id || '本地导入素材' }}</small>
                </td>
                <td>
                  <div class="group-types">
                    <template v-for="kind in kinds" :key="kind">
                      <span v-if="group.kind_counts[kind]" class="type-chip" :data-kind="kind"
                        >{{ libraryTypeLabels[kind] }} <b>{{ group.kind_counts[kind] }}</b></span
                      >
                    </template>
                  </div>
                </td>
                <td>
                  <strong class="file-count">{{ group.file_count }} 个文件</strong
                  ><small class="cell-note">{{ formatFileSize(group.size) }}</small>
                </td>
                <td class="group-time">{{ formatDownloadTime(group.downloaded_at) }}</td>
                <td>
                  <button
                    class="expand-button"
                    :aria-expanded="expanded === group.group_key"
                    :aria-controls="'group-' + group.group_key"
                    @click="expanded = expanded === group.group_key ? undefined : group.group_key"
                  >
                    {{ expanded === group.group_key ? '收起文件' : '查看文件' }}
                    <span aria-hidden="true">{{ expanded === group.group_key ? '⌃' : '⌄' }}</span>
                  </button>
                </td>
              </tr>
              <tr v-if="expanded === group.group_key" class="expanded-row">
                <td colspan="5" :id="'group-' + group.group_key">
                  <MediaGroupFiles :key="group.group_key" :group="group" />
                </td>
              </tr>
            </template>
            <tr v-if="resource.data.value && !groups.length">
              <td colspan="5" class="empty">暂无符合条件的素材。</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <button
          id="media-prev"
          :disabled="!history.length || resource.loading.value || busy"
          @click="page(false)"
        >
          上一页
        </button>
        <span
          >第 {{ history.length + 1 }} 页 · 共 {{ resource.data.value?.total ?? 0 }} 组 · 本页
          {{ groups.length }} 组</span
        >
        <button
          id="media-next"
          :disabled="!resource.data.value?.next_cursor || resource.loading.value || busy"
          @click="page(true)"
        >
          下一页
        </button>
      </div>
    </section>
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
    >
      <form @submit.prevent="write(dialog!)">
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
              @change="file($event, 'cover')" /></label
        ></template>
        <template v-else
          ><p>
            将删除 {{ instances.name
            }}<template v-if="instances.selectedId">（{{ instances.selectedId }}）</template
            >的全部素材、该实例的 OSS
            交付文件、下载缓存和相关执行记录、统计，仅保留下载去重记录。此操作无法撤销。
          </p>
          <p class="cell-note">{{ resource.data.value?.directory }}</p>
          <label
            >输入“清空”确认<input
              v-model="confirmation"
              aria-label="确认清空"
              :disabled="busy"
              autocomplete="off" /></label
        ></template>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        <button type="submit" :disabled="busy">
          {{ busy ? '正在提交…' : dialog === 'import' ? '确认导入' : '确认清空' }}
        </button>
      </form>
    </WorkspaceDialog>
  </main>
</template>
<style scoped>
.filter-hint {
  margin: -6px 0 14px;
  font-size: 12px;
}
.group-title-cell {
  width: 37%;
  max-width: 440px;
}
.asset-title {
  align-items: flex-start;
}
.asset-title strong {
  font-size: 13px;
  line-height: 1.6;
}
.asset-mark {
  color: #0f766e;
  background: #ecfdf5;
  border: 1px solid #ccfbf1;
}
.source-id {
  margin-left: 43px;
  margin-top: 5px;
  font-size: 11px;
}
.group-types {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-width: 300px;
}
.type-chip {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  border: 1px solid #cbd5e1;
  padding: 4px 7px;
  border-radius: 5px;
  background: #f8fafc;
  color: #475569;
  font-size: 11px;
  white-space: nowrap;
}
.type-chip b {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.type-chip[data-kind='subtitle'] {
  background: #ecfdf5;
  border-color: #a7f3d0;
  color: #047857;
}
.type-chip[data-kind='av'],
.type-chip[data-kind='video_only'] {
  background: #eff6ff;
  border-color: #bfdbfe;
  color: #1d4ed8;
}
.type-chip[data-kind='audio_only'] {
  background: #fffbeb;
  border-color: #fde68a;
  color: #92400e;
}
.file-count {
  font-size: 12px;
  white-space: nowrap;
}
.group-time {
  color: #64748b;
  font-size: 12px;
}
.expand-button {
  white-space: nowrap;
  color: #0f766e;
  border-color: #a7d8cf;
  background: #fff;
  font-size: 12px;
}
.expand-button span {
  margin-left: 7px;
}
.group-row.is-expanded > td {
  background: #f0fdfa;
  border-bottom-color: #ccfbf1;
}
.expanded-row > td {
  background: #f8fafc;
  padding: 8px 16px;
}
</style>
