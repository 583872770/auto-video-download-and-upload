<script setup lang="ts">
import { computed } from 'vue';
import { outputLabels as labels, type DeliverySpec, type OutputKind } from './model';
const model = defineModel<DeliverySpec>({ required: true });
const spec = computed(() => model.value);
const mode = computed(() => spec.value.subtitles.mode || 'selected');
const translate = computed(() => mode.value !== 'original');
const languageOptions: [string, string][] = [
  ['zh-Hans', '简体中文'],
  ['zh-Hant', '繁体中文'],
  ['en', '英语'],
  ['ja', '日语'],
  ['ko', '韩语'],
  ['fr', '法语'],
  ['de', '德语'],
  ['es', '西班牙语'],
  ['ru', '俄语'],
  ['ar', '阿拉伯语'],
];
function toggleTranslation(enabled: boolean) {
  spec.value.subtitles.mode = enabled ? 'original_and_translated' : 'original';
  if (enabled && !spec.value.subtitles.languages.length)
    spec.value.subtitles.languages = ['zh-Hans'];
}
function updateLanguages(value: string) {
  model.value = {
    ...model.value,
    subtitles: {
      ...model.value.subtitles,
      languages: value
        .split(',')
        .map((x) => x.trim())
        .filter(Boolean),
    },
  };
}
</script>
<template>
  <div class="delivery-editor">
    <div class="actions">
      <button type="button" @click="spec.outputs = ['av']">仅成品</button
      ><button type="button" @click="spec.outputs = ['subtitle']">仅字幕</button
      ><button type="button" @click="spec.outputs = Object.keys(labels) as OutputKind[]">
        四项全选
      </button>
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
        >字幕格式<select v-model="spec.subtitles.format" name="delivery-sub-format">
          <option value="srt">SRT</option>
          <option value="vtt">VTT</option>
        </select></label
      >
      <label v-if="translate" class="field"
        >语言要求<select v-model="spec.subtitles.language_policy" name="delivery-language-policy">
          <option value="available_selected">目标语言中至少一种</option>
          <option value="all_selected">每种目标语言都必须有</option>
        </select></label
      >
      <label class="field"
        >{{ mode === 'selected' ? '字幕来源' : '原始字幕来源'
        }}<select
          :value="spec.subtitles.sources.join(',')"
          name="delivery-sub-sources"
          @change="spec.subtitles.sources = ($event.target as HTMLSelectElement).value.split(',')"
        >
          <option value="manual,automatic">人工优先，原语言自动字幕备选</option>
          <option value="manual">仅人工</option>
          <option value="automatic">仅原语言自动字幕</option>
        </select></label
      >
    </div>
    <p v-if="spec.outputs.includes('subtitle') && !translate">
      默认下载视频现有人工字幕及原语言自动字幕，保留实际语言，不请求自动翻译。
    </p>
    <section v-if="spec.outputs.includes('subtitle')" class="subtitle-translation">
      <label class="choice"
        ><input
          type="checkbox"
          name="delivery-auto-translate"
          :checked="translate"
          @change="toggleTranslation(($event.target as HTMLInputElement).checked)"
        />同时下载自动翻译字幕</label
      >
      <p v-if="mode === 'selected'" class="muted">
        此历史任务沿用指定语言要求。关闭自动翻译后，将改为下载原始字幕。
      </p>
      <template v-if="translate">
        <p class="muted">保留原始字幕，同时获取所选语言的字幕。目标语言已有人工字幕时优先使用。</p>
        <div class="choices">
          <label v-for="[code, label] in languageOptions" :key="code" class="choice">
            <input
              v-model="spec.subtitles.languages"
              type="checkbox"
              name="delivery-translation-language"
              :value="code"
            />{{ label }}
          </label>
        </div>
        <label class="field"
          >目标语言代码<input
            name="delivery-languages"
            :value="spec.subtitles.languages.join(',')"
            @input="updateLanguages(($event.target as HTMLInputElement).value)"
          /><small>支持自定义代码，逗号分隔，最多 8 种。</small></label
        >
      </template>
    </section>
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
    <p>费用按实际交付文件大小累计。多种产物分别计量，同一产物重试不重复收费，测试处理不收费。</p>
  </div>
</template>
<style scoped>
.subtitle-translation {
  margin: 12px 0;
  padding: 14px 16px;
  border: 1px solid var(--line, #dfe7e7);
  border-radius: 10px;
  background: #f7faf9;
}
.subtitle-translation .choices {
  margin: 10px 0 14px;
  gap: 8px;
}
.subtitle-translation .choices .choice {
  flex: 0 1 auto;
  min-width: 0;
  padding: 8px 10px;
}
</style>
