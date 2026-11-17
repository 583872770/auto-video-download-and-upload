<script setup lang="ts">
import { groups } from './fields';
import FieldGroup from './FieldGroup.vue';
</script>
<template>
  <section class="card">
    <h2>yt-dlp · 账号池下载</h2>
    <p>每个下载任务先从账号池取得可用账号。视频、音频、字幕和封面共用同一执行链与请求节奏。</p>
    <p>账号分配、登录、冷却及请求间隔在账号管理中维护；下载内容和保存位置在任务中心配置。</p>
  </section>
  <FieldGroup title="失败重试" :fields="groups.retry" />
</template>
