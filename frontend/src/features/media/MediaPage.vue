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
import ArtifactPreview from '../delivery/ArtifactPreview.vue';
import MediaPreview from './MediaPreview.vue';
import { mediaAddress, type MediaItem, type UnifiedMediaPage } from './contracts';
import { formatGB } from '../jobs/contracts';
import { outputLabels, type DeliveryArtifact, type OutputKind } from '../delivery/model';

type AssetFilter = 'all' | 'video' | 'audio' | 'subtitle';
type AssetKind = 'source_video' | OutputKind;
interface LibraryAsset {
  id: string;
  title: string;
  size: number;
  kind: AssetKind;
  kindLabel: string;
  origin: string;
  sourceId: string;
  local?: MediaItem;
  artifact?: DeliveryArtifact;
}

const instances = useInstances(),
  editable = bootstrap().role !== 'viewer';
const resource = useResource<UnifiedMediaPage>(),
  ready = ref(false),
  busy = ref(false),
  query = ref(''),
  assetFilter = ref<AssetFilter>('all');
const cursor = ref<string | null>(null),
  artifactCursor = ref<string | null>(null),
  history = ref<Array<{ media: string | null; artifacts: string | null }>>([]);
const preview = ref<MediaItem>(),
  artifactPreview = ref<DeliveryArtifact>(),
  dialog = ref<'import' | 'clear'>(),
  confirmation = ref('');
const video = ref<File>(),
  cover = ref<File>(),
  error = ref(''),
  message = ref('');
const task = ref<{ id: string; instance_id: string }>();
const typeLabels: Record<AssetKind, string> = {
  source_video: '视频素材',
  av: outputLabels.av,
  video_only: outputLabels.video_only,
  audio_only: outputLabels.audio_only,
  subtitle: outputLabels.subtitle,
};

const allAssets = computed<LibraryAsset[]>(() => {
  const local = (resource.data.value?.media.items || []).map((item) => ({
    id: 'media:' + item.id,
    title: item.title || item.name,
    size: item.size,
    kind: 'source_video' as const,
    kindLabel: typeLabels.source_video,
    origin: '本地素材目录',
    sourceId: item.video_id || item.name,
    local: item,
  }));
  const outputs = (resource.data.value?.artifacts.items || []).map((item) => ({
    id: 'artifact:' + item.id,
    title: item.title || item.video_id,
    size: item.size,
    kind: item.kind,
    kindLabel: typeLabels[item.kind],
    origin: '任务交付产物',
    sourceId: item.video_id,
    artifact: item,
  }));
  return [...local, ...outputs];
});
const assets = computed(() =>
  allAssets.value.filter((item) => matchesFilter(item.kind, assetFilter.value)),
);
const filterCounts = computed(() => ({
  all: allAssets.value.length,
  video: allAssets.value.filter((item) => matchesFilter(item.kind, 'video')).length,
  audio: allAssets.value.filter((item) => matchesFilter(item.kind, 'audio')).length,
  subtitle: allAssets.value.filter((item) => matchesFilter(item.kind, 'subtitle')).length,
}));
const loadedBytes = computed(() => assets.value.reduce((total, item) => total + item.size, 0));

function matchesFilter(kind: AssetKind, filter: AssetFilter) {
  if (filter === 'all') return true;
  if (filter === 'video') return ['source_video', 'video_only', 'av'].includes(kind);
  if (filter === 'audio') return ['audio_only', 'av'].includes(kind);
  return kind === 'subtitle';
}
async function refresh(signal?: AbortSignal) {
  if (!ready.value || busy.value) return;
  const scope = instances.scope();
  const params = new URLSearchParams({
    q: query.value,
    media_limit: '24',
    artifact_limit: '50',
  });
  if (cursor.value) params.set('media_cursor', cursor.value);
  if (artifactCursor.value) params.set('artifact_cursor', artifactCursor.value);
  if (instances.selectedId) params.set('instance_id', instances.selectedId);
  await resource.load(
    (requestSignal) => api.request('/api/media/assets?' + params, { scope, signal: requestSignal }),
    signal,
  );
}
function filter() {
  cursor.value = null;
  artifactCursor.value = null;
  history.value = [];
  void refresh();
}
function page(next: boolean) {
  if (
    next &&
    (resource.data.value?.media.next_cursor || resource.data.value?.artifacts.next_cursor)
  ) {
    history.value.push({ media: cursor.value, artifacts: artifactCursor.value });
    cursor.value = resource.data.value?.media.next_cursor || null;
    artifactCursor.value = resource.data.value?.artifacts.next_cursor || null;
  } else if (!next) {
    const previous = history.value.pop();
    cursor.value = previous?.media || null;
    artifactCursor.value = previous?.artifacts || null;
  }
  void refresh();
}
function instanceChanged() {
  resource.clear();
  preview.value = undefined;
  artifactPreview.value = undefined;
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
    artifactCursor.value = null;
    history.value = [];
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : String(failure);
  } finally {
    busy.value = false;
    await refresh();
  }
}
function selectAsset(asset: LibraryAsset) {
  if (asset.local) {
    preview.value = {
      ...asset.local,
      instance_id: instances.selectedId || asset.local.instance_id,
    };
  } else if (asset.artifact) artifactPreview.value = asset.artifact;
}
function assetUrl(asset: LibraryAsset) {
  return asset.local
    ? mediaAddress('videos', asset.local.name, instances.selectedId || asset.local.instance_id)
    : '/api/delivery/files/' + encodeURIComponent(asset.artifact!.id) + '?download=1';
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
        <p>视频、音频、字幕和音视频成品统一查看、预览与下载。</p>
      </div>
      <InstancePicker :disabled="busy || !!dialog" @change="instanceChanged" />
    </div>
    <p v-if="message" class="notice" role="status">{{ message }}</p>
    <p v-if="error && !dialog" class="form-error" role="alert">{{ error }}</p>
    <div class="summary-grid media-summary">
      <div class="card">
        <small>本地视频</small><strong>{{ resource.data.value?.media.total ?? '—' }}</strong
        ><small>当前实例已索引</small>
      </div>
      <div class="card">
        <small>当前页资产</small><strong>{{ assets.length }}</strong
        ><small>视频、音频与字幕</small>
      </div>
      <div class="card">
        <small>当前页大小</small><strong>{{ formatGB(loadedBytes) }}</strong
        ><small>按文件类型合计</small>
      </div>
      <div class="card">
        <small>交付产物</small
        ><strong>{{ resource.data.value?.artifacts.items.length ?? '—' }}</strong
        ><small>已验证可用文件</small>
      </div>
    </div>
    <section class="card library-card">
      <div class="section-heading">
        <div>
          <h2>全部资产</h2>
          <p class="cell-note">交付产物与本地素材共用一个清单，按类型快速定位。</p>
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
      <div class="asset-filters" aria-label="资产类型">
        <button
          v-for="item in [
            ['all', '全部'],
            ['video', '视频'],
            ['audio', '音频'],
            ['subtitle', '字幕'],
          ] as const"
          :key="item[0]"
          type="button"
          :class="{ active: assetFilter === item[0] }"
          @click="assetFilter = item[0]"
        >
          {{ item[1] }} <span>{{ filterCounts[item[0]] }}</span>
        </button>
      </div>
      <p v-if="resource.error.value" class="form-error" role="alert">
        {{ resource.error.value }}
        <button @click="refresh()">重试</button>
      </p>
      <p v-if="resource.loading.value && !resource.data.value" role="status">正在读取素材…</p>
      <div class="table-scroll" id="asset-table">
        <table>
          <thead>
            <tr>
              <th>资产</th>
              <th>类型</th>
              <th>来源</th>
              <th>大小</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="asset in assets" :key="asset.id">
              <td>
                <div class="asset-title">
                  <span class="asset-mark">{{ asset.kindLabel.slice(0, 2) }}</span
                  ><strong>{{ asset.title }}</strong>
                </div>
                <small class="cell-note">{{ asset.sourceId }}</small>
              </td>
              <td>
                <span class="badge">{{ asset.kindLabel }}</span>
              </td>
              <td>
                {{ asset.origin
                }}<small v-if="asset.artifact" class="cell-note"
                  >{{ asset.artifact.storage }} · {{ asset.artifact.instance_id }}</small
                ><small v-else class="cell-note">{{ instances.name }}</small>
              </td>
              <td>
                {{ formatGB(asset.size)
                }}<small class="cell-note">{{ asset.size.toLocaleString() }} 字节</small>
              </td>
              <td>
                <div class="actions">
                  <button :data-preview="asset.id" @click="selectAsset(asset)">
                    {{
                      asset.kind === 'audio_only'
                        ? '试听'
                        : asset.kind === 'subtitle'
                          ? '查看'
                          : '预览'
                    }}</button
                  ><a class="text-link" :href="assetUrl(asset)">下载</a>
                </div>
              </td>
            </tr>
            <tr v-if="resource.data.value && !assets.length">
              <td colspan="5" class="empty">暂无符合条件的资产。</td>
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
        <span>第 {{ history.length + 1 }} 页</span>
        <button
          id="media-next"
          :disabled="
            (!resource.data.value?.media.next_cursor &&
              !resource.data.value?.artifacts.next_cursor) ||
            resource.loading.value ||
            busy
          "
          @click="page(true)"
        >
          下一页
        </button>
      </div>
    </section>
    <MediaPreview v-if="preview" :item="preview" @close="preview = undefined" />
    <ArtifactPreview
      v-if="artifactPreview"
      :key="artifactPreview.id"
      :artifact="artifactPreview"
      @close="artifactPreview = undefined"
    />
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
            >素材目录中的文件，无法撤销。
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
