'use strict';

// Executor-scoped Cookie library and pool membership.
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
        `${session?.state === 'manual' ? '需人工处理 · 暂停新请求' : session?.state === 'cooldown' ? '冷却中 · 暂停新请求' : session?.state === 'recovery' ? '低频恢复中' : '可参与调度'}：设置上限 ${account.configured_limit || pool.session_limit}，当前最多 ${session?.limit ?? account.limit ?? 1} 个任务，启动间隔至少 ${session?.interval || account.interval || 60} 秒。` +
        (session?.state === 'manual' ? `原因：${failureKindNames[session.halt_kind] || '登录或验证未通过'}；处理后点击“检测登录”，通过后恢复该账号，其他账号继续。` : '') +
        (session?.state === 'cooldown' ? `原因：${failureKindNames[session.cooldown_kind] || '临时故障'}；恢复检查时间 ${new Date(session.cooldown_until * 1000).toLocaleString()}；其他健康账号继续。` : '') +
        (session?.state === 'recovery' ? `启动间隔延长原因：${failureKindNames[session.cooldown_kind] || '此前故障'}；本轮有效成功 ${session.recovery_successes} 次，达标后自动恢复设置并发。` : '') +
        (session?.last_failure_at ? ` 最近异常 ${new Date(session.last_failure_at * 1000).toLocaleString()}。` : '') +
        (account.shared_with ? ` 与「${session?.name || '同账号文件'}」共享并发、间隔和冷却。` : '');
    }
    const check = row.querySelector('[data-cookie-login-check]');
    if (check && check.textContent === '检测登录') {
      check.disabled = session?.state === 'cooldown' ||
        session?.state === 'manual' && session?.cooldown_until > Date.now() / 1000;
      check.title = check.disabled ? pool.halted
        ? '池仍有人工拦截；冷却结束后须按提示检查并解除，不会自动恢复。'
        : '该账号冷却中，暂不发送登录检测请求；到期自动低频恢复。' : '';
    }
  }
}
let cookiePoolPolling = false;
setInterval(async () => {
  if (location.hash !== '#accounts' || accountTab !== 'pool' || document.hidden || cookiePoolPolling) return;
  cookiePoolPolling = true;
  const instanceId = state.instanceId;
  try {
    const data = await api('/api/youtube/cookies');
    if (state.instanceId === instanceId && location.hash === '#accounts' && accountTab === 'pool') updateCookiePoolLive(data);
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
        (item.session.state === 'recovery' ? ` · 恢复成功 ${item.session.recovery_successes || 0} 次` : '');
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
      if (!await confirmDialog(`删除“${item.name}”？${selectionNote}删除后需重新上传才能使用。`, {title: '删除 Cookie', confirmText: '确认删除'})) return;
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
  if (typeof openAccountCookie === 'function') return openAccountCookie(state.instanceId || accountPoolInstance, item.id, 'schedule');
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
      ? `未保存：已勾选 ${cookiePoolDraft.ids.length} 份 Cookie，点击页面底部“保存账号池与节奏”后生效。`
      : '未保存：将关闭账号池，点击页面底部“保存账号池与节奏”后生效。';
  } else {
    const count = state.config?.youtube_cookie_pool?.length || 0;
    status.textContent = count ? `已保存：账号池已启用，共 ${count} 份 Cookie。`
      : '账号池未启用。选择至少 2 份 Cookie，仅支持单独使用 yt-dlp；修改后点击页面底部“保存账号池与节奏”。';
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
