'use strict';

function setInstanceReady(ready, error = '') {
  state.instanceReady = ready;
  state.instanceError = error;
  for (const id of ['settings-view', 'clash-view', 'media-view', 'overview-view', 'deployment-view']) {
    document.getElementById(id).inert = !ready;
  }
  const retry = $('#fleet-reload');
  if (retry) { retry.hidden = ready; retry.disabled = !!state.instanceLoading; }
  renderInstanceScope();
}
function instanceHasDraft() {
  const subscriptionName = $('#clash-subscription-name').value.trim();
  const savedName = state.clash?.subscriptions?.find(item => item.id === subscriptionSelection)?.name || '';
  return state.deploymentDirty || settingsDirty || providerDraft || cookiePoolDraft || state.accountPoolDirty || state.accountSourceDirty || (typeof accountHasDraft === 'function' && accountHasDraft()) || rotationDirty ||
    !!$('#clash-subscription-url').value.trim() || subscriptionName !== savedName ||
    !!$('[data-cookie-file]', $('#youtube-cookie-form')).files.length ||
    !!$('[data-cookie-name]', $('#youtube-cookie-form')).value.trim();
}
async function loadSelectedInstance({ reset = true } = {}) {
  const instance = state.instanceId;
  const epoch = (state.instanceEpoch || 0) + 1;
  state.instanceEpoch = epoch;
  state.instanceLoading = true;
  setInstanceReady(false);
  if (reset) {
    for (const dialog of $$('dialog[open]')) dialog.close();
    state.status = null; state.clash = null; state.media = []; state.config = null; state.configInstance = null;
    state.clearMediaDirectory = null;
    resetInstanceDeployment();
    nodeSignature = ''; subscriptionInstance = undefined; subscriptionSelection = null; rotationDraft = null;
    $('#clash-subscription-name').value = ''; $('#clash-subscription-url').value = '';
    $('#youtube-cookie-list').replaceChildren();
    resetPage('media'); renderMedia();
  }
  try {
    // Keep configuration loading independent of workbench failures.
    const data = await api('/api/config');
    if (epoch !== state.instanceEpoch || instance !== state.instanceId) return;
    if (data.instance_id && data.instance_id !== instance) throw new Error('返回的配置不属于所选实例，请检查实例地址。');
    fillConfig(data.config);
    if (reset) {
      await resetWorkbench().catch(error => {
        if (epoch === state.instanceEpoch && instance === state.instanceId) toast(`工作台读取失败：${error.message}`, true);
      });
      if (epoch !== state.instanceEpoch || instance !== state.instanceId) return;
      fillConfig(data.config);
    }
    setInstanceReady(true);
    renderDownloadControl();
    await loadFleet();
    if (location.hash === '#accounts' && typeof accountTab !== 'undefined' && accountTab === 'pool') loadCookieProfiles().catch(error => {
      if (epoch === state.instanceEpoch && instance === state.instanceId && error.name !== 'AbortError') {
        $('#youtube-cookie-list').textContent = error.message;
      }
    });
    if (location.hash === '#deployment') loadInstanceDeployment();
    await refresh();
    if (epoch !== state.instanceEpoch || instance !== state.instanceId) return;
    if (typeof refreshDisk === 'function') refreshDisk();
  } catch (error) {
    if (epoch !== state.instanceEpoch || instance !== state.instanceId) return;
    setInstanceReady(false, `无法读取“${instanceName(instance)}”：${error.message}。请重试或选择其他实例。`);
    throw error;
  } finally {
    if (epoch === state.instanceEpoch) {
      state.instanceLoading = false;
      const retry = $('#fleet-reload');
      if (retry) retry.disabled = false;
      renderInstanceScope();
    }
  }
}

function renderInstanceScope() {
  const local = ['overview','media','settings','clash','instances','deployment',''].includes(location.hash.slice(1)) || location.hash === '#accounts' && document.querySelector('[data-account-pane=pool]')?.hidden === false;
  $('#fleet-selection-bar').hidden = !fleetEnabled || !local;
  const target = instanceName(state.instanceId);
  const item = state.fleet?.items.find(item => item.id === state.instanceId);
  $('#fleet-target-name').textContent = state.instanceId ? target : '尚未选择';
  $('#fleet-target-name').title = state.instanceId || '';
  $('#fleet-entry-name').textContent = state.fleet?.instance_id ? instanceName(state.fleet.instance_id) : location.host;
  $('#fleet-entry-name').title = state.fleet?.instance_id || location.host;
  const badge = $('#fleet-target-status');
  badge.textContent = item ? (item.online ? `在线 · ${instanceState(item)}` : '离线') : state.instanceId ? '未登记' : '待选择';
  badge.dataset.online = String(!!item?.online);
  for (const [selector, label] of [
    ['#settings-form button[type="submit"]', `保存设置到「${target}」`],
    ['#instance-deployment-form button[type="submit"]', `保存部署配置到「${target}」`],
    ['#instance-deployment-test', `从「${target}」测试连接`],
    ['#rotation-save', `保存轮换配置到「${target}」`],
  ]) {
    const button = $(selector);
    if (button?.lastChild?.nodeType === Node.TEXT_NODE && button.lastChild.textContent !== label) button.lastChild.textContent = label;
  }
  renderInstanceChoices();
  if (typeof renderAccountScope === 'function') renderAccountScope();
  if (state.instanceError || state.instanceLoading) {
    $('#fleet-selection-note').textContent = state.instanceError || `正在读取“${instanceName(state.instanceId)}”，操作将在读取完成后恢复。`;
  } else if (state.fleet?.items.some(item => ['worker', 'hybrid'].includes(item.role))) {
    $('#fleet-selection-note').textContent = location.hash === '#instances' ? '下方展示全部实例；每张卡片操作对应实例，全局并发设置影响全部实例。工作台和设置跟随上方所选实例。' : location.hash === '#deployment' ? '部署配置保存到上方管理实例，重启该实例服务后生效。访问入口不影响保存目标。' : '工作台、系统设置、代理和素材库跟随上方管理实例；任务中心、客户和账单为全局共享。';
  }
}

function renderInstanceChoices() {
  const selector = $('#fleet-selection');
  const workers = state.fleet?.items.filter(item => ['worker', 'hybrid'].includes(item.role)) || [];
  const query = $('#fleet-search').value.trim().toLocaleLowerCase();
  const matches = workers.filter(item => !query || [item.name, item.id, item.host].some(value => String(value || '').toLocaleLowerCase().includes(query)));
  const option = item => `<option value="${escapeHTML(item.id)}">${escapeHTML(item.name || item.id)} · ${escapeHTML(item.id)}${item.id === state.fleet?.instance_id ? '（访问入口）' : ''} · ${instanceState(item)}</option>`;
  const selected = workers.find(item => item.id === state.instanceId);
  const retained = state.instanceId && !matches.some(item => item.id === state.instanceId);
  const current = retained ? `<optgroup label="当前管理（保留选择）">${selected ? option(selected) : `<option value="${escapeHTML(state.instanceId)}">${escapeHTML(state.instanceId)} · 未登记</option>`}</optgroup>` : '';
  const options = current + matches.map(option).join('');
  if (selector.innerHTML !== options) selector.innerHTML = options;
  selector.value = state.instanceId || '';
  $('#fleet-search-result').hidden = !query;
  $('#fleet-search-result').textContent = query ? `找到 ${matches.length} / ${workers.length} 个实例。${retained ? '当前管理实例已保留，搜索不会切换实例。' : '选择实例后切换管理目标。'}` : '';
}

function instanceName(identity) {
  return state.fleet?.items.find(item => item.id === identity)?.name || identity || '旧版实例';
}
function mediaAddress(kind, name, identity) {
  const target = identity || state.instanceId;
  return `/media/${kind}/${encodeURIComponent(name)}${target ? `?instance_id=${encodeURIComponent(target)}` : ''}`;
}
function instanceMode(item) {
  return item.cookie_count ? 'cookie_pool' : item.concurrency?.mode || item.mode || item.throughput?.mode || 'manual';
}
function instanceLive(item) {
  const job = item.selected_job;
  const mode = instanceMode(item), pool = item.concurrency?.cookie_pool;
  const source = mode === 'cookie_pool' ? `本实例 Cookie 池 · ${item.cookie_count || 0} 份${pool?.effective_sessions == null ? '' : ` · ${pool.effective_sessions} 个核心会话`}` : `${mode === 'auto' ? '自动并发' : '手动并发'} · ${item.has_cookie ? '本实例单 Cookie' : '未配置 Cookie'}`;
  const limit = item.concurrency?.effective_limit;
  const capacity = !item.online || item.paused ? '' : mode === 'cookie_pool' && item.concurrency && !pool?.known ? ' · 会话容量待采样'
    : limit == null ? '' : ` · 有效传输上限 ${escapeHTML(limit)}`;
  return `<div class="instance-task"><span class="micro-copy">下载任务</span><strong>${job ? escapeHTML(job.customer)+' / '+escapeHTML(job.name) : '尚未选择任务'}</strong></div><div class="fleet-metrics instance-live-metrics"><span>处理中视频 <b>${item.online ? item.running : '—'}</b></span><span>传输中视频 <b>${item.online ? item.transferring : '—'}</b></span><span>总传输速度 <b>${item.online ? bytes(item.speed)+'/s' : '—'}</b></span></div><p class="micro-copy instance-capacity">${escapeHTML(source)}${capacity}</p>`;
}
function instanceState(item) {
  return !item.online ? '离线' : item.paused ? item.running ? item.stop_requested ? '正在停止' : '等待当前视频完成' : '已停止' : item.running ? '已启动 · 处理中' : '已启动 · 等待任务';
}
function instanceActionDisabled(item, action) {
  if (!item || state.fleetControlBusy) return true;
  const paused = !!item.paused, stopping = !!item.stop_requested;
  if (action === 'resume') return !item.online || !paused || (stopping && item.running > 0);
  if (action === 'drain') return paused;
  return stopping || (paused && !item.running);
}
function syncInstanceControls() {
  for (const button of $$('[data-instance-action]')) {
    const item = state.fleet?.items.find(item => item.id === button.dataset.instance);
    button.disabled = instanceActionDisabled(item, button.dataset.instanceAction);
  }
}
function instanceCard(item, expanded) {
  const id=escapeHTML(item.id), address=encodeURIComponent(item.id);
  const disabled = action => instanceActionDisabled(item, action) ? ' disabled' : '';
  return `<article class="fleet-instance panel"><form data-instance-form="${id}">
    <div class="panel-heading"><div><h2>${escapeHTML(item.name)}${item.id===state.fleet.instance_id?'（本机访问入口）':''}</h2><p class="micro-copy">${escapeHTML(item.host)}</p></div><span class="count-pill" data-instance-state="${id}" data-online="${item.online}">${instanceState(item)}</span></div>
    <p class="task-error" data-instance-error="${id}" ${item.error?'':'hidden'}>${escapeHTML(item.error || '')}</p>
    <div data-instance-live="${id}">${instanceLive(item)}</div>
    <div class="instance-links"><a class="text-button" href="/?instance_id=${address}#overview">下载工作台 ↗</a><button type="button" class="text-button" data-manage-account-instance="${id}">管理账号 ↗</button><a class="text-button" href="/?instance_id=${address}#settings">实例设置 ↗</a></div>
    <div class="media-library-actions instance-controls"><button type="button" class="button button-white" data-instance-action="resume" data-instance="${id}"${disabled('resume')}>启动</button><button type="button" class="button button-white" data-instance-action="drain" data-instance="${id}"${disabled('drain')}>完成当前后停止</button><button type="button" class="button button-danger" data-instance-action="stop" data-instance="${id}"${disabled('stop')}>立即停止</button></div>
    <details class="instance-options" data-instance-options="${id}" ${expanded?'open':''}><summary>实例详情与名称</summary><div class="fleet-fields"><label>显示名称<input name="name" value="${escapeHTML(item.name)}" required maxlength="100"></label></div><p class="micro-copy">实例 ID：${id}<br>主机：${escapeHTML(item.host)}<br>素材目录：${escapeHTML(item.directory)}</p><p class="micro-copy">进程容量在系统设置修改；账号池成员和节奏在账号管理统一保存。</p><button type="submit" class="button button-white">保存显示名称</button></details>
  </form></article>`;
}
let fleetLoading = false;
let fleetRetryAt = 0;
let fleetFailures = 0;
let fleetStructureKey = '';
let fleetSampleAt = 0;
async function loadFleet({ poll = false } = {}) {
  const view = location.hash.slice(1) || 'overview';
  const interval = ['overview', 'settings', 'instances'].includes(view) ? 2000 : 10000;
  if (!fleetEnabled || fleetLoading || (poll && (Date.now() < fleetRetryAt || Date.now() - fleetSampleAt < interval - 200))) return;
  fleetLoading = true;
  fleetSampleAt = Date.now();
  const requestedInstance = state.instanceId;
  const sampledAt = performance.now();
  try {
  const query = new URLSearchParams({compact: '1'});
  if (poll) query.set('poll', '1');
  const data = await api('/api/instances?' + query);
  if (requestedInstance !== state.instanceId) return;
  fleetFailures = 0;
  state.fleet = data;
  applyStartup(data.startup, sampledAt);
  const workers = data.items.filter(item => ['worker', 'hybrid'].includes(item.role));
  const previous = state.instanceId;
  const hadConfig = !!state.config;
  if (!state.instanceId && workers.length) {
    state.instanceId = workers.find(item => item.id === data.instance_id)?.id || workers.find(item => item.online)?.id || workers[0]?.id || '';
  }
  if (state.instanceId !== previous && typeof refreshDisk==='function') refreshDisk();
  if (view === 'overview') renderDownloadControl();
  if (state.instanceId && state.instanceId !== previous && hadConfig && !settingsDirty) {
    await loadSelectedInstance();
  }
  renderInstanceScope();
  if (!workers.length && !state.instanceId) $('#fleet-selection-note').textContent = '还没有下载实例，请启动服务并检查数据库连接。';
  if (view !== 'instances') {
    if (['overview', 'settings'].includes(view)) renderConcurrency();
    return;
  }
  const monitor=[['在线实例',`${workers.filter(item=>item.online).length} / ${workers.length}`],['处理中视频',workers.reduce((sum,item)=>sum+Number(item.running||0),0)],['总传输速度',bytes(workers.reduce((sum,item)=>sum+Number(item.online?item.speed||0:0),0))+'/s'],['异常实例',workers.filter(item=>!item.online||item.error).length]];
  $('#fleet-monitor-summary').innerHTML=monitor.map(([label,value])=>`<div class="monitor-metric"><span>${label}</span><strong>${escapeHTML(value)}</strong></div>`).join('');
  const structure = JSON.stringify(workers.map(item => [item.id, item.name, item.role, item.host, item.directory, item.worker_count, item.yt5s_browser_count, item.min_free_gb, instanceMode(item), item.resources, item.uses_browser]));
  if (!state.fleetFormDirty && document.activeElement !== $('#fleet-global-limit')) $('#fleet-global-limit').value = data.global_limit;
  if (structure !== fleetStructureKey && !state.fleetControlBusy && !state.fleetFormDirty && !document.activeElement?.matches('#instances-view input')) {
    fleetStructureKey = structure;
    $('#fleet-global-limit').value = data.global_limit;
    const expanded=new Set($$('[data-instance-options][open]').map(node=>node.dataset.instanceOptions));
    $('#fleet-instances').innerHTML = workers.length ? workers.map(item=>instanceCard(item,expanded.has(item.id))).join('') : emptyState('还没有下载实例', '在其他机器启动服务，连接同一个 MySQL 后会自动注册。');
  }
  for (const item of workers) {
    const live = [...$$('[data-instance-live]')].find(node => node.dataset.instanceLive===item.id);
    const html = instanceLive(item);
    if (live && live.innerHTML!==html) live.innerHTML=html;
    const badge = [...$$('[data-instance-state]')].find(node => node.dataset.instanceState===item.id);
    if (badge) { badge.textContent = instanceState(item); badge.dataset.online=String(item.online); }
    const error = [...$$('[data-instance-error]')].find(node => node.dataset.instanceError===item.id);
    if (error) { error.textContent=item.error || ''; error.hidden=!item.error; }
  }
  syncInstanceControls();
  renderConcurrency();
  } catch (error) {
    fleetFailures += 1;
    fleetRetryAt = Date.now() + Math.min(30000, 2000 * 2 ** Math.min(fleetFailures - 1, 4));
    $('#fleet-selection-note').textContent = '实例状态暂未更新，请等待连接恢复。';
    throw error;
  } finally {
    fleetLoading = false;
    if (!fleetFailures) fleetRetryAt = 0;
  }
}
async function initializeFleet() {
  if (!fleetEnabled) return;
  state.instanceReady = false;
  setInstanceReady(false);
  $('#fleet-nav').hidden = false;
  renderInstanceScope();
  state.instanceId = new URLSearchParams(location.search).get('instance_id') || localStorage.getItem('frameflow-instance:' + location.origin) || '';
  if (state.instanceId) localStorage.setItem('frameflow-instance:' + location.origin, state.instanceId);
  const address = new URL(location.href);
  if (address.searchParams.has('instance_id')) {
    address.searchParams.delete('instance_id');
    history.replaceState(null, '', address.pathname + address.search + address.hash);
  }
  $('#instances-view').addEventListener('input', () => { state.fleetFormDirty = true; });
  $('#fleet-search').addEventListener('input', renderInstanceChoices);
  $('#fleet-reload').addEventListener('click', async () => {
    if (state.instanceLoading || instanceHasDraft()) {
      toast('请先保存或清除当前实例的未提交修改。', true); return;
    }
    try {
      if (!state.instanceId) await loadFleet();
      if (!state.instanceId) throw new Error('还没有可管理的实例，请检查服务注册及数据库连接。');
      await loadSelectedInstance();
    } catch (error) { toast(error.message, true); }
  });
  document.addEventListener('viewchange', () => loadFleet().catch(() => {}));
  $('#fleet-selection').addEventListener('change', async event => {
    if (queueControlBusy || clashBusy || state.instanceWrites || downloadMethodBusy) { event.target.value = state.instanceId || ''; return; }
    if (instanceHasDraft()) {
      event.target.value = state.instanceId || '';
      toast('请先保存当前修改，再切换实例。', true);
      return;
    }
    state.instanceId = event.target.value;
    const diskChip=$('#workspace-disk-status');
    diskChip.textContent='磁盘状态读取中'; diskChip.classList.remove('warning','unavailable'); diskChip.title='当前实例的磁盘状态正在更新';
    localStorage.setItem('frameflow-instance:' + location.origin, state.instanceId);
    try {
      await loadSelectedInstance();
    }
    catch (error) { toast(error.message, true); }
  });
  $('#fleet-global-form').addEventListener('submit', event => {
    event.preventDefault();
    runButton($('button', event.target), async () => {
      const result = await api('/api/instances/limits', { method: 'PUT', body: { global_limit: Number($('#fleet-global-limit').value) } });
      state.fleetFormDirty = false;
      toast(result.message); await loadFleet();
    });
  });
  $('#fleet-instances').addEventListener('submit', event => {
    event.preventDefault();
    const form = event.target;
    const data = Object.fromEntries(new FormData(form));
    for (const key of ['worker_count', 'yt5s_browser_count', 'min_free_gb', 'builtin_transfer_limit', 'youtube_cookie_session_limit', 'youtube_cookie_pool_start_interval', 'youtube_cookie_pool_worker_limit']) if (key in data) data[key] = Number(data[key]);
    runButton($('button[type="submit"]', form), async () => {
      const result = await api(`/api/instances/${encodeURIComponent(form.dataset.instanceForm)}`, { method: 'PUT', body: data });
      state.fleetFormDirty = false;
      toast(result.message); await loadFleet();
    });
  });
  $('#fleet-instances').addEventListener('click', async event => {
    const button = event.target.closest('[data-instance-action]');
    if (!button || button.disabled || state.fleetControlBusy) return;
    const label = button.textContent;
    state.fleetControlBusy = true;
    syncInstanceControls();
    button.textContent = button.dataset.instanceAction === 'resume' ? '正在检查并启动…' : '正在提交…';
    button.setAttribute('aria-busy', 'true');
    try {
      const result = await api(`/api/instances/${encodeURIComponent(button.dataset.instance)}`, { method: 'PUT', body: { action: button.dataset.instanceAction }, signal: AbortSignal.timeout(90000) });
      toast(result.message);
    } catch (error) {
      toast(error.name === 'TimeoutError' ? '实例指令等待超过 90 秒，结果尚未确认，请刷新查看最新状态。' : error.message, true);
    } finally {
      // Keep controls disabled until the result has been read back. Polling
      // must also update them while the instance configuration form is dirty.
      await loadFleet().catch(error => toast(error.message, true));
      state.fleetControlBusy = false;
      button.textContent = label;
      button.setAttribute('aria-busy', 'false');
      syncInstanceControls();
      refreshSelectedJob().catch(error => toast(error.message, true));
      refresh().catch(error => toast(error.message, true));
    }
  });
  setInterval(() => { if (!document.hidden) loadFleet({ poll: true }).catch(() => {}); }, 2000);
  await loadFleet();
}
