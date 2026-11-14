<script setup lang="ts">
import { computed } from 'vue';
import { outputLabels as labels, type DeliverySpec, type OutputKind } from './model';
const model = defineModel<DeliverySpec>({ required: true });
const spec = computed(() => model.value);
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
        >字幕语言<input
          name="delivery-languages"
          :value="spec.subtitles.languages.join(',')"
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
        >语言要求<select v-model="spec.subtitles.language_policy" name="delivery-language-policy">
          <option value="available_selected">指定语言中至少一种</option>
          <option value="all_selected">每种指定语言都必须有</option>
        </select></label
      >
      <label class="field"
        >字幕来源<select
          :value="spec.subtitles.sources.join(',')"
          name="delivery-sub-sources"
          @change="spec.subtitles.sources = ($event.target as HTMLSelectElement).value.split(',')"
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
    <p>费用按实际交付文件大小累计。多种产物分别计量，同一产物重试不重复收费，测试处理不收费。</p>
  </div>
</template>
