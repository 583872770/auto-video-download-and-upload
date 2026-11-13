import type { RuntimeSummary } from '../../api/types';

const bytes = (n: number) => (n / 1024 ** 2).toFixed(1) + ' MiB';
const date = (n: number) => new Date(n * 1000).toLocaleString('zh-CN');
export function summaryText(result: RuntimeSummary): string {
  const totals = result.totals;
  const reasons = Object.entries(result.failure_reasons || {})
    .map(([reason, count]) => `${reason}：${count} 个`)
    .join('\n');
  const hours = (result.hours || [])
    .map(
      (row) =>
        `${date(row.hour)}：成功 ${row.succeeded} · 失败 ${row.failed} · 跳过 ${row.skipped} · ${bytes(row.file_bytes)}`,
    )
    .join('\n');
  let text = `实例：${result.scope}\n范围：${date(result.since)} 至 ${date(result.until)}\n${result.coverage}\n\n完成 ${totals.completed} 个，成功 ${totals.succeeded + totals.test_succeeded} 个（测试 ${totals.test_succeeded} 个），失败 ${totals.failed} 个，跳过 ${totals.skipped} 个\n成功率：${totals.success_rate == null ? '暂无数据' : totals.success_rate + '%'}\n输出文件：${totals.file_count} 个 / ${bytes(totals.file_bytes)}\n\n失败原因\n${reasons || '暂无失败记录'}\n\n逐小时结果\n${hours || '暂无完成记录'}`;
  const retention = result.log_retention;
  if (retention)
    text += `\n\n日志清理采样：${date(retention.sampled_at)}\n日志预算 ${bytes(retention.budget_bytes)}，保留 ${retention.retained_task_files} 份任务日志。${retention.database_available ? `本次提前清理未到期失败日志 ${retention.unexpired_failure_files_removed} 份。` : '采样时数据库不可用，无法确认日志分类。'}容量优先于保留天数。`;
  return text;
}
