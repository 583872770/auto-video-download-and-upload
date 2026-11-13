<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import { ElButton } from 'element-plus';
import 'element-plus/es/components/button/style/css';
interface Spec {
  schema_version: number;
  outputs: string[];
  video: { max_height: number; container: string };
  audio: { format: string };
  av: { container: string };
  subtitles: {
    languages: string[];
    format: string;
    sources: string[];
    language_policy: string;
    missing_policy: string;
  };
}
const spec = ref<Spec>(),
  baseline = ref(''),
  loading = ref(true),
  saving = ref(false),
  error = ref(''),
  message = ref('');
const dirty = computed(() => !!spec.value && JSON.stringify(spec.value) !== baseline.value);
const admin = bootstrap().role === 'admin';
const labels = { subtitle: '字幕', video_only: '无声视频', audio_only: '纯音频', av: '音视频成品' };
const languages = ref('');
function accept(value: Spec) {
  spec.value = value;
  languages.value = value.subtitles.languages.join(',');
  baseline.value = JSON.stringify(value);
}
function updateLanguages(value: string) {
  languages.value = value;
  if (spec.value)
    spec.value.subtitles.languages = value
      .split(',')
      .map((x) => x.trim())
      .filter(Boolean);
}
async function load() {
  loading.value = true;
  error.value = '';
  try {
    accept(
      (await api.request<{ spec: Spec }>('/api/delivery/defaults', { cache: 'no-store' })).spec,
    );
  } catch (failure) {
    error.value = String(failure);
  } finally {
    loading.value = false;
  }
}
async function save() {
  if (!spec.value || !dirty.value || saving.value || !admin) return;
  error.value = '';
  message.value = '';
  if (!spec.value.outputs.length) {
    error.value = '请至少选择一种交付内容。';
    return;
  }
  if (
    !spec.value.subtitles.languages.length ||
    spec.value.subtitles.languages.length > 8 ||
    spec.value.subtitles.languages.some((value) => !/^[A-Za-z][A-Za-z0-9-]{0,30}$/.test(value))
  ) {
    error.value = '请填写 1～8 个有效字幕语言代码。';
    return;
  }
  saving.value = true;
  const submitted = JSON.stringify(spec.value);
  try {
    const result = await api.request<{ spec: Spec }>('/api/delivery/defaults', {
      method: 'PUT',
      body: JSON.parse(submitted),
    });
    const saved = await api.request<{ spec: Spec }>('/api/delivery/defaults', {
      cache: 'no-store',
    });
    if (JSON.stringify(saved.spec) !== JSON.stringify(result.spec))
      throw new Error('读回结果发生变化，请重新读取核对。');
    accept(saved.spec);
    message.value = '新任务默认要求已保存，仅影响之后创建的任务。';
  } catch (failure) {
    error.value = '保存结果尚未确认：' + String(failure) + '；修改已保留，可重新读取核对。';
  } finally {
    saving.value = false;
  }
}
function reset() {
  if (baseline.value) accept(JSON.parse(baseline.value));
  message.value = '';
  error.value = '';
}
onMounted(load);
defineExpose({ dirty, saving });
</script>
<template>
  <section class="card delivery-defaults">
    <h2>新任务交付默认</h2>
    <p>这是整个工作区的默认要求，独立保存。当前实例的其他设置不会改变这些默认值。</p>
    <p v-if="loading" role="status">正在读取默认要求…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="message" class="success" role="status">{{ message }}</p>
    <button v-if="!spec && !loading" type="button" @click="load">重新读取</button>
    <form v-if="spec" novalidate @submit.prevent="save">
      <fieldset :disabled="!admin || loading || saving">
        <div class="actions">
          <button type="button" @click="spec.outputs = ['av']">仅成品</button
          ><button type="button" @click="spec.outputs = ['subtitle']">仅字幕</button
          ><button type="button" @click="spec.outputs = Object.keys(labels)">四项全选</button>
        </div>
        <div class="choices">
          <label v-for="(label, key) in labels" :key="key" class="choice"
            ><input v-model="spec.outputs" type="checkbox" name="delivery-output" :value="key" />{{
              label
            }}</label
          >
        </div>
        <p>
          每条来源交付：{{
            spec.outputs.map((key) => labels[key as keyof typeof labels]).join(' + ') ||
            '请至少选择一种'
          }}
        </p>
        <div v-if="spec.outputs.includes('subtitle')" class="fields">
          <label class="field"
            >字幕语言<input
              name="delivery-languages"
              :value="languages"
              @input="updateLanguages(($event.target as HTMLInputElement).value)"
            /><small>如 en,zh-Hans,zh-Hant；最多 8 种。</small></label
          >
          <label class="field"
            >字幕格式<select v-model="spec.subtitles.format" name="delivery-sub-format">
              <option value="srt">SRT</option>
              <option value="vtt">VTT</option>
            </select></label
          >
          <label class="field"
            >语言要求<select
              v-model="spec.subtitles.language_policy"
              name="delivery-language-policy"
            >
              <option value="available_selected">指定语言中至少一种</option>
              <option value="all_selected">每种指定语言都必须有</option>
            </select></label
          >
          <label class="field"
            >字幕来源<select
              :value="spec.subtitles.sources.join(',')"
              name="delivery-sub-sources"
              @change="
                spec.subtitles.sources = ($event.target as HTMLSelectElement).value.split(',')
              "
            >
              <option value="manual,automatic">人工优先，自动备选</option>
              <option value="manual">仅人工</option>
              <option value="automatic">仅自动</option>
            </select></label
          >
        </div>
        <p v-if="spec.outputs.includes('subtitle')">
          来源缺少所需字幕时标记待确认，其他已完成文件保留。字幕单独交付。
        </p>
        <div class="fields">
          <label v-if="spec.outputs.some((x) => ['av', 'video_only'].includes(x))" class="field"
            >最高画质<select v-model="spec.video.max_height" name="delivery-height">
              <option
                v-for="height in [0, 360, 480, 720, 1080, 1440, 2160, 4320]"
                :key="height"
                :value="height"
              >
                {{ height ? height + 'p' : '原始最高画质' }}
              </option>
            </select></label
          >
          <label v-if="spec.outputs.includes('video_only')" class="field"
            >无声视频容器<select v-model="spec.video.container" name="delivery-video-container">
              <option value="source">原始格式</option>
              <option value="mp4">MP4（可能转码）</option>
            </select></label
          >
          <label v-if="spec.outputs.includes('audio_only')" class="field"
            >纯音频格式<select v-model="spec.audio.format" name="delivery-audio-format">
              <option value="source">原始音质</option>
              <option value="mp3">MP3（转码）</option>
            </select></label
          >
          <label v-if="spec.outputs.includes('av')" class="field"
            >音视频成品容器<select v-model="spec.av.container" name="delivery-av-container">
              <option value="auto">兼容时 MP4，否则 MKV</option>
              <option value="mp4">MP4（可能转码）</option>
              <option value="mkv">MKV</option>
            </select></label
          >
        </div>
        <p>
          费用按实际交付文件大小累计。多种产物分别计量，同一产物重试不重复收费，测试处理不收费。
        </p>
        <div class="actions">
          <ElButton type="primary" native-type="submit" :disabled="!dirty" :loading="saving"
            >保存新任务默认要求</ElButton
          ><ElButton :disabled="!dirty" @click="reset">放弃修改</ElButton>
        </div>
      </fieldset>
    </form>
  </section>
</template>
