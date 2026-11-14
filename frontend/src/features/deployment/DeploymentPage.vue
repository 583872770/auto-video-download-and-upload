<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { api } from '../../api';
import { ApiError } from '../../api/client';
import InstancePicker from '../../components/InstancePicker.vue';
import { useInstances } from '../../stores/instances';
import { useOperation } from '../../composables/operation';
import { confirmNavigation, useLeaveGuard } from '../../composables/leaveGuard';
import { usePolling } from '../../composables/polling';
const props = defineProps<{ setup?: boolean }>();
interface Configuration {
  config: Record<string, string | number | boolean>;
  instance_id?: string;
  revision?: string;
  redis_address?: string;
  mysql_password_configured?: boolean;
  pending_restart: boolean;
  running: boolean;
  starting?: boolean;
  stage?: string;
  stage_elapsed?: number;
  database_configured?: boolean;
  error?: string;
  cluster_state?: { instance_id: string };
  diagnostic?: {
    summary: string;
    remedy: string;
    detail: string;
    stage: string;
    type: string;
    location?: string[];
  };
}
const instances = useInstances(),
  op = useOperation(),
  current = ref<Configuration>(),
  baseline = ref(''),
  code = ref('');
const form = reactive<Record<string, string | number | boolean>>({});
const dirty = computed(() => !!current.value && JSON.stringify(form) !== baseline.value);
useLeaveGuard(() => ({ dirty: dirty.value, busy: op.busy.value }));
const fields = [
  ['instance_name', '实例显示名称'],
  ['instance_id', '实例 ID'],
  ['instance_url', '其他实例可访问的地址'],
  ['web_hosts', '允许访问的域名 / IP'],
];
const mysql = [
  ['mysql_host', '数据库地址'],
  ['mysql_port', '端口'],
  ['mysql_database', '数据库名称'],
  ['mysql_user', '用户名'],
  ['mysql_password', '新密码'],
];
async function read(fill = true) {
  const scope = props.setup ? undefined : instances.scope();
  const value = await api.request<Configuration>(
    props.setup ? '/api/deployment' : '/api/instance-deployment',
    { scope },
  );
  if (!props.setup && value.instance_id && value.instance_id !== scope?.id)
    throw new Error('部署配置返回了其他实例，请检查实例地址。');
  current.value = value;
  if (fill) {
    for (const key of Object.keys(form)) delete form[key];
    Object.assign(form, value.config);
    if (props.setup) {
      const hosts = new Set(
        String(form.web_hosts || '')
          .split(',')
          .filter(Boolean),
      );
      hosts.add(location.hostname);
      form.web_hosts = [...hosts].join(',');
    }
    baseline.value = JSON.stringify(form);
  }
}
async function reload() {
  if (dirty.value && !(await confirmNavigation())) return;
  await op.run(() => read());
}
async function changed() {
  current.value = undefined;
  await op.run(() => read());
}
async function unlock() {
  await op.run(async () => {
    await api.request('/api/deployment/unlock', { method: 'POST', body: { code: code.value } });
    code.value = '';
    await read();
  });
}
async function submit(test: boolean) {
  await op.run(async () => {
    const body = props.setup
      ? { role: 'hybrid', ...Object.fromEntries(fields.map(([key]) => [key!, form[key!] || ''])) }
      : { ...form, mysql_port: Number(form.mysql_port), revision: current.value?.revision };
    const path = props.setup
      ? '/api/deployment/' + (test ? 'test' : 'save')
      : '/api/instance-deployment' + (test ? '/test' : '');
    try {
      const result = await api.request<{ message: string }>(path, {
        method: props.setup || test ? 'POST' : 'PUT',
        scope: props.setup ? undefined : instances.scope(),
        body,
      });
      if (!test) await read();
      op.message.value = result.message;
    } catch (error) {
      if (props.setup) await read(false).catch(() => {});
      throw error;
    }
  });
}
usePolling(
  computed(() => !!props.setup && !!current.value?.starting && !op.busy.value),
  async () => {
    await read(false);
  },
  1500,
);
onMounted(async () => {
  if (props.setup) {
    try {
      await read();
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 403)) op.error.value = String(error);
    }
  }
});
</script>
<template>
  <main class="content" :class="{ 'access-page': setup }">
    <div class="page-heading">
      <div>
        <span class="eyebrow">DEPLOYMENT</span>
        <h1>{{ setup ? '本机部署与恢复' : '实例部署设置' }}</h1>
        <p>
          {{
            setup
              ? '使用本机独立部署访问码；数据库不可用时也可恢复实例设置。'
              : '配置保存到所选实例，重启该实例服务后生效。'
          }}
        </p>
      </div>
      <a v-if="setup && current?.running" href="/">进入工作区</a>
    </div>
    <InstancePicker v-if="!setup" :disabled="dirty || op.busy.value" @change="changed" />
    <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }} 输入已保留。</p>
    <p v-if="op.message.value" class="notice" role="status">{{ op.message.value }}</p>
    <form v-if="setup && !current" class="card form-grid" @submit.prevent="unlock">
      <h2>打开部署设置</h2>
      <p>访问码位于本机启动日志中，每台机器独立。</p>
      <label
        >部署设置访问码<input v-model="code" type="password" autocomplete="off" required /></label
      ><button :disabled="op.busy.value">进入配置</button>
    </form>
    <button v-if="!setup" :disabled="op.busy.value" @click="reload">
      {{ dirty ? '放弃修改并重新读取' : '重新读取' }}
    </button>
    <template v-if="current">
      <p v-if="current.pending_restart" class="notice">配置已保存，等待重启本机服务生效。</p>
      <p v-if="current.starting" class="notice" role="status">
        {{ current.stage }} · 当前阶段 {{ Math.floor(current.stage_elapsed || 0) }} 秒
      </p>
      <section v-if="current.diagnostic" class="card notice error">
        <h2>{{ current.diagnostic.summary }}</h2>
        <p>{{ current.diagnostic.remedy }}</p>
        <details>
          <summary>技术详情</summary>
          <pre
            >{{ current.diagnostic.stage }} · {{ current.diagnostic.type }}\n{{
              current.diagnostic.detail
            }}\n{{ current.diagnostic.location?.join('\n') }}</pre
          >
        </details>
      </section>
      <p v-else-if="current.error" class="notice error">{{ current.error }}</p>
      <form class="card" @submit.prevent="submit(false)">
        <fieldset :disabled="op.busy.value || current.starting">
          <legend>实例与访问</legend>
          <p v-if="current.cluster_state">
            识别到原实例 {{ current.cluster_state.instance_id }}。<button
              type="button"
              @click="form.instance_id = current.cluster_state!.instance_id"
            >
              恢复原实例 ID
            </button>
          </p>
          <div class="form-grid">
            <label v-for="[key, label] in fields" :key="key"
              >{{ label
              }}<input
                v-model="form[key!]"
                :type="key === 'instance_url' ? 'url' : 'text'"
                :required="key === 'web_hosts' || (!setup && key !== 'instance_url')"
                :maxlength="key === 'instance_id' ? 64 : key === 'instance_name' ? 100 : 4096"
                :pattern="key === 'instance_id' ? '[A-Za-z0-9_-]{1,64}' : undefined"
            /></label>
          </div>
          <p class="muted">
            实例 ID 对应任务和 Cookie 归属，请保持稳定。允许访问的域名 / IP
            用逗号分隔，不带协议和端口。
          </p>
        </fieldset>
        <fieldset v-if="!setup" :disabled="op.busy.value">
          <legend>共享 MySQL</legend>
          <div class="form-grid">
            <label v-for="[key, label] in mysql" :key="key"
              >{{ label
              }}<input
                v-model="form[key!]"
                :type="
                  key === 'mysql_port' ? 'number' : key === 'mysql_password' ? 'password' : 'text'
                "
                :required="key !== 'mysql_password'"
                :min="key === 'mysql_port' ? 1 : undefined"
                :max="key === 'mysql_port' ? 65535 : undefined"
                autocomplete="off"
                :placeholder="
                  key === 'mysql_password'
                    ? current.mysql_password_configured
                      ? '已设置，留空保留'
                      : '尚未设置'
                    : undefined
                "
            /></label>
          </div>
        </fieldset>
        <fieldset v-if="!setup" :disabled="op.busy.value">
          <legend>共享 Redis</legend>
          <label><input v-model="form.redis_enabled" type="checkbox" />启用 Redis 进度缓存</label
          ><label
            >新 Redis 连接地址<input
              v-model="form.redis_url"
              type="password"
              autocomplete="new-password"
              placeholder="redis://…；留空保留已有连接"
          /></label>
          <p>已保存：{{ current.redis_address || '未配置' }}（认证信息隐藏）</p>
        </fieldset>
        <p v-if="setup" class="notice">
          {{
            current.database_configured
              ? '已读取数据库启动配置，可测试连接。'
              : '数据库配置不完整，请补齐启动配置后重启。'
          }}
          数据库连接由 .env 或启动环境的 MYSQL_* 参数提供。
        </p>
        <p class="muted">
          页面保存实例配置到状态目录。宿主机端口、卷映射和服务重启由部署工具管理。
        </p>
        <div class="actions">
          <button type="button" :disabled="op.busy.value || current.starting" @click="submit(true)">
            测试连接</button
          ><button class="primary" :disabled="op.busy.value || current.starting">
            {{ setup && !current.running ? '保存并启动服务' : '保存部署配置' }}
          </button>
        </div>
      </form>
    </template>
    <p v-if="!setup" class="muted">
      服务无法启动时，访问目标服务器的 /setup，使用该服务器的部署访问码恢复。
    </p>
  </main>
</template>
