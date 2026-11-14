<script setup lang="ts">
import { gb, integer, type Method } from './contracts';
defineProps<{ items: Method[] }>();
</script>
<template>
  <div class="table-scroll">
    <table>
      <caption>
        实际下载提供方
      </caption>
      <thead>
        <tr>
          <th>提供方</th>
          <th>尝试 / 运行</th>
          <th>成功 / 失败</th>
          <th>跳过 / 未完成</th>
          <th>交付</th>
          <th>测试（不计费）</th>
          <th>全部完成</th>
          <th>切换来源任务</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in items" :key="row.method">
          <td>{{ row.label }}</td>
          <td>{{ integer(row.attempted) }} / {{ integer(row.running) }}</td>
          <td>
            {{ integer(BigInt(row.succeeded || 0) + BigInt(row.test_succeeded || 0) + '') }} /
            {{ integer(row.failed)
            }}<small class="cell-note"
              >含测试 {{ integer(row.test_succeeded) }} · 成功率
              {{ row.success_rate === null ? '—' : row.success_rate + '%' }}</small
            >
          </td>
          <td>{{ integer(row.skipped) }} / {{ integer(row.unfinished) }}</td>
          <td>
            {{ gb(row.file_bytes_exact)
            }}<small class="cell-note"
              >{{ integer(row.file_count) }} 个 · {{ integer(row.file_bytes_exact) }} 字节</small
            >
          </td>
          <td>
            {{ gb(row.test_file_bytes_exact)
            }}<small class="cell-note">{{ integer(row.test_file_count) }} 个</small>
          </td>
          <td>
            {{ gb(row.downloaded_file_bytes_exact)
            }}<small class="cell-note">{{ integer(row.downloaded_file_count) }} 个</small>
          </td>
          <td>{{ integer(row.fallback_tasks) }}</td>
        </tr>
        <tr v-if="!items.length">
          <td colspan="8" class="empty">暂无提供方记录。</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
