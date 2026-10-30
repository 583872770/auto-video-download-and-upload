'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const csrf = $('meta[name="csrf-token"]').content;
const fleetEnabled = $('meta[name="fleet-enabled"]')?.content === 'true';
const state = { config: null, media: [], tasks: [], counts: null, status: null, clash: null, selectedTask: null, logOffset: 0, logs: '', polling: false, detailBusy: false };
const initialJobId = new URLSearchParams(location.search).get('job');
let selectedJob = null;
let selectedJobId = '';
let lastStartedJobId = null;
let jobOptions = [];
let jobsCursor = null;
let jobsSearchEpoch = 0;
let jobsSearchTimer;
let clashBusy = false;
let subscriptionSelection = null;
let subscriptionOptionsSignature = '';
let subscriptionInstance;
let downloadMethodBusy = false;
let providerDraft = null;
let settingsDirty = false;
let settingsBaseline = {};
let settingsChannelBaseline = '';
let startupPolling = false;
state.startup = { state: 'idle' };
const qualityLabel = (value, provider) => !value || value === 'best' ? `尽可能最高画质${provider === 'downmp3' ? '（当前最高 1080p）' : ''}` : `${value}p`;
const providerNames = { yt_dlp: 'yt-dlp', yt5s: 'yt5s', downmp3: 'downmp3', yt1d: 'yt1d', bergung: 'Bergung / Cobalt' };
$('#settings-form [name="yt1d_timeout"]').closest('label').after($('#bergung-settings-template').content.cloneNode(true));
const browserCountLabel = $('#settings-form [name="yt5s_browser_count"]').closest('label');
$('.task-defaults-panel').after($('#channel-settings-template').content.cloneNode(true));
browserCountLabel.querySelector('small').textContent = '默认 2；自动模式会按资源与解析供给调整，并遵守此上限。Bergung 共享解析服务会复用空闲浏览器，解析完的地址在本机有界等待传输。';
browserCountLabel.dataset.providerSetting += ' bergung';
browserCountLabel.firstChild.textContent = '浏览器并发数（yt5s / yt1d / Bergung 共享）';
const selectedProviders = () => state.config?.download_methods || (state.config?.download_method === 'yt5s_fallback' ? ['yt5s', 'yt_dlp'] : [state.config?.download_method || 'yt_dlp']);
let nodeSignature = '';
let rotationDraft = null;
let rotationDirty = false;
let rotationSavedSignature = '';
let rotationNodeSignature = '';
const pages = Object.fromEntries(['tasks', 'media'].map(name => [name, { cursor: null, next: null, history: [] }]));
state.overview = { recent_media: [], recent_tasks: [], media: { count: 0 } };
state.batchTasks = [];

const actionNames = { download: '下载视频', check: '下载环境检查', import: '导入下载清单', index: '同步素材索引', clear_media: '清空素材库' };
const actionIcons = { download: 'download', check: 'server', import: 'upload', index: 'refresh' };
const statusNames = { queued: '排队中', running: '进行中', succeeded: '已完成', failed: '失败', interrupted: '已中断' };
const viewNames = {
  instances: ['下载实例', '下载实例', '集中查看各实例的任务、速度与异常；每个实例独立控制。'],
  overview: ['下载工作台', '下载工作台', '选择已有任务，开始下载并查看运行状态。'],
  media: ['素材库', '素材库', '查看当前实例的成品、封面和文件大小。'],
  tasks: ['执行日志', '执行日志', '排查单个视频或后台操作的执行结果；客户任务在任务中心管理。'],
  statistics: ['交付与账单', '交付与账单', '按客户清单累计成品大小和费用，保留每次开始与暂停的会话明细。'],
  settings: ['系统设置', '系统设置', '维护当前下载实例的下载策略、目录、登录和资源配置。'],
  clash: ['网络与代理', '网络与代理', '管理 Clash 订阅、选择下载节点，并测试延迟与连通性。'],
};

function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}
function icon(name, cls = '') { return `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`; }
function formatDate(value, full = false) {
  if (!value) return '—';
  // Schedule timestamps without a timezone represent server wall time; preserve them verbatim.
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(value)) {
    return value.slice(full ? 0 : 5, 16).replace('T', ' ').replaceAll('-', '/');
  }
  const date = new Date(value);
  return date.toLocaleString('zh-CN', { ...(full ? { year: 'numeric' } : {}), month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}
function bytes(value) {
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024).toFixed(1)} KB`;
}
function toast(message, error = false) {
  const item = document.createElement('div');
  item.className = `toast${error ? ' error' : ''}`;
  item.textContent = message;
  $('#toasts').append(item);
  setTimeout(() => item.remove(), error ? 8500 : 4500);
}
async function api(path, options = {}) {
  const headers = { 'X-CSRF-Token': csrf, ...options.headers };
  if (state.instanceId && !Object.hasOwn(headers, 'X-Instance-ID')) headers['X-Instance-ID'] = state.instanceId;
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers });
  const data = await response.json();
  if (response.status === 401) { location.assign('/login'); throw new Error('请先登录管理账户。'); }
  if (!response.ok) { const error = new Error(data.error || `请求失败（${response.status}）`); error.status = response.status; throw error; }
  return data;
}
function setView(name) {
  if (!viewNames[name]) return;
  const [label, title, description] = viewNames[name];
  $$('.view').forEach(view => { view.hidden = view.id !== `${name}-view`; });
  $$('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.view === name));
  $('#breadcrumb-current').textContent = label;
  $('#page-title').replaceChildren(document.createTextNode(title));
  $('#page-description').textContent = description;
  $('#sidebar').classList.remove('mobile-open');
  history.replaceState(null, '', `${location.search}#${name}`);
  if (typeof renderInstanceScope === 'function') renderInstanceScope();
  document.dispatchEvent(new CustomEvent('viewchange', { detail: name }));
}
function emptyState(title, description, button = false) {
  return `<div class="empty-state"><div class="empty-icon">${icon('film')}</div><div><strong>${escapeHTML(title)}</strong><p>${escapeHTML(description)}</p>${button ? `<button class="button button-white" data-view="overview">${icon('download')}开始下载视频</button>` : ''}</div></div>`;
}
function renderMedia() {
  const count = state.overview.media.count || 0;
  $('#nav-media-count').textContent = count;
  $('#all-media-count').textContent = state.mediaTotal ?? count;
  const cards = items => `<div class="media-grid">${items.map(item => `<button type="button" class="media-card" data-media="${escapeHTML(item.name)}" data-media-instance="${escapeHTML(item.instance_id || '')}" aria-label="${item.playable ? '播放' : '查看'}：${escapeHTML(item.title || item.name)}"><span class="media-image">${item.cover ? `<img src="${mediaAddress('covers', item.cover, item.instance_id)}" alt="${escapeHTML(item.title || item.name)}的封面" loading="lazy">` : icon('film')}${item.playable ? `<span class="media-play">${icon('play')}</span>` : ''}<span class="media-type">${item.playable ? escapeHTML(item.name.split('.').pop().toUpperCase()) : '待合并'}</span></span><span class="media-info"><span class="media-name" title="${escapeHTML(item.name)}">${escapeHTML(item.title || item.name)}</span><span class="media-meta"><span>${bytes(item.size)}</span><span>${item.playable ? '点击播放' : '音视频尚未合并'}</span></span></span></button>`).join('')}</div>`;
  $('#all-media').innerHTML = state.media.length ? cards(state.media) : emptyState('素材库还是空的', '下载完成后，视频与封面会出现在这里。', true);
}
function openMedia(name, instanceId) {
  const item = [...state.media, ...state.overview.recent_media].find(media => media.name === name && (!instanceId || media.instance_id === instanceId));
  if (!item) return;
  const player = $('#media-player');
  const warning = $('#media-player-error');
  $('#media-dialog-title').textContent = item.title || item.name;
  $('#media-player-filename').textContent = item.name;
  warning.hidden = item.playable;
  warning.textContent = item.playable ? '' : '音视频尚未合并，请检查下载任务日志和 FFmpeg 安装情况。';
  player.hidden = !item.playable;
  const url = mediaAddress('videos', item.name, item.instance_id);
  $('#media-download').href = url;
  $('#media-download').download = item.name;
  if (item.cover) player.poster = mediaAddress('covers', item.cover, item.instance_id);
  else player.removeAttribute('poster');
  if (item.playable) player.src = url;
  $('#media-dialog').showModal();
  if (item.playable) player.play().catch(() => {});
}
function taskRows(tasks) {
  return tasks.map(task => `<button class="task-row" data-task="${task.id}"><span class="task-action-icon">${icon(actionIcons[task.action] || 'list')}</span><span class="task-row-text"><strong>${escapeHTML(task.progress?.title || task.video_id || actionNames[task.action] || task.action)}</strong><small>${['download', 'import'].includes(task.action) ? progressLabel(task) : task.action === 'index' ? '同步素材索引' : '本机工具'} · ${task.id.slice(0, 8)}${task.instance_id ? ' &middot; ' + escapeHTML(instanceName(task.instance_id)) : ''}</small></span><span class="task-status status-${task.status}">${taskStatus(task)}</span><time class="task-time" title="${task.finished_at ? '结束时间' : '创建时间'}">${escapeHTML(formatDate(task.finished_at || task.created_at))}</time>${icon('arrow', 'task-arrow')}</button>`).join('');
}
function taskPaused(task) {
  if (task.action !== 'download' || task.status !== 'queued') return false;
  const item = state.fleet?.items.find(item => item.id === task.instance_id);
  return task.instance_id && task.instance_id !== state.instanceId ? !!item?.paused : !!state.workbench?.paused;
}
function taskStatus(task) {
  return taskPaused(task) ? '已暂停' : statusNames[task.status] || task.status;
}
function etaText(seconds) {
  if (!Number.isFinite(seconds)) return '—';
  const value = Math.ceil(seconds);
  return value >= 3600 ? `${Math.floor(value / 3600)}小时${Math.floor(value % 3600 / 60)}分` : value >= 60 ? `${Math.floor(value / 60)}分${value % 60}秒` : `${value}秒`;
}
function progressLabel(task) {
  if (taskPaused(task)) return '已暂停 · 等待继续';
  if (task.status === 'queued' && task.progress?.stage === 'paused') return '等待继续下载';
  if (task.status === 'queued') return task.progress?.stage === 'retrying' ? '等待退避重试' : '排队中';
  if (task.action === 'import' && task.status === 'running') return `正在导入 · 新增 ${task.progress?.added || 0} · 重复 ${task.progress?.duplicates || 0} · 无效 ${task.progress?.invalid || 0}`;
  if (task.status === 'failed') return '下载失败';
  if (task.status === 'interrupted') return '已中断';
  if (task.status === 'succeeded') return task.result?.test_mode ? '测试完成 · 文件已删除' : task.result?.skipped ? '已下载过 · 已跳过' : '下载完成';
  const stage = task.progress?.stage;
  if (stage === 'preparing_workspace') return '准备保存目录';
  if (stage === 'disk_wait') return '等待磁盘空间或临时文件预算';
  if (stage === 'preparing_proxy') return '准备代理节点';
  if (stage === 'requesting_api') return 'downmp3 · 提交转换请求';
  if (stage === 'waiting_api_conversion') return 'downmp3 · 远端转换中（尚未下载）';
  if (stage === 'switching_provider') return `正在切换到 ${providerNames[task.progress?.download_method] || '下一个下载器'}`;
  const browserStages = { waiting_browser: '等待浏览器名额', starting_browser: '启动浏览器', connecting_browser: '连接浏览器', opening_site: '打开网页', waiting_site_form: '等待网页输入框', submitting_site: '提交视频链接', waiting_verification: '等待网站验证', converting: '网站正在转换' };
  if (browserStages[stage]) return `${providerNames[task.progress?.download_method] || '下载器'} · ${browserStages[stage]}`;
  return { checking_environment: '检查下载环境', waiting_browser: 'yt5s · 等待浏览器名额', starting_browser: 'yt5s · 启动浏览器', connecting_browser: 'yt5s · 连接浏览器', opening_site: 'yt5s · 打开网页', waiting_site_form: 'yt5s · 等待网页输入框', submitting_site: 'yt5s · 提交视频链接', waiting_verification: 'yt5s · 等待网站验证', converting: 'yt5s · 网站正在转换', checking_login: '正在检测 YouTube 登录', extracting: '正在解析视频', waiting_transfer: '已解析 · 等待传输名额', downloading: '正在下载', processing: '合并 / 处理文件', cleaning: '测试下载完成 · 正在清理文件', skipped: '已下载过 · 已跳过', completed: '下载完成' }[stage] || '准备下载';
}
function downloadProgress(task) {
  const data = task.progress || {};
  const active = task.status === 'running';
  const queued = task.status === 'queued';
  const stopped = task.status === 'queued' && data.stage === 'paused';
  const converting = active && data.stage === 'waiting_api_conversion';
  const measured = converting ? data.provider_percent : data.percent;
  const percentage = task.status === 'queued' && !stopped ? 0 : task.status === 'succeeded' ? 100 : Number.isFinite(measured) ? Math.max(0, Math.min(100, measured)) : null;
  const label = task.status === 'queued' && !stopped ? '等待开始' : percentage === null ? (converting ? '等待远端转换' : '大小未知') : `${converting ? '转换 ' : ''}${percentage.toFixed(1)}%`;
  const importing = task.action === 'import';
  const speed = !active ? '—' : importing && Number.isFinite(data.records_per_second) ? `${data.records_per_second.toLocaleString('zh-CN', { maximumFractionDigits: 0 })} 条/秒` : Number.isFinite(data.speed) ? `${bytes(data.speed)}/s` : data.stage === 'downloading' ? '计算中…' : '—';
  const provider = providerNames[data.download_method] || '';
  const stageSeconds = active && data.stage_started_at ? Math.max(0, Date.now() / 1000 - data.stage_started_at) : null;
  const slowest = Object.entries(data.stage_seconds || {}).filter(([stage]) => !active || stage !== data.stage).sort((a, b) => b[1] - a[1])[0];
  const stageName = stage => progressLabel({ action: 'download', status: 'running', progress: { stage } });
  const timing = stageSeconds !== null || slowest ? `<p class="micro-copy stage-timing">${stageSeconds !== null ? `本阶段已用 ${etaText(stageSeconds)}` : `${queued ? '上次' : '本次'}执行已记录用时 ${etaText(data.elapsed || 0)}`}${slowest ? ` · ${active ? '已结束阶段中最久' : '耗时最长阶段'}：${escapeHTML(stageName(slowest[0]))} ${etaText(slowest[1])}` : ''}</p>` : '';
  const methods = data.selected_download_methods;
  const policy = Array.isArray(methods) && methods.length ? `<p class="micro-copy execution-policy">${queued ? '上次' : '本次'}执行顺序：${escapeHTML(methods.map(method => providerNames[method] || method).join(' → '))}${queued ? '；重新领取时读取执行实例的最新设置。' : ''}${task.instance_id && fleetEnabled ? ` <a href="/?instance_id=${encodeURIComponent(task.instance_id)}#settings">配置执行实例：${escapeHTML(instanceName(task.instance_id))} ↗</a>` : ''}</p>` : '';
  return `<div class="download-meter"><div class="download-meter-label"><span>${escapeHTML(progressLabel(task))}${active && data.stage === 'downloading' && data.format ? ` · ${escapeHTML(data.format)}` : ''}</span><strong>${label}</strong></div><progress max="100"${percentage === null ? '' : ` value="${percentage}"`} aria-label="${importing ? '导入' : '下载'}进度"></progress><div class="download-metrics"><span>${importing ? '已读取' : queued ? '上次已下载' : '已下载'} <b>${data.downloaded_bytes ? bytes(data.downloaded_bytes) : '—'}</b>${data.total_bytes ? ` / ${bytes(data.total_bytes)}` : ''}</span><span>${importing ? '处理速度' : '速度'} <b>${speed}</b></span><span>${importing ? '预计剩余' : '当前文件剩余'} <b>${active ? etaText(data.eta) : '—'}</b></span>${provider ? `<span>${queued ? '上次下载器' : '下载器'} <b>${provider}${data.fallback_used ? '（已自动切换）' : ''}</b></span>` : ''}${data.proxy_node ? `<span>${queued ? '上次节点' : '节点'} <b>${escapeHTML(data.proxy_node)}</b></span>` : ''}</div>${policy}${timing}${data.fallback_reason ? `<p class="micro-copy">${escapeHTML(data.fallback_from || "上一下载器")} 失败原因：${escapeHTML(data.fallback_reason)}</p>` : ''}</div>`;
}
function renderBatches() {
  const tasks = state.batchTasks.filter(task => task.batch_id === selectedJobId);
  if (!selectedJobId) {
    $('#batch-count').textContent = '0';
    $('#batch-summary').textContent = '此处显示所选任务的实时执行情况。';
    $('#batch-progress').innerHTML = emptyState('请选择下载任务', '任务视频在任务中心准备；选择任务后可以开始下载。');
    return;
  }
  const queue = state.workbench || {};
  $('#batch-count').textContent = tasks.length;
  $('#batch-summary').textContent = `${selectedJob?.name || '所选任务'} · 正在下载 ${tasks.length} 个 · 待下载 ${selectedJob?.queued || 0} 个`;
  $('#batch-progress').innerHTML = tasks.length ? tasks.map((task, index) => `<article class="batch-video" data-download-id="${task.id}"><div class="batch-video-heading"><span class="batch-video-index">${index + 1}</span><div class="batch-video-title"><strong title="${escapeHTML(task.progress?.title || task.url || '')}">${escapeHTML(task.progress?.title || task.video_id || task.url || '视频')}</strong><small>${escapeHTML(task.video_id || task.id.slice(0, 8))}${task.instance_id ? ' &middot; ' + escapeHTML(instanceName(task.instance_id)) : ''}</small></div><button type="button" class="text-button" data-task="${task.id}">查看日志 ↗</button></div>${downloadProgress(task)}${task.error ? `<p class="batch-video-error">${escapeHTML(readableError(task.error))}</p>` : ''}</article>`).join('') : emptyState('当前没有正在下载的视频', state.workbench?.paused ? '选择任务并开始后，这里会显示实时进度。' : '此任务执行时在这里显示进度；视频结果在任务详情查看。');
  renderPagination();
}
function renderPagination() {
  for (const [name, page] of Object.entries(pages)) {
    const prev = $(`[data-page="${name}"][data-direction="prev"]`);
    const next = $(`[data-page="${name}"][data-direction="next"]`);
    if (prev) prev.disabled = !page.history.length;
    if (next) next.disabled = !page.next;
    const label = $(`#${name}-page-label`);
    if (label) label.textContent = `第 ${page.history.length + 1} 页`;
  }
}
function resetPage(name) { pages[name] = { cursor: null, next: null, history: [] }; }
function listQuery(name, params = {}) {
  const values = new URLSearchParams(params);
  if (pages[name].cursor) values.set('cursor', pages[name].cursor);
  return values.toString();
}

function decimalGB(value) {
  const amount = Number(value || 0);
  return amount > 0 && amount < 1000 ? '< 0.000001 GB' : `${(amount / 1e9).toLocaleString('zh-CN', { maximumFractionDigits: 6 })} GB`;
}
function renderTasks() {
  const counts = state.overview.download_counts || {};
  $('#stat-active').textContent = selectedJob?.queued || 0;
  $('#stat-running').textContent = state.workbench?.running || 0;
  $('#stat-complete').textContent = selectedJob?.file_count || 0;
  $('#nav-task-count').textContent = state.counts ? ['succeeded', 'failed', 'interrupted'].reduce((sum, status) => sum + (state.counts[status] || 0), 0) : state.tasks.length;
  $('#active-caption').textContent = state.workbench?.paused ? '当前实例已停止领取' : '选择任务后开始下载';
  $('#all-tasks').innerHTML = state.tasks.length ? taskRows(state.tasks) : emptyState('暂无已结束的任务', '可调整搜索条件；任务结束后会自动显示在这里。');
  renderBatches();
}
function renderStatus() {
  if (!state.status) return;
  const { tools } = state.status;
  if (!fleetEnabled) renderChannelPerformance(state.status.throughput);
  const methods = selectedProviders();
  const methodFour = methods.length === 1 && methods[0] === 'downmp3';
  const browserReady = !methods.some(method => ['yt5s', 'yt1d', 'bergung'].includes(method)) || (tools.yt5s_browser && tools.yt5s_display && tools.playwright);
  const nativeReady = !methods.includes('yt_dlp') || (tools.ffmpeg && tools.ffprobe && (tools.node || tools.deno));
  const needsProbe = methods.some(method => ['downmp3', 'yt1d', 'bergung'].includes(method));
  const ready = browserReady && nativeReady && (!needsProbe || tools.ffprobe);
  $('.environment-card').classList.toggle('warning', !ready);
  $('#environment-title').innerHTML = `${ready ? '工具已就绪' : '待完善'}<span class="status-dot"></span>`;
  $('#environment-note').textContent = !browserReady ? '缺少网页下载器所需浏览器或显示环境' : !nativeReady ? '缺少 yt-dlp 所需 FFmpeg 或 JS 运行时' : needsProbe && !tools.ffprobe ? '缺少 ffprobe' : methodFour ? 'API 下载 · 无需浏览器和 JS 运行时' : `Python ${state.status.python}`;
  renderYouTubeLogin();
  $('#database-health').textContent = `数据库：${state.status.database === 'mysql' ? 'MySQL' : 'SQLite 测试模式'} · 最近请求成功`;
  $('#proxy-health').textContent = state.config?.proxy_mode === 'clash' ? `代理：Mihomo · ${state.clash?.running ? '控制接口可用' : '尚未确认内核状态'}（不代表下载链路已验证）` : state.config?.proxy_mode === 'manual' && state.config?.proxy ? '代理：手动代理（未探测连通性）' : '连接：直连（未探测外网连通性）';
  renderDownloadMethod();
  $$('[data-youtube-status]').forEach(element => {
    const auth = state.status.youtube?.auth;
    element.textContent = auth?.state === 'valid' ? 'YouTube 登录已验证' : !state.status.youtube?.saved ? '尚未登录 YouTube' : auth?.state === 'unchecked' ? 'Cookie 已保存 · 待检测登录' : '登录检查未通过';
  });
  const auth = state.status.youtube?.auth;
  $('#youtube-auth-status').textContent = auth?.message || '供 yt-dlp 任务使用；其他下载器不需要此 Cookie。';
  $('#youtube-auth-status').classList.toggle('error', !!auth && !['valid', 'unchecked'].includes(auth.state));
  $('#youtube-auth-check').disabled = authChecking || !!state.status.youtube?.login?.active;
  $('#youtube-auth-check').textContent = authChecking ? '正在检测…' : '检测登录';
}
let authChecking = false;
$('#youtube-auth-check').addEventListener('click', async () => {
  if (authChecking) return;
  authChecking = true;
  renderStatus();
  try {
    const result = await api('/api/youtube/auth/check', { method: 'POST' });
    if (state.status?.youtube) state.status.youtube.auth = result.auth;
    toast(result.message);
  } catch (error) { toast(error.message, true); }
  finally { authChecking = false; await refresh(); renderStatus(); }
});
let youtubeLoginBusy = false;
function renderYouTubeLogin() {
  const login = state.status?.youtube?.login;
  const busy = youtubeLoginBusy || !!login?.active;
  const needsStop = !!state.status?.connection_busy;
  $('#youtube-login-start').disabled = !login || busy || needsStop;
  $('#youtube-login-start').textContent = login?.active ? '登录进行中…' : login?.state === 'idle' ? '打开登录窗口' : '重新打开登录窗口';
  $('#youtube-login-cancel').hidden = !login?.active;
  $('#youtube-login-cancel').disabled = youtubeLoginBusy || ['cancelling', 'committing'].includes(login?.state);
  const status = $('#youtube-login-status');
  status.textContent = needsStop && !login?.active ? '请先停止全部下载，等待当前下载结束后再打开登录窗口。' : login?.message || '正在读取登录状态…';
  status.classList.toggle('error', ['failed', 'interrupted'].includes(login?.state));
  status.classList.toggle('success', login?.state === 'succeeded');
  const connection = state.clash?.mode === 'clash' ? `Clash · ${state.clash.selected || '尚未选择节点'}` : state.config?.proxy_mode === 'manual' && state.config?.proxy ? '手动代理' : '直连';
  $('#youtube-login-connection').textContent = `登录连接：${login?.active ? login.connection : connection}`;
  $$('[data-youtube-login-summary]').forEach(element => {
    element.hidden = !login || login.state === 'idle';
    element.textContent = login?.message || '';
  });
  if (login?.state === 'succeeded' && state.config) state.config.youtube_cookie = 'Cookies/youtube.txt';
}
async function youtubeLoginAction(action) {
  if (youtubeLoginBusy) return;
  youtubeLoginBusy = true;
  renderYouTubeLogin();
  try {
    const login = await api(`/api/youtube/login${action === 'cancel' ? '/cancel' : ''}`, { method: 'POST' });
    if (state.status?.youtube) state.status.youtube.login = login;
    renderYouTubeLogin();
    await refresh();
  } catch (error) { toast(error.message, true); }
  finally { youtubeLoginBusy = false; renderYouTubeLogin(); }
}
$('#youtube-login-start').addEventListener('click', () => youtubeLoginAction('start'));
$('#youtube-login-cancel').addEventListener('click', () => youtubeLoginAction('cancel'));

function renderClash() {
  const data = state.clash;
  if (!data) return;
  const testing = data.testing.running;
  renderRotation(data);
  renderSubscriptions(data);
  $('#clash-core-status').textContent = data.running ? '代理运行中' : data.installed ? '代理已停止' : '内核未安装';
  $('#proxy-mode-select').value = data.mode;
  $('#proxy-mode-select').disabled = clashBusy || (data.connection_edit_locked ?? data.locked);
  $('#clash-selected').textContent = data.selected || '尚未选择节点';
  $('#clash-connection-info').textContent = (data.mode === 'clash' ? '待处理任务开始时使用 Clash 当前节点或轮换配置。' : data.mode === 'direct' ? '待处理任务开始时直接连接网络。' : '待处理任务开始时使用“下载设置”中的当前手动代理地址。') + '连接设置保存后自动生效，无需重新导入清单。';
  $('#clash-apply-connection').disabled = clashBusy || data.connection_apply_locked;
  const applied = data.download_connection;
  const appliedMode = applied?.mode === 'clash' ? 'Clash' : applied?.mode === 'manual' ? '手动代理' : '直连';
  $('#clash-apply-note').textContent = clashBusy ? '正在处理代理操作，请稍候…'
    : (applied ? `上次检查：${formatDate(applied.updated_at, true)} · ${appliedMode}。` : '')
      + (data.connection_apply_reason || '待处理任务自动使用当前连接；检查不会启动下载。');
  $('#clash-lock-note').hidden = !data.locked;
  $('#clash-error').hidden = !data.error;
  $('#clash-error').textContent = data.error || '';
  const subscriptionLocked = data.subscription_locked ?? data.locked;
  $('#clash-import').disabled = clashBusy || subscriptionLocked || testing;
  $('#clash-subscription-select').disabled = clashBusy;
  $('#clash-subscription-new').disabled = clashBusy;
  $('#clash-subscription-name').disabled = clashBusy;
  $('#clash-subscription-url').disabled = clashBusy;
  $('#clash-subscription-switch').disabled = clashBusy || subscriptionLocked || testing || !subscriptionSelection || subscriptionSelection === data.active_subscription_id;
  $('#clash-subscription-delete').disabled = clashBusy || subscriptionLocked || testing || !subscriptionSelection || subscriptionSelection === data.active_subscription_id;
  $('#clash-subscription-note').textContent = clashBusy ? '正在处理代理操作，请稍候…'
    : testing ? '节点正在测速，请等待测速完成后更新订阅。'
    : subscriptionLocked ? data.subscription_lock_reason || '请先停止全部下载并等待当前任务结束，再更新订阅。'
    : subscriptionSelection ? '链接留空可更新所选订阅。切换使用已保存的节点；更新或切换失败时保留当前配置。'
    : '首个订阅保存后自动启用；新增备用订阅后可手动切换。最多保存 20 个订阅。';
  $('#clash-start').disabled = clashBusy || data.locked || data.running || !data.nodes.length;
  $('#clash-stop').disabled = clashBusy || data.locked || testing || !data.running;
  $('#clash-test-youtube').disabled = clashBusy || !data.running || !data.selected;
  $('#clash-test-all').disabled = clashBusy || testing || !data.running || !data.nodes.length;
  $('#clash-test-progress').textContent = testing ? `正在测速 ${data.testing.done} / ${data.testing.total}，结果自动更新…` : data.testing.total ? `测速完成 ${data.testing.done} / ${data.testing.total} · 超时表示该节点暂时无法连通测试地址` : '延迟测试不会切换当前节点；测试结果不代表实际下载速度。';
  const query = $('#clash-node-search').value.trim().toLowerCase();
  let nodes = data.nodes.filter(node => `${node.name} ${node.type}`.toLowerCase().includes(query));
  const order = $('#clash-node-sort').value;
  if (order === 'delay') nodes = [...nodes].sort((a, b) => (a.delay?.ms ?? Infinity) - (b.delay?.ms ?? Infinity));
  if (order === 'name') nodes = [...nodes].sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'));
  $('#clash-node-count').textContent = query ? `${nodes.length} / ${data.nodes.length}` : data.nodes.length;
  const signature = JSON.stringify([nodes, data.selected, data.locked, data.running, testing, clashBusy]);
  if (signature === nodeSignature) return;
  nodeSignature = signature;
  $('#clash-nodes').innerHTML = nodes.length ? `<div class="clash-node-grid">${nodes.map(node => {
    const selected = node.name === data.selected;
    const delay = node.cooling_until > Date.now() / 1000 ? `线路冷却 ${Math.ceil(node.cooling_until - Date.now() / 1000)} 秒` : node.delay ? node.delay.ms === null ? '超时 / 不可用' : `${node.delay.ms} ms` : '未测速';
    return `<article class="clash-node${selected ? ' selected' : ''}"><div class="clash-node-heading"><strong title="${escapeHTML(node.name)}">${escapeHTML(node.name)}</strong>${selected ? '<span class="count-pill">当前节点</span>' : ''}</div><div class="clash-node-meta"><span>${escapeHTML(node.type.toUpperCase())}</span><span class="${node.delay?.ms ? 'delay-ok' : node.delay ? 'delay-failed' : ''}" title="${node.delay ? escapeHTML(formatDate(node.delay.checked_at, true)) : ''}">${delay}</span></div><div class="clash-node-actions"><button type="button" class="text-button" data-node-delay="${escapeHTML(node.name)}"${clashBusy || testing || !data.running ? ' disabled' : ''}>测试延迟</button><button type="button" class="button ${selected ? 'button-dark' : 'button-white'}" data-node-select="${escapeHTML(node.name)}"${clashBusy || data.locked || selected ? ' disabled' : ''}>${selected ? '使用中' : '使用此节点'}</button></div></article>`;
  }).join('')}</div>` : emptyState(data.nodes.length ? '没有匹配的节点' : '还没有订阅节点', data.nodes.length ? '试试其他名称或协议。' : '在上方粘贴订阅链接，保存后即可选择节点。');
}
function renderSubscriptions(data) {
  const profiles = data.subscriptions || [];
  if (subscriptionInstance !== state.instanceId) {
    subscriptionInstance = state.instanceId;
    subscriptionSelection = null;
    subscriptionOptionsSignature = '';
    $('#clash-subscription-url').value = '';
  }
  if (subscriptionSelection === null || (subscriptionSelection && !profiles.some(item => item.id === subscriptionSelection))) {
    subscriptionSelection = data.active_subscription_id || '';
    $('#clash-subscription-name').value = profiles.find(item => item.id === subscriptionSelection)?.name || '';
    $('#clash-subscription-url').value = '';
  }
  const signature = JSON.stringify([profiles, data.active_subscription_id]);
  if (signature !== subscriptionOptionsSignature) {
    $('#clash-subscription-select').innerHTML = profiles.map(item => `<option value="${escapeHTML(item.id)}">${escapeHTML(item.name)}${item.id === data.active_subscription_id ? '（当前使用）' : ''}</option>`).join('') + '<option value="">＋ 新增订阅</option>';
    subscriptionOptionsSignature = signature;
  }
  $('#clash-subscription-select').value = subscriptionSelection;
  const selected = profiles.find(item => item.id === subscriptionSelection);
  const active = profiles.find(item => item.id === data.active_subscription_id);
  $('#clash-subscription-badge').textContent = profiles.length ? `已保存 ${profiles.length} 个` : '未配置';
  $('#clash-subscription-url').required = !selected;
  $('#clash-subscription-url').placeholder = selected ? '留空更新已保存链接；填写新链接可替换此订阅' : '粘贴 Clash / Mihomo 订阅链接';
  $('#clash-import').lastChild.textContent = selected ? '保存并更新此订阅' : '保存新订阅';
  $('#clash-subscription-info').textContent = (active ? `当前使用：${active.name}。` : '') + (selected ? `${selected.host} · ${selected.node_count} 个节点 · 更新于 ${formatDate(selected.updated_at, true)}` : '可保存多个订阅并手动切换。') + ' 链接仅保存在当前实例，保存后不回显。' + (selected?.id === data.active_subscription_id && data.notices?.length ? '\n' + data.notices.join(' · ') : '');
}
function chooseSubscription(identity) {
  subscriptionSelection = identity;
  $('#clash-subscription-name').value = state.clash?.subscriptions?.find(item => item.id === identity)?.name || '';
  $('#clash-subscription-url').value = '';
  renderClash();
}
function renderRotation(data) {
  const saved = data.rotation || { enabled: false, nodes: [], every: 1, count_failures: true };
  const signature = JSON.stringify([saved.id, saved.enabled, saved.nodes, saved.every, saved.count_failures]);
  if (!rotationDraft || (!rotationDirty && rotationSavedSignature !== signature)) {
    rotationDraft = { enabled: saved.enabled, nodes: [...saved.nodes], every: saved.every, count_failures: saved.count_failures };
    $('#rotation-enabled').checked = rotationDraft.enabled;
    $('#rotation-every').value = rotationDraft.every;
    $('#rotation-count-failures').checked = rotationDraft.count_failures;
    rotationSavedSignature = signature;
  }
  const locked = clashBusy || (data.rotation_locked ?? data.locked);
  $$('#clash-rotation-form input, #rotation-save').forEach(input => { input.disabled = locked; });
  $('#rotation-save').disabled = locked || !data.nodes.length;
  $('#rotation-badge').textContent = saved.enabled ? data.mode === 'clash' ? '已开启' : '已保存 · Clash 模式生效' : '未开启';
  $('#rotation-draft-note').textContent = data.rotation_locked ? data.rotation_lock_reason : rotationDirty ? '有未保存的修改' : '保存后对待下载任务生效';
  $('#rotation-sequence').textContent = rotationDraft.nodes.length ? `轮换顺序：${rotationDraft.nodes.map((name, i) => `${i + 1}. ${name}`).join(' → ')}` : '请勾选至少两个节点。';
  $('#rotation-status').textContent = saved.enabled ? data.mode !== 'clash' ? '当前连接方式未使用 Clash，切换到 Clash 订阅代理后轮换才会生效。' : `${saved.worker_count > 1 ? '各进程独立计数 · 主进程：' : ''}当前配置已计数 ${saved.completed || 0} 个视频 · ${data.locked ? '当前轮换节点' : '下次下载使用'} ${saved.next_node} · ${saved.count_failures ? `再处理 ${saved.remaining} 个视频后轮换（含失败）` : `再成功下载 ${saved.remaining} 个视频后轮换`}` : '开启后可在所选节点之间自动循环。';
  const nodeSignature = JSON.stringify([data.nodes.map(node => [node.name, node.delay]), rotationDraft.nodes, locked]);
  if (nodeSignature === rotationNodeSignature) return;
  rotationNodeSignature = nodeSignature;
  $('#rotation-nodes').innerHTML = data.nodes.length ? data.nodes.map(node => {
    const index = rotationDraft.nodes.indexOf(node.name);
    const delay = node.cooling_until > Date.now() / 1000 ? `线路冷却 ${Math.ceil(node.cooling_until - Date.now() / 1000)} 秒` : node.delay ? node.delay.ms === null ? '超时 / 不可用' : `${node.delay.ms} ms` : '未测速';
    const delayClass = node.delay?.ms ? 'delay-ok' : node.delay ? 'delay-failed' : '';
    const checkedAt = node.delay ? escapeHTML(formatDate(node.delay.checked_at, true)) : '';
    return `<label class="rotation-node${index >= 0 ? ' checked' : ''}"><input type="checkbox" data-rotation-node="${escapeHTML(node.name)}"${index >= 0 ? ' checked' : ''}${locked ? ' disabled' : ''}><span>${escapeHTML(node.name)}</span><small class="rotation-node-delay ${delayClass}" title="${checkedAt}">${delay}</small>${index >= 0 ? `<b>${index + 1}</b>` : ''}</label>`;
  }).join('') : '<p class="micro-copy">导入订阅后可选择轮换节点。</p>';
}
async function clashAction(operation) {
  if (clashBusy) return;
  clashBusy = true;
  const instanceSelector = $('#fleet-selection');
  if (instanceSelector) instanceSelector.disabled = true;
  renderClash();
  try { await operation(); }
  catch (error) { toast(error.message || '代理操作失败，请稍后重试。', true); }
  finally {
    clashBusy = false;
    if (instanceSelector) instanceSelector.disabled = false;
    try { await refresh(); } catch (error) { toast(error.message, true); }
    renderClash();
  }
}
function fillConfig(config) {
  state.config = config;
  const form = $('#settings-form');
  if (settingsDirty) { renderDownloadMethod(); return; }
  settingsBaseline = {};
  for (const element of $$('input[name], select[name], textarea[name]', form)) {
    const value = config[element.name];
    element.value = Array.isArray(value) ? value.join('\n') : value ?? '';
    settingsBaseline[element.name] = element.value;
  }
  renderChannels(config.download_channels || []);
  settingsChannelBaseline = channelSignature(config.download_channels || []);
  renderStorageSettings();
  renderDownloadMethod();
}
const channelFields = [['id','渠道标识'],['label','名称'],['site_url','网站地址'],['api_url','API 地址'],['upstream_group','带宽组'],['resolver_group','解析组'],['quota_group','配额组'],['transfer_limit','同组传输上限'],['resolver_limit','同组解析上限'],['connection_limit','同组媒体连接上限'],['task_limit','此入口任务上限'],['weight','分配权重'],['verification','网站验证流程']];
const channelNumbers = {transfer_limit:64,resolver_limit:16,connection_limit:128,task_limit:64,weight:10};
function renderChannels(entries) {
  $('#channel-entries').innerHTML = entries.map(item => `<fieldset class="channel-entry"><div class="scale-fields">${channelFields.map(([key,label])=>`<label>${label}${key === 'verification' ? `<select data-channel-field="verification"><option value="auto"${item[key]==='auto'?' selected':''}>由网站自动处理（推荐）</option><option value="required"${!item[key]||item[key]==='required'?' selected':''}>先等待验证控件</option><option value="none"${item[key]==='none'?' selected':''}>直接提交网站</option></select>` : `<input data-channel-field="${key}" value="${escapeHTML(item[key] ?? (key==='task_limit'?8:''))}" ${channelNumbers[key]?`type="number" min="1" max="${channelNumbers[key]}"`:'type="text"'} required>`}</label>`).join('')}</div><button type="button" class="text-button" data-channel-remove>移除此入口</button></fieldset>`).join('');
}
function readChannels() {
  return $$('.channel-entry').map(row => Object.fromEntries([['adapter','bergung'], ...channelFields.map(([key]) => {
    const input = $(`[data-channel-field="${key}"]`, row);
    return [key, channelNumbers[key] ? Number(input.value) : input.value.trim()];
  })]));
}
function channelSignature(entries) {
  return JSON.stringify(entries.map(item => channelFields.map(([key]) => {
    const value = item[key] ?? (key==='task_limit'?8:key==='verification'?'required':'');
    return ['site_url','api_url'].includes(key) && value ? String(value).replace(/\/+$/, '') + '/' : value;
  })));
}
function settingsValue(key, value) {
  if (key === 'download_channels') return channelSignature(value || []);
  if ($(`#settings-form [name="${key}"]`)?.type === 'number') return String(Number(value));
  return Array.isArray(value) ? JSON.stringify(value) : String(value ?? '');
}
function unconfirmedSettings(data, config) {
  return Object.keys(data).filter(key => settingsValue(key, data[key]) !== settingsValue(key, config[key]));
}
function renderChannelPerformance(rate) {
  $('#channel-performance').textContent = rate ? `媒体接收 ${rate.media_receive_mbps ?? '—'} Mbps · 校验成品 ${rate.verified_mbps ?? '—'} Mbps（滚动窗口） · ${rate.reason || '等待采样'}` : '等待实例资源采样。';
  $('#channel-rates').replaceChildren(...Object.values(rate?.channels || {}).filter(item => item.receive_mbps || item.succeeded || item.failed || item.resolved).map(item => {
    const line = document.createElement('p');
    line.textContent = `${item.label}：接收 ${item.receive_mbps} Mbps · 成功 ${item.succeeded} · 失败 ${item.failed}${item.resolve_seconds == null ? '' : ` · 平均解析 ${item.resolve_seconds} 秒`}（当前滚动窗口）`;
    return line;
  }));
}
$('#channel-add').addEventListener('click', () => {
  const entries = readChannels();
  if (entries.length >= 16) { toast('最多配置 16 个渠道。', true); return; }
  let suffix = entries.length + 1;
  while (entries.some(entry => entry.id === `bergung_${suffix}`)) suffix++;
  entries.push({id:`bergung_${suffix}`,label:`Bergung ${suffix}`,site_url:'https://bergung.hoffnungfuerdiezukunft.net/',api_url:'https://bergung-api.hoffnungfuerdiezukunft.net/',upstream_group:'unverified-media',resolver_group:'bergung',quota_group:'bergung',transfer_limit:8,resolver_limit:4,connection_limit:16,task_limit:4,weight:1,verification:'auto'});
  renderChannels(entries); settingsDirty = true;
});
$('#cobalt-catalog-add').addEventListener('click', () => {
  const option = $('#cobalt-catalog-choice').selectedOptions[0];
  if (!option) return;
  const entry = JSON.parse(option.dataset.channel);
  const entries = readChannels();
  if (entries.some(item => item.id === entry.id || item.api_url === entry.api_url)) { toast('这个后台已经在当前实例的渠道中。'); return; }
  if (entries.length >= 16) { toast('最多配置 16 个渠道。', true); return; }
  entries.push(entry); renderChannels(entries); settingsDirty = true;
  const methods = providerDraft || selectedProviders();
  if (!methods.includes('bergung')) providerDraft = [...methods, 'bergung'];
  renderDownloadMethod(); renderDownloadControl();
  $('#settings-save-state').textContent = '入口已添加，点击保存设置后生效';
  toast('入口已添加到当前实例，请保存设置。');
});
$('#channel-entries').addEventListener('click', event => {
  const button = event.target.closest('[data-channel-remove]');
  if (button) { button.closest('.channel-entry').remove(); settingsDirty = true; }
});
function renderProviderChoices(container, methods, busy = false) {
  const auto = $('#settings-form [name="scheduler_mode"]').value === 'auto';
  $('.task-defaults-panel .field-label').textContent = auto ? '参与自动调度的下载方式' : '下载方式与优先级';
  const signature = JSON.stringify([methods, busy, auto]);
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;
  const order = [...methods, ...Object.keys(providerNames).filter(method => !methods.includes(method))];
  container.innerHTML = order.map(method => {
    const index = methods.indexOf(method);
    const enabled = index >= 0;
    return `<div class="provider-choice" data-provider="${method}"><label><input type="checkbox" data-provider-toggle ${enabled ? 'checked' : ''} ${busy ? 'disabled' : ''}><span class="provider-rank">${enabled ? auto ? '✓' : index + 1 : '—'}</span><strong>${providerNames[method]}</strong><small>${enabled ? auto ? '自动候选' : index === 0 ? '优先使用' : '前项失败后尝试' : '未启用'}</small></label><div class="provider-order"${auto?' hidden':''}><button type="button" data-method-move="-1" aria-label="提高 ${providerNames[method]} 优先级" ${busy || !enabled || index === 0 ? 'disabled' : ''}>↑</button><button type="button" data-method-move="1" aria-label="降低 ${providerNames[method]} 优先级" ${busy || !enabled || index === methods.length - 1 ? 'disabled' : ''}>↓</button></div></div>`;
  }).join('');
}
function readProviderChoices(container) {
  return $$('.provider-choice', container).filter(row => $('input', row).checked).map(row => row.dataset.provider);
}
function renderDownloadMethod() {
  const methods = providerDraft || selectedProviders();
  renderStrategySaveState();
  renderProviderChoices($('#download-method'), methods, downloadMethodBusy || !state.config);
  const tools = state.status?.tools;
  const auto = $('#settings-form [name="scheduler_mode"]').value === 'auto';
  const notes = [auto ? `自动候选：${methods.map(method => providerNames[method]).join('、')}。各入口按实际吞吐、稳定性及当前负载分配任务，不使用下载器优先级。服务器负载或限流升高时自动收敛并发。修改后点击下方“保存设置”生效。` : `尝试顺序：${methods.map(method => providerNames[method]).join(' → ')}。勾选启用，上下箭头调整优先级；成功后停止，失败后尝试下一项。修改后统一点击下方“保存设置”生效。`];
  const throughput = $('#settings-form [name="throughput_mode"]');
  // Auto routing and the resource controller are one saved strategy.
  if (auto) throughput.value = 'auto';
  throughput.disabled = auto;
  if (methods.includes('downmp3')) notes.push(`downmp3 ${qualityLabel(state.config?.downmp3_quality, 'downmp3')}，转换等待上限 ${state.config?.downmp3_timeout ?? 300} 秒。`);
  if (methods.includes('yt5s')) notes.push(`yt5s ${qualityLabel(state.config?.yt5s_quality, 'yt5s')}，浏览器并发 ${state.config?.yt5s_browser_count ?? 2}。${tools && (!tools.yt5s_browser || !tools.yt5s_display) ? '当前缺少浏览器或显示环境。' : '可能需要网站验证。'}`);
  if (methods.includes('yt1d')) notes.push(`yt1d ${qualityLabel(state.config?.yt1d_quality, 'yt1d')}，验证 / 转换每阶段等待上限 ${state.config?.yt1d_timeout ?? 180} 秒，与 yt5s 共享 ${state.config?.yt5s_browser_count ?? 2} 个浏览器名额，可能需要网站验证。`);
  if (methods.includes('bergung')) notes.push(`Bergung ${qualityLabel(state.config?.bergung_quality, 'bergung')}，验证等待上限 ${state.config?.bergung_timeout ?? 180} 秒；与其他网页下载器共享浏览器名额，可能需要人工完成 Cloudflare 验证。`);
  notes.push(methods.includes('yt_dlp') ? '开始前检查 YouTube Cookie；yt-dlp 默认选择最高分辨率和帧率，不限制编码。' : '当前选择无需 YouTube Cookie。');
  notes.push('保存后仅影响当前实例后续领取的视频，无需暂停或重启；其他实例使用各自策略，正在执行的视频保持本次配置。');
  $('#download-method-note').textContent = notes.join(' ');
}
function renderStrategySaveState() {
  const target = state.instanceId ? `实例 ${instanceName(state.instanceId)}` : '本机';
  $('#strategy-save-state').textContent = downloadMethodBusy ? `正在保存并核对 · ${target}`
    : settingsDirty || providerDraft ? `${target}有未保存修改，请点击下方“保存设置”`
    : `已保存 · ${target} · 仅作用于当前实例后续领取的视频`;
}
$('#settings-form [name="scheduler_mode"]').addEventListener('change', renderDownloadMethod);
for (const id of ['download-method']) {
  const container = $(`#${id}`);
  const update = async (event, moving) => {
    const row = event.target.closest('[data-provider]');
    if (!row || (id === 'download-method' && (downloadMethodBusy || !state.config))) return;
    const before = JSON.parse(container.dataset.signature)[0];
    const methods = moving ? [...before] : readProviderChoices(container);
    if (moving) {
      const index = methods.indexOf(row.dataset.provider);
      const next = index + Number(moving.dataset.methodMove);
      if (index < 0 || next < 0 || next >= methods.length) return;
      [methods[index], methods[next]] = [methods[next], methods[index]];
    }
    if (!methods.length) {
      toast('请至少选择一种下载器。', true);
      container.dataset.signature = '';
      renderProviderChoices(container, before);
      return;
    }
    providerDraft = JSON.stringify(methods) === JSON.stringify(selectedProviders()) ? null : methods;
    renderDownloadMethod();
    renderDownloadControl();
  };
  container.addEventListener('change', event => { if (event.target.matches('[data-provider-toggle]')) update(event); });
  container.addEventListener('click', event => {
    const button = event.target.closest('[data-method-move]');
    if (button) update(event, button);
  });
}
function renderStorageSettings() {
  const test = $('#storage-mode').value === 'test';
  $$('[data-local-storage]').forEach(label => {
    label.hidden = test;
    $('input', label).disabled = test;
  });
  $('#storage-mode-note').textContent = test ? '真实下载后自动删除视频、封面、字幕及元数据，仅保留任务记录和日志，不加入素材库。此模式只作用于当前实例后续领取的视频。' : '视频及配套文件保存到指定目录，下载完成后可在素材库查看。此模式只作用于当前实例后续领取的视频。';
}
$('#storage-mode').addEventListener('change', renderStorageSettings);
const refreshedAt = {};
async function refresh({ poll = false, viewChanged = false } = {}) {
  if ((location.hash.slice(1) || 'overview') === 'overview') refreshExecution().catch(() => {});
  if (state.polling) {
    if (!poll || viewChanged) state.refreshQueued = true;
    return;
  }
  state.polling = true;
  const instance = state.instanceId;
  try {
    const view = location.hash.slice(1) || 'overview';
    const stamp = Date.now();
    const due = (name, visible, interval) => !poll || (visible && (viewChanged || stamp - (refreshedAt[name] || 0) >= interval));
    const requests = {
      media: (!fleetEnabled || state.instanceId) && due('media', view === 'media', 10000) ? `/api/media?${listQuery('media', { limit: 24, q: $('#media-search').value })}` : null,
      tasks: due('tasks', view === 'tasks', 4000) ? `/api/tasks?${listQuery('tasks', { scope: 'download', view: 'history', limit: 50, status: $('#task-filter').value === 'all' ? '' : $('#task-filter').value, q: $('#task-search').value })}` : null,
      status: due('status', true, view === 'overview' || $('#youtube-login-dialog').open ? 2000 : 10000) ? '/api/status' : null,
      clash: (!fleetEnabled || state.instanceId) && due('clash', view === 'clash', 15000) ? '/api/clash' : null,
      overview: due('overview', true, view === 'overview' ? 2000 : 10000) ? '/api/overview' : null,
    };
    const pendingRequests = Object.entries(requests).filter(([, path]) => path);
    const results = await Promise.allSettled(pendingRequests.map(async ([name, path]) => [name, await api(path)]));
    if (instance !== state.instanceId) { state.refreshQueued = true; return; }
    const entries = results.filter(result => result.status === 'fulfilled').map(result => result.value);
    const failed = results.filter(result => result.status === 'rejected');
    const warning = $('#connection-warning');
    warning.hidden = !failed.length;
    if (failed.length) {
      warning.textContent = `部分状态暂时无法更新：${failed[0].reason.message}。当前保留上次成功数据，请勿把旧状态当作实时状态。`;
      $('#database-health').textContent = failed.some(result => result.reason.status === 503) ? '数据库：连接异常' : '数据库：状态未更新';
    }
    const data = Object.fromEntries(entries);
    for (const [name] of entries) refreshedAt[name] = stamp;
    const { media, tasks, clash, overview } = data;
    const mediaBefore = JSON.stringify([state.media, state.overview.media, state.overview.recent_media]);
    if (overview) { state.overview = overview; state.counts = overview.counts; }
    if (media) { state.media = media.items; state.mediaTotal = media.total; pages.media.next = media.next_cursor; }
    if (tasks) { state.tasks = tasks.items; pages.tasks.next = tasks.next_cursor; }
    if (data.status) state.status = data.status;
    if (clash) state.clash = clash;
    if ((media || overview) && mediaBefore !== JSON.stringify([state.media, state.overview.media, state.overview.recent_media])) renderMedia();
    if (tasks || overview || data.status) renderTasks();
    if (clash) renderClash();
    if (data.status) { renderDownloadControl(); renderStatus(); }
    if (failed.length) $('#database-health').textContent = failed.some(result => result.reason.status === 503) ? '数据库：连接异常' : '部分状态暂未更新，请查看上方提示';
    renderPagination();
    if (media) {
      $('#media-directory').textContent = media.directory;
      state.mediaDirectory = media.directory;
    }
    const status = state.status;
    if (!status) return;
    $('#database-backend').textContent = `${failed.length ? '部分状态暂未更新' : status.database === 'mysql' ? 'MySQL 最近请求成功' : 'SQLite 测试模式'} · ${status.worker_count} 个下载进程 · 素材按页查询`;
    const workers = status.workers || [];
    const paused = workers.filter(worker => worker.state === 'paused');
    $('#worker-status').hidden = !paused.length;
    $('#worker-status').textContent = paused.map(worker => worker.message).join('；');
    if (state.selectedTask && $('#task-dialog').open) await updateTaskDetail();
  } finally {
    state.polling = false;
    if (state.refreshQueued) {
      state.refreshQueued = false;
      queueMicrotask(() => refresh().catch(error => toast(error.message, true)));
    }
  }
}
document.addEventListener('viewchange', () => {
  if (state.config) refresh({ poll: true, viewChanged: true }).catch(error => toast(error.message, true));
});
window.addEventListener('hashchange', () => {
  setView(location.hash.slice(1) || 'overview');
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && state.config) refresh({ poll: true, viewChanged: true }).catch(error => toast(error.message, true));
});

const startupNames = { checking_instances: '检查下载实例及其环境', checking_database: '连接数据库', reading_queue: '读取待处理清单', preparing_proxy: '准备代理节点', checking_environment: '检查下载工具', checking_login: '验证 YouTube Cookie', releasing_queue: '放行队列并启动进程' };
function renderStartup() {
  const value = state.startup;
  $('#download-startup').hidden = !value || value.state === 'idle';
  if (!value || value.state === 'idle') return;
  const labels = { running: '启动检查中', succeeded: '启动检查已完成', failed: '启动未完成', cancelled: '启动已取消' };
  $('#startup-title').textContent = `${value.state === 'running' && value.cancel_requested ? '正在取消启动' : labels[value.state] || value.state} · ${value.elapsed || 0} 秒`;
  $('#startup-steps').innerHTML = (value.steps || []).map((step, index) => `<li class="${value.state === 'running' && index === value.steps.length - 1 ? 'is-current' : ''}"><span>${escapeHTML(startupNames[step.stage] || step.stage.replace(/^instance_ready:/, '实例就绪：'))}</span><b>${Number(step.elapsed) || 0} 秒</b></li>`).join('');
  $('#startup-message').textContent = (value.message || '检查通过后自动执行；可点击暂停取消启动。') + (value.slowest ? ` 当前耗时最长：${startupNames[value.slowest] || value.slowest}。` : '');
  $('#download-startup').classList.toggle('startup-failed', value.state === 'failed');
}
function renderJobOptions() {
  const options = jobOptions.map(job => job.id === selectedJob?.id ? selectedJob : job);
  if (selectedJob && !options.some(job => job.id === selectedJob.id)) options.unshift(selectedJob);
  $('#download-job').replaceChildren(new Option('请选择一个任务', ''), ...options.map(job =>
    new Option(`${job.customer} / ${job.name} · 待下载 ${job.queued}`, job.id)));
  $('#download-job').value = selectedJobId;
  $('#download-jobs-more').hidden = !jobsCursor;
}
async function loadDownloadJobs(more = false) {
  const ticket = ++jobsSearchEpoch;
  const params = new URLSearchParams({ q: $('#download-job-search').value.trim() });
  if (more && jobsCursor) params.set('cursor', jobsCursor);
  $('#download-jobs-more').disabled = true;
  try {
    const result = await api('/api/jobs?' + params);
    if (ticket !== jobsSearchEpoch) return;
    jobOptions = more ? [...jobOptions, ...result.items] : result.items;
    jobsCursor = result.next_cursor;
    renderJobOptions();
    if (!jobOptions.length && !selectedJob) $('#download-job-note').textContent = '没有匹配的任务。请修改搜索条件，或前往任务中心新建任务。';
  } finally { if (ticket === jobsSearchEpoch) $('#download-jobs-more').disabled = false; }
}
let executionPolling = false;
let workbenchEpoch = 0;
async function refreshExecution() {
  if (executionPolling || document.hidden) return;
  executionPolling = true;
  try {
    await refreshSelectedJob();
  } finally { executionPolling = false; }
}
setInterval(() => {
  if ((location.hash.slice(1) || 'overview') === 'overview') refreshExecution().catch(() => {});
}, 1000);
async function refreshSelectedJob() {
  if (queueControlBusy || (fleetEnabled && !state.instanceId)) return;
  const id = selectedJobId;
  const instance = state.instanceId;
  const epoch = ++workbenchEpoch;
  try {
    const workbench = await api(`/api/jobs/workbench${id ? '?job_id='+encodeURIComponent(id) : ''}`);
    if (epoch !== workbenchEpoch || state.instanceId !== instance || selectedJobId !== id) return;
    applyWorkbench(workbench);
    renderJobOptions();
    renderDownloadControl();
  } catch (error) {
    if (epoch !== workbenchEpoch || state.instanceId !== instance || selectedJobId !== id) return;
    // Retain the selection but prevent a start using stale task information.
    selectedJob = null;
    renderDownloadControl();
    $('#download-job-note').textContent = error.message;
    throw error;
  }
}
function applyWorkbench(workbench) {
  const id = workbench.selected_job_id || '';
  if (lastStartedJobId !== id && !queueControlBusy && (lastStartedJobId !== null || !selectedJobId)) {
    selectedJobId = id; queueResumeError = '';
    clearJobLink(); renderJobOptions(); renderBatches();
    if (workbench.job?.id !== id) {
      selectedJob = null;
      // The selected job changed in another tab; fetch its details next.
      refreshSelectedJob().catch(() => {});
    }
  }
  lastStartedJobId = id;
  state.workbench = workbench;
  selectedJob = workbench.job?.id === selectedJobId ? workbench.job : null;
  state.batchTasks = workbench.active_tasks || [];
  renderTasks();
  if (typeof renderFleetJob === 'function') renderFleetJob();
}
async function resetWorkbench() {
  ++workbenchEpoch;
  state.workbench = null; state.config = null; selectedJobId = ''; selectedJob = null; lastStartedJobId = null;
  state.batchTasks = []; queueResumeError = '';
  clearJobLink(); renderJobOptions(); renderTasks(); renderDownloadControl();
  await refreshSelectedJob();
}
function clearJobLink() {
  const url = new URL(location.href);
  url.searchParams.delete('job');
  history.replaceState(null, '', url.pathname + url.search + (url.hash || '#overview'));
}
function requireDownloadJob() {
  if (!selectedJobId || selectedJob?.id !== selectedJobId) {
    $('#download-job').focus();
    throw new Error('请先选择一个有效的下载任务。');
  }
  return selectedJobId;
}
$('#download-job-search').addEventListener('input', () => {
  clearTimeout(jobsSearchTimer);
  ++jobsSearchEpoch;
  jobsCursor = null;
  $('#download-jobs-more').hidden = true;
  jobsSearchTimer = setTimeout(() => loadDownloadJobs().catch(error => toast(error.message, true)), 300);
});
$('#download-jobs-more').addEventListener('click', () => loadDownloadJobs(true).catch(error => toast(error.message, true)));
$('#download-job').addEventListener('change', async event => {
  selectedJobId = event.target.value; selectedJob = null; queueResumeError = '';
  clearJobLink();
  state.batchTasks = []; renderBatches(); renderDownloadControl();
  refresh().catch(error => toast(error.message, true));
  try { await refreshSelectedJob(); } catch (error) { toast(error.message, true); }
});
function renderDownloadControl() {
  const queue = state.workbench;
  const starting = false;
  const viewer = document.body.dataset.consoleRole === 'viewer';
  const job = selectedJob;
  $('#download-job').disabled = queueControlBusy;
  $('#download-job-search').disabled = queueControlBusy;
  $('#download-job-note').textContent = job ? `${job.customer} / ${job.name}` : selectedJobId ? '正在读取所选任务…' : '请选择任务，查看待下载的视频和当前状态。';
  $('#download-job-detail').href = job ? `/jobs#${job.id}` : '/jobs';
  const summary = $('#download-job-summary'); summary.hidden = !job;
  if (job) {
    summary.innerHTML = `<div class="selected-job-counts">${[['待下载',job.queued],['当前实例下载中',job.running],['任务处理成功',job.succeeded],['任务失败',job.failed]].map(([label,count])=>`<div><span>${label}</span><strong>${Number(count)||0}</strong></div>`).join('')}</div><p class="micro-copy">${escapeHTML(downloadPolicyText(job))}${job.importing?` · 正在导入 ${job.importing} 份文件`:''}</p><p class="micro-copy"><a href="#settings">修改当前实例策略 ↗</a></p>`;
  }
  const alreadyStarted = !!job?.released && !queue?.paused && queue?.selected_job_id===job.id;
  $('#download-queue-start').textContent = queueResuming ? '正在启动…' : alreadyStarted ? '任务已开始' : '开始下载';
  $('#download-queue-start').setAttribute('aria-busy', String(queueResuming));
  $('#download-queue-start').disabled = viewer || queueControlBusy || starting || !state.config || !queue || !queue.online || !job || job.archived || (!job.queued && !job.running && !job.importing) || alreadyStarted || (queue.stop_requested && !!queue.running);
  $('#download-job-pause').disabled = viewer || queueControlBusy || !queue || queue.paused;
  $('#download-job-stop').disabled = viewer || queueControlBusy || !queue || (queue.paused && !queue.running);
  $('#download-queue-toggle').textContent = '停止当前实例';
  $('#download-queue-toggle').disabled = viewer || queueControlBusy || (!starting && (!queue || (queue.paused && !queue.running) || (!queue.running && !queue.queued)));
  const differentJob = queue?.selected_job_id && queue.selected_job_id !== selectedJobId;
  $('#download-selection-status').textContent = differentJob ? '点击开始后，当前实例切换到所选任务。' : !job ? '' : job.waiting_reason || (job.running ? (queue?.paused ? '当前实例将在当前视频完成后暂停。' : '此任务正在下载。') : job.released && job.importing ? '等待文件导入，随后自动下载。' : !job.queued && !job.importing ? '没有待下载视频。请到该任务中添加视频或重试失败条目。' : job.released ? '此任务正在排队。' : '此任务已准备好，可以开始下载。');
  $('#download-queue-status').textContent = queue ? `当前实例：执行中 ${queue.running}。${queue.paused ? queue.running ? '完成当前视频后暂停。' : '已停止领取。' : '正在领取所选任务的视频。'}` : '正在读取当前实例状态…';
  const execution = $('#download-execution-status'); execution.replaceChildren();
  if (queueResuming) execution.append(document.createTextNode('正在检查当前实例的下载环境，请稍候。'));
  if (queueResumeError) execution.append(document.createTextNode(queueResumeError));
  if (!queueResuming && queue?.error) execution.append(document.createTextNode(queue.error));
  renderStartup();
}
function instancePolicyText(policy) {
  const methods = (policy.download_methods || (policy.download_method==='yt5s_fallback'?['yt5s','yt_dlp']:[policy.download_method || 'yt_dlp'])).map(method => providerNames[method] || method).join(' → ');
  const qualities = {yt5s:policy.yt5s_quality, downmp3:policy.downmp3_quality, yt1d:policy.yt1d_quality, bergung:policy.bergung_quality};
  const quality = Object.entries(qualities).filter(([method]) => (policy.download_methods || [policy.download_method]).includes(method)).map(([method,value]) => `${method} ${!value || value==='best'?'最高画质':value+'p'}`).join(' / ');
  return `下载顺序：${methods}${quality?' · '+quality:''} · ${policy.storage_mode==='test'?'测试模式，完成后删除文件':'保存成品'}`;
}
function downloadPolicyText() {
  if (!state.config) return '正在读取当前实例策略…';
  return `当前实例 · ${instancePolicyText(state.config || {})}`;
}
let queueControlBusy = false;
let queueResuming = false;
let queueResumeError = '';
async function pollStartup() {
  if (startupPolling || document.hidden) return;
  startupPolling = true;
  try {
    const previous = state.startup;
    state.startup = await api('/api/downloads/startup');
    renderDownloadControl();
    if (previous?.state === 'running' && state.startup.state !== 'running') refresh().catch(() => {});
  } catch { /* The main connection banner reports service availability. */ }
  finally { startupPolling = false; }
}
setInterval(() => { if (state.startup?.state === 'running' || location.hash === '#overview') pollStartup(); }, 1000);
$('#download-queue-start').addEventListener('click', async () => {
  if (queueControlBusy || document.body.dataset.consoleRole === 'viewer') return;
  if (providerDraft || downloadMethodBusy) {
    toast('下载方式尚未保存完成，请先在系统设置点击“保存设置”，再开始下载。', true);
    setView('settings');
    return;
  }
  let id;
  try { id = requireDownloadJob(); } catch (error) { toast(error.message, true); return; }
  queueControlBusy = true; queueResuming = true; queueResumeError = '';
  ++workbenchEpoch;
  renderDownloadControl();
  try {
    const result = await api('/api/jobs/workbench', { method: 'POST', body: { action: 'start', job_id: id }, signal: AbortSignal.timeout(90000) });
    ++workbenchEpoch; applyWorkbench(result);
    lastStartedJobId = result.job.id; clearJobLink();
    toast(result.warnings?.length ? result.warnings.join('；') : `已开始下载：${result.job.customer} / ${result.job.name}`);
  } catch (error) {
    queueResumeError = error.name === 'TimeoutError' ? '启动请求等待超过 90 秒，结果尚未确认，请刷新查看最新状态。' : error.message;
    toast(queueResumeError, true);
  }
  finally { queueControlBusy = false; queueResuming = false; renderDownloadControl(); }
  refresh().catch(error => toast(error.message, true));
});
for (const [selector, action] of [['#download-job-pause','pause'],['#download-job-stop','stop']]) {
  $(selector).addEventListener('click', async () => {
    if (queueControlBusy || document.body.dataset.consoleRole === 'viewer') return;
    queueControlBusy = true; queueResumeError = ''; renderDownloadControl();
    ++workbenchEpoch;
    try {
      const result = await api('/api/jobs/workbench',{method:'POST',body:{action}});
      ++workbenchEpoch; applyWorkbench(result);
      toast(action==='pause'?'当前实例将在当前视频完成后暂停。':'已停止当前实例，执行进程将在下次心跳退出。');
      await refresh();
    } catch (error) { queueResumeError = error.message; toast(error.message,true); }
    finally { queueControlBusy = false; renderDownloadControl(); }
  });
}
$('#download-queue-toggle').addEventListener('click', async () => {
  if (queueControlBusy) return;
  queueControlBusy = true;
  ++workbenchEpoch;
  renderDownloadControl();
  try {
    const result = await api('/api/jobs/workbench', { method: 'POST', body: {action:'stop'} });
    ++workbenchEpoch; applyWorkbench(result);
    toast('已停止当前实例。');
    await pollStartup();
    await refresh();
  } catch (error) { toast(error.message, true); }
  finally { queueControlBusy = false; renderDownloadControl(); }
});

async function startTask(action, extra = {}) {
  if (!state.config) throw new Error('配置尚未读取完成，请稍后重试。');
  const task = await api('/api/tasks', { method: 'POST', body: { action, scope: 'download', ...extra } });
  toast(`${actionNames[action]}已加入后台队列。`);
  await openTask(task.id);
  return task;
}
async function openTask(taskId) {
  state.selectedTask = taskId;
  state.logs = ''; state.logOffset = 0;
  $('#task-log').textContent = '等待任务开始…';
  $('#task-result').replaceChildren();
  $('#task-error').hidden = true;
  $('#task-download-progress').replaceChildren();
  $('.task-technical').open = false;
  if (!$('#task-dialog').open) $('#task-dialog').showModal();
  try { await updateTaskDetail(); } catch (error) { toast(error.message, true); }
}
async function updateTaskDetail() {
  if (state.detailBusy) return;
  state.detailBusy = true;
  try {
  const taskId = state.selectedTask;
  const [task, logs] = await Promise.all([api(`/api/tasks/${taskId}`), $('.task-technical').open ? api(`/api/tasks/${taskId}/logs?offset=${state.logOffset}`) : Promise.resolve({ text: '', offset: state.logOffset })]);
  if (state.selectedTask !== taskId) return;
  $('#task-dialog-title').textContent = actionNames[task.action] || task.action;
  $('#task-detail-meta').innerHTML = `<span class="task-status status-${task.status}">${taskStatus(task)}</span><span>${task.id.slice(0, 8)}</span><span>创建于 ${escapeHTML(formatDate(task.created_at, true))}</span>${task.started_at ? `<span>开始于 ${escapeHTML(formatDate(task.started_at, true))}</span>` : ''}${task.finished_at ? `<span>结束于 ${escapeHTML(formatDate(task.finished_at, true))}</span>` : ''}`;
  $('#task-error').hidden = !task.error;
  $('#task-error').textContent = task.error ? readableError(task.error) : '';
  $('#task-download-progress').innerHTML = task.action === 'clear_media' ? `<p class="micro-copy">已删除 ${Number(task.progress?.deleted_files || 0)} 个文件 · 释放 ${bytes(task.progress?.deleted_bytes || 0)}${task.progress?.failed_files ? ` · ${Number(task.progress.failed_files)} 个文件未能删除` : ''}</p>` : ['download', 'import'].includes(task.action) ? downloadProgress(task) : task.progress?.scanned ? `<p class="micro-copy">已扫描 ${task.progress.scanned} 个文件</p>` : '';
  const result = task.result;
  if (result?.message) { $('#task-result').innerHTML = `<div class="result-summary">${escapeHTML(result.message)}</div>`; }
  if (logs.text) {
    const log = $('#task-log');
    const atBottom = log.scrollHeight - log.clientHeight - log.scrollTop < 45;
    state.logs = ((logs.pruned ? '[早期日志已自动清理]\n' : state.logs) + logs.text).slice(-200000);
    log.textContent = state.logs;
    if (atBottom) log.scrollTop = log.scrollHeight;
  } else if (logs.message) {
    state.logs = '';
    $('#task-log').textContent = logs.message;
  }
  state.logOffset = logs.offset;
  } finally { state.detailBusy = false; }
}

function readableError(message) {
  if (/Can't connect to MySQL|No route to host|sqlalchemy|pymysql/i.test(message)) return '数据库连接异常，下载状态或计费记录无法保存。请检查 MySQL 网络和服务；技术日志中有具体原因。';
  if (/no space left|disk full|磁盘剩余空间/i.test(message)) return '磁盘空间不足，请释放空间后继续。';
  if (/Cookie|登录验证/.test(message)) return 'YouTube 登录验证未通过，请检查 Cookie 和当前节点。';
  if (/HTTP.*(?:502|503|504)|转换.*超时/.test(message)) return '下载服务暂时不可用或转换超时，请查看失败的下载器和节点。';
  return String(message).slice(0, 500);
}
$('.task-technical').addEventListener('toggle', () => { if ($('.task-technical').open && state.selectedTask) updateTaskDetail().catch(error => toast(error.message, true)); });

document.addEventListener('click', event => {
  const nav = event.target.closest('[data-view]');
  if (nav) { event.preventDefault(); setView(nav.dataset.view); }
  const open = event.target.closest('[data-open]');
  if (open) $(`#${open.dataset.open}`).showModal();
  if (event.target.closest('[data-close]')) event.target.closest('dialog').close();
  const task = event.target.closest('[data-task]');
  if (task) openTask(task.dataset.task);
  const media = event.target.closest('[data-media]');
  if (media) openMedia(media.dataset.media, media.dataset.mediaInstance);
  const selectNode = event.target.closest('[data-node-select]');
  if (selectNode && !selectNode.disabled) clashAction(async () => {
    await api('/api/clash/select', { method: 'POST', body: { name: selectNode.dataset.nodeSelect } });
    $('#clash-youtube-result').textContent = '';
    toast('下载节点已切换。');
  });
  const delayNode = event.target.closest('[data-node-delay]');
  if (delayNode && !delayNode.disabled) clashAction(() => api('/api/clash/delay', { method: 'POST', body: { name: delayNode.dataset.nodeDelay } }));
});
async function runButton(button, operation) {
  if (button.disabled) return;
  button.disabled = true;
  try { await operation(); } catch (error) { toast(error.message || '请求失败，请稍后重试。', true); }
  finally { button.disabled = false; }
}
$('#youtube-cookie-form').addEventListener('submit', event => {
  event.preventDefault();
  runButton($('button[type="submit"]', event.target), async () => {
    const errorBox = $('#youtube-cookie-error');
    errorBox.hidden = true;
    try {
      const file = $('input[name="cookie"]', event.target).files[0];
      if (!file || file.size === 0 || file.size > 1024 * 1024) throw new Error('请选择非空的 Cookie .txt 文件，最大 1 MB。');
      const data = await api('/api/youtube/cookie', { method: 'POST', body: new FormData(event.target) });
      if (state.config) state.config.youtube_cookie = data.cookie_path;
      $('#youtube-cookie-dialog').close();
      $('#youtube-cookie-form').reset();
      toast(data.message);
      await refresh();
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
      throw error;
    }
  });
});
$('#check-environment').addEventListener('click', event => runButton(event.currentTarget, () => startTask('check')));
$('#refresh-media').addEventListener('click', event => runButton(event.currentTarget, async () => { const task = await api('/api/media/reindex', { method: 'POST' }); toast('已开始同步本地文件，完成后素材库会自动更新。'); await openTask(task.id); }));
$('#clear-media').addEventListener('click', event => runButton(event.currentTarget, async () => {
  const media = await api('/api/media?limit=1');
  state.clearMediaDirectory = media.directory;
  $('#media-clear-directory').textContent = `当前目录：${media.directory} · 已索引 ${media.total} 个素材`;
  $('#media-clear-confirmation').value = '';
  $('#media-clear-submit').disabled = true;
  $('#media-clear-dialog').showModal();
  $('#media-clear-confirmation').focus();
}));
$('#media-clear-confirmation').addEventListener('input', event => {
  $('#media-clear-submit').disabled = event.target.value !== '清空';
});
$('#media-clear-form').addEventListener('submit', event => {
  event.preventDefault();
  runButton($('#media-clear-submit'), async () => {
    const task = await api('/api/media/clear', { method: 'POST', body: {
      confirmation: $('#media-clear-confirmation').value, directory: state.clearMediaDirectory,
    } });
    $('#media-clear-dialog').close();
    const player = $('#media-player');
    player.pause(); player.removeAttribute('src'); player.load();
    resetPage('media');
    toast('已开始后台清空素材库，可在任务详情中查看进度。');
    await openTask(task.id);
    await refresh();
  });
});
$('#task-filter').addEventListener('change', () => { resetPage('tasks'); refresh(); });
document.addEventListener('click', event => {
  const button = event.target.closest('[data-page]');
  if (!button || button.disabled) return;
  const name = button.dataset.page;
  const page = pages[name];
  if (button.dataset.direction === 'next' && page.next) { page.history.push(page.cursor); page.cursor = page.next; }
  else if (button.dataset.direction === 'prev' && page.history.length) page.cursor = page.history.pop();
  runButton(button, refresh);
});
for (const name of ['media', 'tasks']) {
  let timeout;
  $(`#${name === 'tasks' ? 'task' : 'media'}-search`).addEventListener('input', () => {
    clearTimeout(timeout); timeout = setTimeout(() => { resetPage(name); refresh(); }, 300);
  });
}
$('#clash-node-search').addEventListener('input', renderClash);
$('#clash-node-sort').addEventListener('change', renderClash);
$('#clash-rotation-form').addEventListener('change', event => {
  if (!rotationDraft) return;
  const name = event.target.dataset.rotationNode;
  if (name !== undefined) {
    rotationDraft.nodes = rotationDraft.nodes.filter(node => node !== name);
    if (event.target.checked) rotationDraft.nodes.push(name);
  }
  rotationDraft.enabled = $('#rotation-enabled').checked;
  rotationDraft.every = Number($('#rotation-every').value);
  rotationDraft.count_failures = $('#rotation-count-failures').checked;
  rotationDirty = true;
  renderRotation(state.clash);
});
$('#clash-rotation-form').addEventListener('submit', event => {
  event.preventDefault();
  clashAction(async () => {
    const result = await api('/api/clash/rotation', { method: 'POST', body: rotationDraft });
    rotationDirty = false;
    rotationSavedSignature = '';
    toast(result.message);
  });
});
$('#clash-subscription-form').addEventListener('submit', event => {
  event.preventDefault();
  const body = { action: 'save', id: subscriptionSelection || '', name: $('#clash-subscription-name').value.trim(), url: $('#clash-subscription-url').value.trim() };
  clashAction(async () => {
    const result = await api('/api/clash/subscription', { method: 'POST', body });
    state.clash = result;
    subscriptionSelection = result.saved_subscription_id;
    $('#clash-subscription-url').value = '';
    $('#clash-youtube-result').textContent = '';
    fillConfig((await api('/api/config')).config);
    toast(result.message);
  });
});
$('#clash-subscription-select').addEventListener('change', event => chooseSubscription(event.target.value));
$('#clash-subscription-new').addEventListener('click', () => chooseSubscription(''));
$('#clash-subscription-switch').addEventListener('click', () => {
  const identity = subscriptionSelection;
  clashAction(async () => {
    const result = await api('/api/clash/subscription', { method: 'POST', body: { action: 'switch', id: identity } });
    state.clash = result;
    rotationDirty = false; rotationSavedSignature = '';
    $('#clash-youtube-result').textContent = '';
    fillConfig((await api('/api/config')).config);
    toast(result.message);
  });
});
$('#clash-subscription-delete').addEventListener('click', () => {
  const identity = subscriptionSelection;
  clashAction(async () => {
    const result = await api('/api/clash/subscription', { method: 'POST', body: { action: 'delete', id: identity } });
    state.clash = result;
    subscriptionSelection = null;
    toast(result.message);
  });
});
$('#proxy-mode-select').addEventListener('change', event => {
  const mode = event.target.value;
  clashAction(async () => {
    await api('/api/config', { method: 'PUT', body: { proxy_mode: mode } });
    fillConfig((await api('/api/config')).config);
    toast('下载连接方式已保存。');
  });
});
$('#clash-start').addEventListener('click', () => clashAction(() => api('/api/clash/start', { method: 'POST' })));
$('#clash-apply-connection').addEventListener('click', () => clashAction(async () => {
  const result = await api('/api/downloads/connection', { method: 'POST' });
  toast(result.message);
}));
$('#clash-stop').addEventListener('click', () => clashAction(() => api('/api/clash/stop', { method: 'POST' })));
$('#clash-test-all').addEventListener('click', () => clashAction(() => api('/api/clash/delay', { method: 'POST', body: {} })));
$('#clash-test-youtube').addEventListener('click', () => clashAction(async () => {
  $('#clash-youtube-result').textContent = '正在检测 YouTube 连通性…';
  try {
    const result = await api('/api/clash/test', { method: 'POST' });
    $('#clash-youtube-result').textContent = `YouTube 连接成功 · ${result.ms} ms · ${result.node}`;
  } catch (error) {
    $('#clash-youtube-result').textContent = error.message;
    throw error;
  }
}));
$('#mobile-menu').addEventListener('click', () => $('#sidebar').classList.toggle('mobile-open'));
$('#task-dialog').addEventListener('close', () => { state.selectedTask = null; });
$('#media-dialog').addEventListener('close', () => {
  const player = $('#media-player');
  player.pause();
  player.removeAttribute('src');
  player.load();
});
$('#media-player').addEventListener('error', () => {
  const warning = $('#media-player-error');
  warning.textContent = '浏览器无法播放该视频格式，可以保存到电脑后用本地播放器打开。';
  warning.hidden = false;
});
$('#copy-logs').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(state.logs); toast('日志已复制。'); }
  catch { toast('复制失败，请手动选择并复制日志。', true); }
});
function markSettingsDirty() {
  settingsDirty = true;
  $('#settings-save-state').textContent = '有未保存的设置，保存后生效';
  renderStrategySaveState(); renderDownloadControl();
}
$('#settings-form').addEventListener('input', markSettingsDirty);
$('#settings-form').addEventListener('change', markSettingsDirty);
$('#settings-form').addEventListener('submit', event => {
  event.preventDefault();
  runButton($('button[type="submit"]', event.target), async () => {
    if (downloadMethodBusy) return;
    downloadMethodBusy = true; renderDownloadMethod();
    const data = Object.fromEntries([...new FormData(event.target)].filter(([key,value]) => value !== settingsBaseline[key]));
    const channels = readChannels();
    if (channelSignature(channels) !== settingsChannelBaseline) data.download_channels = channels;
    if (providerDraft) data.download_methods = [...providerDraft];
    const instance = state.instanceId;
    const headers = { 'X-Instance-ID': instance || '' };
    const form = event.target;
    form.inert = true;
    form.setAttribute('aria-busy', 'true');
    $('#settings-save-state').textContent = '正在保存设置并核对服务器中的实际值…';
    try {
      await api('/api/config', { method:'PUT', body:data, headers });
      const saved = (await api('/api/config', { headers, cache:'no-store' })).config;
      const mismatches = unconfirmedSettings(data, saved);
      if (mismatches.length) {
        const labels = mismatches.map(key => key === 'download_methods' ? '下载方式'
          : key === 'download_channels' ? '多渠道配置'
          : $(`[name="${key}"]`, form)?.closest('label')?.firstChild.textContent.trim() || key);
        const providers = value => Array.isArray(value) ? value.map(method => providerNames[method] || method).join('、') : '未返回有效列表';
        const detail = mismatches.includes('download_methods')
          ? `（提交：${providers(data.download_methods)}；读回：${providers(saved.download_methods)}）` : '';
        throw new Error(`保存结果未确认：${labels.join('、')}与提交值不一致${detail}，修改已保留，请重试；持续出现时将此提示提供给维护人员`);
      }
      if (instance !== state.instanceId) throw new Error('核对期间所选实例发生变化，请返回原实例确认设置');
      settingsDirty = false; providerDraft = null;
      fillConfig(saved);
      $('#settings-save-state').textContent = '当前实例设置已保存，后续领取的视频使用新策略；正在执行的视频保持本次配置。';
      toast('设置已保存。');
      await refresh();
    } catch (error) {
      $('#settings-save-state').textContent = settingsDirty || providerDraft
        ? `保存失败：${error.message || '请求失败，请稍后重试。'}；修改已保留，请重试保存。`
        : `设置已提交，重新读取时发生错误：${error.message || '请求失败，请刷新页面确认。'}`;
      throw error;
    } finally {
      form.inert = false; form.removeAttribute('aria-busy');
      downloadMethodBusy = false; renderDownloadMethod();
    }
  });
});

async function init() {
  $('#today').textContent = new Date().toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).replaceAll('/', '.');
  renderMedia(); renderTasks();
  setView(location.hash.slice(1) || 'overview');
  try {
    await initializeFleet();
    const config = await api('/api/config');
    fillConfig(config.config);
  } catch (error) { toast(`读取实例配置失败：${error.message}`, true); }
  try {
    selectedJobId = /^[a-f0-9]{32}$/.test(initialJobId || '') ? initialJobId : '';
    clearJobLink();
    await refreshSelectedJob().catch(error => toast(error.message, true));
    await loadDownloadJobs();
    await pollStartup(); await refresh();
  }
  catch (error) { toast(`读取工作台失败：${error.message}`, true); }
  let failures = 0;
  setInterval(async () => {
    if (document.hidden) return;
    try { await refresh({ poll: true }); failures = 0; }
    catch { failures += 1; if (failures === 3) toast('暂时无法连接服务，正在自动重试。', true); }
  }, 2000);
}
init();
