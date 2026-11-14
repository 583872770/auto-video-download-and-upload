<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useLeaveGuard } from '../../composables/leaveGuard';
import { ElButton, ElMessageBox } from 'element-plus';
import 'element-plus/es/components/button/style/css';
import 'element-plus/es/components/message-box/style/css';
import { bootstrap } from '../../bootstrap';
import { useInstances } from '../../stores/instances';
import { useSettings } from '../../stores/settings';
import { allFields, type Section } from './fields';
import { validate } from './model';
import StorageSettings from './StorageSettings.vue';
import DownloadSettings from './DownloadSettings.vue';
import PerformanceSettings from './PerformanceSettings.vue';
import MaintenanceSettings from './MaintenanceSettings.vue';
import DiagnosticsSettings from './DiagnosticsSettings.vue';
import DeliveryDefaults from './DeliveryDefaults.vue';
const instances = useInstances(),
  settings = useSettings();
const boot = bootstrap(),
  admin = boot.role === 'admin';
const sections: Array<[Section, string, string]> = [
  ['storage', '文件保存', '本地目录与阿里云 OSS'],
  ['download', '下载策略', '下载方式、画质与重试'],
  ['performance', '并发与渠道', '资源预算与渠道调度'],
  ['maintenance', '空间与日志', '空间水位、清理与日志'],
  ['diagnostics', '运行诊断', '工具状态与风险排查'],
  ['delivery', '新任务交付默认', '新任务的全局默认要求'],
];
const section = ref<Section>('storage'),
  switching = ref(false);
const route = useRoute(),
  router = useRouter();
const delivery = ref<InstanceType<typeof DeliveryDefaults>>();
const busy = computed(
  () => settings.loading || settings.saving || instances.loading || switching.value,
);
const anyDirty = computed(() => settings.dirty || !!delivery.value?.dirty);
function readHash() {
  const key = route.hash.slice(1);
  section.value = sections.some(([id]) => id === key) ? (key as Section) : 'storage';
}
function show(key: Section) {
  section.value = key;
  void router.replace({ path: '/settings', query: route.query, hash: '#' + key });
}
async function confirmDiscard(message: string) {
  try {
    await ElMessageBox.confirm(message, '有未保存的修改', {
      confirmButtonText: '放弃并继续',
      cancelButtonText: '继续编辑',
      type: 'warning',
    });
    return true;
  } catch {
    return false;
  }
}
async function changeInstance(event: Event) {
  const input = event.target as HTMLSelectElement,
    id = input.value;
  input.value = instances.selectedId;
  if (id === instances.selectedId || busy.value) return;
  switching.value = true;
  try {
    if (
      settings.dirty &&
      !(await confirmDiscard('切换实例会放弃当前实例未保存的系统设置。全局交付默认草稿会保留。'))
    )
      return;
    settings.reset();
    instances.select(id);
    await settings.load(true);
  } finally {
    switching.value = false;
  }
}
async function save() {
  if (!admin || busy.value) return;
  const issues = validate(settings.draft);
  settings.issues = Object.fromEntries(issues.map((item) => [item.key, item.message]));
  if (issues.length) {
    const key = issues[0]!.key;
    show(
      allFields.find((field) => field.key === key)?.section ||
        (key === 'download_methods'
          ? 'download'
          : key === 'download_channels'
            ? 'performance'
            : 'storage'),
    );
    await nextTick();
    const element = document.querySelector<HTMLElement>(`[name="${key}"]`);
    let parent = element?.parentElement;
    while (parent) {
      if (parent instanceof HTMLDetailsElement) parent.open = true;
      parent = parent.parentElement;
    }
    element?.focus();
    element?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    return;
  }
  await settings.save();
}
async function discard() {
  if (await confirmDiscard('放弃当前实例所有分类的未保存修改？')) settings.reset();
}
async function initialize() {
  await instances.load();
  if (!instances.error) await settings.load(true);
}
async function reload() {
  if (settings.dirty && !(await confirmDiscard('重新读取会放弃当前实例所有分类的未保存修改。')))
    return;
  await settings.load(true);
}
useLeaveGuard(() => ({ dirty: anyDirty.value, busy: busy.value || !!delivery.value?.saving }));
watch(() => route.hash, readHash);
onMounted(() => {
  readHash();
  void initialize();
});
</script>
<template>
  <main class="content" :data-ready="settings.loaded">
    <div class="page-heading">
      <div>
        <span class="eyebrow">WORKSPACE SETTINGS</span>
        <h1>系统设置</h1>
        <p>按用途管理配置，所有分类的修改统一保存到当前实例。</p>
      </div>
      <label v-if="boot.fleetEnabled" class="instance-picker"
        >当前实例<select
          aria-label="当前实例"
          :value="instances.selectedId"
          :disabled="busy || !instances.items.length"
          @change="changeInstance"
        >
          <option v-for="item in instances.items" :key="item.id" :value="item.id">
            {{ item.name }}{{ item.online ? '' : '（离线）' }}
          </option>
        </select></label
      ><span v-else class="badge">当前实例</span>
    </div>
    <p v-if="!admin" class="notice">当前角色只有查看权限，修改设置需要管理员登录。</p>
    <p v-if="instances.current && !instances.current.online" class="notice">
      当前实例离线，读取和保存可能失败。请在下载实例中检查连接。
    </p>
    <div v-if="instances.error" class="notice error" role="alert">
      {{ instances.error }} <button type="button" @click="initialize">重试连接</button>
    </div>
    <div class="settings-layout">
      <nav class="settings-nav" aria-label="设置分类">
        <button
          v-for="[id, label, help] in sections"
          :key="id"
          type="button"
          :aria-current="section === id ? 'page' : undefined"
          :class="{ active: section === id }"
          @click="show(id)"
        >
          <strong>{{ label }}</strong
          ><small>{{ help }}</small>
        </button>
      </nav>
      <div class="settings-body">
        <p v-if="settings.loading" role="status" class="notice">
          正在读取「{{ instances.name }}」的设置…
        </p>
        <div v-if="settings.error" class="notice error" role="alert">
          {{ settings.error }}
          <button type="button" :disabled="busy" @click="reload">重新读取</button>
        </div>
        <p v-if="settings.message" class="notice success" role="status">
          {{ settings.message }}
        </p>
        <DeliveryDefaults ref="delivery" v-show="section === 'delivery'" />
        <template v-if="settings.loaded && !instances.error">
          <DiagnosticsSettings v-if="section === 'diagnostics'" :key="instances.epoch" />
          <form v-else-if="section !== 'delivery'" novalidate @submit.prevent="save">
            <fieldset :disabled="busy || (!admin && section !== 'maintenance')">
              <StorageSettings v-if="section === 'storage'" /><DownloadSettings
                v-if="section === 'download'"
              /><PerformanceSettings v-if="section === 'performance'" /><MaintenanceSettings
                v-if="section === 'maintenance'"
                :key="instances.epoch"
              />
            </fieldset>
          </form>
        </template>
      </div>
    </div>
  </main>
  <footer v-if="section !== 'delivery' && section !== 'diagnostics'" class="savebar">
    <div>
      <strong>{{
        settings.dirty ? `有 ${settings.changedCount} 项未保存修改` : '修改将在此统一保存'
      }}</strong
      ><small>作用实例：{{ instances.name }} · 包含所有设置分类</small>
    </div>
    <div class="actions">
      <ElButton :disabled="!settings.dirty || busy || !admin" @click="discard">放弃修改</ElButton
      ><ElButton
        type="primary"
        :loading="settings.saving"
        :disabled="!settings.loaded || !settings.dirty || busy || !admin"
        @click="save"
        >保存设置</ElButton
      >
    </div>
  </footer>
</template>
