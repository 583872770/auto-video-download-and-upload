'use strict';

const waitResourceNames = {
  upstream: '共享上游额度', 'local-upstream': '本实例渠道额度', quota: '共享渠道配额',
  connections: '共享连接预算', 'local-connections': '本机连接预算',
  'local-transfers': '本实例传输名额', resolver: '解析名额',
  'backend-quota': '后台共享配额', 'backend-resolver': '后台解析名额',
};
function concurrencyCountdown(seconds, sampledAt) {
  const deadline = (sampledAt + seconds) * 1000;
  const left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
  return `<span data-capacity-deadline="${deadline}">${left ? left + ' 秒' : '等待状态更新'}</span>`;
}
function concurrencyHTML(data, {compact = false, key = 'main'} = {}) {
  if (!data) return '<p class="capacity-empty">正在读取并发与渠道状态…</p>';
  const channels = data.channels || [];
  const cooled = channels.filter(row => row.status === 'cooldown');
  const recovering = channels.filter(row => row.status === 'recovering');
  const stale = Date.now() / 1000 - data.time > 20;
  const notices = [];
  const subtitles = data.subtitles || {};
  if (subtitles.queued || subtitles.running || subtitles.failed) notices.push(['info', '字幕独立补下载', `待补 ${subtitles.queued || 0} · 执行中 ${subtitles.running || 0} · 补下载失败 ${subtitles.failed || 0}。${subtitles.cooldown_seconds ? '字幕上游冷却：'+concurrencyCountdown(subtitles.cooldown_seconds, data.time)+'。' : ''}字幕队列不占用视频传输额度，也不改变视频成功及计费；完成当前后停止时不再领取新字幕任务。`, '#tasks', '查看执行日志']);
  if (!data.online || stale) notices.push(['unavailable', !data.online ? '实例离线' : '状态暂未更新', '下方保留最近数据，无法确认当前可用额度。请检查实例连接。', '#instances', '查看实例']);
  if (cooled.length) {
    const subtitle = cooled.some(row => row.throttle_kind === 'subtitles');
    const code = cooled.some(row => row.throttle_status === 429) ? ' · HTTP 429' : '';
    notices.push(['warning', `渠道冷却中${code}`, `${subtitle ? '字幕请求触发限流，当前与视频下载共用冷却范围。' : '上游限流或连续故障触发退避。'}${cooled.map(row => `${escapeHTML(row.label)}：${concurrencyCountdown(row.cooldown_seconds, data.time)}`).join('；')}后重新检查；到期不保证立即恢复满速。`, '#settings', '查看下载策略']);
  } else if (recovering.length) notices.push(['warning', '渠道正在恢复', `限流冷却已结束，相关共享资源暂按 1 路放行，等待恢复窗口结束。${concurrencyCountdown(Math.max(...recovering.map(row => row.recovery_seconds)), data.time)}`, '#settings', '查看渠道']);
  if (data.disk_blocked || data.disk_message) notices.push(['warning', '临时空间等待', `${escapeHTML(data.disk_message || '临时文件预算已用满，暂停领取新任务。')} 磁盘剩余空间与临时文件预算分别计算。`, '#settings', '查看磁盘预算']);
  const restricted = channels.filter(row => Math.min(row.transfer_limit, row.shared_limit) < data.requested);
  if (restricted.length) notices.push(['info', '渠道额度低于设置并发', `设置 ${data.requested} 路，${restricted.map(row => `${escapeHTML(row.label)} 单渠道上限 ${row.transfer_limit} 路、共享上游上限 ${row.shared_limit} 路`).join('；')}。提高工作进程数不会同步提高这些额度。`, '#settings', '查看渠道上限']);
  if (data.global_limit) notices.push(['info', '全部实例共用总上限', `全部实例合计已占用 ${data.global_running || 0} / ${data.global_limit} 个视频名额，包含解析和合并；其他实例也会占用名额。`, '#instances', '查看全局上限']);
  if (data.mode === 'auto' && !data.sample_fresh) notices.push(['info', '等待资源采样', '自动调节数据缺失或已过期，暂按保守额度调度。', '#instances', '查看实例']);
  else if (data.mode === 'auto' && data.transfer_limit < data.requested) notices.push(['info', '自动调节尚未使用全部额度', `设置上限 ${data.requested} 路，目前调度 ${data.transfer_limit} 路。${escapeHTML(data.adjustment_reason || '系统根据资源及实际吞吐调整并发。')}`, '#instances', '查看实例']);
  if (data.paused) notices.unshift(['info', '已停止领取新任务', '正在执行的视频按当前停止方式完成或退出。', '#overview', '查看下载控制']);
  const waitText = Object.entries(data.wait_resources || {}).map(([name, count]) => `${escapeHTML(waitResourceNames[name.split(':')[0]] || '其他资源')} ${count} 个`).join(' · ');
  const tone = !data.online || stale ? 'unavailable' : cooled.length || recovering.length || data.disk_blocked ? 'warning' : 'normal';
  const label = !data.online || stale ? '状态待确认' : cooled.length ? '渠道冷却' : recovering.length ? '逐步恢复' : data.disk_blocked ? '空间等待' : data.paused ? '已停止领取' : '调度中';
  const details = `<details class="capacity-details" data-capacity-details="${escapeHTML(key)}"><summary>渠道上限与实时占用 <span>${channels.length} 个入口 · 同组共享额度</span></summary><div class="capacity-table-wrap"><table class="capacity-table"><thead><tr><th>下载渠道 / 共享上游</th><th>渠道上限</th><th>当前额度估算</th><th>共享上游占用</th><th>共享连接占用</th><th>状态</th></tr></thead><tbody>${channels.map(row => `<tr><td><strong>${escapeHTML(row.label)}</strong><small>${escapeHTML(row.group)}</small></td><td>${row.transfer_limit} 路<small>${row.resolver_separate ? `解析上限 ${row.resolver_limit} 路` : '解析与传输共用名额'}</small></td><td><strong>${row.effective_limit} 路</strong>${row.connection_units ? `<small>每任务预留 ${row.connection_units} 个连接</small>` : ''}</td><td>${row.occupied} / ${row.shared_limit}<small>含其他实例</small></td><td>${row.connection_used} / ${row.connection_limit}</td><td><span class="capacity-channel-state ${row.status}">${row.status === 'cooldown' ? '冷却 · '+concurrencyCountdown(row.cooldown_seconds, data.time) : row.status === 'recovering' ? '恢复观察 · '+concurrencyCountdown(row.recovery_seconds, data.time) : '可参与调度'}</span></td></tr>`).join('')}</tbody></table></div><p class="capacity-footnote">当前额度是新任务准入上界估算，综合实例、渠道、连接及共享资源限制；多个入口的额度不能直接相加。上限降低后，已有任务可能暂时超出新额度。</p></details>`;
  return `<div class="capacity-body" data-tone="${tone}"><div class="capacity-heading"><div><span class="capacity-eyebrow">当前实例 · 实时调度</span><h2>并发与等待</h2></div><span class="capacity-status ${tone}" role="status">${label}</span></div><div class="capacity-metrics"><div><span>设置并发</span><strong>${data.requested}<small> 路</small></strong><p>${data.mode === 'auto' ? '自动模式传输目标上限' : '手动模式工作进程上限'}</p></div><div><span>当前额度估算</span><strong>${data.online && !stale ? data.effective_limit : '—'}<small> 路</small></strong><p>综合渠道及共享资源限制</p></div><div><span>实际传输</span><strong>${data.online && !stale ? data.transferring : '—'}<small> 个视频</small></strong><p>处理中共 ${data.running} 个</p></div><div><span>等待传输</span><strong>${data.waiting_transfer}<small> 个视频</small></strong><p>准备 ${data.preparing} · 合并 / 后处理 ${data.processing}</p></div></div>${waitText ? `<p class="capacity-waits">等待原因：${waitText}</p>` : ''}<div class="capacity-notices">${notices.map(([kind, title, description, href, action]) => `<div class="capacity-notice ${kind}"><span class="capacity-notice-icon" aria-hidden="true">${kind === 'warning' ? '!' : 'i'}</span><div><strong>${escapeHTML(title)}</strong><p>${description}</p></div><a href="/${href}">${action} ↗</a></div>`).join('') || '<p class="capacity-clear">当前未检测到共享资源冷却或磁盘阻塞；任务准备、视频大小和上游响应仍会影响实际占用。</p>'}</div><p class="capacity-footnote">本机连接预留 ${data.connection_used || 0} / ${data.connection_budget || 32}（文件与分片共用，非实际套接字数）</p>${compact ? '' : details}<p class="capacity-footnote">每次请求读取共享额度，资源采样约每 5 秒更新。${data.sample_fresh ? '' : '资源吞吐采样尚未更新；渠道状态以上次读取为准。'}</p></div>`;
}
function renderConcurrency() {
  const item = state.fleet?.items.find(value => value.id === state.instanceId);
  const data = fleetEnabled ? item?.concurrency : state.status?.concurrency;
  for (const target of document.querySelectorAll('[data-concurrency-panel]')) {
    const open = target.querySelector('details')?.open;
    target.innerHTML = concurrencyHTML(data, {key: target.dataset.concurrencyPanel});
    target.dataset.capacitySampledAt = data?.time || '';
    target.classList.toggle('capacity-stale', !!data && Date.now() / 1000 - data.time > 20);
    if (open) target.querySelector('details').open = true;
  }
}
setInterval(() => {
  for (const node of document.querySelectorAll('[data-capacity-deadline]')) {
    const left = Math.max(0, Math.ceil((Number(node.dataset.capacityDeadline) - Date.now()) / 1000));
    node.textContent = left ? `${left} 秒` : '等待状态更新';
  }
  if ([...document.querySelectorAll('[data-capacity-sampled-at]')].some(node =>
      node.dataset.capacitySampledAt && Date.now() / 1000 - Number(node.dataset.capacitySampledAt) > 20 && !node.classList.contains('capacity-stale'))) renderConcurrency();
}, 1000);
