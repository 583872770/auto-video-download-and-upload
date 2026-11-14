<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { useRoute } from 'vue-router';
import { api } from '../../api';
import InstancePicker from '../../components/InstancePicker.vue';
import AppIcon from '../../components/AppIcon.vue';
import { useInstances } from '../../stores/instances';
import { useResource } from '../../composables/resource';
import { usePolling } from '../../composables/polling';
import { useOperation } from '../../composables/operation';
import AccountDetail from './AccountDetail.vue';
import AccountImport from './AccountImport.vue';
import AccountPool from './AccountPool.vue';
import { base, authLabels, downloadLabels, type Overview } from './contracts';
const route = useRoute(),
  instances = useInstances(),
  resource = useResource<Overview>(),
  op = useOperation();
const filter = reactive({ q: '', instance: '', auth: '', download: '', linked: '', pooled: '' }),
  page = ref(1),
  tab = ref(route.query.tab === 'pool' ? 'pool' : 'library'),
  selected = ref<string | null>(null),
  importing = ref(false),
  ready = ref(false),
  pending = ref(false),
  admin = ref(false),
  capabilities = ref<{ encryption_ready: boolean; message: string }>();
async function refresh(signal?: AbortSignal) {
  await resource.load(
    (s) =>
      api.request(
        base +
          '/overview?' +
          new URLSearchParams({ ...filter, page: String(page.value), size: '50' }),
        { signal: s },
      ),
    signal,
  );
}
function search() {
  page.value = 1;
  void refresh();
}
function change() {
  ready.value = true;
}
async function saved() {
  await refresh();
}
onMounted(() =>
  op.run(async () => {
    const [c, s] = await Promise.all([
      api.request<{ encryption_ready: boolean; message: string }>(base + '/capabilities'),
      api.request<{ user: { role: string } | null }>('/api/auth/status'),
    ]);
    capabilities.value = c;
    admin.value = s.user?.role === 'admin';
  }),
);
usePolling(
  computed(() => selected.value === null && !importing.value),
  refresh,
  30000,
);
</script>
<template>
  <main class="content">
    <div class="page-heading">
      <div>
        <span class="eyebrow">ACCOUNTS & COOKIES</span>
        <h1>账号管理</h1>
        <p>全局账号清单；Cookie 池与任务节奏按实例独立。</p>
      </div>
      <div class="actions">
        <button @click="refresh()">刷新账号</button>
        <button :disabled="!admin || !capabilities?.encryption_ready" @click="importing = true">
          导入账号
        </button>
        <button
          class="primary"
          :disabled="!admin || !capabilities?.encryption_ready"
          @click="selected = ''"
        >
          <AppIcon name="plus" />新增账号
        </button>
      </div>
    </div>
    <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }}</p>
    <p v-if="capabilities && !capabilities.encryption_ready" class="notice">
      {{ capabilities.message }}
    </p>
    <p v-if="!admin" class="muted">新增、导入、读取密钥与登录需要管理员登录。</p>
    <div class="resource-context">
      <div class="tabbar">
        <button
          :class="{ active: tab === 'library' }"
          :aria-pressed="tab === 'library'"
          @click="tab = 'library'"
        >
          账号清单</button
        ><button
          :class="{ active: tab === 'pool' }"
          :aria-pressed="tab === 'pool'"
          @click="tab = 'pool'"
        >
          实例账号池与节奏
        </button>
      </div>
      <InstancePicker :disabled="pending" @change="change" />
    </div>
    <section v-show="tab === 'library'" class="card">
      <form class="toolbar" @submit.prevent="search">
        <input
          v-model="filter.q"
          aria-label="搜索账号"
          type="search"
          placeholder="邮箱、备注或 Cookie 名称"
        /><select v-model="filter.instance" aria-label="按绑定实例筛选" @change="search">
          <option value="">全部实例</option>
          <option value="unbound">尚未绑定</option>
          <option v-for="node in resource.data.value?.instances" :key="node.id" :value="node.id">
            {{ node.name }} · {{ node.id }}
          </option></select
        ><select v-model="filter.auth" aria-label="登录状态筛选" @change="search">
          <option value="">全部登录状态</option>
          <option v-for="(label, key) in authLabels" :key="key" :value="key">
            {{ label }}
          </option></select
        ><select v-model="filter.download" aria-label="下载状态筛选" @change="search">
          <option value="">全部下载状态</option>
          <option v-for="(label, key) in downloadLabels" :key="key" :value="key">
            {{ label }}
          </option></select
        ><select v-model="filter.linked" aria-label="资料关联筛选" @change="search">
          <option value="">全部关联状态</option>
          <option value="missing">尚未登录</option></select
        ><select v-model="filter.pooled" aria-label="账号池筛选" @change="search">
          <option value="">全部账号池状态</option>
          <option value="1">已加入账号池</option>
          <option value="0">未加入账号池</option></select
        ><button>查询</button>
      </form>
      <p v-if="resource.error.value" class="form-error" role="alert">{{ resource.error.value }}</p>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>账号</th>
              <th>所属实例</th>
              <th>登录状态</th>
              <th>下载状态</th>
              <th>账号池</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in resource.data.value?.items" :key="item.id">
              <td>
                {{ item.email }}<small class="cell-note">{{ item.note }}</small>
              </td>
              <td>
                {{ item.instance_name || '登录时绑定'
                }}<small class="cell-note"
                  >{{ item.instance_id
                  }}{{ item.instance_id ? (item.online ? ' · 在线' : ' · 离线') : '' }}</small
                >
              </td>
              <td>{{ authLabels[item.auth_state] || item.auth_state }}</td>
              <td>{{ downloadLabels[item.download_state] || item.download_state }}</td>
              <td>{{ item.cookies.some((c) => c.pooled) ? '已加入' : '未加入' }}</td>
              <td>
                <button @click="selected = item.id">
                  {{ admin ? '详情 / 编辑 / 登录' : '查看详情' }}
                </button>
              </td>
            </tr>
            <tr v-if="resource.data.value && !resource.data.value.items.length">
              <td colspan="6" class="empty">暂无符合条件的账号。</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <span>共 {{ resource.data.value?.total || 0 }} 项 · 第 {{ page }} 页</span
        ><button
          :disabled="page <= 1 || resource.loading.value"
          @click="
            page--;
            refresh();
          "
        >
          上一页</button
        ><button
          :disabled="
            !resource.data.value || page * 50 >= resource.data.value.total || resource.loading.value
          "
          @click="
            page++;
            refresh();
          "
        >
          下一页
        </button>
      </div>
    </section>
    <AccountPool
      v-if="ready"
      v-show="tab === 'pool'"
      :key="'pool:' + instances.epoch"
      :admin="admin"
      @pending="pending = $event"
      @saved="saved"
    />
    <AccountDetail
      v-if="selected !== null"
      :key="selected"
      :id="selected || undefined"
      :nodes="resource.data.value?.instances || []"
      :admin="admin"
      :blocked="pending"
      @close="selected = null"
      @saved="saved"
    /><AccountImport v-if="importing" @close="importing = false" @saved="saved" />
  </main>
</template>
