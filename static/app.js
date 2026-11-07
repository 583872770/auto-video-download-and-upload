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
let cookiePoolDraft = null;
let startupPolling = false;
state.startup = { state: 'idle' };
const qualityLabel = (value, provider) => !value || value === 'best' ? `尽可能最高画质${provider === 'downmp3' ? '（当前最高 1080p）' : ''}` : `${value}p`;
const providerNames = { yt_dlp: 'yt-dlp', yt5s: 'yt5s', downmp3: 'downmp3', yt1d: 'yt1d', bergung: 'Bergung / Cobalt' };
const selectedProviders = () => state.config?.download_methods || (state.config?.download_method === 'yt5s_fallback' ? ['yt5s', 'yt_dlp'] : [state.config?.download_method || 'yt_dlp']);
let nodeSignature = '';
let rotationDraft = null;
let rotationDirty = false;
let rotationSavedSignature = '';
let rotationNodeSignature = '';
const pages = Object.fromEntries(['tasks', 'media'].map(name => [name, { cursor: null, next: null, history: [] }]));
state.overview = { recent_media: [], recent_tasks: [], media: { count: 0 } };
state.batchTasks = [];

const actionNames = { download: '下载视频', subtitles: '补下载字幕', check: '下载环境检查', import: '导入下载清单', index: '同步素材索引', clear_media: '清空素材库' };
const actionIcons = { download: 'download', check: 'server', import: 'upload', index: 'refresh' };
const statusNames = { queued: '排队中', running: '进行中', succeeded: '已完成', failed: '失败', interrupted: '已中断' };
const viewNames = {
  instances: ['下载实例', '下载实例', '汇总全部实例的处理进度、传输速度与异常；每个实例可单独启动和停止。'],
  overview: ['下载工作台', '下载工作台', '选择实例与任务，启动下载并查看视频处理进度。'],
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
  const { responseType, timeoutMs, signal, ...fetchOptions } = options;
  if (fetchOptions.body && !(fetchOptions.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    fetchOptions.body = JSON.stringify(fetchOptions.body);
  }
  const reading = ['GET', 'HEAD'].includes((fetchOptions.method || 'GET').toUpperCase());
  const controller = new AbortController();
  const abort = () => controller.abort(signal.reason);
  if (signal?.aborted) abort();
  else signal?.addEventListener('abort', abort, { once: true });
  const timer = setTimeout(() => controller.abort(new DOMException('Request timed out', 'TimeoutError')),
    timeoutMs ?? (responseType === 'file' ? 120000 : reading ? 12000 : 90000));
  try {
    const response = await fetch(path, { ...fetchOptions, headers, signal: controller.signal });
  if (responseType === 'file' && response.ok && !response.redirected
      && response.headers.get('Content-Disposition')?.toLowerCase().startsWith('attachment')) return response;
  const raw = await response.text();
  let data;
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch {
    if (response.redirected || response.url.includes('/login')) {
      throw new Error('管理登录已过期，请刷新页面后重新登录。');
    }
    throw new Error(`服务返回了非 JSON 响应（HTTP ${response.status}），请刷新页面后重试。`);
  }
  if (response.status === 401) { location.assign('/login'); throw new Error('请先登录管理账户。'); }
  if (!response.ok) { const error = new Error(data.error || `请求失败（${response.status}）`); error.status = response.status; throw error; }
  return data;
  } catch (error) {
    if (controller.signal.aborted && controller.signal.reason?.name === 'TimeoutError') {
      const failure = new Error(reading ? '读取超时，稍后自动重试；当前显示上次成功的数据。' : '操作等待超时，结果尚未确认，请刷新查看后再操作。');
      failure.name = 'TimeoutError';
      throw failure;
    }
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
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
  if (name === 'settings' && $('#youtube-cookie-list')) loadCookieProfiles().catch(error => {
    $('#youtube-cookie-list').textContent = error.message;
  });
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
  return tasks.map(task => `<button class="task-row" data-task="${task.id}"><span class="task-action-icon">${icon(actionIcons[task.action] || 'list')}</span><span class="task-row-text"><strong>${escapeHTML(task.progress?.title || task.video_id || actionNames[task.action] || task.action)}</strong><small>${['download', 'import', 'subtitles'].includes(task.action) ? progressLabel(task) : task.action === 'index' ? '同步素材索引' : '本机工具'} · ${task.id.slice(0, 8)}${task.instance_id ? ' &middot; ' + escapeHTML(instanceName(task.instance_id)) : ''}</small></span><span class="task-status status-${task.status}">${taskStatus(task)}</span><time class="task-time" title="${task.finished_at ? '结束时间' : '创建时间'}">${escapeHTML(formatDate(task.finished_at || task.created_at))}</time>${icon('arrow', 'task-arrow')}</button>`).join('');
}
function taskPaused(task) {
  if (!['download', 'subtitles'].includes(task.action) || task.status !== 'queued') return false;
  const item = state.fleet?.items.find(item => item.id === task.instance_id);
  return task.instance_id && task.instance_id !== state.instanceId ? !!item?.paused : !!state.workbench?.paused;
}
function taskStatus(task) {
  return taskPaused(task) ? '等待启动' : statusNames[task.status] || task.status;
}
function etaText(seconds) {
  if (!Number.isFinite(seconds)) return '—';
  const value = Math.ceil(seconds);
  return value >= 3600 ? `${Math.floor(value / 3600)}小时${Math.floor(value % 3600 / 60)}分` : value >= 60 ? `${Math.floor(value / 60)}分${value % 60}秒` : `${value}秒`;
}
function progressLabel(task) {
  if (taskPaused(task)) return '实例已停止 · 等待启动';
  if (task.action === 'subtitles') {
    if (task.status === 'failed') return '字幕补下载失败 · 视频已保留';
    if (task.status === 'succeeded') return task.result?.skipped ? '字幕任务已跳过' : '字幕任务已处理';
    if (task.status === 'interrupted') return '字幕任务已中断 · 视频已保留';
    if (task.status === 'queued') return task.progress?.stage === 'retrying' ? '字幕等待退避重试' : '字幕排队中 · 独立限速';
    if (task.progress?.stage === 'uploading_oss') return '正在同步字幕到阿里云 OSS';
    return task.progress?.stage === 'disk_wait' ? '字幕等待临时空间' : '正在补下载字幕 · 视频已完成';
  }
  if (task.status === 'queued' && task.progress?.stage === 'paused') return '等待启动';
  if (task.status === 'queued') return task.progress?.stage === 'retrying' ? '等待退避重试' : '排队中';
  if (task.action === 'import' && task.status === 'running') return `正在导入 · 新增 ${task.progress?.added || 0} · 重复 ${task.progress?.duplicates || 0} · 无效 ${task.progress?.invalid || 0}`;
  if (task.status === 'failed') return '下载失败';
  if (task.status === 'interrupted') return '已中断';
  if (task.status === 'succeeded') return task.result?.test_mode ? '测试完成 · 文件已删除' : task.result?.skipped ? '已下载过 · 已跳过' : '下载完成';
  const stage = task.progress?.stage;
  if (stage === 'preparing_workspace') return '准备保存目录';
  if (stage === 'uploading_oss') return '正在上传阿里云 OSS';
  if (stage === 'disk_wait') return '等待磁盘空间或临时文件预算';
  if (stage === 'preparing_proxy') return '准备代理节点';
  if (stage === 'requesting_api') return 'downmp3 · 提交转换请求';
  if (stage === 'waiting_api_conversion') return 'downmp3 · 网站正在转换（尚未传输）';
  if (stage === 'waiting_cookie') return '等待 Cookie 会话名额 / 启动窗口';
  if (stage === 'waiting_resolver') return `${providerNames[task.progress?.download_method] || '下载器'} · 等待解析名额 / 渠道配额`;
  if (stage === 'switching_provider') return `正在切换到 ${providerNames[task.progress?.download_method] || '下一个下载器'}`;
  const browserStages = { waiting_browser: '等待浏览器名额', starting_browser: '启动浏览器', connecting_browser: '连接浏览器', opening_site: '打开网页', waiting_site_form: '等待网页输入框', submitting_site: '提交视频链接', waiting_verification: '等待网站验证', converting: '网站正在转换' };
  if (browserStages[stage]) return `${providerNames[task.progress?.download_method] || '下载器'} · ${browserStages[stage]}`;
  return { waiting_request: '\u7b49\u5f85 YouTube \u8bf7\u6c42\u95f4\u9694', checking_environment: '检查下载环境', checking_login: '正在检测 YouTube 登录', extracting: '正在解析视频', waiting_transfer: '已解析 · 等待传输', waiting_processing: '等待本地后处理名额', downloading: '正在传输', processing: '合并 / 处理文件', cleaning: '测试下载完成 · 正在清理文件', skipped: '已下载过 · 已跳过', completed: '下载完成' }[stage] || '准备下载';
}
function progressVisualState(task) {
  if (task.status === 'failed') return { className: 'failed', order: 90, label: '失败' };
  if (task.status === 'interrupted') return { className: 'failed', order: 91, label: '已中断' };
  if (task.status === 'succeeded') return { className: 'success', order: 92, label: '已完成' };
  if (task.status === 'queued') return { className: 'queued', order: 80, label: taskPaused(task) ? '等待启动' : '排队中' };
  const stage = task.progress?.stage || '';
  if (stage === 'downloading') return { className: 'transferring', order: 0, label: '正在传输' };
  if (stage === 'uploading_oss') return { className: 'finalizing', order: 30, label: '上传 OSS' };
  if (stage === 'processing' || stage === 'cleaning') return { className: 'finalizing', order: 30, label: stage === 'cleaning' ? '正在清理' : '合并 / 处理' };
  if (stage.startsWith('waiting_') || stage === 'disk_wait' || stage === 'switching_provider') {
    const labels = { waiting_request: '等待请求', waiting_cookie: '等待 Cookie', waiting_transfer: '等待传输', waiting_processing: '等待后处理', waiting_resolver: '等待解析', waiting_browser: '等待浏览器', waiting_verification: '等待验证', waiting_api_conversion: '等待转换', disk_wait: '等待空间', switching_provider: '切换渠道' };
    return { className: 'waiting', order: 20, label: labels[stage] || '等待资源' };
  }
  if (stage === 'extracting' || stage === 'checking_environment' || stage === 'checking_login' || stage === 'preparing_workspace' || stage === 'preparing_proxy') {
    return { className: 'preparing', order: 10, label: stage === 'extracting' ? '正在解析' : '准备中' };
  }
  return { className: 'preparing', order: 15, label: '处理中' };
}
function liveTransferSpeed(task) {
  const p = task.progress || {};
  if (task.status !== 'running' || !['downloading', 'uploading_oss'].includes(p.stage)) return null;
  const stamp = task.progress_updated_at || p._reported_at;
  const bytesAt = p.stage === 'uploading_oss' ? p.oss_last_bytes_at : p.last_bytes_at;
  if ((stamp && Date.now() / 1000 - stamp > 10) || (bytesAt && Date.now() / 1000 - bytesAt > 10)) return null;
  return Number.isFinite(p.speed) && p.speed >= 0 ? p.speed : null;
}
function progressFreshness(task) {
  if (task.status !== 'running') return '';
  const p = task.progress || {}, now = Date.now() / 1000;
  const stamp = task.progress_updated_at || p._reported_at;
  const bytesAt = p.stage === 'uploading_oss' ? p.oss_last_bytes_at : p.last_bytes_at;
  const facts = [];
  if (stamp) facts.push(`进度更新于 ${etaText(Math.max(0, now - stamp))}前`);
  if (bytesAt) facts.push(`字节变化于 ${etaText(Math.max(0, now - bytesAt))}前`);
  if (task.lease_until) facts.push(task.lease_until > now ? '执行心跳租约有效' : '执行租约待确认');
  return facts.length ? `<p class="micro-copy progress-freshness">${facts.join(' · ')}</p>` : '';
}
function downloadProgress(task) {
  const data = task.progress || {};
  const active = task.status === 'running';
  const queued = task.status === 'queued';
  const stopped = task.status === 'queued' && data.stage === 'paused';
  const converting = active && data.stage === 'waiting_api_conversion';
  const uploading = data.stage === 'uploading_oss';
  const transferred = uploading ? data.oss_uploaded_bytes : data.downloaded_bytes;
  const total = uploading ? data.oss_total_bytes : data.total_bytes;
  const measured = uploading ? (total > 0 ? 100 * (transferred || 0) / total : null) : converting ? data.provider_percent : data.percent;
  const percentage = task.status === 'queued' && !stopped ? 0 : task.status === 'succeeded' ? 100 : Number.isFinite(measured) ? Math.max(0, Math.min(100, measured)) : null;
  const label = task.status === 'queued' && !stopped ? '等待开始' : percentage === null ? (converting ? '等待网站转换' : '大小未知') : `${converting ? '转换 ' : ''}${percentage.toFixed(1)}%`;
  const importing = task.action === 'import';
  const speed = !active ? '—' : importing && Number.isFinite(data.records_per_second) ? `${data.records_per_second.toLocaleString('zh-CN', { maximumFractionDigits: 0 })} 条/秒` : liveTransferSpeed(task) !== null ? `${bytes(liveTransferSpeed(task))}/s` : data.stage === 'downloading' ? '计算中…' : '—';
  const provider = providerNames[data.download_method] || '';
  const stageSeconds = active && data.stage_started_at ? Math.max(0, Date.now() / 1000 - data.stage_started_at) : null;
  const slowest = Object.entries(data.stage_seconds || {}).filter(([stage]) => !active || stage !== data.stage).sort((a, b) => b[1] - a[1])[0];
  const stageName = stage => progressLabel({ action: 'download', status: 'running', progress: { stage } });
  const timing = stageSeconds !== null || slowest ? `<p class="micro-copy stage-timing">${stageSeconds !== null ? `本阶段已用 ${etaText(stageSeconds)}` : `${queued ? '上次' : '本次'}执行已记录用时 ${etaText(data.elapsed || 0)}`}${slowest ? ` · ${active ? '已结束阶段中最久' : '耗时最长阶段'}：${escapeHTML(stageName(slowest[0]))} ${etaText(slowest[1])}` : ''}</p>` : '';
  const methods = data.selected_download_methods;
  const policy = Array.isArray(methods) && methods.length ? `<p class="micro-copy execution-policy">${queued ? '上次' : '本次'}下载方式：${escapeHTML(methods.map(method => providerNames[method] || method).join('、'))}${queued ? '；下次启动处理时使用该实例的最新设置。' : ''}${task.instance_id && fleetEnabled ? ` <a href="/?instance_id=${encodeURIComponent(task.instance_id)}#settings">实例设置：${escapeHTML(instanceName(task.instance_id))} ↗</a>` : ''}</p>` : '';
  const cookie = active && data.cookie_profile_name ? `<span class="active-cookie-metric">Cookie <b>${escapeHTML(data.cookie_profile_name)}</b></span>` : '';
  return `<div class="download-meter"><div class="download-meter-label"><span>${escapeHTML(progressLabel(task))}${active && data.stage === 'downloading' && data.format ? ` · ${escapeHTML(data.format)}` : ''}</span><strong>${label}</strong></div><progress max="100"${percentage === null ? '' : ` value="${percentage}"`} aria-label="${importing ? '导入' : uploading ? '上传' : '下载'}进度"></progress><div class="download-metrics"><span>${importing ? '已读取' : uploading ? '当前文件已上传' : queued ? '上次已下载' : '已下载'} <b>${Number.isFinite(transferred) ? bytes(transferred) : '—'}</b>${total ? ` / ${bytes(total)}` : ''}</span><span>${importing ? '处理速度' : '传输速度'} <b>${speed}</b></span><span>${importing ? '预计剩余' : '当前文件剩余'} <b>${active ? etaText(data.eta) : '—'}</b></span>${cookie}${provider ? `<span>${queued ? '上次下载器' : '下载器'} <b>${provider}${data.fallback_used ? '（已自动切换）' : ''}</b></span>` : ''}${data.proxy_node ? `<span>${queued ? '上次节点' : '节点'} <b>${escapeHTML(data.proxy_node)}</b></span>` : ''}</div>${uploading && data.oss_object ? `<p class="micro-copy">OSS 对象：${escapeHTML(data.oss_object)}</p>` : ''}${policy}${timing}${progressFreshness(task)}${data.fallback_reason ? `<p class="micro-copy">${escapeHTML(data.fallback_from || "上一下载器")} 失败原因：${escapeHTML(data.fallback_reason)}</p>` : ''}</div>`;
}
function renderBatches() {
  const tasks = state.batchTasks.filter(task => task.batch_id === selectedJobId);
  if (!selectedJobId) {
    $('#batch-count').textContent = '0';
    $('#batch-summary').textContent = '查看所选任务在当前实例的处理进度。';
    $('#batch-progress').innerHTML = emptyState('请选择下载任务', '任务视频在任务中心准备；选择任务后可以开始下载。');
    return;
  }
  const queue = state.workbench || {};
  $('#batch-count').textContent = tasks.length;
  $('#batch-summary').textContent = `${selectedJob?.name || '所选任务'} · 当前实例执行中的视频，按处理阶段排序`;
  const orderedTasks = [...tasks].sort((a, b) => {
    const left = progressVisualState(a), right = progressVisualState(b);
    return left.order - right.order || (Number(a.seq) || 0) - (Number(b.seq) || 0);
  });
  $('#batch-progress').innerHTML = orderedTasks.length ? orderedTasks.map((task, index) => { const visual = progressVisualState(task); const cookie = task.status === 'running' && task.progress?.cookie_profile_name ? ` · Cookie：${escapeHTML(task.progress.cookie_profile_name)}` : ''; return `<article class="batch-video batch-video-${visual.className}" data-download-id="${task.id}" data-progress-state="${visual.className}"><div class="batch-video-heading"><span class="batch-video-index">${index + 1}</span><div class="batch-video-title"><strong title="${escapeHTML(task.progress?.title || task.url || '')}">${escapeHTML(task.progress?.title || task.video_id || task.url || '视频')}</strong><small>${escapeHTML(task.video_id || task.id.slice(0, 8))}${task.instance_id ? ' &middot; ' + escapeHTML(instanceName(task.instance_id)) : ''}${cookie}</small></div><span class="batch-video-state"><i aria-hidden="true"></i>${visual.label}</span><button type="button" class="text-button" data-task="${task.id}">查看日志 ↗</button></div>${downloadProgress(task)}${task.error ? `<p class="batch-video-error">${escapeHTML(readableError(task.error))}</p>` : ''}</article>`; }).join('') : emptyState('当前实例没有正在处理的所选任务视频', state.workbench?.paused ? '启动后，在这里查看视频处理进度。' : '视频开始处理后在这里显示进度；已结束的视频请到任务详情查看。');
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
function renderWorkbenchSpeed() {
  const workbench = state.workbench;
  const transfers = (workbench?.active_tasks || []).filter(task => task.status === 'running' && task.progress?.stage === 'downloading');
  const measured = transfers.filter(task => liveTransferSpeed(task) !== null);
  const available = workbench && workbench.online !== false;
  const rate = measured.reduce((sum, task) => sum + liveTransferSpeed(task), 0);
  const known = available && (!transfers.length || measured.length);
  const [value, unit] = known ? bytes(rate).split(' ') : ['—', ''];
  $('#stat-speed').textContent = value;
  $('#stat-speed-unit').textContent = unit ? `${unit}/s` : '';
  $('#speed-caption').textContent = !workbench ? '等待实例状态' : !available ? '当前实例离线' : !transfers.length ? '当前没有视频传输' :
    measured.length < transfers.length ? '部分视频速度待更新' : '汇总当前正在传输的视频速度';
}
function renderOutputSpeed() {
  const element = $('#stat-output-speed');
  if (!element) return;
  const session = state.workbench?.session;
  const value = session?.output_mbps;
  element.textContent = value === null || value === undefined || !Number.isFinite(Number(value))
    ? '—' : Number(value).toFixed(2);
  const recent = session?.output_recent_mbps;
  $('#stat-output-recent').textContent = recent === null || recent === undefined || !Number.isFinite(Number(recent))
    ? '—' : Number(recent).toFixed(2);
  const tip = $('#stat-output-speed-tip');
  if (tip) {
    const elapsed = Number(session?.elapsed_seconds || 0);
    const detail = session
      ? `本次会话 #${session.seq}：计费大小 ${decimalGB(session.file_bytes_exact)} + 测试大小 ${decimalGB(session.test_file_bytes_exact)}；会话用时 ${etaText(elapsed)}。`
      : '当前实例暂无进行中的下载会话；启动后开始统计。';
    tip.innerHTML = `<strong>计算口径</strong><span>本次会话平均速度 =（计费成品字节数 + 测试成品字节数）× 8 ÷ 会话用时（秒）÷ 1,000,000，单位 Mbps。</span><span>近 10 分钟平均只累计最近 600 秒完成登记的成品；会话不足 10 分钟时，除以实际会话时长。没有成品产出的等待时间也计入。</span><span>${escapeHTML(detail)}</span><span>用时包含解析、等待和重试。已删除的测试文件仍计入产出，不计费。重新启动实例后按新会话统计。</span>`;
  }
  const foot = $('#stat-output-speed-foot');
  if (foot) foot.textContent = session
    ? `本次用时 ${etaText(Number(session.elapsed_seconds || 0))}${Number(session.output_recent_seconds) < 600 ? ` · 近均按 ${etaText(Number(session.output_recent_seconds || 0))}` : ' · 含计费与测试'}` : '暂无当前会话';
  renderOutputChart(session);
}

let outputChartSelection = null;
let outputChartSession = null;
const outputChartTime = value => new Date(value * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false});
function renderOutputChart(session) {
  const chart = $('#output-chart-points');
  if (!chart) return;
  const history = session?.output_history;
  const points = history?.points || [];
  $('#output-chart-content').hidden = !points.length;
  $('#output-chart-empty').hidden = !!points.length;
  if (session?.id !== outputChartSession) { outputChartSelection = null; outputChartSession = session?.id; }
  if (!points.length) return;
  const peak = Math.max(0, ...points.map(point => Number(point.output_mbps) || 0));
  const magnitude = 10 ** Math.floor(Math.log10(peak || 1));
  const scale = Math.max(1, Math.ceil(peak / magnitude / 2) * magnitude * 2);
  const tick = value => value >= 10 ? value.toFixed(0) : value.toFixed(1).replace(/\.0$/, '');
  $('#output-chart-y').innerHTML = `<span>${tick(scale)}</span><span>${tick(scale / 2)}</span><span>0</span>`;
  const signature = `${session.id}:${points[0].start_at}`;
  if (chart.dataset.window !== signature) {
    const hadFocus = chart.contains(document.activeElement);
    chart.replaceChildren(...points.map(point => {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'output-chart-point'; button.dataset.start = point.start_at;
      button.innerHTML = '<span></span>';
      return button;
    }));
    chart.dataset.window = signature;
    if (hadFocus) [...chart.children].find(button => Number(button.dataset.start) === outputChartSelection)?.focus();
  }
  let line = '', currentLine = '', previous = null;
  for (let index = 0; index < points.length; index++) {
    const point = points[index], button = chart.children[index];
    const available = point.output_mbps !== null;
    const current = index === points.length - 1;
    button.classList.toggle('is-empty', !available);
    button.classList.toggle('is-current', current);
    const height = available ? point.output_mbps / scale : 0;
    button.firstElementChild.style.top = `${(1 - height) * 100}%`;
    const coordinates = `${((index + .5) / points.length * 1000).toFixed(2)},${((1 - height) * 140).toFixed(2)}`;
    if (!available) previous = null;
    else {
      if (current && previous) currentLine = `M${previous} L${coordinates}`;
      else line += `${previous ? ' L' : ' M'}${coordinates}`;
      previous = coordinates;
    }
    button.setAttribute('aria-label', outputChartDescription(point, current));
    button.setAttribute('aria-pressed', String(Number(button.dataset.start) === outputChartSelection));
  }
  $('#output-chart-line-complete').setAttribute('d', line.trim());
  $('#output-chart-line-current').setAttribute('d', currentLine);
  $('#output-chart-x').innerHTML = [points[0], points[14], points[29]].map(point => `<span>${outputChartTime(point.start_at)}</span>`).join('');
  renderOutputChartDetail();
}
function outputChartDescription(point, current) {
  const period = `${outputChartTime(point.start_at)}–${outputChartTime(point.end_at)}`;
  return point.output_mbps === null ? `${period} · 会话尚未开始或正在计时` :
    `${period} · ${Number(point.output_mbps).toFixed(2)} Mbps · ${point.file_count} 个成品 · ${bytes(point.file_bytes)}${current ? ' · 本分钟累计中' : ''}${point.partial ? ` · 按实际 ${Number(point.elapsed_seconds).toFixed(0)} 秒计算` : ''}`;
}
function renderOutputChartDetail() {
  const points = state.workbench?.session?.output_history?.points || [];
  if (!points.length) return;
  const index = points.findIndex(point => point.start_at === outputChartSelection);
  const selected = index < 0 ? points.length - 1 : index;
  $('#output-chart-detail').textContent = outputChartDescription(points[selected], selected === points.length - 1);
  $$('.output-chart-point').forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.start) === points[selected].start_at)));
}
for (const eventName of ['pointerover', 'focusin', 'click']) {
  $('#output-chart-points')?.addEventListener(eventName, event => {
    const button = event.target.closest('.output-chart-point');
    if (!button) return;
    outputChartSelection = Number(button.dataset.start);
    renderOutputChartDetail();
  });
}
document.addEventListener('click', event => {
  const help = event.target.closest('.metric-help-button');
  const open = $('.metric-help-wrap.is-open');
  if (help) {
    const wrap = help.closest('.metric-help-wrap');
    wrap.classList.toggle('is-open');
    if (open && open !== wrap) open.classList.remove('is-open');
    event.stopPropagation();
  } else if (open && !event.target.closest('.metric-help-wrap')) {
    open.classList.remove('is-open');
  }
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') $('.metric-help-wrap.is-open')?.classList.remove('is-open');
});
function renderTasks() {
  const counts = state.overview.download_counts || {};
  $('#stat-active').textContent = selectedJob?.queued || 0;
  renderOutputSpeed();
  renderWorkbenchSpeed();
  $('#stat-complete').textContent = selectedJob?.file_count || 0;
  $('#nav-task-count').textContent = state.counts ? ['succeeded', 'failed', 'interrupted'].reduce((sum, status) => sum + (state.counts[status] || 0), 0) : state.tasks.length;
  $('#active-caption').textContent = selectedJob ? '本任务在全部实例间共享的待下载视频' : '选择任务后查看待下载视频';
  $('#all-tasks').innerHTML = state.tasks.length ? taskRows(state.tasks) : emptyState('暂无已结束的任务', '可调整搜索条件；任务结束后会自动显示在这里。');
  renderBatches();
}
function renderStatus() {
  if (!state.status) return;
  renderConcurrency();
  renderOutputSpeed();
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
    element.textContent = auth?.state === 'valid' ? 'YouTube 登录已验证' : auth?.state === 'rate_limited' ? 'YouTube 请求限流 · 不代表登录失效' : !state.status.youtube?.saved ? '尚未登录 YouTube' : auth?.state === 'unchecked' ? 'Cookie 已保存 · 待检测登录' : '登录检查未通过';
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
let refreshedLoginCookie = '';
function renderYouTubeLogin() {
  const login = state.status?.youtube?.login;
  const busy = youtubeLoginBusy || !!login?.active;
  $('#youtube-login-start').disabled = !login || busy;
  $('#youtube-login-start').textContent = login?.active ? '登录进行中…' : login?.state === 'idle' ? '打开登录窗口' : '重新打开登录窗口';
  $('#youtube-login-cancel').hidden = !login?.active;
  $('#youtube-login-cancel').disabled = youtubeLoginBusy || ['cancelling', 'committing'].includes(login?.state);
  const status = $('#youtube-login-status');
  status.textContent = login?.message || '正在读取登录状态…';
  status.classList.toggle('error', ['failed', 'interrupted'].includes(login?.state));
  status.classList.toggle('success', login?.state === 'succeeded');
  const connection = state.clash?.mode === 'clash' ? `Clash · ${state.clash.selected || '尚未选择节点'}` : state.config?.proxy_mode === 'manual' && state.config?.proxy ? '手动代理' : '直连';
  $('#youtube-login-connection').textContent = `登录连接：${login?.active ? login.connection : connection}`;
  $$('[data-youtube-login-summary]').forEach(element => {
    element.hidden = !login || login.state === 'idle';
    element.textContent = login?.message || '';
  });
  const savedLogin = login?.state === 'succeeded' ? `${state.instanceId || 'local'}:${login.id}` : '';
  if (savedLogin && refreshedLoginCookie !== savedLogin) {
    refreshedLoginCookie = savedLogin;
    loadCookieProfiles().catch(error => {
      if (refreshedLoginCookie === savedLogin) refreshedLoginCookie = '';
      toast(`Cookie 已保存，列表刷新失败：${error.message}`, true);
    });
  }
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
  renderConcurrencyDraft();
  renderCookiePoolSaveState();
}
function renderConcurrencyDraft() {
  renderYouTubePacingRuntime();
  const form = $('#settings-form');
  const number = name => Number(form.elements[name]?.value || 0);
  if (state.config?.youtube_cookie_pool?.length) {
    $('#concurrency-config-note').textContent = `Cookie 池独立调度：每会话预热前 1 个任务，预热后最多 ${number('youtube_cookie_session_limit')} 个；不使用运行模式与实例并发上限。相同核心会话合并计数，总执行容量最多 64；启动间隔、冷却及渠道连接额度仍生效。`;
    $('#concurrency-config-note').classList.remove('capacity-draft-warning');
    return;
  }
  const auto = form.elements.scheduler_mode?.value === 'auto';
  const requested = number('worker_count'), channel = number('builtin_transfer_limit') || (auto ? 32 : 8);
  const shared = number('builtin_connection_limit') || (auto ? 64 : 16);
  const connection = number('download_connection_limit');
  const fragments = Math.min(number('fragment_concurrency') || 4, shared, Math.max(1, Math.floor(connection / Math.max(1, requested))));
  const methods = providerDraft || selectedProviders();
  const native = methods.includes('yt_dlp');
  const cap = Math.min(requested, channel, native ? Math.floor(Math.min(connection, shared) / fragments) : requested);
  const warning = requested > cap;
  $('#concurrency-config-note').textContent = `待保存配置：实例上限 ${requested}，内置渠道上限 ${channel}，本机 / 共享连接预算 ${connection} / ${shared}。${warning ? `受这些设置限制，单个内置渠道最多约 ${cap} 路。` : '内置渠道配置与实例上限一致或更高。'}共享上游、其他实例、临时预算与限流仍可能降低实际额度。`;
  $('#concurrency-config-note').classList.toggle('capacity-draft-warning', warning);
}
$('#settings-form').addEventListener('input', renderConcurrencyDraft);
$('#settings-form').addEventListener('change', renderConcurrencyDraft);
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
  if (key === 'oss_endpoint') {
    const endpoint = String(value ?? '').trim().replace(/\/+$/, '');
    return endpoint && !endpoint.includes('://') ? `https://${endpoint}` : endpoint;
  }
  if (key === 'oss_prefix') return String(value ?? '').replace(/^\/+|\/+$/g, '');
  if ($(`#settings-form [name="${key}"]`)?.type === 'number') return String(Number(value));
  return Array.isArray(value) ? JSON.stringify(value) : String(value ?? '');
}
function unconfirmedSettings(data, config) {
  return Object.keys(data).filter(key => ['oss_access_key_secret', 'oss_security_token'].includes(key)
    ? !!data[key] && !config[key + '_configured']
    : settingsValue(key, data[key]) !== settingsValue(key, config[key]));
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
  $$('[data-provider-setting]', $('#settings-form')).forEach(field => {
    field.hidden = !field.dataset.providerSetting.split(/\s+/).some(method => methods.includes(method));
  });
  renderStrategySaveState();
  renderProviderChoices($('#download-method'), methods, downloadMethodBusy || !state.config);
  const tools = state.status?.tools;
  const auto = $('#settings-form [name="scheduler_mode"]').value === 'auto';
  const notes = [auto ? `自动候选：${methods.map(method => providerNames[method]).join('、')}。各入口按实际吞吐、稳定性及当前负载分配任务，不使用下载器优先级。修改后点击下方“保存设置”生效。` : `尝试顺序：${methods.map(method => providerNames[method]).join(' → ')}。勾选启用，上下箭头调整优先级；成功后停止，失败后尝试下一项。修改后统一点击下方“保存设置”生效。`];
  const throughput = $('#settings-form [name="throughput_mode"]');
  const pooled = !!state.config?.youtube_cookie_pool?.length;
  throughput.disabled = pooled;
  $('#settings-form [name="worker_count"]').disabled = pooled;
  $('#settings-form [name="bandwidth_mbps"]').disabled = pooled;
  const controlNote = $('#control-mode-note');
  if (controlNote) controlNote.textContent = pooled
    ? 'Cookie 池已启用，按有效会话与每会话任务上限独立调度。运行模式、实例并发上限和自动调节带宽目标暂不生效；关闭池后恢复原设置。'
    : throughput.value === 'auto'
    ? '自动调节：依据媒体下载吞吐、资源压力和渠道状态，在实例并发上限内调整并发。'
    : '自定义：保持设定的实例并发上限；启动间隔、渠道、连接及冷却仍会限制实际数量。';
  if (methods.includes('downmp3')) notes.push(`downmp3 ${qualityLabel(state.config?.downmp3_quality, 'downmp3')}，转换等待上限 ${state.config?.downmp3_timeout ?? 300} 秒。`);
  if (methods.includes('yt5s')) notes.push(`yt5s ${qualityLabel(state.config?.yt5s_quality, 'yt5s')}，浏览器并发 ${state.config?.yt5s_browser_count ?? 2}。${tools && (!tools.yt5s_browser || !tools.yt5s_display) ? '当前缺少浏览器或显示环境。' : '可能需要网站验证。'}`);
  if (methods.includes('yt1d')) notes.push(`yt1d ${qualityLabel(state.config?.yt1d_quality, 'yt1d')}，验证 / 转换每阶段等待上限 ${state.config?.yt1d_timeout ?? 180} 秒，与 yt5s 共享 ${state.config?.yt5s_browser_count ?? 2} 个浏览器名额，可能需要网站验证。`);
  if (methods.includes('bergung')) notes.push(`Bergung ${qualityLabel(state.config?.bergung_quality, 'bergung')}，验证等待上限 ${state.config?.bergung_timeout ?? 180} 秒；与其他网页下载器共享浏览器名额，可能需要人工完成 Cloudflare 验证。`);
  notes.push(methods.includes('yt_dlp') ? '开始前检查 YouTube Cookie；yt-dlp 默认选择最高分辨率和帧率，不限制编码。' : '当前选择无需 YouTube Cookie。');
  notes.push('保存后仅影响当前实例后续领取的视频，无需暂停或重启；其他实例使用各自策略，正在执行的视频保持本次配置。');
  $('#download-method-note').textContent = notes.join(' ');
  renderConcurrencyDraft();
}
function renderStrategySaveState() {
  const target = state.instanceId ? `实例 ${instanceName(state.instanceId)}` : '本机';
  $('#strategy-save-state').textContent = downloadMethodBusy ? `正在保存并核对 · ${target}`
    : settingsDirty || providerDraft ? `${target}有未保存修改，请点击下方“保存设置”`
    : `已保存 · ${target} · 仅作用于当前实例后续领取的视频`;
}
$('#settings-form [name="scheduler_mode"]').addEventListener('change', renderDownloadMethod);
$('#settings-form [name="throughput_mode"]').addEventListener('change', renderDownloadMethod);
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
  const oss = $('#storage-mode').value === 'oss';
  $$('[data-oss-storage]').forEach(panel => {
    panel.hidden = !oss;
    $$('input', panel).forEach(input => { input.disabled = !oss; });
  });
  for (const key of ['oss_access_key_secret', 'oss_security_token']) {
    const input = $(`[name="${key}"]`);
    if (input) input.placeholder = state.config?.[key + '_configured'] ? '已保存，留空保持不变' : '未保存，可填写或使用环境变量';
  }
  $$('[data-local-storage]').forEach(label => {
    label.hidden = test;
    $('input', label).disabled = test;
  });
  $('#storage-mode-note').textContent = test ? '真实下载后自动删除视频、封面、字幕及元数据，仅保留任务记录和日志，不加入素材库。此模式只作用于当前实例后续领取的视频。' : oss ? '视频与配套文件上传到阿里云 OSS，同时保留本地副本；请在“存储与清理”填写 OSS 参数。上传成功后任务才算完成。' : '视频及配套文件保存到指定目录，下载完成后可在素材库查看。此模式只作用于当前实例后续领取的视频。';
}
$('#storage-mode').addEventListener('change', renderStorageSettings);
const refreshedAt = {};
let startupSampleAt = 0;
function applyStartup(value, sampledAt) {
  if (value && sampledAt >= startupSampleAt) {
    state.startup = value;
    startupSampleAt = sampledAt;
  }
}
const refreshInFlight = new Map();
const refreshFailures = {};
let refreshFailureInstance;
function applyRefreshData(data, sampledAt = performance.now()) {
    const { media, tasks, clash, overview } = data;
    const mediaBefore = JSON.stringify([state.media, state.overview.media, state.overview.recent_media]);
    if (overview) { state.overview = overview; state.counts = overview.counts; }
    if (media) { state.media = media.items; state.mediaTotal = media.total; pages.media.next = media.next_cursor; }
    if (tasks) { state.tasks = tasks.items; pages.tasks.next = tasks.next_cursor; }
    if (data.status) {
      state.status = data.status;
      if (!fleetEnabled) applyStartup(data.status.startup, sampledAt);
    }
    if (clash) state.clash = clash;
    if ((media || overview) && mediaBefore !== JSON.stringify([state.media, state.overview.media, state.overview.recent_media])) renderMedia();
    if (tasks || overview || data.status) renderTasks();
    if (clash) renderClash();
    if (data.status) { renderDownloadControl(); renderStatus(); }
    renderPagination();
    if (media) {
      $('#media-directory').textContent = media.directory;
      state.mediaDirectory = media.directory;
    }
    const status = state.status;
    if (!status) return;
    $('#database-backend').textContent = `${status.database === 'mysql' ? 'MySQL 最近请求成功' : 'SQLite 测试模式'} · ${status.concurrency?.mode === 'cookie_pool' ? 'Cookie 池独立调度' : `${status.worker_count} 个下载进程`} · 素材按页查询`;
    const cache = status.concurrency?.local_health?.progress_cache;
    if (cache?.enabled) $('#database-backend').textContent += cache.backoff_seconds > 0 ? ' · 进度缓存已回退数据库' : ' · 进度缓存已启用';
    const workers = status.workers || [];
    const paused = workers.filter(worker => worker.state === 'paused');
    $('#worker-status').hidden = !paused.length;
    $('#worker-status').textContent = paused.map(worker => worker.message).join('；');

}
async function refresh({ poll = false, viewChanged = false } = {}) {
  if ((location.hash.slice(1) || 'overview') === 'overview') refreshExecution({force: !poll || viewChanged}).catch(() => {});
  state.polling = true;
  const instance = state.instanceId;
  if (refreshFailureInstance !== instance) {
    refreshFailureInstance = instance;
    for (const name of Object.keys(refreshFailures)) delete refreshFailures[name];
    for (const name of Object.keys(refreshedAt)) delete refreshedAt[name];
  }
  try {
    const view = location.hash.slice(1) || 'overview';
    const stamp = Date.now();
    const due = (name, visible, interval) => !poll || (visible &&
      (viewChanged || stamp >= (refreshFailures[name]?.nextAt || 0)) &&
      (viewChanged || stamp - (refreshedAt[name] || 0) >= interval));
    const requests = {
      media: (!fleetEnabled || state.instanceId) && due('media', view === 'media', 10000) ? `/api/media?${listQuery('media', { limit: 24, q: $('#media-search').value })}` : null,
      tasks: due('tasks', view === 'tasks', 4000) ? `/api/tasks?${listQuery('tasks', { scope: 'download', view: 'history', limit: 50, status: $('#task-filter').value === 'all' ? '' : $('#task-filter').value, q: $('#task-search').value })}` : null,
      status: due('status', true, $('#youtube-login-dialog').open || (!fleetEnabled && view === 'overview') ? 2000 : 10000) ? '/api/status' : null,
      clash: (!fleetEnabled || state.instanceId) && due('clash', view === 'clash', 15000) ? '/api/clash' : null,
      overview: due('overview', true, 10000) ? '/api/overview' : null,
    };
    await Promise.allSettled(Object.entries(requests).filter(([name, path]) => {
      if (!path) return false;
      const previous = refreshInFlight.get(name);
      if (previous && previous.instance === instance && previous.path === path) return false;
      previous?.controller.abort();
      return true;
    }).map(async ([name, path]) => {
      const ticket = { instance, path, sampledAt: performance.now(), controller: new AbortController() };
      refreshInFlight.set(name, ticket);
      try {
        const value = await api(path, { signal: ticket.controller.signal });
        if (instance !== state.instanceId || refreshInFlight.get(name) !== ticket) return;
        refreshedAt[name] = Date.now();
        delete refreshFailures[name];
        if (!['media', 'tasks', 'clash'].includes(name) || view === (location.hash.slice(1) || 'overview')) {
          applyRefreshData({ [name]: value }, ticket.sampledAt);
        }
      } catch (error) {
        if (instance === state.instanceId && refreshInFlight.get(name) === ticket && error.name !== 'AbortError') {
          const failures = (refreshFailures[name]?.failures || 0) + 1;
          refreshFailures[name] = { failures, nextAt: Date.now() + Math.min(30000, 2000 * 2 ** Math.min(failures - 1, 4)), error };
        }
        throw error;
      } finally {
        if (refreshInFlight.get(name) === ticket) refreshInFlight.delete(name);
      }
    }));
    if (instance !== state.instanceId) { state.refreshQueued = true; return; }
    const failed = Object.values(refreshFailures);
    const warning = $('#connection-warning');
    warning.hidden = !failed.length;
    if (failed.length) {
      warning.textContent = `部分状态暂时无法更新：${failed[0].error.message} 当前保留上次成功数据。`;
      $('#database-health').textContent = failed.some(item => item.error.status === 503) ? '数据库：连接异常' : '部分状态等待更新';
    }
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
  renderConcurrency();
  if (state.config) refresh({ poll: true, viewChanged: true }).catch(error => toast(error.message, true));
});
window.addEventListener('hashchange', () => {
  setView(location.hash.slice(1) || 'overview');
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && state.config) refresh({ poll: true, viewChanged: true }).catch(error => toast(error.message, true));
});

const startupNames = { checking_instances: '检查下载实例及其环境', checking_database: '连接数据库', reading_queue: '读取待处理清单', preparing_proxy: '准备代理节点', checking_environment: '检查下载工具', checking_login: '验证 YouTube Cookie', releasing_queue: '启动下载' };
function renderStartup() {
  const value = state.startup;
  $('#download-startup').hidden = !value || value.state === 'idle';
  if (!value || value.state === 'idle') return;
  const labels = { running: '启动检查中', succeeded: '启动检查已完成', failed: '启动未完成', cancelled: '启动已取消' };
  $('#startup-title').textContent = `${value.state === 'running' && value.cancel_requested ? '正在取消启动' : labels[value.state] || value.state} · ${value.elapsed || 0} 秒`;
  $('#startup-steps').innerHTML = (value.steps || []).map((step, index) => `<li class="${value.state === 'running' && index === value.steps.length - 1 ? 'is-current' : ''}"><span>${escapeHTML(startupNames[step.stage] || step.stage.replace(/^instance_ready:/, '实例就绪：'))}</span><b>${Number(step.elapsed) || 0} 秒</b></li>`).join('');
  $('#startup-message').textContent = (value.message || '检查通过后自动启动下载。') + (value.slowest ? ` 当前耗时最长：${startupNames[value.slowest] || value.slowest}。` : '');
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
let executionRetryAt = 0;
let executionFailures = 0;
let workbenchEpoch = 0;
let executionSampleAt = 0;
let executionSampleKey = '';
async function refreshExecution({force = false} = {}) {
  const key = `${state.instanceId || ''}:${selectedJobId || ''}`;
  const stamp = Date.now();
  if (executionPolling || document.hidden || (!force && (stamp < executionRetryAt ||
      (key === executionSampleKey && stamp - executionSampleAt < 1800)))) return;
  executionPolling = true;
  executionSampleAt = stamp; executionSampleKey = key;
  try {
    await refreshSelectedJob({poll: !force});
    executionFailures = 0; executionRetryAt = 0;
  } catch (error) {
    executionFailures += 1;
    executionRetryAt = Date.now() + Math.min(30000, 2000 * 2 ** Math.min(executionFailures - 1, 4));
    throw error;
  } finally { executionPolling = false; }
}
async function refreshSelectedJob({poll = false} = {}) {
  if (queueControlBusy || (fleetEnabled && !state.instanceId)) return;
  const id = selectedJobId;
  const instance = state.instanceId;
  const epoch = ++workbenchEpoch;
  try {
    const query = new URLSearchParams();
    if (id) query.set('job_id', id);
    if (poll) query.set('poll', '1');
    const workbench = await api(`/api/jobs/workbench${query.size ? '?' + query : ''}`);
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
    summary.innerHTML = `<div class="selected-job-counts">${[['任务待下载视频',job.queued],['当前实例处理中视频',job.running],['任务处理成功',job.succeeded],['任务处理失败',job.failed]].map(([label,count])=>`<div><span>${label}</span><strong>${Number(count)||0}</strong></div>`).join('')}</div><p class="micro-copy">${escapeHTML(downloadPolicyText(job))}${job.importing?` · 正在导入 ${job.importing} 份文件`:''}</p><p class="micro-copy"><a href="#settings">修改当前实例策略 ↗</a></p>`;
  }
  const alreadyStarted = !!job?.released && !queue?.paused && queue?.selected_job_id===job.id;
  $('#download-queue-start').textContent = queueResuming ? '正在启动…' : alreadyStarted ? '已启动' : '启动';
  $('#download-queue-start').setAttribute('aria-busy', String(queueResuming));
  $('#download-queue-start').disabled = viewer || queueControlBusy || starting || !state.config || !queue || !queue.online || !job || job.archived || (!job.queued && !job.running && !job.importing) || alreadyStarted || (queue.stop_requested && !!queue.running);
  $('#download-job-pause').disabled = viewer || queueControlBusy || !queue || queue.paused;
  $('#download-job-stop').disabled = viewer || queueControlBusy || !queue || (queue.paused && !queue.running);
  $('#download-queue-toggle').textContent = '立即停止';
  $('#download-queue-toggle').disabled = viewer || queueControlBusy || (!starting && (!queue || (queue.paused && !queue.running) || (!queue.running && !queue.queued)));
  const differentJob = queue?.selected_job_id && queue.selected_job_id !== selectedJobId;
  $('#download-selection-status').textContent = differentJob ? '点击“启动”后，当前实例切换到所选任务。' : !job ? '' : job.waiting_reason || (job.running ? (queue?.paused ? '当前实例将在正在处理的视频全部完成后停止。' : '当前实例正在处理所选任务的视频。') : job.released && job.importing ? '等待文件导入，随后自动下载。' : !job.queued && !job.importing ? '没有待下载视频。请到该任务中添加视频或重试失败条目。' : job.released ? '已启动，等待处理所选任务的待下载视频。' : '任务已就绪，点击“启动”开始处理。');
  $('#download-queue-status').textContent = queue ? `当前实例：${instanceState(queue)} · 处理中视频 ${queue.running} 个。` : '正在读取当前实例状态…';
  const execution = $('#download-execution-status'); execution.replaceChildren();
  if (queueResuming) execution.append(document.createTextNode('正在检查当前实例的下载环境，请稍候。'));
  if (queueResumeError) execution.append(document.createTextNode(queueResumeError));
  if (!queueResuming && queue?.error) execution.append(document.createTextNode(queue.error));
  renderStartup();
}
function instancePolicyText(policy) {
  const auto = policy.scheduler_mode === 'auto';
  const methods = (policy.download_methods || (policy.download_method==='yt5s_fallback'?['yt5s','yt_dlp']:[policy.download_method || 'yt_dlp'])).map(method => providerNames[method] || method).join(auto ? '、' : ' → ');
  const qualities = {yt5s:policy.yt5s_quality, downmp3:policy.downmp3_quality, yt1d:policy.yt1d_quality, bergung:policy.bergung_quality};
  const quality = Object.entries(qualities).filter(([method]) => (policy.download_methods || [policy.download_method]).includes(method)).map(([method,value]) => `${method} ${!value || value==='best'?'最高画质':value+'p'}`).join(' / ');
  return `${auto ? '自动调度下载方式' : '下载方式尝试顺序'}：${methods}${quality?' · '+quality:''} · ${policy.storage_mode==='test'?'测试模式，完成后删除文件':policy.storage_mode==='oss'?'阿里云 OSS + 本地副本':'保存成品'}`;
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
    const sampledAt = performance.now();
    applyStartup(await api('/api/downloads/startup'), sampledAt);
    renderDownloadControl();
    if (previous?.state === 'running' && state.startup.state !== 'running') refresh().catch(() => {});
  } catch { /* The main connection banner reports service availability. */ }
  finally { startupPolling = false; }
}
setInterval(() => { if (state.startup?.state === 'running') pollStartup(); }, 1000);
$('#download-queue-start').addEventListener('click', async () => {
  if (queueControlBusy || document.body.dataset.consoleRole === 'viewer') return;
  if (providerDraft || downloadMethodBusy) {
    toast('下载方式尚未保存完成，请先在系统设置点击“保存设置”，再启动下载。', true);
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
    toast(result.warnings?.length ? result.warnings.join('；') : `已启动：${result.job.customer} / ${result.job.name}`);
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
      toast(action==='pause'?'当前实例将在正在处理的视频全部完成后停止。':'已发送立即停止指令，正在停止当前实例的下载。');
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
    toast('已发送立即停止指令，正在停止当前实例的下载。');
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
  $('#task-risk').replaceChildren();
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
  $('#task-risk').innerHTML = taskRiskHTML(task);
  $('#task-error').hidden = !task.error;
  $('#task-error').textContent = task.error ? readableError(task.error) : '';
  $('#task-download-progress').innerHTML = task.action === 'clear_media' ? `<p class="micro-copy">已删除 ${Number(task.progress?.deleted_files || 0)} 个文件 · 释放 ${bytes(task.progress?.deleted_bytes || 0)}${task.progress?.failed_files ? ` · ${Number(task.progress.failed_files)} 个文件未能删除` : ''}</p>` : ['download', 'import'].includes(task.action) ? downloadProgress(task) : task.progress?.scanned ? `<p class="micro-copy">已扫描 ${task.progress.scanned} 个文件</p>` : '';
  const result = task.result;
  $('#task-result').innerHTML = result?.message ? `<div class="result-summary">${escapeHTML(result.message)}</div>` : '';
  if (task.subtitle_tasks?.length) $('#task-result').innerHTML += `<p class="micro-copy">字幕补下载（独立于视频计费和成功状态）：${task.subtitle_tasks.map(child => `<button type="button" class="text-button" data-task="${escapeHTML(child.id)}">${escapeHTML({queued:'待补下载',running:'正在补下载',succeeded:'已处理',failed:'补下载失败',interrupted:'已中断'}[child.status] || child.status)} ↗</button>`).join(' · ')}</p>`;
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
  const cookieJump = event.target.closest('[data-scroll-cookie]');
  if (cookieJump) {
    event.preventDefault();
    $('#youtube-cookie-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  const nav = event.target.closest('[data-view]');
  if (nav) { event.preventDefault(); setView(nav.dataset.view); }
  const open = event.target.closest('[data-open]');
  if (open) {
    $(`#${open.dataset.open}`).showModal();
    if (open.dataset.open === 'youtube-cookie-dialog') loadCookieProfiles().catch(error => {
      $('#youtube-cookie-list').textContent = error.message;
    });
  }
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
function updateCookiePoolLive(data) {
  const pool = data.pool;
  const live = $('#youtube-cookie-pool-live');
  if (live) live.innerHTML = cookieAccountNoticesHTML(pool?.capacity_summary, Date.now() / 1000);
  for (const row of document.querySelectorAll('[data-cookie-profile-row]')) {
    const account = pool?.accounts.find(a => a.id === row.dataset.cookieProfileRow);
    const session = pool?.session_states?.find(s => s.id === (account?.shared_with || account?.id));
    row.classList.toggle('cookie-cooling', session?.state === 'cooldown');
    row.classList.toggle('cookie-recovery', session?.state === 'recovery');
    const policy = row.querySelector('[data-cookie-session-status]');
    if (policy && account) {
      policy.textContent = account.unavailable ? 'Cookie 文件不可用，请更新。' :
        `${session?.state === 'cooldown' ? '冷却中 · 暂停新请求' : session?.state === 'recovery' ? '低频恢复中' : '可参与调度'}：设置上限 ${account.configured_limit || pool.session_limit}，当前最多 ${session?.limit ?? account.limit ?? 1} 个任务，启动间隔至少 ${session?.interval || account.interval || 60} 秒。` +
        (session?.state === 'cooldown' ? `原因：${failureKindNames[session.cooldown_kind] || '临时故障'}；恢复检查时间 ${new Date(session.cooldown_until * 1000).toLocaleString()}；其他健康账号继续。` : '') +
        (session?.state === 'recovery' ? `启动间隔延长原因：${failureKindNames[session.cooldown_kind] || '此前故障'}；本轮连续成功 ${session.recovery_successes} 次，达标后自动恢复设置并发。` : '') +
        (session?.last_failure_at ? ` 最近异常 ${new Date(session.last_failure_at * 1000).toLocaleString()}。` : '') +
        (account.shared_with ? ` 与「${session?.name || '同账号文件'}」共享并发、间隔和冷却。` : '');
    }
    const check = row.querySelector('[data-cookie-login-check]');
    if (check && check.textContent === '检测登录') {
      check.disabled = session?.state === 'cooldown';
      check.title = check.disabled ? pool.halted
        ? '池仍有人工拦截；冷却结束后须按提示检查并解除，不会自动恢复。'
        : '该账号冷却中，暂不发送登录检测请求；到期自动低频恢复。' : '';
    }
  }
}
let cookiePoolPolling = false;
setInterval(async () => {
  if (location.hash !== '#settings' || document.hidden || cookiePoolPolling) return;
  cookiePoolPolling = true;
  const instanceId = state.instanceId;
  try {
    const data = await api('/api/youtube/cookies');
    if (state.instanceId === instanceId && location.hash === '#settings') updateCookiePoolLive(data);
  }
  catch (_) { const live = $('#youtube-cookie-pool-live'); if (live) live.textContent = '账号状态刷新失败，请重新打开设置查看最新状态。'; }
  finally { cookiePoolPolling = false; }
}, 10000);
async function exportCookieProfile(item) {
  const response = await api('/api/youtube/cookies/export', {
    method: 'POST', body: { id: item.id }, responseType: 'file',
  });
  const disposition = response.headers.get('Content-Disposition') || '';
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  const plain = disposition.match(/filename="((?:[^"\\]|\\.)*)"|filename=([^;]+)/i);
  let filename = `youtube-cookie-${item.id}.txt`;
  if (encoded) {
    try { filename = decodeURIComponent(encoded[1]); } catch { /* Keep the safe fallback name. */ }
  } else if (plain) filename = (plain[1] ?? plain[2]).replace(/\\(.)/g, '$1').trim();
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  try {
    document.body.append(link);
    link.click();
  } finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  toast('Cookie 已导出。');
}
async function loadCookieProfiles() {
  const instance = state.instanceId || '';
  const data = await api('/api/youtube/cookies');
  if (instance !== (state.instanceId || '')) return loadCookieProfiles();
  if (state.config) {
    state.config.youtube_cookie = data.cookie_path;
    state.config.youtube_cookie_pool = data.items.filter(item => item.pooled).map(item => item.id);
    renderDownloadMethod();
  }
  if (cookiePoolDraft && cookiePoolDraft.instance !== instance) cookiePoolDraft = null;
  if (cookiePoolDraft) cookiePoolDraft.ids = cookiePoolDraft.ids.filter(id => data.items.some(item => item.id === id));
  const poolEnabled = $('#youtube-cookie-pool-enabled');
  if (poolEnabled) poolEnabled.checked = cookiePoolDraft ? cookiePoolDraft.enabled : !!data.pool;
  renderCookiePoolSaveState();
  const list = $('#youtube-cookie-list');
  list.replaceChildren();
  const note = document.createElement('p');
  note.className = 'micro-copy';
  note.textContent = data.custom_active ? '当前使用配置文件指定的其他 Cookie。' :
    data.items.some(item => item.active) ? '标记“当前使用”的配置供此实例下载使用，登录有效性请另行检查。' : '当前未选用已保存的 Cookie。';
  list.append(note);
  if (data.pool) {
    const sessions = data.pool.effective_sessions ?? data.pool.effective_accounts;
    note.textContent = `${data.items.filter(item => item.pooled).length} 份 Cookie · ${sessions} 个核心会话 · 按本地名称 / 核心凭据合并，未核验真实 Google 账号身份。` +
      (data.pool.halted ? `会话池已停止：${data.pool.reason}${data.pool.limited_profile_name ? `；触发账号：${data.pool.limited_profile_name}` : ''}` :
      `Cookie 池独立调度：当前会话容量 ${data.pool.capacity_summary.capacity}，每会话预热后最多 ${data.pool.session_limit} 个任务；不使用运行模式与实例并发上限。可在“Cookie 与登录”的高级设置中修改。启动间隔与冷却继续生效。不同会话不代表独立源站额度。点击“使用此 Cookie”会关闭会话池。`);
    if (data.pool.halted) {
      const recover = document.createElement('button');
      recover.type = 'button';
      recover.className = 'button button-white';
      recover.textContent = '检查全部账号并解除拦截';
      recover.addEventListener('click', () => runButton(recover, async () => {
        const result = await api('/api/youtube/cookies/pool/recover', { method: 'POST', body: {} });
        toast(result.message);
        await loadCookieProfiles();
      }));
      list.append(recover);
    }
  }
  for (const item of data.items) {
    const row = document.createElement('div');
    row.className = 'cookie-profile-row';
    row.dataset.cookieProfileRow = item.id;
    const details = document.createElement('div');
    details.className = 'cookie-profile-details';
    const name = document.createElement('strong');
    name.textContent = item.name + (item.pooled ? ' · 账号池成员' : '') + (item.active ? ' · 当前使用' : '');
    const status = document.createElement('p');
    status.className = 'micro-copy cookie-profile-status';
    const renderStatus = auth => {
      if (!auth) { status.textContent = '尚未检测登录'; return; }
      const labels = { valid: '登录有效', rate_limited: 'HTTP 429 请求限流', not_logged_in: '未登录', invalid: 'Cookie 无效或过期',
        missing: 'Cookie 文件缺失', verification_required: '需要额外验证 / 出口受限',
        network_error: '网络异常，无法确认', unconfirmed: '尚未确认登录', checking: '检测进行中' };
      const expired = auth.expires_at && auth.expires_at * 1000 <= Date.now();
      status.textContent = `${labels[auth.state] || '尚未检测'}${expired ? '（历史结果，请重新检测）' : ''} · ${auth.message}` +
        (auth.checked_at ? ` 检测时间：${new Date(auth.checked_at * 1000).toLocaleString()}` : '') +
        (auth.connection ? ` 检测出口：${auth.connection}` : '');
    };
    renderStatus(item.auth);
    details.append(name, status);
    const poolCheck = document.createElement('input');
    poolCheck.type = 'checkbox';
    poolCheck.dataset.cookiePoolId = item.id;
    poolCheck.checked = cookiePoolDraft ? cookiePoolDraft.ids.includes(item.id) : !!item.pooled;
    poolCheck.title = '加入 Cookie 账号池';
    poolCheck.setAttribute('aria-label', `加入账号池：${item.name}`);
    if (item.pooled) {
      const policy = document.createElement('p');
      policy.className = 'cookie-session-status';
      policy.dataset.cookieSessionStatus = '';
      details.append(policy);
    }
    if (item.session && !item.pooled) {
      const pacing = document.createElement('p');
      pacing.className = 'cookie-session-status';
      const stateName = item.session.state === 'cooldown' ? '冷却中' : item.session.state === 'recovery' ? '低频恢复中' : '可参与调度';
      pacing.textContent = `${stateName} · 当前启动间隔至少 ${item.session.interval || 60} 秒` +
        (item.session.cooldown_until > Date.now() / 1000 ? ` · 冷却截止 ${new Date(item.session.cooldown_until * 1000).toLocaleString()}` : '') +
        (item.session.recovery_mode ? ` · 恢复成功 ${item.session.recovery_successes || 0} 次` : '');
      details.append(pacing);
    }
    if (item.latest_event) {
      const latest = document.createElement('p');
      latest.className = 'micro-copy cookie-profile-latest';
      latest.textContent = `最近事件：${new Date(item.latest_event.occurred_at * 1000).toLocaleString()} · ${item.latest_event.message || item.latest_event.kind || '已记录'}`;
      details.append(latest);
    }
    const check = document.createElement('button');
    check.type = 'button';
    check.dataset.cookieLoginCheck = '';
    check.className = 'button button-white';
    check.textContent = '检测登录';
    check.setAttribute('aria-label', `检测登录：${item.name}`);
    check.addEventListener('click', () => runButton(check, async () => {
      check.textContent = '检测中…';
      status.textContent = '正在通过当前实例的下载出口检测，请稍候…';
      try {
        const result = await api('/api/youtube/cookies/check', { method: 'POST', body: { id: item.id } });
        item.auth = result.auth;
        renderStatus(result.auth);
      } catch (error) {
        renderStatus(item.auth);
        status.textContent = `本次检测未完成：${error.message}。 ${status.textContent}`;
        throw error;
      } finally { check.textContent = '检测登录'; }
    }));
    const timeline = document.createElement('button');
    timeline.type = 'button';
    timeline.className = 'button button-white';
    timeline.textContent = '查看记录';
    timeline.addEventListener('click', () => openCookieTimeline(item));
    const exportButton = document.createElement('button');
    exportButton.type = 'button';
    exportButton.className = 'button button-white';
    exportButton.textContent = '导出';
    exportButton.setAttribute('aria-label', `导出 ${item.name}`);
    exportButton.addEventListener('click', () => runButton(exportButton, () => exportCookieProfile(item)));
    const select = document.createElement('button');
    select.type = 'button';
    select.className = 'button button-white';
    select.textContent = item.active ? '当前使用' : '使用此 Cookie';
    select.disabled = item.active && !data.pool;
    select.addEventListener('click', () => runButton(select, async () => {
      const result = await api('/api/youtube/cookies/select', { method: 'POST', body: { id: item.id } });
      cookiePoolDraft = null;
      if (state.config) state.config.youtube_cookie = result.cookie_path;
      toast(result.message);
      await loadCookieProfiles();
      await refresh();
    }));
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'button button-white';
    remove.textContent = '删除';
    remove.addEventListener('click', () => runButton(remove, async () => {
      const selectionNote = item.active ? (data.items.length > 1
        ? '删除后将自动选择列表中的其他 Cookie 作为当前使用项。' : '删除后将进入无 Cookie 状态。') : '';
      if (!window.confirm(`删除“${item.name}”？${selectionNote}删除后需重新上传才能使用。`)) return;
      const result = await api('/api/youtube/cookies/delete', { method: 'POST', body: { id: item.id } });
      if (state.config) {
        state.config.youtube_cookie = result.cookie_path;
        state.config.youtube_cookie_pool = result.cookie_pool;
      }
      toast(result.message);
      await loadCookieProfiles();
      await refresh();
    }));
    row.append(poolCheck, details, check, timeline, exportButton, select, remove);
    list.append(row);
  }
  updateCookiePoolLive(data);
}
async function openCookieTimeline(item) {
  const dialog = $('#youtube-cookie-timeline-dialog');
  const list = $('#youtube-cookie-timeline-list');
  const summary = $('#youtube-cookie-timeline-summary');
  if (!dialog || !list) return;
  dialog.dataset.profileId = item.id;
  $('#youtube-cookie-timeline-title').textContent = `${item.name} · 事件时间线`;
  summary.textContent = item.session
    ? `当前启动间隔至少 ${item.session.interval || 60} 秒。${item.session.state === 'cooldown' ? '当前由账号冷却控制。' : item.session.state === 'recovery' ? '当前由低频恢复策略控制。' : '当前可参与调度。'}`
    : '该 Cookie 尚未加入账号池，以下记录主要来自登录检测。';
  list.textContent = '正在读取记录…';
  dialog.showModal();
  await refreshCookieTimeline();
}
async function refreshCookieTimeline() {
  const dialog = $('#youtube-cookie-timeline-dialog');
  const list = $('#youtube-cookie-timeline-list');
  if (!dialog || !list || !dialog.dataset.profileId) return;
  const range = Number($('#youtube-cookie-timeline-range')?.value || 604800);
  const changes = $('#youtube-cookie-timeline-changes')?.checked;
  const query = range ? `?from=${(Date.now() / 1000 - range).toFixed(3)}${changes ? '&only_interval_changes=1' : ''}` : (changes ? '?only_interval_changes=1' : '');
  try {
    const data = await api(`/api/youtube/cookies/${encodeURIComponent(dialog.dataset.profileId)}/timeline${query}`);
    list.replaceChildren();
    if (!data.events.length) { list.textContent = '当前筛选范围内没有记录。'; return; }
    for (const event of data.events) {
      const article = document.createElement('article');
      article.className = `cookie-timeline-event ${event.event_type === 'failure' ? 'failure' : 'change'}`;
      const heading = document.createElement('h4');
      heading.textContent = `${new Date(event.occurred_at * 1000).toLocaleString()} · ${event.event_type === 'interval_change' ? '启动间隔调整' : event.event_type === 'auth_check' ? '登录检测' : event.event_type === 'recovery' ? '恢复进度' : '错误事件'}`;
      const message = document.createElement('p'); message.textContent = event.message || event.kind || '';
      article.append(heading, message);
      const meta = [];
      if (event.kind) meta.push(`类型：${event.kind}`);
      if (event.phase) meta.push(`阶段：${event.phase}`);
      if (event.http_status) meta.push(`HTTP ${event.http_status}`);
      if (event.previous_interval != null && event.new_interval != null && event.previous_interval !== event.new_interval) meta.push(`启动间隔：${event.previous_interval} 秒 → ${event.new_interval} 秒`);
      if (event.cooldown_until) meta.push(`冷却截止：${new Date(event.cooldown_until * 1000).toLocaleString()}`);
      const small = document.createElement('small'); small.textContent = meta.join(' · '); article.append(small);
      list.append(article);
    }
  } catch (error) { list.textContent = `读取记录失败：${error.message}`; }
}
$('#youtube-cookie-timeline-range')?.addEventListener('change', refreshCookieTimeline);
$('#youtube-cookie-timeline-changes')?.addEventListener('change', refreshCookieTimeline);
function renderCookiePoolSaveState() {
  const status = $('#youtube-cookie-pool-status');
  if (!status) return;
  if (cookiePoolDraft) {
    status.textContent = cookiePoolDraft.enabled
      ? `未保存：已勾选 ${cookiePoolDraft.ids.length} 份 Cookie，点击页面底部“保存设置”后生效。`
      : '未保存：将关闭账号池，点击页面底部“保存设置”后生效。';
  } else {
    const count = state.config?.youtube_cookie_pool?.length || 0;
    status.textContent = count ? `已保存：账号池已启用，共 ${count} 份 Cookie。`
      : '账号池未启用。选择至少 2 份 Cookie，仅支持单独使用 yt-dlp；修改后点击页面底部“保存设置”。';
  }
}
$('#youtube-cookie-section').addEventListener('change', event => {
  if (!event.target.matches('#youtube-cookie-pool-enabled, [data-cookie-pool-id]')) return;
  cookiePoolDraft = { instance: state.instanceId, enabled: $('#youtube-cookie-pool-enabled').checked,
    ids: $$('[data-cookie-pool-id]:checked').map(input => input.dataset.cookiePoolId) };
  renderCookiePoolSaveState();
});
$('#youtube-cookie-submit').addEventListener('click', event => {
  runButton(event.currentTarget, async () => {
    const form = $('#youtube-cookie-form');
    const errorBox = $('#youtube-cookie-error');
    errorBox.hidden = true;
    try {
      const file = $('[data-cookie-file]', form).files[0];
      if (!file || file.size === 0 || file.size > 1024 * 1024) throw new Error('请选择非空的 Cookie .txt 文件，最大 1 MB。');
      const upload = new FormData();
      upload.append('name', $('[data-cookie-name]', form).value);
      upload.append('cookie', file);
      const data = await api('/api/youtube/cookies', { method: 'POST', body: upload });
      $('[data-cookie-name]', form).value = '';
      $('[data-cookie-file]', form).value = '';
      toast(data.message);
      await loadCookieProfiles();
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
  renderYouTubePacingRuntime();
  $('#settings-save-state').textContent = '有未保存的修改（包括账号池选择），统一点击“保存设置”后生效。新任务使用新策略，正在执行的任务保留原配置。';
  renderStrategySaveState(); renderDownloadControl();
}
$('#settings-form').addEventListener('input', markSettingsDirty);
$('#settings-form').addEventListener('change', markSettingsDirty);
$('#settings-form').addEventListener('submit', event => {
  event.preventDefault();
  runButton($('button[type="submit"]', event.target), async () => {
    if (downloadMethodBusy) return;
    const data = Object.fromEntries([...new FormData(event.target)].filter(([key,value]) => value !== settingsBaseline[key]));
    const channels = readChannels();
    if (channelSignature(channels) !== settingsChannelBaseline) data.download_channels = channels;
    if (providerDraft) data.download_methods = [...providerDraft];
    if (cookiePoolDraft && cookiePoolDraft.instance === state.instanceId) {
      if (cookiePoolDraft.enabled && cookiePoolDraft.ids.length < 2) throw new Error('账号池至少需要选择 2 份 Cookie。');
      if (cookiePoolDraft.enabled && (providerDraft || selectedProviders()).some(method => method !== 'yt_dlp')) {
        throw new Error('Cookie 账号池仅支持单独使用 yt-dlp，请先在下载策略中关闭其他下载方式。');
      }
      data.youtube_cookie_pool = cookiePoolDraft.enabled ? [...cookiePoolDraft.ids] : [];
    }
    downloadMethodBusy = true; renderDownloadMethod();
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
          : key === 'youtube_cookie_pool' ? 'Cookie 账号池'
          : $(`[name="${key}"]`, form)?.closest('label')?.firstChild.textContent.trim() || key);
        const providers = value => Array.isArray(value) ? value.map(method => providerNames[method] || method).join('、') : '未返回有效列表';
        const detail = mismatches.includes('download_methods')
          ? `（提交：${providers(data.download_methods)}；读回：${providers(saved.download_methods)}）` : '';
        throw new Error(`保存结果未确认：${labels.join('、')}与提交值不一致${detail}，修改已保留，请重试；持续出现时将此提示提供给维护人员`);
      }
      if (instance !== state.instanceId) throw new Error('核对期间所选实例发生变化，请返回原实例确认设置');
      settingsDirty = false; providerDraft = null; cookiePoolDraft = null;
      fillConfig(saved);
      renderCookiePoolSaveState();
      $('#settings-save-state').textContent = '当前实例设置已保存，包含账号池选择及其他设置修改。后续领取或重试的视频使用新配置，正在执行的任务保留原配置。';
      toast('设置已保存。');
      await refresh();
      if ('youtube_cookie_pool' in data || 'worker_count' in data || 'youtube_cookie_session_limit' in data) await loadCookieProfiles();
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
renderDownloadMethod();
init();
