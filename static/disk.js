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
    const result = await api('/api/disk/logs');
    content.textContent = result.text || '暂无实例日志。';
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
window.addEventListener('hashchange', () => { if (location.hash === '#settings') refreshDisk(); });
setInterval(() => { if (!document.hidden) refreshDisk(); }, 10000);
refreshDisk();
