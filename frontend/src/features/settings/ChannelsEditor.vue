<script setup lang="ts">
import { bootstrap } from '../../bootstrap';
import { useSettings } from '../../stores/settings';
import type { DownloadChannel } from '../../generated/config';
const store = useSettings();
const catalog = bootstrap().cobaltCatalog;
const textFields = {
  id: '渠道 ID',
  label: '名称',
  site_url: '网站地址',
  api_url: 'API 地址',
  upstream_group: '上游分组',
  resolver_group: '解析分组',
  quota_group: '配额分组',
};
const numberFields = {
  transfer_limit: '传输上限',
  resolver_limit: '解析上限',
  connection_limit: '连接上限',
  task_limit: '任务上限',
  weight: '权重',
};
function update(index: number, key: keyof DownloadChannel, value: string | number) {
  store.update(
    'download_channels',
    store.draft.download_channels.map((item, i) =>
      i === index ? { ...item, [key]: value } : item,
    ),
  );
}
function add(channel?: DownloadChannel) {
  if (store.draft.download_channels.length >= 16) return;
  const id = 'custom_' + Date.now().toString(36);
  store.update('download_channels', [
    ...store.draft.download_channels,
    channel
      ? { ...channel }
      : {
          id,
          label: '自定义渠道',
          adapter: 'bergung',
          site_url: '',
          api_url: '',
          upstream_group: 'unverified-media',
          resolver_group: id,
          quota_group: id,
          transfer_limit: 8,
          resolver_limit: 2,
          connection_limit: 16,
          task_limit: 4,
          weight: 1,
          verification: 'required',
        },
  ]);
}
function remove(index: number) {
  store.update(
    'download_channels',
    store.draft.download_channels.filter((_, i) => i !== index),
  );
}
</script>
<template>
  <section class="card">
    <h2>自定义渠道</h2>
    <p>
      最多 16 个，仅支持 Bergung / Cobalt
      兼容网站。同一上游或配额应使用相同分组，避免把多个入口误当成独立容量。
    </p>
    <p v-if="store.issues.download_channels" class="error">{{ store.issues.download_channels }}</p>
    <div v-for="(channel, index) in store.draft.download_channels" :key="index" class="channel">
      <details>
        <summary>{{ channel.label || '未命名渠道' }} · {{ channel.adapter }}</summary>
        <div class="fields">
          <label v-for="(label, key) in textFields" :key="key" class="field"
            >{{ label
            }}<input
              :name="index === 0 && key === 'id' ? 'download_channels' : `channel-${index}-${key}`"
              :value="channel[key]"
              @input="update(index, key, ($event.target as HTMLInputElement).value)"
          /></label>
          <label class="field"
            >适配器<select :value="channel.adapter" disabled>
              <option value="bergung">Bergung / Cobalt</option>
            </select></label
          >
          <label v-for="(label, key) in numberFields" :key="key" class="field"
            >{{ label
            }}<input
              type="number"
              min="1"
              :max="
                {
                  transfer_limit: 64,
                  resolver_limit: 16,
                  connection_limit: 128,
                  task_limit: 64,
                  weight: 10,
                }[key]
              "
              :value="channel[key]"
              @input="
                update(
                  index,
                  key,
                  ($event.target as HTMLInputElement).value === ''
                    ? NaN
                    : Number(($event.target as HTMLInputElement).value),
                )
              "
          /></label>
          <label class="field"
            >网站验证<select
              :value="channel.verification"
              @change="update(index, 'verification', ($event.target as HTMLSelectElement).value)"
            >
              <option value="auto">自动判断</option>
              <option value="required">需要验证</option>
              <option value="none">无需验证</option>
            </select></label
          >
        </div>
      </details>
      <button type="button" class="danger-link" @click="remove(index)">删除渠道</button>
    </div>
    <button
      type="button"
      name="download_channels"
      :disabled="store.draft.download_channels.length >= 16"
      @click="add()"
    >
      新增自定义渠道
    </button>
    <details>
      <summary>从入口目录添加</summary>
      <p>目录是历史验证记录，可用性仍取决于当前出口与上游状态。</p>
      <div class="catalog-item" v-for="item in catalog" :key="item.channel.id">
        <span
          ><strong>{{ item.host }}</strong
          ><small>{{ item.reason }}</small></span
        ><button
          type="button"
          :disabled="
            item.status !== 'verified' ||
            store.draft.download_channels.length >= 16 ||
            store.draft.download_channels.some((channel) => channel.id === item.channel.id)
          "
          @click="add(item.channel)"
        >
          添加
        </button>
      </div>
    </details>
  </section>
</template>
