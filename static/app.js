'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const csrf = $('meta[name="csrf-token"]').content;
const state = { config: null, media: [], tasks: [], counts: null, status: null, clash: null, selectedTask: null, logOffset: 0, logs: '', polling: false, detailBusy: false };
let clashBusy = false;
let downloadSubmitting = false;
let downloadMethodBusy = false;
let nodeSignature = '';
let rotationDraft = null;
let rotationDirty = false;
let rotationSavedSignature = '';
let rotationNodeSignature = '';
const pages = Object.fromEntries(['tasks', 'media'].map(name => [name, { cursor: null, next: null, history: [] }]));
state.overview = { recent_media: [], recent_tasks: [], media: { count: 0 } };
state.batchTasks = [];

const actionNames = { download: '下载视频', check: '下载环境检查', import: '导入下载清单', index: '同步素材索引' };
const actionIcons = { download: 'download', check: 'server', import: 'upload', index: 'refresh' };
const statusNames = { queued: '排队中', running: '进行中', succeeded: '已完成', failed: '失败', interrupted: '已中断' };
const viewNames = {
  overview: ['下载概览', 'YouTube 视频，一处下载', '粘贴视频或播放列表链接，原片和配套资源自动保存到本机。'],
  media: ['素材库', '下载的视频，都在这里', '查看本机保存的视频原片、封面和文件大小。'],
  tasks: ['任务记录', '每一次下载，都有记录', '查看已结束的下载与环境检查记录，以及执行结果和日志。'],
  settings: ['下载设置', '按你的习惯，保存视频', '配置视频保存位置、下载代理和 YouTube Cookie。'],
  clash: ['代理与节点', '选一个节点，连接 YouTube', '管理 Clash 订阅、选择下载节点，并测试延迟与连通性。'],
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
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data;
}
function setView(name) {
  if (!viewNames[name]) return;
  const [label, title, description] = viewNames[name];
  $$('.view').forEach(view => { view.hidden = view.id !== `${name}-view`; });
  $$('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.view === name));
  $('#breadcrumb-current').textContent = label;
  $('#page-title').replaceChildren(document.createTextNode(title));
  const dot = document.createElement('span'); dot.className = 'heading-dot'; dot.textContent = '.';
  $('#page-title').append(dot);
  $('#page-description').textContent = description;
  $('#sidebar').classList.remove('mobile-open');
  history.replaceState(null, '', `#${name}`);
}
function emptyState(title, description, button = false) {
  return `<div class="empty-state"><div class="empty-icon">${icon('film')}</div><div><strong>${escapeHTML(title)}</strong><p>${escapeHTML(description)}</p>${button ? `<button class="button button-white" data-view="overview">${icon('download')}开始下载视频</button>` : ''}</div></div>`;
}
function renderMedia() {
  const count = state.overview.media.count || 0;
  ['stat-media', 'nav-media-count', 'recent-media-count', 'all-media-count'].forEach(id => { $(`#${id}`).textContent = count; });
  const cards = items => `<div class="media-grid">${items.map(item => `<button type="button" class="media-card" data-media="${escapeHTML(item.name)}" aria-label="${item.playable ? '播放' : '查看'}：${escapeHTML(item.title || item.name)}"><span class="media-image">${item.cover ? `<img src="/media/covers/${encodeURIComponent(item.cover)}" alt="${escapeHTML(item.title || item.name)}的封面" loading="lazy">` : icon('film')}${item.playable ? `<span class="media-play">${icon('play')}</span>` : ''}<span class="media-type">${item.playable ? escapeHTML(item.name.split('.').pop().toUpperCase()) : '待合并'}</span></span><span class="media-info"><span class="media-name" title="${escapeHTML(item.name)}">${escapeHTML(item.title || item.name)}</span><span class="media-meta"><span>${bytes(item.size)}</span><span>${item.playable ? '点击播放' : '音视频尚未合并'}</span></span></span></button>`).join('')}</div>`;
  $('#recent-media').innerHTML = state.overview.recent_media.length ? cards(state.overview.recent_media) : emptyState('还没有下载的视频', '在上方粘贴 YouTube 链接，开始第一次下载。');
  $('#all-media').innerHTML = state.media.length ? cards(state.media) : emptyState('素材库还是空的', '下载完成后，视频与封面会出现在这里。', true);
}
function openMedia(name) {
  const item = [...state.media, ...state.overview.recent_media].find(media => media.name === name);
  if (!item) return;
  const player = $('#media-player');
  const warning = $('#media-player-error');
  $('#media-dialog-title').textContent = item.title || item.name;
  $('#media-player-filename').textContent = item.name;
  warning.hidden = item.playable;
  warning.textContent = item.playable ? '' : '音视频尚未合并，请检查下载任务日志和 FFmpeg 安装情况。';
  player.hidden = !item.playable;
  const url = `/media/videos/${encodeURIComponent(item.name)}`;
  $('#media-download').href = url;
  $('#media-download').download = item.name;
  if (item.cover) player.poster = `/media/covers/${encodeURIComponent(item.cover)}`;
  else player.removeAttribute('poster');
  if (item.playable) player.src = url;
  $('#media-dialog').showModal();
  if (item.playable) player.play().catch(() => {});
}
function taskRows(tasks) {
  return tasks.map(task => `<button class="task-row" data-task="${task.id}"><span class="task-action-icon">${icon(actionIcons[task.action] || 'list')}</span><span class="task-row-text"><strong>${escapeHTML(task.progress?.title || task.video_id || actionNames[task.action] || task.action)}</strong><small>${['download', 'import'].includes(task.action) ? progressLabel(task) : task.action === 'index' ? '同步素材索引' : '本机工具'} · ${task.id.slice(0, 8)}</small></span><span class="task-status status-${task.status}">${taskStatus(task)}</span><time class="task-time" title="${task.finished_at ? '结束时间' : '创建时间'}">${escapeHTML(formatDate(task.finished_at || task.created_at))}</time>${icon('arrow', 'task-arrow')}</button>`).join('');
}
function taskStatus(task) {
  return ['download', 'import'].includes(task.action) && state.status?.download_queue?.paused && ['queued', 'running'].includes(task.status) ? (task.status === 'running' ? '正在停止' : '已停止') : statusNames[task.status] || task.status;
}
function etaText(seconds) {
  if (!Number.isFinite(seconds)) return '—';
  const value = Math.ceil(seconds);
  return value >= 3600 ? `${Math.floor(value / 3600)}小时${Math.floor(value % 3600 / 60)}分` : value >= 60 ? `${Math.floor(value / 60)}分${value % 60}秒` : `${value}秒`;
}
function progressLabel(task) {
  if (['download', 'import'].includes(task.action) && state.status?.download_queue?.paused && ['queued', 'running'].includes(task.status)) return task.status === 'running' ? '正在停止' : '已停止 · 等待继续';
  if (task.status === 'queued' && task.progress?.stage === 'paused') return '等待继续下载';
  if (task.status === 'queued') return task.progress?.stage === 'retrying' ? '等待退避重试' : '排队中';
  if (task.action === 'import' && task.status === 'running') return `正在导入 · 新增 ${task.progress?.added || 0} · 重复 ${task.progress?.duplicates || 0} · 无效 ${task.progress?.invalid || 0}`;
  if (task.status === 'failed') return '下载失败';
  if (task.status === 'interrupted') return '已中断';
  if (task.status === 'succeeded') return task.result?.test_mode ? '测试完成 · 文件已删除' : task.result?.skipped ? '已下载过 · 已跳过' : '下载完成';
  const stage = task.progress?.stage;
  return { waiting_browser: '等待方式二浏览器', waiting_verification: '方式二 · 等待网站验证', converting: '方式二 · 网站正在转换', checking_login: '正在检测 YouTube 登录', extracting: '正在解析视频', downloading: '正在下载', processing: '合并 / 处理文件', cleaning: '测试下载完成 · 正在清理文件', skipped: '已下载过 · 已跳过', completed: '下载完成' }[stage] || '准备下载';
}
function downloadProgress(task) {
  const data = task.progress || {};
  const active = task.status === 'running';
  const stopped = task.status === 'queued' && data.stage === 'paused';
  const percentage = task.status === 'queued' && !stopped ? 0 : task.status === 'succeeded' ? 100 : Number.isFinite(data.percent) ? Math.max(0, Math.min(100, data.percent)) : null;
  const label = task.status === 'queued' && !stopped ? '等待开始' : percentage === null ? '大小未知' : `${percentage.toFixed(1)}%`;
  return `<div class="download-meter"><div class="download-meter-label"><span>${escapeHTML(progressLabel(task))}${active && data.stage === 'downloading' && data.format ? ` · ${escapeHTML(data.format)}` : ''}</span><strong>${label}</strong></div><progress max="100"${percentage === null ? '' : ` value="${percentage}"`} aria-label="下载进度"></progress><div class="download-metrics"><span>${task.action === 'import' ? '已读取' : '已下载'} <b>${data.downloaded_bytes ? bytes(data.downloaded_bytes) : '—'}</b>${data.total_bytes ? ` / ${bytes(data.total_bytes)}` : ''}</span><span>速度 <b>${active && Number.isFinite(data.speed) ? `${bytes(data.speed)}/s` : '—'}</b></span><span>当前文件剩余 <b>${active ? etaText(data.eta) : '—'}</b></span>${data.proxy_node ? `<span>节点 <b>${escapeHTML(data.proxy_node)}</b></span>` : ''}</div></div>`;
}
function renderBatches() {
  const tasks = state.batchTasks;
  const queue = state.status?.download_queue || {};
  $('#batch-count').textContent = tasks.length;
  $('#batch-summary').textContent = `${queue.paused ? '下载已停止' : '正在下载 ' + tasks.length + ' 个'} · 排队 ${queue.queued || 0} 个 · 仅显示当前正在下载的视频，完成后可在任务记录中查看`;
  $('#batch-progress').innerHTML = tasks.length ? tasks.map((task, index) => `<article class="batch-video" data-download-id="${task.id}"><div class="batch-video-heading"><span class="batch-video-index">${index + 1}</span><div class="batch-video-title"><strong title="${escapeHTML(task.progress?.title || task.url || '')}">${escapeHTML(task.progress?.title || task.video_id || task.url || '视频')}</strong><small>${escapeHTML(task.video_id || task.id.slice(0, 8))}</small></div><button type="button" class="text-button" data-task="${task.id}">查看日志 ↗</button></div>${downloadProgress(task)}${task.error ? `<p class="batch-video-error">${escapeHTML(task.error)}</p>` : ''}</article>`).join('') : emptyState('当前没有正在下载的视频', state.status?.download_queue?.paused ? '下载已停止，点击继续下载后显示实时进度。' : '排队任务开始下载后会显示在这里，已结束的任务请到任务记录查看。');
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

function inputAsIds(value) {
  const lines = Array.isArray(value) ? value : String(value || '').split(/\r?\n/);
  return lines.map(line => {
    const item = line.trim();
    try {
      const url = new URL(item);
      if (['www.youtube.com', 'youtube.com', 'm.youtube.com'].includes(url.hostname) && url.searchParams.get('v')) return url.searchParams.get('v');
      if (url.hostname === 'youtu.be') return url.pathname.slice(1);
    } catch {}
    return item;
  }).join('\n');
}
function updateInputCount() {
  const lines = [...new Set($('#download-urls').value.split(/\r?\n/).map(line => line.trim()).filter(Boolean))];
  $('#download-input-count').textContent = lines.length ? `共 ${lines.length} 个输入 · 自动补全链接并去重` : '输入视频 ID，自动补全链接；也支持完整链接';
}
function renderTasks() {
  const active = state.counts ? (state.counts.queued || 0) + (state.counts.running || 0) : state.tasks.filter(task => ['queued', 'running'].includes(task.status)).length;
  $('#stat-active').textContent = active;
  const stopped = state.status?.download_queue?.paused;
  $('#stat-active').closest('.stat-card').querySelector('.stat-label').firstChild.nodeValue = stopped ? '已停止 · 待继续 ' : '正在处理 ';
  $('#stat-complete').textContent = state.counts ? state.counts.succeeded || 0 : state.tasks.filter(task => task.status === 'succeeded').length;
  $('#nav-task-count').textContent = state.counts ? ['succeeded', 'failed', 'interrupted'].reduce((sum, status) => sum + (state.counts[status] || 0), 0) : state.tasks.length;
  $('#active-caption').textContent = stopped ? '下载已停止，待处理任务保留，点击继续恢复' : active ? '后台按顺序执行，可继续浏览页面' : '准备好开启下一次创作';
  $('#recent-tasks').innerHTML = state.overview.recent_tasks.length ? taskRows(state.overview.recent_tasks.slice(0, 3)) : emptyState('还没有任务记录', '任务结束后，执行结果会在这里出现。');
  $('#all-tasks').innerHTML = state.tasks.length ? taskRows(state.tasks) : emptyState('暂无已结束的任务', '可调整搜索条件；任务结束后会自动显示在这里。');
  renderBatches();
}
function renderStatus() {
  if (!state.status) return;
  const { tools } = state.status;
  const ready = tools.ffmpeg && tools.ffprobe && (tools.node || tools.deno);
  $('.environment-card').classList.toggle('warning', !ready);
  $('#environment-title').innerHTML = `${ready ? '工具已就绪' : '待完善'}<span class="status-dot"></span>`;
  $('#environment-note').textContent = !tools.ffmpeg || !tools.ffprobe ? '缺少 FFmpeg / ffprobe' : !tools.node && !tools.deno ? '缺少 JS 运行时' : `Python ${state.status.python}`;
  renderYouTubeLogin();
  const methodTwo = state.config?.download_method === 'yt5s';
  renderDownloadMethod();
  $$('[data-youtube-status]').forEach(element => {
    const auth = state.status.youtube?.auth;
    element.textContent = methodTwo ? '方式二不使用 YouTube Cookie' : auth?.state === 'valid' ? 'YouTube 登录已验证' : !state.status.youtube?.saved ? '尚未登录 YouTube' : auth?.state === 'unchecked' ? 'Cookie 已保存 · 待检测登录' : '登录检查未通过';
  });
  const auth = state.status.youtube?.auth;
  $('#youtube-auth-status').textContent = methodTwo ? '方式二通过 yt5s 网站解析，可能需要在服务器浏览器中完成网站验证。' : auth?.message || '下载前将自动检测 YouTube 登录状态。';
  $('#youtube-auth-status').classList.toggle('error', !methodTwo && !!auth && !['valid', 'unchecked'].includes(auth.state));
  $('#youtube-auth-check').disabled = methodTwo || authChecking || !!state.status.youtube?.login?.active;
  $('#youtube-auth-check').textContent = methodTwo ? '方式二无需登录检测' : authChecking ? '正在检测…' : '检测登录';
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
  const queue = state.status?.download_queue;
  const busy = youtubeLoginBusy || !!login?.active;
  const needsStop = !!queue && (queue.running > 0 || (!queue.paused && queue.queued > 0));
  $('#download-form button[type=submit]').disabled = downloadSubmitting || downloadMethodBusy || !!login?.active;
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
  $('#clash-subscription-badge').textContent = data.subscription_saved ? '订阅已保存' : '未配置';
  $('#clash-subscription-url').placeholder = data.subscription_saved ? '已保存，留空更新原订阅；填写新链接可替换' : '粘贴 Clash / Mihomo 订阅链接';
  $('#clash-subscription-info').textContent = data.subscription_saved ? `${data.subscription_host} · 更新于 ${formatDate(data.updated_at, true)}${data.notices?.length ? '\n' + data.notices.join(' · ') : ''}` : '导入订阅后即可选择节点。链接仅保存在本机，保存后不回显。';
  $('#clash-core-status').textContent = data.running ? '代理运行中' : data.installed ? '代理已停止' : '内核未安装';
  $('#proxy-mode-select').value = data.mode;
  $('#proxy-mode-select').disabled = clashBusy || data.locked;
  $('#clash-selected').textContent = data.selected || '尚未选择节点';
  $('#clash-connection-info').textContent = data.mode === 'clash' ? '下载使用所选节点；停止后将在下一次下载时自动启动。' : data.mode === 'direct' ? '当前下载直接连接网络。' : '当前下载使用“下载设置”中的手动代理地址。';
  $('#download-proxy-note').textContent = data.mode === 'clash' ? `Clash · ${data.selected || '尚未配置'}${data.rotation?.enabled ? ` · 每 ${data.rotation.every} 个视频轮换` : ''}` : data.mode === 'direct' ? '下载连接：直连' : `下载连接：${state.config?.proxy ? '手动代理' : '直连'}`;
  $('#clash-lock-note').hidden = !data.locked;
  $('#clash-error').hidden = !data.error;
  $('#clash-error').textContent = data.error || '';
  $('#clash-import').disabled = clashBusy || data.locked || testing;
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
    const delay = node.delay ? node.delay.ms === null ? '超时 / 不可用' : `${node.delay.ms} ms` : '未测速';
    return `<article class="clash-node${selected ? ' selected' : ''}"><div class="clash-node-heading"><strong title="${escapeHTML(node.name)}">${escapeHTML(node.name)}</strong>${selected ? '<span class="count-pill">当前节点</span>' : ''}</div><div class="clash-node-meta"><span>${escapeHTML(node.type.toUpperCase())}</span><span class="${node.delay?.ms ? 'delay-ok' : node.delay ? 'delay-failed' : ''}" title="${node.delay ? escapeHTML(formatDate(node.delay.checked_at, true)) : ''}">${delay}</span></div><div class="clash-node-actions"><button type="button" class="text-button" data-node-delay="${escapeHTML(node.name)}"${clashBusy || testing || !data.running ? ' disabled' : ''}>测试延迟</button><button type="button" class="button ${selected ? 'button-dark' : 'button-white'}" data-node-select="${escapeHTML(node.name)}"${clashBusy || data.locked || selected ? ' disabled' : ''}>${selected ? '使用中' : '使用此节点'}</button></div></article>`;
  }).join('')}</div>` : emptyState(data.nodes.length ? '没有匹配的节点' : '还没有订阅节点', data.nodes.length ? '试试其他名称或协议。' : '在上方粘贴订阅链接，保存后即可选择节点。');
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
  const nodeSignature = JSON.stringify([data.nodes.map(node => node.name), rotationDraft.nodes, locked]);
  if (nodeSignature === rotationNodeSignature) return;
  rotationNodeSignature = nodeSignature;
  $('#rotation-nodes').innerHTML = data.nodes.length ? data.nodes.map(node => {
    const index = rotationDraft.nodes.indexOf(node.name);
    return `<label class="rotation-node${index >= 0 ? ' checked' : ''}"><input type="checkbox" data-rotation-node="${escapeHTML(node.name)}"${index >= 0 ? ' checked' : ''}${locked ? ' disabled' : ''}><span>${escapeHTML(node.name)}</span>${index >= 0 ? `<b>${index + 1}</b>` : ''}</label>`;
  }).join('') : '<p class="micro-copy">导入订阅后可选择轮换节点。</p>';
}
async function clashAction(operation) {
  if (clashBusy) return;
  clashBusy = true;
  renderClash();
  try { await operation(); }
  catch (error) { toast(error.message || '代理操作失败，请稍后重试。', true); }
  finally {
    clashBusy = false;
    try { await refresh(); } catch (error) { toast(error.message, true); }
    renderClash();
  }
}
function fillConfig(config) {
  state.config = config;
  const form = $('#settings-form');
  for (const element of $$('input[name], select[name], textarea[name]', form)) {
    const value = config[element.name];
    element.value = Array.isArray(value) ? value.join('\n') : value ?? '';
  }
  renderStorageSettings();
  renderDownloadMethod();
}
function renderDownloadMethod() {
  const method = state.config?.download_method || 'yt_dlp';
  $('#download-method').value = method;
  $('#download-method').disabled = downloadMethodBusy || !state.config;
  const tools = state.status?.tools;
  const missing = tools && (!tools.yt5s_browser || !tools.yt5s_display);
  $('#download-method-note').textContent = method === 'yt5s'
    ? `方式二：yt5s · ${state.config?.yt5s_quality || '360'}p · 无需 YouTube Cookie。${missing ? '当前环境缺少浏览器或显示环境，Linux 请启用浏览器版 Docker 编排。' : '网站可能要求验证，建议先小批量测试。'}切换仅影响新任务。`
    : '方式一：yt-dlp，下载前检测 YouTube Cookie 登录。切换仅影响新任务。';
}
$('#download-method').addEventListener('change', async event => {
  const method = event.target.value;
  downloadMethodBusy = true;
  renderYouTubeLogin();
  $('#download-method').disabled = true;
  try {
    await api('/api/config', { method: 'PUT', body: { download_method: method } });
    state.config.download_method = method;
    $('#settings-download-method').value = method;
    toast(`已切换为${method === 'yt5s' ? '方式二：yt5s' : '方式一：yt-dlp'}，对新任务生效。`);
    await refresh();
  } catch (error) { toast(error.message, true); }
  finally { downloadMethodBusy = false; renderDownloadMethod(); renderStatus(); }
});
function renderStorageSettings() {
  const test = $('#storage-mode').value === 'test';
  $$('[data-local-storage]').forEach(label => {
    label.hidden = test;
    $('input', label).disabled = test;
  });
  $('#storage-mode-note').textContent = test ? '真实下载后自动删除视频、封面、字幕及元数据，仅保留任务记录和日志，不加入素材库。切换前请先停止全部下载；保存后对尚未开始的任务生效。' : '视频及配套文件保存到指定目录，下载完成后可在素材库查看。网盘保存待开发。';
}
$('#storage-mode').addEventListener('change', renderStorageSettings);
async function refresh() {
  if (state.polling) return;
  state.polling = true;
  try {
    const [media, tasks, status, clash, overview, active] = await Promise.all([
      api(`/api/media?${listQuery('media', { limit: 24, q: $('#media-search').value })}`),
      api(`/api/tasks?${listQuery('tasks', { scope: 'download', view: 'history', limit: 50, status: $('#task-filter').value === 'all' ? '' : $('#task-filter').value, q: $('#task-search').value })}`),
      api('/api/status'), api('/api/clash'), api('/api/overview'), api('/api/tasks?view=active&limit=100&order=asc')
    ]);
    const mediaChanged = JSON.stringify([state.media, state.overview.media, state.overview.recent_media]) !== JSON.stringify([media.items, overview.media, overview.recent_media]);
    state.overview = overview;
    state.media = media.items;
    state.tasks = tasks.items;
    state.counts = overview.counts;
    state.status = status;
    renderDownloadControl();
    state.clash = clash;
    state.batchTasks = active.items;
    pages.media.next = media.next_cursor;
    pages.tasks.next = tasks.next_cursor;
    if (mediaChanged) renderMedia();
    renderTasks(); renderStatus(); renderClash(); renderPagination();
    $('#media-directory').textContent = media.directory;
    const testStorage = status.storage_mode === 'test';
    $('#download-storage-label').textContent = testStorage ? '保存位置 · 测试模式' : '保存位置 · 指定目录';
    $('#download-directory').textContent = testStorage ? '下载完成后自动删除本次文件，仅保留任务记录和日志。' : media.directory;
    $('#download-final-step').textContent = testStorage ? '完成后删除' : '保存到素材库';
    $('#database-backend').textContent = `${status.database === 'postgresql' ? 'PostgreSQL 已连接' : 'SQLite 便携模式'} · ${status.worker_count} 个下载进程 · 素材按页查询`;
    const workers = status.workers || [];
    const paused = workers.filter(worker => worker.state === 'paused');
    $('#worker-status').hidden = !paused.length;
    $('#worker-status').textContent = paused.map(worker => worker.message).join('；');
    if (state.selectedTask && $('#task-dialog').open) await updateTaskDetail();
  } finally { state.polling = false; }
}

function renderDownloadControl() {
  const queue = state.status?.download_queue;
  const button = $('#download-queue-toggle');
  button.textContent = queueControlBusy && queue?.paused ? '正在检测登录…' : queue?.paused ? (queue.running ? '正在停止…' : '继续下载') : '停止全部下载';
  button.disabled = queueControlBusy || !queue || (queue.paused ? queue.running > 0 || !!state.status?.youtube?.login?.active : !queue.running && !queue.queued);
  button.classList.toggle('button-dark', !!queue?.paused);
  const note = $('#download-queue-status');
  note.textContent = queue?.paused ? (queue.running ? '正在结束当前下载，排队任务已保留。' : `下载已停止，保留 ${queue.queued} 个待处理任务。点击“继续下载”后恢复。`) : '停止会保留队列和部分文件，继续时尝试断点续传。';
  const auth = state.status?.youtube?.auth_block || state.status?.youtube?.auth;
  if (queue?.paused && auth && !['valid', 'unchecked'].includes(auth.state)) note.textContent = `${auth.message} 队列已停止，待处理任务已保留。`;
  note.classList.toggle('queue-stopped', !!queue?.paused);
}

let queueControlBusy = false;
$('#download-queue-toggle').addEventListener('click', async () => {
  if (queueControlBusy || !state.status?.download_queue) return;
  queueControlBusy = true;
  const paused = state.status.download_queue.paused;
  renderDownloadControl();
  try {
    const result = await api(`/api/downloads/${paused ? 'resume' : 'stop'}`, { method: 'POST' });
    state.status.download_queue = result.download_queue;
    toast(result.message);
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
  if (!$('#task-dialog').open) $('#task-dialog').showModal();
  try { await updateTaskDetail(); } catch (error) { toast(error.message, true); }
}
async function updateTaskDetail() {
  if (state.detailBusy) return;
  state.detailBusy = true;
  try {
  const taskId = state.selectedTask;
  const [task, logs] = await Promise.all([api(`/api/tasks/${taskId}`), api(`/api/tasks/${taskId}/logs?offset=${state.logOffset}`)]);
  if (state.selectedTask !== taskId) return;
  $('#task-dialog-title').textContent = actionNames[task.action] || task.action;
  $('#task-detail-meta').innerHTML = `<span class="task-status status-${task.status}">${taskStatus(task)}</span><span>${task.id.slice(0, 8)}</span><span>创建于 ${escapeHTML(formatDate(task.created_at, true))}</span>${task.started_at ? `<span>开始于 ${escapeHTML(formatDate(task.started_at, true))}</span>` : ''}${task.finished_at ? `<span>结束于 ${escapeHTML(formatDate(task.finished_at, true))}</span>` : ''}`;
  $('#task-error').hidden = !task.error;
  $('#task-error').textContent = task.error || '';
  $('#task-download-progress').innerHTML = ['download', 'import'].includes(task.action) ? downloadProgress(task) : task.progress?.scanned ? `<p class="micro-copy">已扫描 ${task.progress.scanned} 个文件</p>` : '';
  const result = task.result;
  if (result?.message) { $('#task-result').innerHTML = `<div class="result-summary">${escapeHTML(result.message)}</div>`; }
  if (logs.text) {
    const log = $('#task-log');
    const atBottom = log.scrollHeight - log.clientHeight - log.scrollTop < 45;
    state.logs = (state.logs + logs.text).slice(-200000);
    log.textContent = state.logs;
    if (atBottom) log.scrollTop = log.scrollHeight;
  }
  state.logOffset = logs.offset;
  } finally { state.detailBusy = false; }
}

document.addEventListener('click', event => {
  const nav = event.target.closest('[data-view]');
  if (nav) setView(nav.dataset.view);
  const open = event.target.closest('[data-open]');
  if (open) $(`#${open.dataset.open}`).showModal();
  if (event.target.closest('[data-close]')) event.target.closest('dialog').close();
  const task = event.target.closest('[data-task]');
  if (task) openTask(task.dataset.task);
  const media = event.target.closest('[data-media]');
  if (media) openMedia(media.dataset.media);
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
$('#download-form').addEventListener('submit', event => {
  event.preventDefault();
  runButton($('button[type="submit"]', event.target), async () => {
    const button = $('button[type="submit"]', event.target);
    const label = button.innerHTML;
    downloadSubmitting = true;
    button.textContent = state.config?.download_method === 'yt5s' ? '正在提交方式二任务…' : '正在检查登录并提交…';
    let batch;
    try { batch = await api('/api/downloads', { method: 'POST', body: { urls: $('#download-urls').value, scope: 'download' } }); }
    finally { downloadSubmitting = false; button.innerHTML = label; }
    toast(`${batch.items.length} 个视频已加入下载队列。`);
    $('#batch-progress-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    await refresh();
  });
});
$('#download-urls').addEventListener('input', updateInputCount);
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
$('#bulk-import-form').addEventListener('submit', event => {
  event.preventDefault();
  runButton($('button[type="submit"]', event.target), async () => {
    const file = $('input[name="file"]', event.target).files[0];
    if (!file || !file.size || file.size > 1024 ** 3) throw new Error('请选择非空的 TXT / CSV 文件，最大 1 GB。');
    const task = await api('/api/downloads/import', { method: 'POST', body: new FormData(event.target) });
    $('#bulk-import-dialog').close(); event.target.reset(); resetPage('tasks');
    toast('清单已上传，正在后台分批导入。'); await openTask(task.id); await refresh();
  });
});
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
  clashAction(async () => {
    const result = await api('/api/clash/subscription', { method: 'POST', body: { url: $('#clash-subscription-url').value.trim() } });
    $('#clash-subscription-url').value = '';
    $('#clash-youtube-result').textContent = '';
    fillConfig((await api('/api/config')).config);
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
$('#settings-form').addEventListener('submit', event => {
  event.preventDefault();
  runButton($('button[type="submit"]', event.target), async () => {
    const data = Object.fromEntries(new FormData(event.target));
    await api('/api/config', { method: 'PUT', body: data });
    fillConfig((await api('/api/config')).config);
    $('#download-urls').value = inputAsIds(state.config.url);
    updateInputCount();
    await refresh();
    toast('设置已保存，后续任务将使用新的配置。');
  });
});

async function init() {
  $('#today').textContent = new Date().toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).replaceAll('/', '.');
  renderMedia(); renderTasks();
  setView(location.hash.slice(1) || 'overview');
  try {
    const config = await api('/api/config');
    fillConfig(config.config);
    const url = config.config.url;
    $('#download-urls').value = inputAsIds(url);
    updateInputCount();
    await refresh();
  } catch (error) { toast(`读取工作台失败：${error.message}`, true); }
  let failures = 0;
  setInterval(async () => {
    if (document.hidden) return;
    try { await refresh(); failures = 0; }
    catch { failures += 1; if (failures === 3) toast('暂时无法连接服务，正在自动重试。', true); }
  }, 2000);
}
init();
