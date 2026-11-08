'use strict';

let diskRefreshRunning = false;
const diskBytes = value => value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB`
  : `${(value / 1024 ** 2).toFixed(2)} MiB`;
async function refreshDisk() {
  if (diskRefreshRunning || !document.getElementById('disk-panel')) return;
  if (fleetEnabled && !state.instanceId) {
    document.getElementById('disk-summary').textContent = '请先选择要查看和配置的实例。';
    document.getElementById('workspace-disk-status').textContent = '尚无下载实例';
    return;
  }
  diskRefreshRunning = true;
  const instance = state.instanceId;
  const chip = document.getElementById('workspace-disk-status');
  try {
    const disk = await api('/api/disk');
    if (instance !== state.instanceId) return;
    chip.textContent = `实例磁盘剩余 ${diskBytes(disk.free_bytes)}`;
    chip.classList.toggle('warning', !!disk.low_space || disk.committed_bytes >= disk.budget_bytes);
    chip.classList.remove('unavailable');
    chip.title = `${instance ? instanceName(instance) : '本机'} · 临时文件 ${diskBytes(disk.used_bytes)} / ${diskBytes(disk.budget_bytes)}${disk.message?' · '+disk.message:''} · 点击查看磁盘管理与自动清理`;
    document.getElementById('disk-summary').textContent =
      `临时文件 ${diskBytes(disk.used_bytes)} / ${diskBytes(disk.budget_bytes)} · 含预留 ${diskBytes(disk.committed_bytes)} · 磁盘剩余 ${diskBytes(disk.free_bytes)}`;
    const names = {test: '测试文件', browsers: '浏览器缓存', logs: '技术日志', imports: '导入与上传', cache: '诊断及缓存'};
    const rows = Object.entries(names).map(([key, name]) => {
      const value = disk.categories[key];
      return `${name}：${diskBytes(value.used_bytes)} / ${diskBytes(value.budget_bytes)}`;
    });
    if (disk.message) rows.push(disk.message);
    if (disk.log_warning) rows.push(disk.log_warning);
    const estimate = disk.capacity_estimate;
    if (estimate) rows.push(`容量估算（近 24 小时 ${estimate.sample_count} 个成功任务）：按近期较大视频及合并预留，每路约 ${diskBytes(estimate.peak_bytes)}；现有预算约可支持 ${estimate.suggested_slots} 路，当前可新增约 ${estimate.additional_slots} 路。设置 ${estimate.configured_slots} 路约需 ${diskBytes(estimate.budget_for_configured_bytes)} 测试预算。仅供规划，实际以任务预留为准，预算不会自动增大。`);
    else if (state.config?.storage_mode === 'test') rows.push('积累至少 3 个成功视频后，将显示按近期视频大小与合并峰值估算的预算并发。');
    rows.push(...Object.entries(disk.categories).filter(([, value]) => value.committed_bytes > value.used_bytes).map(([key, value]) => `${names[key] || key}已用加执行预留：${diskBytes(value.committed_bytes)} / ${diskBytes(value.budget_bytes)}`));
    rows.push(`当前下载执行 ${Number(disk.active.test || 0) + Number(disk.active.cache || 0)} 个 · ${disk.waiting_tasks || 0} 个视频有磁盘等待记录`);
    if (disk.last_cleanup?.pending_results) rows.push(`${disk.last_cleanup.pending_results} 个执行结果待核对，已保留文件`);
    if (disk.last_cleanup?.failed) rows.push(`${disk.last_cleanup.failed} 项清理失败，稍后自动重试`);
    document.getElementById('disk-usage').replaceChildren(...rows.map(text => {
      const row = document.createElement('p'); row.textContent = text; return row;
    }));
  } catch (error) {
    if (instance !== state.instanceId) return;
    chip.textContent = '磁盘状态暂不可用';
    chip.classList.remove('warning');
    chip.classList.add('unavailable');
    chip.title = error.message;
    document.getElementById('disk-summary').textContent = `磁盘状态未更新：${error.message}`;
  } finally {
    diskRefreshRunning = false;
    if (instance !== state.instanceId) refreshDisk();
  }
}

document.getElementById('disk-cleanup')?.addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  const message = document.getElementById('disk-cleanup-result');
  message.textContent = '正在检查可清理文件…';
  try {
    const result = await api('/api/disk/cleanup', {method: 'POST'});
    message.textContent = result.message;
    await refreshDisk();
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
document.getElementById('instance-logs-open')?.addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  const content = document.getElementById('instance-logs-content');
  try {
    const result = await api('/api/disk/logs?scope=current');
    content.textContent = result.text || '本次启动后暂无保留的实例日志。';
  } catch (error) {
    content.textContent = `读取实例日志失败：${error.message}`;
  } finally {
    document.getElementById('instance-logs-dialog').showModal();
    button.disabled = false;
  }
});
document.getElementById('instance-logs-close')?.addEventListener('click', () => {
  document.getElementById('instance-logs-dialog').close();
});
let runtimeSummaryRequest = 0;
async function showRuntimeSummary() {
  const requestId = ++runtimeSummaryRequest;
  const instance = state.instanceId;
  const content = document.getElementById('runtime-summary-content');
  content.textContent = '正在读取当前管理实例的运行汇总…';
  try {
    const days = Number(document.getElementById('runtime-summary-days').value);
    const result = await api(`/api/disk/runtime-summary?days=${days}`);
    if (requestId !== runtimeSummaryRequest || instance !== state.instanceId) return;
    const totals = result.totals;
    const reasons = Object.entries(result.failure_reasons || {}).map(([reason, count]) =>
      `${failureKindNames[reason] || reason}：${count} 个`).join('\n');
    const hours = (result.hours || []).map(row => `${new Date(row.hour * 1000).toLocaleString('zh-CN')}：成功 ${row.succeeded} · 失败 ${row.failed} · 跳过 ${row.skipped} · ${diskBytes(row.file_bytes)}`).join('\n');
    content.textContent = `实例：${result.scope}\n范围：${new Date(result.since * 1000).toLocaleString('zh-CN')} 至 ${new Date(result.until * 1000).toLocaleString('zh-CN')}\n${result.coverage}\n\n完成 ${totals.completed} 个，成功 ${totals.succeeded + totals.test_succeeded} 个（其中测试 ${totals.test_succeeded} 个），失败 ${totals.failed} 个，跳过 ${totals.skipped} 个\n成功率：${totals.success_rate == null ? '暂无数据' : totals.success_rate + '%'}\n输出文件：${totals.file_count} 个 / ${diskBytes(totals.file_bytes)}\n\n失败原因\n${reasons || '暂无失败记录'}\n\n逐小时结果\n${hours || '暂无完成记录'}`;
    const retention = result.log_retention;
    if (retention) content.textContent += `\n\n日志清理采样：${new Date(retention.sampled_at * 1000).toLocaleString('zh-CN')}\n日志预算 ${diskBytes(retention.budget_bytes)}，当前保留 ${retention.retained_task_files} 份任务日志。${retention.database_available ? `本次提前清理未到期失败日志 ${retention.unexpired_failure_files_removed} 份。` : '采样时数据库不可用，无法确认日志的成功或失败分类。'}容量优先于保留天数。`;
  } catch (error) {
    if (requestId === runtimeSummaryRequest && instance === state.instanceId) content.textContent = `读取运行汇总失败：${error.message}`;
  }
}
document.getElementById('runtime-summary-open')?.addEventListener('click', () => {
  document.getElementById('runtime-summary-dialog').showModal();
  showRuntimeSummary();
});
document.getElementById('runtime-summary-days')?.addEventListener('change', showRuntimeSummary);
document.getElementById('runtime-summary-close')?.addEventListener('click', () => {
  ++runtimeSummaryRequest;
  document.getElementById('runtime-summary-dialog').close();
});
window.addEventListener('hashchange', () => { if (location.hash === '#settings') refreshDisk(); });
setInterval(() => { if (!document.hidden) refreshDisk(); }, 10000);
refreshDisk();
