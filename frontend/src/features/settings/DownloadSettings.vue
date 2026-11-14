<script setup lang="ts">
import { useSettings } from '../../stores/settings';
import type { Provider } from '../../generated/config';
import { groups, qualityFields } from './fields';
import FieldGroup from './FieldGroup.vue';
import SettingField from './SettingField.vue';
const store = useSettings();
const providers: Array<[Provider, string]> = [
  ['yt_dlp', 'yt-dlp'],
  ['yt5s', 'yt5s'],
  ['downmp3', 'DownMP3'],
  ['yt1d', 'yt1d'],
  ['bergung', 'Bergung'],
];
function toggle(provider: Provider, checked: boolean) {
  store.setProviders(
    checked ? [...store.providers, provider] : store.providers.filter((item) => item !== provider),
  );
}
function move(index: number) {
  const list = [...store.providers];
  [list[index - 1], list[index]] = [list[index]!, list[index - 1]!];
  store.setProviders(list);
}
</script>
<template>
  <section class="card">
    <h2>下载方式</h2>
    <p v-if="store.draft.youtube_cookie_pool.length">
      账号池已启用，下载方式仅支持 yt-dlp。账号及登录状态请在账号管理中维护。
    </p>
    <p v-else>启用需要使用的下载方式。按顺序尝试策略使用下方顺序，自动策略会根据运行状况择优。</p>
    <div class="choices">
      <label v-for="[key, label] in providers" :key="key" class="choice"
        ><input
          type="checkbox"
          name="download_methods"
          :value="key"
          :checked="store.providers.includes(key)"
          :disabled="!!store.draft.youtube_cookie_pool.length && key !== 'yt_dlp'"
          @change="toggle(key, ($event.target as HTMLInputElement).checked)"
        />{{ label }}</label
      >
    </div>
    <p v-if="store.issues.download_methods" class="error">{{ store.issues.download_methods }}</p>
    <ol class="provider-order">
      <li v-for="(key, index) in store.providers" :key="key">
        {{ key }} <button v-if="index" type="button" @click="move(index)">上移</button>
      </li>
    </ol>
  </section>
  <FieldGroup title="失败重试" :fields="groups.retry" />
  <template v-for="provider in store.providers" :key="provider"
    ><FieldGroup
      v-if="qualityFields[provider]"
      :title="provider + ' 下载参数'"
      :fields="qualityFields[provider]!"
  /></template>
  <section class="card">
    <details>
      <summary>历史任务字幕兼容</summary>
      <p>新任务的交付要求请在任务中心创建或编辑任务时指定。这里只影响旧任务。</p>
      <div class="fields">
        <SettingField v-for="field in groups.subtitles" :key="field.key" :field="field" />
      </div>
    </details>
  </section>
</template>
