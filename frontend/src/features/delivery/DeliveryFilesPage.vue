<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { RouterLink, useRoute } from 'vue-router';
import { api } from '../../api';
import { useResource } from '../../composables/resource';
import { formatGB, type Page } from '../jobs/contracts';
import { outputLabels, type DeliveryArtifact } from './model';
import ArtifactPreview from './ArtifactPreview.vue';
const route = useRoute(),
  query = ref(''),
  kind = ref(''),
  language = ref('');
const jobId = computed(() => (typeof route.query.job_id === 'string' ? route.query.job_id : ''));
const cursor = ref<string | null>(null),
  history = ref<Array<string | null>>([]),
  preview = ref<DeliveryArtifact>();
const resource = useResource<Page<DeliveryArtifact>>();
async function refresh() {
  const params = new URLSearchParams({
    q: query.value,
    kind: kind.value,
    language: language.value,
  });
  if (cursor.value) params.set('cursor', cursor.value);
  const path = jobId.value
    ? '/api/jobs/' + encodeURIComponent(jobId.value) + '/artifacts'
    : '/api/delivery/artifacts';
  await resource.load((signal) => api.request(path + '?' + params, { signal }));
}
function filter() {
  cursor.value = null;
  history.value = [];
  void refresh();
}
watch(
  jobId,
  () => {
    resource.clear();
    filter();
  },
  { immediate: true },
);
function page(next: boolean) {
  if (next && resource.data.value?.next_cursor) {
    history.value.push(cursor.value);
    cursor.value = resource.data.value.next_cursor;
  } else if (!next) cursor.value = history.value.pop() || null;
  void refresh();
}
</script>
<template>
  <main class="content">
    <div class="page-heading">
      <div>
        <span class="eyebrow">DELIVERY FILES</span>
        <h1>交付文件</h1>
        <p>核对字幕、无声视频、纯音频和音视频成品。</p>
      </div>
      <RouterLink class="text-link" to="/media">历史视频素材 ↗</RouterLink>
    </div>
    <p v-if="jobId" class="notice">
      仅显示任务 {{ jobId }} 的文件。<RouterLink class="text-link" to="/delivery-files"
        >查看全部文件</RouterLink
      >
      · <RouterLink class="text-link" :to="'/jobs/' + jobId">返回任务</RouterLink>
    </p>
    <section class="card">
      <form id="delivery-filter" class="toolbar" @submit.prevent="filter">
        <input
          v-model="query"
          aria-label="来源 ID 或标题"
          placeholder="来源 ID 或标题"
          maxlength="200"
        /><select v-model="kind" aria-label="文件类型">
          <option value="">全部类型</option>
          <option v-for="(label, key) in outputLabels" :key="key" :value="key">
            {{ label }}
          </option></select
        ><input
          v-model="language"
          aria-label="字幕语言"
          placeholder="字幕语言，如 en"
          maxlength="31"
        /><button type="submit">查询</button>
      </form>
      <p v-if="resource.error.value" id="delivery-error" role="alert" class="form-error">
        {{ resource.error.value }} <button @click="refresh">重试</button>
      </p>
      <p v-if="resource.loading.value && !resource.data.value" role="status">正在读取交付文件…</p>
      <div class="table-scroll" id="delivery-files">
        <table>
          <thead>
            <tr>
              <th>来源 / 标题</th>
              <th>类型 / 语言</th>
              <th>大小</th>
              <th>位置</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="file in resource.data.value?.items" :key="file.id">
              <td>
                {{ file.title }}<small class="cell-note">{{ file.video_id }}</small>
              </td>
              <td>
                {{ outputLabels[file.kind]
                }}<span v-if="file.language"> · {{ file.language }}</span>
              </td>
              <td>
                {{ formatGB(file.size) }}<small class="cell-note">{{ file.size }} 字节</small>
              </td>
              <td>
                {{ file.storage }} · {{ file.instance_id
                }}<small class="cell-note">{{ file.available ? '已登记' : '已清理' }}</small>
              </td>
              <td>
                <div class="actions">
                  <button :data-preview="file.id" @click="preview = file">
                    {{ file.kind === 'audio_only' ? '试听' : '预览' }}</button
                  ><a
                    class="text-link"
                    :href="'/api/delivery/files/' + encodeURIComponent(file.id) + '?download=1'"
                    >下载</a
                  >
                </div>
              </td>
            </tr>
            <tr v-if="resource.data.value && !resource.data.value.items.length">
              <td colspan="5" class="empty">尚无匹配的正式交付文件。</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <button
          id="delivery-prev"
          :disabled="!history.length || resource.loading.value"
          @click="page(false)"
        >
          上一页</button
        ><span>第 {{ history.length + 1 }} 页</span
        ><button
          id="delivery-next"
          :disabled="!resource.data.value?.next_cursor || resource.loading.value"
          @click="page(true)"
        >
          下一页
        </button>
      </div>
      <p>测试文件结束后删除，不出现在正式交付清单中。文件是否可读取以实际存储状态为准。</p>
    </section>
    <ArtifactPreview
      v-if="preview"
      :key="preview.id"
      :artifact="preview"
      @close="preview = undefined"
    />
  </main>
</template>
