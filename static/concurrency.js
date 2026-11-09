'use strict';

// Countdown text changes locally each second; absolute deadlines and all other
// markup still participate in the comparison so real state changes render.
const capacityMarkup = new WeakMap();
function capacityMarkupChanged(target, html) {
  const key = html.replace(/(<span data-capacity-deadline="[^"]+">)[^<]*(<\/span>)/g, '$1$2');
  if (capacityMarkup.get(target) === key) return false;
  capacityMarkup.set(target, key);
  return true;
}

const waitResourceNames = {
  upstream: '共享上游额度', 'local-upstream': '本实例渠道额度', quota: '共享渠道配额',
  connections: '共享连接预算', 'local-connections': '本机连接预算',
  'local-transfers': '本实例传输名额', resolver: '解析名额',
  'backend-quota': '后台共享配额', 'backend-resolver': '后台解析名额',
  channel: '渠道健康与任务额度', route: '入口准备名额', 'backend-route': '后端准备名额',
  'route-probe': '未验证入口的单任务探测名额',
};
function concurrencyCountdown(seconds, sampledAt) {
  const deadline = Math.round((sampledAt + seconds) * 1000);
  const left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
  return `<span data-capacity-deadline="${deadline}">${left ? left + ' 秒' : '等待状态更新'}</span>`;
}
function decisionExplanationHTML(data, key) {
  const value = data.decisions;
  if (!value) return '';
  const current = data.online && Date.now() / 1000 - data.time <= 20;
  const actions = {halt:'停止并人工检查',cooldown:'临时冷却',wait:'等待启动窗口',observe:'恢复观察',record:'记录故障'};
  const time = stamp => stamp ? new Date(stamp * 1000).toLocaleString('zh-CN') : '时间未记录';
  const restrictions = value.restrictions || [];
  const active = restrictions.length ? restrictions.map(row => `<li><b>${escapeHTML(row.scope)}</b>：${escapeHTML(actions[row.action] || row.action)}${row.until > data.time ? `，${concurrencyCountdown(row.until - data.time, data.time)}` : row.manual ? row.until ? '；冷却时间已到，仍需人工检查' : '；需人工检查后恢复' : ''}${row.reason ? `。${escapeHTML(failureKindNames[row.reason] || row.reason)}` : ''}${row.legacy ? '。历史记录缺少完整触发证据' : ''}</li>`).join('') : '<li>当前摘要未记录生效中的冷却或启动等待；仍需满足队列和其他资源条件。</li>';
  const recovery = value.recovery || {};
  const checks = value.manual_required ? `<p>恢复条件：实例${recovery.instance_stopped ? '已停止领取' : '仍可领取，需先停止'}；${recovery.downloads_idle ? '当前无执行中的视频' : '需等待执行中的视频结束'}；${recovery.cooldown_complete ? '已记录的等待时间已到' : '仍有等待未到期'}。账号检测结果以解除拦截时的重新检查为准。</p>` : '';
  const history = (value.history || []).map(item => `<article class="risk-guide-item"><h3>${escapeHTML(time(item.failure?.observed_at || item.time))} · 事件 ${escapeHTML(item.incident_id.slice(0, 8))}</h3>${failureEvidenceHTML(item.failure)}<p><b>采取的措施：</b>${item.effects.map(effect => `${escapeHTML(effect.scope)}：${escapeHTML(actions[effect.action] || effect.action)}${effect.until ? `（截止 ${escapeHTML(time(effect.until))}）` : ''}`).join('；')}。</p></article>`).join('');
  return `<div class="capacity-admission decision-explanation"><strong>${current ? '等待与恢复依据' : '上次记录的等待与恢复依据'}</strong>${!current ? '<p>实例离线或摘要已过期，以下不能确认当前状态。</p>' : ''}<ul>${active}</ul><p>${escapeHTML(value.combination || '')}</p>${checks}<p>${escapeHTML(recovery.instructions || '')}</p><details class="capacity-details" data-capacity-details="${escapeHTML(key)}-decisions"><summary>最近触发事件与处理记录 <span>${(value.history || []).length} 个事件 · 同次失败合并展示</span></summary>${history || '<p>暂无结构化事件记录；历史错误不会被补写成新的源站响应。</p>'}</details></div>`;
}
function cookieAccountNoticesHTML(pool, sampledAt, current = true) {
  const affected = (pool?.sessions || []).filter(s => s.state !== 'ready');
  if (!affected.length) return '';
  const title = !current ? '上次记录的账号状态，当前状态待确认' : pool.halted ? '池仍有人工拦截，请查看原拦截原因' : !pool.cooling_sessions
    ? '受影响账号正在低频恢复，其他账号正常调度' : pool.available_sessions
    ? '仅受影响账号暂停，其他账号继续调度' : '全部账号冷却中，队列自动等待，无需手动恢复';
  return `<div class="cookie-account-notices"><strong>${title}</strong>${affected.map(s => `<article class="cookie-account-notice ${s.state === 'cooldown' ? 'cooldown' : 'recovery'}"><strong>${escapeHTML(s.name)} · ${s.state === 'cooldown' ? s.cooldown_kind === 'repeated_http_403' ? '保护性冷却' : '冷却中' : '低频恢复中'}</strong><p>原因：${escapeHTML(failureKindNames[s.cooldown_kind] || '临时故障退避')}。${s.state === 'cooldown' ? `恢复检查时间：${escapeHTML(new Date(s.cooldown_until * 1000).toLocaleString('zh-CN'))} · 剩余 ${concurrencyCountdown(s.cooldown_until - sampledAt, sampledAt)}。冷却中不发送下载、字幕或登录检测请求；已开始的任务可结束。` : `当前最多 ${Number(s.limit)} 个任务，启动间隔至少 ${Number(s.interval)} 秒；本轮有效成功 ${Number(s.recovery_successes)} 次。`}</p>${s.last_failure_at ? `<p>最近异常：${escapeHTML(new Date(s.last_failure_at * 1000).toLocaleString('zh-CN'))}。</p>` : ''}${['account_rate_limit', 'request_rate_limit', 'repeated_http_403'].includes(s.cooldown_kind) ? '<p>到期自动单路、至少 300 秒间隔恢复；成功至少 10 次且观察满一小时后恢复设置的会话并发，启动间隔继续逐步缩短。一天内重复账号限流按 1 / 2 / 4 / 8 小时冷却；连续 403 按 15 / 30 / 60 分钟保护性冷却。源站要求更久则延长等待。</p>' : ''}<button type="button" class="text-button" data-cookie-recovery="${escapeHTML(s.id)}" aria-haspopup="dialog">查看恢复进度与间隔调整</button></article>`).join('')}<small>这是本地重新检查准入的时间，不保证源站届时解除限制；其他共享预算仍然生效。</small></div>`;
}
function concurrencyAdmissionHTML(data) {
  const pool = data.cookie_pool;
  const next = pool?.known && pool.next_start_at > data.time
    ? `下一启动窗口：${concurrencyCountdown(pool.next_start_at - data.time, data.time)}。` : '';
  const limited = pool?.limited_profile_name ? ` · 触发原拦截的账号：${escapeHTML(pool.limited_profile_name)}` : '';
  const sessions = pool ? `<p>${Number(pool.configured_files || 0)} 份 Cookie · ${pool.known ? Number(pool.effective_sessions || 0) : '待确认'} 个核心账号会话${limited}。同一账号的重复导出共享并发、请求间隔和限流状态。${pool.known ? `当前容量 ${Number(pool.capacity || 0)} 路${pool.warming_sessions ? ` · ${Number(pool.warming_sessions)} 个会话尚未完成预热` : ''}${pool.min_interval ? ` · 稳定会话启动间隔至少 ${Number(pool.min_interval)} 秒` : ''}` : ''}${pool.unavailable_files ? ` ${Number(pool.unavailable_files)} 份文件不可用，请在账号管理中检查。` : ''}</p>` : '';
  return `<div class="capacity-admission" role="status"><strong>当前调度说明</strong><p>${escapeHTML(data.bottleneck || '等待实例更新调度说明')}</p>${sessions}${cookieAccountNoticesHTML(pool, data.time, data.online && Date.now() / 1000 - data.time <= 20)}${next ? `<p>${next}</p>` : ''}</div>`;
}
function transferSlotState(stage) {
  if (stage === 'downloading') return {kind: 'transferring', label: '正在传输'};
  if (stage === 'waiting_request') return {kind: 'waiting', label: '等待请求间隔'};
  if (['processing', 'waiting_processing', 'cleaning', 'uploading_oss'].includes(stage))
    return {kind: 'processing', label: {cleaning: '清理文件', uploading_oss: '上传 OSS'}[stage] || '合并 / 后处理'};
  if (stage === 'extracting') return {kind: 'preparing', label: '准备传输'};
  return {kind: 'reserved', label: '已占用 · 状态同步中'};
}
function cookieTaskState(stage) {
  const labels = {extracting: '解析视频', waiting_transfer: '等待传输名额', waiting_resolver: '等待解析名额',
    waiting_cookie: '等待 Cookie', waiting_browser: '等待浏览器', waiting_verification: '等待网页验证',
    checking_environment: '检查环境', checking_login: '检查登录', switching_provider: '切换渠道',
    waiting_processing: '等待后处理', waiting_api_conversion: '等待转换', preparing_workspace: '准备工作目录',
    preparing_proxy: '准备代理', disk_wait: '等待磁盘空间', preparing: '准备任务'};
  if (labels[stage]) return {kind: 'preparing', label: labels[stage]};
  const status = transferSlotState(stage);
  return status.kind === 'reserved' ? {...status, label: '状态同步中'} : status;
}

const cookieTaskFilters = [
  ['all', '全部'], ['transferring', '正在传输'], ['preparing', '准备 / 解析'],
  ['waiting', '等待中'], ['processing', '后处理中'], ['idle', '无任务'],
];
const cookieAccountFilters = [
  ['all', '全部账号'], ['normal', '正常'], ['cooldown', '冷却中'],
  ['recovery', '恢复观察'], ['manual', '需人工处理'], ['unavailable', 'Cookie 不可用'],
  ['unknown', '状态待确认'],
];
// View preferences are local to this page and executor, never execution settings.
const cookieRuntimeFilters = new Map();
function cookieFilterScope() {
  return state.instanceId || 'local';
}
function cookieFilterSelection() {
  return cookieRuntimeFilters.get(cookieFilterScope()) || {task: 'all', account: 'all'};
}
function cookieFilterStates(account, current) {
  const reasons = new Set((account.reasons || []).map(reason => reason.code));
  const health = !current || !account.known ? 'unknown'
    : account.state === 'unavailable' || reasons.has('unavailable') ? 'unavailable'
    : reasons.has('pool_halted') ? 'manual'
    : account.state === 'cooldown' || reasons.has('account_cooldown') || reasons.has('pool_cooldown') ? 'cooldown'
    : account.state === 'recovery' ? 'recovery' : account.state === 'ready' ? 'normal' : 'unknown';
  const stages = new Set();
  if (current) {
    const tasks = account.tasks || [];
    if (!tasks.length) stages.add('idle');
    for (const task of tasks) {
      const stage = typeof task.stage === 'string' ? task.stage : '';
      if (stage === 'downloading') stages.add('transferring');
      else if (stage.startsWith('waiting_') || ['disk_wait', 'scheduling_wait', 'retrying'].includes(stage)) stages.add('waiting');
      else if (['processing', 'cleaning', 'uploading_oss'].includes(stage)) stages.add('processing');
      else if (['extracting', 'checking_environment', 'checking_login', 'switching_provider',
        'preparing_workspace', 'preparing_proxy', 'preparing', 'converting'].includes(stage)) stages.add('preparing');
    }
  }
  return {health, stages};
}
function cookieFilterView(accounts, current, selection) {
  const rows = accounts.map(account => ({account, ...cookieFilterStates(account, current)}));
  const taskMatches = row => selection.task === 'all' || row.stages.has(selection.task);
  const accountMatches = row => selection.account === 'all' || row.health === selection.account;
  const taskCounts = Object.fromEntries(cookieTaskFilters.map(([key]) => [key,
    rows.filter(row => accountMatches(row) && (key === 'all' || row.stages.has(key))).length]));
  const accountCounts = Object.fromEntries(cookieAccountFilters.map(([key]) => [key,
    rows.filter(row => taskMatches(row) && (key === 'all' || row.health === key)).length]));
  return {accounts: rows.filter(row => taskMatches(row) && accountMatches(row)).map(row => row.account),
    taskCounts, accountCounts};
}
function cookieFiltersHTML(view, selection) {
  return `<div class="cookie-runtime-filters" data-cookie-filter-scope="${escapeHTML(cookieFilterScope())}">
    <div class="cookie-filter-task-row"><span>任务状态</span><div class="cookie-task-filters" role="group" aria-label="按任务状态筛选">${cookieTaskFilters.map(([key, label]) => `<button type="button" data-cookie-task-filter="${key}" aria-pressed="${selection.task === key}"><span>${label}</span><b data-cookie-filter-count>${view.taskCounts[key]}</b></button>`).join('')}</div></div>
    <div class="cookie-filter-account-row"><label>账号状态<select data-cookie-account-filter aria-label="按账号状态筛选">${cookieAccountFilters.map(([key, label]) => `<option value="${key}"${selection.account === key ? ' selected' : ''}>${label} · ${view.accountCounts[key]}</option>`).join('')}</select></label><button type="button" class="text-button" data-cookie-filters-clear${selection.task === 'all' && selection.account === 'all' ? ' disabled' : ''}>清除筛选</button></div>
  </div>`;
}
function cookieAccountsHTML(data, {compact = false} = {}) {
  const allocation = data.transfer_allocation;
  const cookies = data.cookie_allocation;
  const current = data.online && Date.now() / 1000 - data.time <= 20;
  const known = current && data.cookie_pool?.known;
  const selection = compact ? null : cookieFilterSelection();
  const view = compact ? null : cookieFilterView(cookies.accounts, current, selection);
  const filters = compact ? '' : cookieFiltersHTML(view, selection);
  const summary = compact ? '' : `<p class="cookie-filter-summary" role="status">当前展示 <b>${view.accounts.length} / ${cookies.accounts.length}</b> 个 Cookie · 上方为实例整体数据。<span>数量按 Cookie 卡片统计，同一卡片可匹配多个任务状态；两项筛选同时满足才展示。${current ? '“等待中”指已有任务等待；“无任务”的具体原因见卡片。' : '实例离线或采样已过期，当前任务状态待确认。'}</span></p>`;
  const heading = `<div class="transfer-allocation-heading"><div><h3>Cookie 账号 <strong>${cookies.accounts.length}<small> 个</small></strong></h3><p>${known ? `已占用 ${allocation.occupied.length} 路 · 空闲 ${allocation.unoccupied} 路${allocation.restricted ? ` · 受限 ${allocation.restricted} 路` : ''}` : current ? `已占用 ${allocation.occupied.length} 路 · 剩余额度待确认` : '当前任务与等待原因待确认'}</p></div><span class="transfer-effective">传输上限 <b>${allocation.configured}</b> 路<br>当前额度估算 <b>${known ? allocation.effective : '—'}</b> 路</span></div>`;
  function taskHTML(task) {
    const status = cookieTaskState(task.stage);
    return `<button type="button" class="cookie-task ${status.kind}" data-task="${escapeHTML(task.task_id)}" title="${escapeHTML(task.title)} · 查看任务日志"><span class="cookie-task-stage">${status.label}</span><span class="cookie-task-title">${escapeHTML(task.title)}</span><small>${task.speed > 0 ? escapeHTML(bytes(task.speed)) + '/s' + ' · ' : ''}${task.transfer_reserved ? '占用传输名额' : '未占用传输名额'}</small></button>`;
  }
  const grid = compact ? '' : view.accounts.length ? `<div class="cookie-runtime-grid">${view.accounts.map(account => {
    const tasks = account.tasks || [];
    const live = current && account.known;
    const downloading = tasks.filter(task => task.stage === 'downloading').length;
    const reasons = account.reasons || [];
    const idleLabels = {paused: '已停止领取', account_budget: '等待账号预算', account_interval: '等待账号间隔', pool_interval: '等待共用启动间隔',
      start_interval: '等待实例启动间隔', transfer_limit: '等待传输额度', disk: '等待磁盘空间',
      removed: '已移出账号池', unassigned: '待分配任务'};
    const blocked = reasons.some(reason => ['account_cooldown', 'pool_cooldown', 'pool_halted', 'unavailable'].includes(reason.code));
    const status = !live ? {kind: 'unknown', label: '状态待确认'} : blocked
      ? {kind: 'restricted', label: account.state === 'unavailable' ? 'Cookie 不可用' : account.state === 'cooldown' ? '账号冷却中' : '暂停新增任务'}
      : downloading ? {kind: 'transferring', label: `${downloading} 个正在传输`}
      : tasks.length ? {kind: 'preparing', label: `${tasks.length} 个任务处理中`}
      : account.shared_with ? {kind: 'reserved', label: '共用账号额度'}
      : {kind: reasons[0]?.code === 'unassigned' ? 'unoccupied' : 'waiting', label: idleLabels[reasons[0]?.code] || '当前无任务'};
    const reasonHTML = current
      ? reasons.map(reason => `<li data-cookie-reason="${escapeHTML(reason.code)}">${escapeHTML(reason.label)}${reason.until > data.time ? ` · ${concurrencyCountdown(reason.until - data.time, data.time)}` : ''}</li>`).join('')
      : '<li>实例离线或账号采样未更新，当前原因待确认。</li>';
    const metadata = account.shared_with ? `共享账号：${escapeHTML(account.shared_name || account.shared_with)}`
      : account.state === 'cooldown' ? '冷却期间不再分配新任务，已开始的任务可结束'
      : `账号并发 ${Number(account.active || 0)} / ${Number(account.limit || 0)}${account.interval ? ` · 启动间隔至少 ${Number(account.interval)} 秒` : ''}${account.state === 'recovery' ? ' · 低频恢复中' : ''}${account.hourly_limit ? ` · 小时启动 ${Number(account.hourly_used || 0)} / ${Number(account.hourly_limit)} · 近24小时启动 ${Number(account.daily_used || 0)} / ${Number(account.daily_limit)}` : ''}`;
    return `<article class="cookie-runtime-card ${status.kind}" data-cookie-id="${escapeHTML(account.id)}"><div class="cookie-runtime-heading"><strong title="${escapeHTML(account.name)}">${escapeHTML(account.name)}</strong><span>${status.label}</span></div>${current && tasks.length ? `<div class="cookie-runtime-tasks">${tasks.map(taskHTML).join('')}</div>` : ''}${live && account.state !== 'unavailable' ? `<p class="cookie-runtime-meta">${metadata}</p>` : ''}${live && account.state !== 'unavailable' ? `<button type="button" class="text-button" data-cookie-recovery="${escapeHTML(account.shared_with || account.id)}" aria-haspopup="dialog">${account.state === 'recovery' ? '查看恢复进度与间隔调整' : '查看启动间隔与调整记录'}</button>` : ''}<div class="cookie-runtime-reasons"><strong>${live ? tasks.length ? '暂未新增任务的原因' : '为什么没有任务' : '状态说明'}</strong><ul>${reasonHTML}</ul>${live && account.cooldown_kind && account.state === 'cooldown' ? `<p>触发原因：${escapeHTML(failureKindNames[account.cooldown_kind] || account.cooldown_kind)}</p>` : ''}</div></article>`;
  }).join('')}</div>` : '<div class="cookie-filter-empty"><strong>没有符合当前筛选条件的 Cookie</strong><p>可清除筛选查看全部账号；账号状态与任务变化后会自动更新结果。</p></div>';
  const spacing = Number.isFinite(cookies.pool_start_interval) ? cookies.pool_start_interval === 0
    ? '已保存的账号池共用启动间隔已关闭。' : `已保存的账号池共用启动间隔为 ${Number(cookies.pool_start_interval)} 秒。`
    : '账号池启动间隔等待采样确认。';
  return `<section class="transfer-allocation cookie-allocation" aria-label="Cookie 账号任务与等待原因">${heading}${filters}${summary}${grid}${!compact && current && cookies.unassigned_tasks?.length ? `<div class="transfer-outside"><strong>尚未关联 Cookie 的任务（不参与账号筛选）</strong><div class="cookie-unassigned-tasks">${cookies.unassigned_tasks.map(taskHTML).join('')}</div></div>` : ''}<p class="transfer-slot-note">每格对应一份已选 Cookie；同一账号的重复 Cookie 共用额度。${spacing}原因依据当前准入记录；等待时间到期后仍需满足其他条件。未观测到限制不代表可以立即启动。点击视频查看任务日志。</p></section>`;
}
function transferSlotsHTML(data, {compact = false} = {}) {
  const allocation = data.transfer_allocation;
  if (!allocation) return '<p class="capacity-empty">等待传输名额状态更新…</p>';
  if (data.cookie_allocation) return cookieAccountsHTML(data, {compact});
  const current = data.online && Date.now() / 1000 - data.time <= 20;
  const allowanceKnown = current && (!data.cookie_pool || data.cookie_pool.known);
  const occupied = allocation.occupied || [];
  const legend = new Map();
  const tiles = occupied.map(task => {
    const status = current ? transferSlotState(task.stage) : {kind: 'unknown', label: '状态待确认'};
    if (current) legend.set(status.label, {kind: status.kind, count: (legend.get(status.label)?.count || 0) + 1});
    return {...task, ...status};
  });
  for (let index = 0; index < allocation.unoccupied + allocation.restricted; index++) {
    const free = index < allocation.unoccupied;
    tiles.push(!allowanceKnown ? {kind: 'unknown', label: '状态待确认'} : free
      ? {kind: 'unoccupied', label: '空闲名额', title: data.paused ? '已停止领取新任务' : '当前无任务占用'}
      : {kind: 'restricted', label: '暂不可用', title: '受当前调度额度限制'});
  }
  const stageLabels = {waiting_request: '等待请求间隔', waiting_cookie: '等待 Cookie', extracting: '解析视频',
    waiting_transfer: '等待传输名额', waiting_resolver: '等待解析名额', waiting_browser: '等待浏览器',
    waiting_verification: '等待网页验证', checking_environment: '检查环境', checking_login: '检查登录',
    processing: '合并 / 后处理', waiting_processing: '等待后处理', cleaning: '清理文件', uploading_oss: '上传 OSS',
    downloading: '传输状态同步中', switching_provider: '切换渠道', disk_wait: '等待磁盘空间'};
  const outside = Object.entries(allocation.outside || {});
  const outsideCount = outside.reduce((sum, [, count]) => sum + count, 0);
  const summary = (current ? [`已占用 ${occupied.length} 路`, ...(allowanceKnown
    ? [`未占用 ${allocation.unoccupied} 路`, `暂不可用 ${allocation.restricted} 路`] : ['剩余额度待确认'])]
    : ['当前占用待确认']).map(label => `<span>${label}</span>`).join('');
  return `<section class="transfer-allocation" aria-label="传输名额状态"><div class="transfer-allocation-heading"><div><h3>传输名额 <strong>${allocation.configured}<small> 路上限</small></strong></h3><p>${summary}</p></div><span class="transfer-effective">当前额度估算 <b>${allowanceKnown ? allocation.effective : '—'}</b> 路</span></div>${current && legend.size ? `<div class="transfer-legend">${[...legend].map(([label, value]) => `<span class="${value.kind}"><i></i>${label} <b>${value.count}</b></span>`).join('')}</div>` : ''}${compact ? '' : `<div class="transfer-slot-grid">${tiles.map((tile, index) => {
    const content = `<span class="transfer-slot-top"><small>名额 ${String(index + 1).padStart(2, '0')}</small><i aria-hidden="true"></i></span><strong>${tile.label}</strong><span class="transfer-slot-title">${escapeHTML(current ? tile.title || '等待任务状态更新' : '等待新鲜采样')}</span>${current && tile.cookie_name ? `<span class="transfer-slot-cookie" title="${escapeHTML(tile.cookie_name)}">${escapeHTML(tile.cookie_name)}</span>` : ''}`;
    return tile.task_id && current ? `<button type="button" class="transfer-slot ${tile.kind}" data-task="${escapeHTML(tile.task_id)}" title="${escapeHTML(tile.title || '')} · 查看任务日志">${content}</button>` : `<div class="transfer-slot ${tile.kind}">${content}</div>`;
  }).join('')}</div>`}${allocation.over_limit && current ? `<p class="capacity-waits">上限已降低，已有 ${allocation.over_limit} 路超出新上限；这些任务结束后释放名额。</p>` : ''}<p class="transfer-slot-note">${current ? '按实际预留记录展示，占用不一定正在收数据。格子随任务更新排序，点击占用中的名额查看日志。' : '实例离线或采样已过期，保留设置上限，当前各路状态待确认。'}</p>${current && outsideCount ? `<div class="transfer-outside"><strong>另有 ${outsideCount} 个视频未占用传输名额</strong><div>${outside.map(([stage, count]) => `<span>${escapeHTML(stageLabels[stage] || '准备 / 其他阶段')} <b>${count}</b></span>`).join('')}</div></div>` : ''}</section>`;
}
function youtubePacingRuntimeHTML(data, draft = {}) {
  if (!data) return '<p>正在读取当前实例的实际启动间隔…</p>';
  const pacing = data.youtube_pacing;
  const mode = pacing ? 'adaptive' : 'manual';
  const minimum = Number(pacing?.minimum_interval ?? data.youtube_start_interval ?? 0);
  const stale = Date.now() / 1000 - data.time > 20;
  const current = data.online && !stale;
  if (data.cookie_pool) {
    const shared = Number(data.youtube_cookie_pool_start_interval ?? data.cookie_allocation?.pool_start_interval ?? 5);
    const pending = (draft.shared !== undefined && draft.shared !== '' && Number(draft.shared) !== shared) ||
      (draft.minimum !== undefined && draft.minimum !== '' && Number(draft.minimum) !== minimum) ||
      (draft.mode !== undefined && draft.mode !== mode);
    const pool = data.cookie_pool;
    const next = current && pool.known && pool.next_start_at > data.time
      ? `<p>下一账号池准入窗口：${concurrencyCountdown(pool.next_start_at - data.time, data.time)}</p>` : '';
    return `<div class="youtube-pacing-runtime-heading"><strong>已保存的账号池共用启动间隔：${shared ? shared + ' 秒' : '已关闭'}</strong><span>全部 Cookie 共用</span></div><p>每个账号自身的最小间隔设置：${minimum} 秒${current && pool.known && pool.min_interval ? `；当前会话采样最短 ${Number(pool.min_interval)} 秒` : ''}。</p>${pacing ? `<p>渐进调速另行约束启动，当前记录间隔 ${Number(pacing.interval)} 秒；累计预算仍生效。</p>` : ''}${next}<p>保存后，后续领取的任务使用新配置；已预留的等待窗口保留到期。账号自身间隔、并发与冷却仍独立生效。</p>${!current || !pool.known ? '<p class="youtube-pacing-runtime-note">运行采样未更新，当前实际等待以新鲜采样为准。</p>' : ''}${pending ? '<p class="youtube-pacing-runtime-note">请求节奏有待确认的修改；上方显示已保存值，统一点击“保存设置”后生效。</p>' : ''}`;
  }
  const pending = (draft.mode !== undefined && draft.mode !== mode) ||
    (draft.minimum !== undefined && draft.minimum !== '' && Number(draft.minimum) !== minimum);
  const actual = pacing && pacing.initialized === false ? '待首次启动确认' : `${Number(pacing?.interval ?? minimum)} 秒`;
  const reason = pacing?.initialized === false ? '尚无执行记录；渐进模式首次从至少 60 秒起步。' :
    pacing?.interval_reason || (pacing ? '系统根据已保存的运行记录自动调整启动间隔。' :
      minimum ? '按已保存的固定间隔启动视频。' : '已关闭固定启动间隔。');
  const next = current && pacing?.next_start_at > data.time
    ? `<p>下一启动窗口：${concurrencyCountdown(pacing.next_start_at - data.time, data.time)}${pacing.reason ? ` · ${escapeHTML(pacing.reason)}` : ''}</p>` : '';
  const pendingText = draft.saved ? '设置已保存，等待后续调度状态更新；上方保留最近一次运行采样。' :
    '请求节奏设置尚未保存；上方显示已保存策略的运行状态，保存后在后续调度中生效。';
  return `<div class="youtube-pacing-runtime-heading"><strong>${current ? '当前实际启动间隔' : '上次记录的启动间隔'}：${actual}</strong><span>${pacing ? '渐进调速' : '固定间隔'}</span></div><p>已保存的最小间隔：${minimum} 秒</p><p>调整原因：${escapeHTML(reason)}</p>${next}${!current ? `<p class="youtube-pacing-runtime-note">${data.online ? '运行状态已过期，等待实例更新。' : '实例离线，当前实际间隔待确认。'}</p>` : ''}${pending ? `<p class="youtube-pacing-runtime-note">${pendingText}</p>` : ''}${data.cookie_pool ? '<p>Cookie 会话间隔、启动预算和冷却也可能延长等待。</p>' : ''}`;
}
function renderYouTubePacingRuntime(data = fleetEnabled
  ? state.fleet?.items.find(value => value.id === state.instanceId)?.concurrency : state.status?.concurrency) {
  const form = document.querySelector('#settings-form');
  const draft = {mode: form?.elements.youtube_pacing_mode?.value,
    minimum: form?.elements.youtube_start_interval?.value,
    shared: form?.elements.youtube_cookie_pool_start_interval?.value, saved: !settingsDirty};
  for (const target of document.querySelectorAll('[data-youtube-pacing-runtime]')) {
    if (target.closest('[hidden]')) continue;
    const html = youtubePacingRuntimeHTML(data, draft);
    if (capacityMarkupChanged(target, html)) target.innerHTML = html;
    target.dataset.capacitySampledAt = data?.time || '';
    target.classList.toggle('capacity-stale', !!data && (!data.online || Date.now() / 1000 - data.time > 20));
  }
}
function concurrencyHTML(data, {compact = false, key = 'main'} = {}) {
  if (!data) return '<p class="capacity-empty">正在读取并发与渠道状态…</p>';
  const channels = data.channels || [];
  const cooled = channels.filter(row => row.status === 'cooldown');
  const recovering = channels.filter(row => row.status === 'recovering');
  const resolverCooled = channels.filter(row => row.resolver_cooldown_seconds > 0);
  const stale = Date.now() / 1000 - data.time > 20;
  const notices = [];
  const poolCooling = Number(data.cookie_pool?.cooling_sessions || 0);
  const poolRecovering = Number(data.cookie_pool?.recovery_sessions || 0);
  const subtitles = data.subtitles || {};
  const subtitleRate = subtitles.cooldown_status ? ` HTTP ${Number(subtitles.cooldown_status)}` : '';
  const subtitleInterval = subtitles.interval_seconds ? `最少请求间隔：${concurrencyCountdown(subtitles.interval_seconds, data.time)}。` : '';
  const pacing = data.youtube_pacing;
  if (pacing) notices.push(['info', 'YouTube 渐进调速', `当前启动间隔 ${Number(pacing.interval)} 秒 · 最近一小时 ${Number(pacing.hourly_used)} / ${Number(pacing.hourly_limit)} 次 · 最近二十四小时 ${Number(pacing.daily_used)} / ${Number(pacing.daily_limit)} 次。${pacing.interval_reason ? escapeHTML(pacing.interval_reason) : ''}${pacing.next_start_at > data.time ? '下一启动窗口：' + concurrencyCountdown(pacing.next_start_at - data.time, data.time) + '。' : ''}${escapeHTML(pacing.reason || '')}。文件传输可并行，累计预算包含启动许可、重试及字幕请求。`, '#settings', '查看请求节奏']);
  if (!pacing && data.cookie_pool) notices.push(['info', '账号池请求节奏', `已保存的共用启动间隔：${Number(data.youtube_cookie_pool_start_interval ?? 5)} 秒（0 表示关闭）。${data.cookie_pool.min_interval ? `当前账号会话采样最短启动间隔 ${Number(data.cookie_pool.min_interval)} 秒。` : ''}账号与共用间隔分别生效，已开始的文件传输可并行。`, '#settings', '查看设置']);
  else if (!pacing && data.youtube_start_interval) notices.push(['info', 'YouTube 请求节奏', `本实例视频启动至少间隔 ${Number(data.youtube_start_interval)} 秒，当前 ${Number(data.youtube_waiting || 0)} 个视频等待；文件传输仍可并行。${data.youtube_source_address ? `直连源地址 ${escapeHTML(data.youtube_source_address)}。` : ''}这是本地保护配置，不是源站公开额度。`, '#settings', '查看设置']);

  if (subtitles.queued || subtitles.running || subtitles.failed) notices.push(['info', '字幕独立补下载', `待补 ${subtitles.queued || 0} · 执行中 ${subtitles.running || 0} · 补下载失败 ${subtitles.failed || 0}。${subtitleInterval}${subtitles.cooldown_seconds ? '字幕上游冷却'+subtitleRate+'：'+concurrencyCountdown(subtitles.cooldown_seconds, data.time)+'。' : ''}字幕队列不占用视频传输额度，也不改变视频成功及计费；完成当前后停止时不再领取新字幕任务。`, '#tasks', '查看执行日志']);
  if (!data.online || stale) notices.push(['unavailable', !data.online ? '实例离线' : '状态暂未更新', '下方保留最近数据，无法确认当前可用额度。请检查实例连接。', '#instances', '查看实例']);
  if (cooled.length) {
    const subtitle = cooled.some(row => row.throttle_kind === 'subtitles');
    const account = cooled.some(row => row.throttle_kind === 'account');
    const codes = [...new Set(cooled.filter(row => row.throttle_kind !== 'account').map(row => row.throttle_status).filter(Boolean))];
    const code = codes.length ? ' · HTTP ' + codes.join(' / ') : '';
    notices.push(['warning', `${account ? '账号限流冷却中' : '渠道冷却中'}${code}`, `${account ? 'YouTube 明确提示账号限流，即使登录有效也可能发生；不一定有 HTTP 429，实例会停止领取并保留任务。' : subtitle ? '字幕请求触发限流，当前与视频下载共用冷却范围。' : '上游限流或连续故障触发退避。'}${cooled.map(row => `${escapeHTML(row.label)}：${concurrencyCountdown(row.cooldown_seconds, data.time)}`).join('；')}后重新检查；到期不保证立即恢复满速。`, '#settings', '查看下载策略']);
  }
  if (recovering.length) notices.push(['warning', '渠道正在恢复', `部分资源处于恢复观察，相关共享资源暂按 1 路放行；同一时间其他资源仍可能冷却。${concurrencyCountdown(Math.max(...recovering.map(row => row.recovery_seconds)), data.time)}`, '#settings', '查看渠道']);
  if (resolverCooled.length) notices.push(['warning', '解析 / 准备资源冷却', `${resolverCooled.map(row => `${escapeHTML(row.label)}：${concurrencyCountdown(row.resolver_cooldown_seconds, data.time)}`).join('；')}。新解析受限，已解析视频的传输仍需单独检查传输额度。`, '#settings', '查看资源明细']);
  if (data.disk_blocked || data.disk_message) notices.push(['warning', '临时空间等待', `${escapeHTML(data.disk_message || '临时文件预算已用满，暂停领取新任务。')} 磁盘剩余空间与临时文件预算分别计算。`, '#settings', '查看磁盘预算']);
  const restricted = channels.filter(row => Math.min(row.transfer_limit, row.shared_limit) < data.requested);
  if (restricted.length) notices.push(['info', '渠道额度低于设置并发', `设置 ${data.requested} 路，${restricted.map(row => `${escapeHTML(row.label)} 单渠道上限 ${row.transfer_limit} 路、共享上游上限 ${row.shared_limit} 路`).join('；')}。提高工作进程数不会同步提高这些额度。`, '#settings', '查看渠道上限']);
  const connections = channels.filter(row => row.connection_units && Math.floor(Math.min(row.connection_limit ?? data.connection_budget, data.connection_budget) / row.connection_units) < data.requested);
  if (connections.length) notices.push(['info', '连接预算限制可用并发', `${connections.map(row => `${escapeHTML(row.label)} 每任务预留 ${row.connection_units} 个连接，本机预算 ${data.connection_budget}${row.connection_limit != null ? `、共享预算 ${row.connection_limit}` : ''}，理论最多约 ${Math.floor(Math.min(row.connection_limit ?? data.connection_budget, data.connection_budget) / row.connection_units)} 路`).join('；')}。文件及分片共用预算。`, '#settings', '查看连接预算']);
  const resolverWaiting = Number(data.stages?.waiting_resolver || 0);
  if (resolverWaiting) notices.push(['info', '解析名额 / 渠道配额等待', `${resolverWaiting} 个视频正在等待解析名额或共享渠道配额，尚未开始媒体传输。yt-dlp 原生解析不占浏览器名额，提高浏览器数量不会解除这类等待。`, '#settings', '查看渠道上限']);
  const browserWaiting = Object.entries(data.stages || {}).filter(([stage]) => ['waiting_browser', 'waiting_verification', 'converting'].includes(stage)).reduce((sum, [, count]) => sum + count, 0);
  if (browserWaiting) notices.push(['info', '浏览器与转换阶段占用', `等待 / 验证 / 转换中的视频 ${browserWaiting} 个，当前浏览器上限 ${data.browser_limit}。网页渠道共用浏览器名额；人工验证、转换和解析等待不等于媒体传输。`, '#settings', '查看浏览器设置']);
  if (!data.disk_sample_fresh) notices.push(['info', '临时预算状态待确认', '尚无新鲜磁盘摘要，无法确认临时预算是否阻塞；实际执行前仍会检查并预留空间。', '#settings', '查看磁盘状态']);
  if (data.global_limit) notices.push(['info', '全部实例共用总上限', `全部实例合计已占用 ${data.global_running || 0} / ${data.global_limit} 个视频名额，包含解析和合并；其他实例也会占用名额。`, '#instances', '查看全局上限']);
  if (data.mode === 'auto' && !data.sample_fresh) notices.push(['info', '等待资源采样', '自动调节数据缺失或已过期，暂按保守额度调度。', '#instances', '查看实例']);
  else if (data.mode === 'auto' && data.transfer_limit < data.requested) notices.push(['info', '自动调节尚未使用全部额度', `设置上限 ${data.requested} 路，目前调度 ${data.transfer_limit} 路。${escapeHTML(data.adjustment_reason || '系统根据资源及实际吞吐调整并发。')}`, '#instances', '查看实例']);
  if (data.paused) notices.unshift(['info', '已停止领取新任务', '正在执行的视频按当前停止方式完成或退出。', '#overview', '查看下载控制']);
  const waitText = Object.entries(data.wait_resources || {}).map(([name, count]) => `${escapeHTML(waitResourceNames[name.split(':')[0]] || '其他资源')} ${count} 个`).join(' · ');
  const warning = poolCooling || poolRecovering || cooled.length || resolverCooled.length || recovering.length || data.disk_blocked || (data.risks || []).some(item => item.severity === 'warning');
  const tone = !data.online || stale ? 'unavailable' : warning ? 'warning' : 'normal';
  const label = !data.online || stale ? '状态待确认' : cooled.length || resolverCooled.length ? '渠道冷却' : recovering.length ? '逐步恢复' : data.disk_blocked ? '空间等待' : data.paused ? '已停止领取' : poolCooling ? data.cookie_pool.available_sessions ? '部分账号冷却' : '账号池等待' : poolRecovering ? '账号低频恢复' : warning ? '有待处理提示' : '调度中';
  const sharedConnections = channels.some(row => row.connection_limit != null);
  const details = `<details class="capacity-details" data-capacity-details="${escapeHTML(key)}"><summary>渠道上限与实时占用 <span>${channels.length} 个入口 · 同组共享额度</span></summary><div class="capacity-table-wrap"><table class="capacity-table"><thead><tr><th>下载渠道 / 共享上游</th><th>渠道上限</th><th>当前额度估算</th><th>共享上游占用</th>${sharedConnections ? '<th>共享连接占用</th>' : ''}<th>状态</th></tr></thead><tbody>${channels.map(row => `<tr><td><strong>${escapeHTML(row.label)}</strong><small>${escapeHTML(row.group)}</small></td><td>${row.transfer_limit} 路<small>${row.resolver_separate ? `解析上限 ${row.resolver_limit} 路` : '解析与传输共用名额'}</small></td><td><strong>${row.effective_limit} 路</strong>${row.connection_units ? `<small>每任务预留 ${row.connection_units} 个连接</small>` : ''}</td><td>${row.occupied} / ${row.shared_limit}<small>含其他实例</small></td>${sharedConnections ? `<td>${row.connection_limit != null ? `${row.connection_used} / ${row.connection_limit}` : '—'}</td>` : ''}<td><span class="capacity-channel-state ${row.status}">${row.status === 'cooldown' ? '冷却 · '+concurrencyCountdown(row.cooldown_seconds, data.time) : row.status === 'recovering' ? '恢复观察 · '+concurrencyCountdown(row.recovery_seconds, data.time) : '可参与调度'}</span></td></tr>`).join('')}</tbody></table></div><p class="capacity-footnote">当前额度是新任务准入上界估算，综合实例、渠道、连接及共享资源限制；多个入口的额度不能直接相加。上限降低后，已有任务可能暂时超出新额度。</p></details>`;
  return `<div class="capacity-body" data-tone="${tone}">
    <div class="capacity-heading"><div><span class="capacity-eyebrow">当前实例 · 实时调度</span><h2>传输并发</h2></div>${poolRecovering ? `<button type="button" class="capacity-status ${tone}" data-cookie-recovery="" aria-haspopup="dialog" title="查看各账号的恢复条件和启动间隔调整">${label} · 查看进度</button>` : `<span class="capacity-status ${tone}" role="status">${label}</span>`}</div>
    ${transferSlotsHTML(data, {compact})}
    ${key === 'overview' ? `<details class="capacity-details capacity-explanations" data-capacity-details="${escapeHTML(key)}-explanations"><summary>调度与异常说明${warning ? ' · 有需要关注的状态' : ''}</summary>` : ''}
    ${concurrencyAdmissionHTML(data)}${decisionExplanationHTML(data, key)}
    ${waitText ? `<p class="capacity-waits">等待原因：${waitText}</p>` : ''}
    <div class="capacity-notices">${riskFactsHTML(data, compact)}${notices.map(([kind, title, description, href, action]) => `<div class="capacity-notice ${kind}"><span class="capacity-notice-icon" aria-hidden="true">${kind === 'warning' ? '!' : 'i'}</span><div><strong>${escapeHTML(title)}</strong><p>${description}</p></div><a href="/${href}">${action} ↗</a></div>`).join('') || '<p class="capacity-clear">当前未检测到共享资源冷却或磁盘阻塞；任务准备、视频大小和上游响应仍会影响实际占用。</p>'}</div>
    <p class="capacity-footnote">本机连接预留 ${data.connection_used || 0} / ${data.connection_budget || 32}（文件与分片共用，非实际套接字数）</p>
    ${compact ? '' : details + riskResourcesHTML(data, key) + riskGuideHTML(key)}
    <p class="capacity-footnote">并发与共享额度定期刷新，资源采样约每 5 秒更新。${data.sample_fresh ? '' : '资源吞吐采样尚未更新；渠道状态以上次读取为准。'}</p>
    ${key === 'overview' ? '</details>' : ''}</div>`;
}
// Retain the live filter controls and their ancestors in the document. A full
// innerHTML replacement on every sample would close an open native dropdown.
function replaceAroundAnchor(parent, anchor, nextParent, nextAnchor) {
  while (parent.firstChild !== anchor) parent.firstChild.remove();
  while (anchor.nextSibling) anchor.nextSibling.remove();
  while (nextParent.firstChild !== nextAnchor) parent.insertBefore(nextParent.firstChild, anchor);
  while (nextAnchor.nextSibling) parent.append(nextAnchor.nextSibling);
}
function updateCookieFilterPanel(target, html) {
  const controls = target.querySelector(':scope > .capacity-body > .cookie-allocation > .cookie-runtime-filters');
  if (!controls) return false;
  const template = document.createElement('template');
  template.innerHTML = html;
  const next = template.content.querySelector('.cookie-runtime-filters');
  if (!next || next.dataset.cookieFilterScope !== controls.dataset.cookieFilterScope) return false;
  for (const button of controls.querySelectorAll('[data-cookie-task-filter]')) {
    const updated = next.querySelector(`[data-cookie-task-filter="${button.dataset.cookieTaskFilter}"]`);
    button.setAttribute('aria-pressed', updated.getAttribute('aria-pressed'));
    const count = button.querySelector('[data-cookie-filter-count]');
    const text = updated.querySelector('[data-cookie-filter-count]').textContent;
    if (count.textContent !== text) count.textContent = text;
  }
  const selector = controls.querySelector('[data-cookie-account-filter]');
  const updatedSelector = next.querySelector('[data-cookie-account-filter]');
  for (const option of selector.options) {
    const text = [...updatedSelector.options].find(item => item.value === option.value).textContent;
    if (option.textContent !== text) option.textContent = text;
  }
  if (selector.value !== updatedSelector.value) selector.value = updatedSelector.value;
  controls.querySelector('[data-cookie-filters-clear]').disabled = next.querySelector('[data-cookie-filters-clear]').disabled;
  const allocation = controls.parentElement, nextAllocation = next.parentElement;
  const body = allocation.parentElement, nextBody = nextAllocation.parentElement;
  replaceAroundAnchor(allocation, controls, nextAllocation, next);
  replaceAroundAnchor(body, allocation, nextBody, nextAllocation);
  body.dataset.tone = nextBody.dataset.tone;
  return true;
}
document.addEventListener('click', event => {
  const button = event.target.closest('[data-cookie-task-filter], [data-cookie-filters-clear]');
  if (!button || button.disabled) return;
  const controls = button.closest('.cookie-runtime-filters');
  if (!controls || controls.dataset.cookieFilterScope !== cookieFilterScope()) return;
  const clear = button.hasAttribute('data-cookie-filters-clear');
  const task = button.dataset.cookieTaskFilter;
  if (!clear && !cookieTaskFilters.some(([key]) => key === task)) return;
  cookieRuntimeFilters.set(cookieFilterScope(), clear ? {task: 'all', account: 'all'}
    : {...cookieFilterSelection(), task});
  renderConcurrency();
  if (clear) controls.querySelector('[data-cookie-task-filter="all"]').focus({preventScroll: true});
});
document.addEventListener('change', event => {
  const selector = event.target.closest('[data-cookie-account-filter]');
  if (!selector || !cookieAccountFilters.some(([key]) => key === selector.value)) return;
  const controls = selector.closest('.cookie-runtime-filters');
  if (!controls || controls.dataset.cookieFilterScope !== cookieFilterScope()) return;
  cookieRuntimeFilters.set(cookieFilterScope(), {...cookieFilterSelection(), account: selector.value});
  renderConcurrency();
});
function renderConcurrency() {
  const item = state.fleet?.items.find(value => value.id === state.instanceId);
  const data = fleetEnabled ? item?.concurrency : state.status?.concurrency;
  renderYouTubePacingRuntime(data);
  if (typeof renderCookieRecovery === 'function') renderCookieRecovery(data);
  for (const target of document.querySelectorAll('[data-concurrency-panel]')) {
    if (target.closest('[hidden]')) continue;
    const html = concurrencyHTML(data, {key: target.dataset.concurrencyPanel});
    target.dataset.capacitySampledAt = data?.time || '';
    target.classList.toggle('capacity-stale', !!data && Date.now() / 1000 - data.time > 20);
    if (!capacityMarkupChanged(target, html)) continue;
    const focused = target.contains(document.activeElement) ? document.activeElement : null;
    const focusedRecovery = focused?.closest('[data-cookie-recovery]')?.dataset.cookieRecovery;
    const focusedTask = focused?.closest('[data-task]')?.dataset.task;
    const focusedDetails = focused?.tagName === 'SUMMARY' ? focused.parentElement.dataset.capacityDetails : null;
    const focusedFilter = focused?.closest('[data-cookie-task-filter]')?.dataset.cookieTaskFilter;
    const focusedAccountFilter = focused?.matches('[data-cookie-account-filter]');
    const opened = new Set([...target.querySelectorAll('details[data-capacity-details][open]')].map(node => node.dataset.capacityDetails));
    if (!updateCookieFilterPanel(target, html)) target.innerHTML = html;
    for (const node of target.querySelectorAll('details[data-capacity-details]')) node.open = opened.has(node.dataset.capacityDetails);
    if (focusedRecovery !== undefined) [...target.querySelectorAll('[data-cookie-recovery]')].find(node => node.dataset.cookieRecovery === focusedRecovery)?.focus({preventScroll: true});
    if (focusedTask) ([...target.querySelectorAll('[data-task]')]
      .find(node => node.dataset.task === focusedTask) || target.querySelector('.cookie-task-filters [aria-pressed="true"]'))?.focus({preventScroll: true});
    if (focusedDetails) [...target.querySelectorAll('details[data-capacity-details]')]
      .find(node => node.dataset.capacityDetails === focusedDetails)?.querySelector(':scope > summary')?.focus({preventScroll: true});
    if (focusedFilter) target.querySelector(`[data-cookie-task-filter="${focusedFilter}"]`)?.focus({preventScroll: true});
    if (focusedAccountFilter) target.querySelector('[data-cookie-account-filter]')?.focus({preventScroll: true});
  }
}
setInterval(() => {
  if (document.hidden) return;
  if (typeof renderCookieRecovery === 'function') renderCookieRecovery();
  for (const node of document.querySelectorAll('[data-capacity-deadline]')) {
    const left = Math.max(0, Math.ceil((Number(node.dataset.capacityDeadline) - Date.now()) / 1000));
    node.textContent = left ? `${left} 秒` : '等待状态更新';
  }
  if ([...document.querySelectorAll('[data-capacity-sampled-at]')].some(node =>
      !node.closest('[hidden]') && node.dataset.capacitySampledAt && Date.now() / 1000 - Number(node.dataset.capacitySampledAt) > 20 && !node.classList.contains('capacity-stale'))) renderConcurrency();
}, 1000);
