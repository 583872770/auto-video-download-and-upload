'use strict';

function renderInstanceScope() {
  const local = ['overview','media','settings','clash','instances',''].includes(location.hash.slice(1));
  $('#fleet-selection-bar').hidden = !fleetEnabled || !local;
}

function instanceName(identity) {
  return state.fleet?.items.find(item => item.id === identity)?.name || identity || '旧版实例';
}
function mediaAddress(kind, name, identity) {
  const target = identity || state.instanceId;
  return `/media/${kind}/${encodeURIComponent(name)}${target ? `?instance_id=${encodeURIComponent(target)}` : ''}`;
}
function instanceStatistics(item) {
  const totals = item.statistics || {};
  return `<div class="fleet-metrics"><span>累计成功 <b>${Number(totals.succeeded || 0) + Number(totals.test_succeeded || 0)}</b></span><span>累计失败 <b>${Number(totals.failed || 0)}</b></span><span>交付文件 <b>${Number(totals.file_count || 0)} 个 / ${bytes(totals.file_bytes || 0)}</b></span><span>测试文件 <b>${Number(totals.test_file_count || 0)} 个 / ${bytes(totals.test_file_bytes || 0)}</b></span></div>`;
}
function renderFleetJob() {
  const item = state.fleet?.items.find(item => item.id === state.instanceId);
  const job = item?.selected_job;
  $('#fleet-job-context').innerHTML = job ? `<h2>当前实例：${escapeHTML(item.name)} · ${escapeHTML(job.customer)} / ${escapeHTML(job.name)}</h2><p class="micro-copy">${item.paused ? item.running ? '完成当前视频后暂停' : '已停止领取' : '仅领取此任务的视频'} · 下载中 ${item.running}</p><a class="text-button" href="/?instance_id=${encodeURIComponent(item.id)}#overview">返回此实例的下载工作台 ↗</a>` : '<h2>当前实例尚未选择下载任务</h2><p class="micro-copy">在下载工作台选择任务并开始，开关和任务选择只作用于当前实例。</p><a class="text-button" href="/#overview">前往下载工作台 ↗</a>';
}
function instanceCurrentTasks(item) {
  const videos = item.current_tasks || [];
  return `<div class="fleet-current-tasks"><h3>当前执行视频</h3>${videos.length ? videos.map(video => {
    const progress = video.progress || {};
    return `<p class="micro-copy"><strong>${escapeHTML(video.title || video.video_id)}</strong>${video.title?' · '+escapeHTML(video.video_id):''} · ${escapeHTML(progressLabel({action:'download',status:'running',progress}))}${progress.percent == null ? '' : ' · '+escapeHTML(progress.percent)+'%'}${progress.speed ? ' · '+bytes(progress.speed)+'/s' : ''}</p>`;
  }).join('') : '<p class="micro-copy">当前没有正在执行的视频。</p>'}</div>`;
}
function instanceLive(item) {
  const job = item.selected_job;
  const rate = item.throughput;
  const throughputDetails = rate ? `<p class="micro-copy">媒体接收 ${escapeHTML(rate.media_receive_mbps ?? '—')} Mbps · 校验成品 ${escapeHTML(rate.verified_mbps ?? '—')} Mbps（滚动窗口） · CPU ${escapeHTML(rate.cpu_percent)}% · 可用内存 ${escapeHTML(rate.memory_available_mb)} MB</p><p class="micro-copy">${Object.entries(rate.groups || {}).map(([group,value])=>`${escapeHTML(group)}：传输 ${escapeHTML(value.active)} / ${escapeHTML(value.limit)}，${escapeHTML(value.receive_mbps)} Mbps`).join('；')}</p>` : '';
  if (item.id === state.instanceId && $('#channel-performance')) $('#channel-performance').textContent = rate ? `媒体接收 ${rate.media_receive_mbps ?? '—'} Mbps · 校验成品 ${rate.verified_mbps ?? '—'} Mbps（滚动窗口） · ${rate.reason}` : '等待实例资源采样。';
  return `<div class="instance-task"><span class="micro-copy">下载任务</span><strong>${job ? escapeHTML(job.customer)+' / '+escapeHTML(job.name) : '尚未选择任务'}</strong></div><div class="fleet-metrics instance-live-metrics"><span>执行中 <b>${item.running}</b></span><span>传输中 <b>${item.transferring}</b></span><span>总速度 <b>${bytes(item.speed)}/s</b></span></div>${item.throughput ? `<p class="micro-copy">${item.throughput.mode === 'auto' ? '自动吞吐' : '手动并发'} · 传输额度 ${escapeHTML(item.throughput.transfers)} · 准备与传输总额度 ${escapeHTML(item.throughput.workers)} · 浏览器 ${escapeHTML(item.throughput.browsers)} · 接收 ${escapeHTML(item.throughput.receive_mbps)} Mbps · ${escapeHTML(item.throughput.reason)}</p>` : ''}${throughputDetails}${instanceCurrentTasks(item)}<p class="micro-copy instance-policy">${escapeHTML(instancePolicyText(item.policy || {}))}</p>${instanceStatistics(item)}`;
}
function instanceState(item) {
  return !item.online ? '离线' : item.paused ? item.running ? '完成当前后暂停' : '已暂停领取' : item.running ? '在线 · 下载中' : '在线 · 等待任务';
}
function instanceCard(item, expanded) {
  const id=escapeHTML(item.id), address=encodeURIComponent(item.id);
  return `<article class="fleet-instance panel"><form data-instance-form="${id}">
    <div class="panel-heading"><div><h2>${escapeHTML(item.name)}${item.id===state.fleet.instance_id?'（本机）':''}</h2><p class="micro-copy">${id} · ${escapeHTML(item.host)}</p></div><span class="count-pill" data-instance-state="${id}" data-online="${item.online}">${instanceState(item)}</span></div>
    <p class="task-error" data-instance-error="${id}" ${item.error?'':'hidden'}>${escapeHTML(item.error || '')}</p>
    <div data-instance-live="${id}">${instanceLive(item)}</div>
    <div class="instance-links"><a class="text-button" href="/?instance_id=${address}#overview">打开下载工作台 ↗</a><a class="text-button" href="/?instance_id=${address}#settings">下载策略与磁盘 ↗</a></div>
    <div class="media-library-actions instance-controls"><button type="button" class="button button-white" data-instance-action="resume" data-instance="${id}">恢复领取</button><button type="button" class="button button-white" data-instance-action="drain" data-instance="${id}">完成当前后暂停</button><button type="button" class="button button-danger" data-instance-action="stop" data-instance="${id}">立即停止</button></div>
    <details class="instance-options" data-instance-options="${id}" ${expanded?'open':''}><summary>实例配置 <span>名称、并发与磁盘预留</span></summary><div class="fleet-fields"><label>显示名称<input name="name" value="${escapeHTML(item.name)}" required maxlength="100"></label><label>下载并发<input name="worker_count" type="number" min="1" max="64" value="${item.worker_count}" required></label><label>浏览器并发<input name="yt5s_browser_count" type="number" min="1" max="16" value="${item.yt5s_browser_count}" required></label><label>磁盘预留 GB<input name="min_free_gb" type="number" min="0" max="10000" value="${item.min_free_gb}" required></label></div><p class="micro-copy">素材目录：${escapeHTML(item.directory)}</p><button type="submit" class="button button-white">保存实例配置</button><p class="micro-copy">降低并发时让当前视频先完成；离线实例的停止指令在恢复连接后处理。</p></details>
  </form></article>`;
}
async function loadFleet() {
  if (!fleetEnabled) return;
  const data = await api('/api/instances');
  state.fleet = data;
  const workers = data.items.filter(item => ['worker', 'hybrid'].includes(item.role));
  const previous = state.instanceId;
  const hadConfig = !!state.config;
  if (!workers.some(item => item.id === state.instanceId)) {
    state.instanceId = workers.find(item => item.id === data.instance_id)?.id || workers.find(item => item.online)?.id || workers[0]?.id || '';
  }
  if (state.instanceId !== previous && lastStartedJobId !== null) await resetWorkbench();
  if (state.instanceId !== previous && typeof refreshDisk==='function') refreshDisk();
  renderFleetJob();
  renderDownloadControl();
  if (state.instanceId && state.instanceId !== previous && hadConfig && !settingsDirty) {
    resetPage('media'); state.media = []; state.mediaTotal = 0; state.mediaDirectory = '';
    const instance = state.instanceId;
    const config = await api('/api/config');
    if (instance !== state.instanceId) return;
    fillConfig(config.config); renderDownloadControl();
  }
  const selector = $('#fleet-selection');
  const options = workers.map(item => `<option value="${escapeHTML(item.id)}">${escapeHTML(item.name)}${item.id === data.instance_id ? '（本机）' : ''} · ${item.online ? item.paused ? '已暂停领取' : '在线' : '离线'}</option>`).join('');
  if (selector.innerHTML !== options) selector.innerHTML = options;
  selector.value = state.instanceId || '';
  $('#fleet-selection-note').textContent = workers.length ? '任务选择、下载控制、策略与素材均按实例独立。' : '还没有下载实例，请启动服务并检查数据库连接。';
  const monitor=[['在线实例',`${workers.filter(item=>item.online).length} / ${workers.length}`],['执行中视频',workers.reduce((sum,item)=>sum+Number(item.running||0),0)],['总传输速度',bytes(workers.reduce((sum,item)=>sum+Number(item.online?item.speed||0:0),0))+'/s'],['异常实例',workers.filter(item=>!item.online||item.error).length]];
  $('#fleet-monitor-summary').innerHTML=monitor.map(([label,value])=>`<div class="monitor-metric"><span>${label}</span><strong>${escapeHTML(value)}</strong></div>`).join('');
  if (!state.fleetControlBusy && !state.fleetFormDirty && !document.activeElement?.matches('#instances-view input')) {
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
}
async function initializeFleet() {
  if (!fleetEnabled) return;
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
  await loadFleet();
  $('#fleet-selection').addEventListener('change', async event => {
    if (queueControlBusy) { event.target.value = state.instanceId || ''; return; }
    if (settingsDirty || providerDraft || downloadMethodBusy) {
      event.target.value = state.instanceId || '';
      toast('请先保存当前修改，再切换实例。', true);
      return;
    }
    state.instanceId = event.target.value;
    const instance = state.instanceId;
    const diskChip=$('#workspace-disk-status');
    diskChip.textContent='磁盘状态读取中'; diskChip.classList.remove('warning','unavailable'); diskChip.title='当前实例的磁盘状态正在更新';
    localStorage.setItem('frameflow-instance:' + location.origin, state.instanceId);
    resetPage('media'); state.media = [];
    try {
      await resetWorkbench();
      const data = await api('/api/config');
      if (state.instanceId !== instance) return;
      fillConfig(data.config); renderDownloadControl(); await refresh();
      if (typeof refreshDisk==='function') refreshDisk();
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
    for (const key of ['worker_count', 'yt5s_browser_count', 'min_free_gb']) data[key] = Number(data[key]);
    runButton($('button[type="submit"]', form), async () => {
      const result = await api(`/api/instances/${encodeURIComponent(form.dataset.instanceForm)}`, { method: 'PUT', body: data });
      state.fleetFormDirty = false;
      toast(result.message); await loadFleet();
    });
  });
  $('#fleet-instances').addEventListener('click', event => {
    const button = event.target.closest('[data-instance-action]');
    if (!button || state.fleetControlBusy) return;
    runButton(button, async () => {
      const label = button.textContent;
      state.fleetControlBusy = true;
      button.textContent = button.dataset.instanceAction === 'resume' ? '正在检查并恢复…' : '正在提交…';
      button.setAttribute('aria-busy', 'true');
      try {
        const result = await api(`/api/instances/${encodeURIComponent(button.dataset.instance)}`, { method: 'PUT', body: { action: button.dataset.instanceAction }, signal: AbortSignal.timeout(90000) });
        toast(button.dataset.instanceAction === 'resume' ? '实例已恢复领取。' : result.message);
      } catch (error) {
        if (error.name === 'TimeoutError') throw new Error('实例指令等待超过 90 秒，结果尚未确认，请刷新查看最新状态。');
        throw error;
      } finally {
        state.fleetControlBusy = false;
        button.textContent = label;
        button.setAttribute('aria-busy', 'false');
        loadFleet().catch(error => toast(error.message, true));
        refreshSelectedJob().catch(error => toast(error.message, true));
        refresh().catch(error => toast(error.message, true));
      }
    });
  });
  document.addEventListener('viewchange', event => { if (event.detail==='instances') loadFleet().catch(error => toast(error.message,true)); });
  setInterval(() => { if (!document.hidden) loadFleet().catch(() => {}); }, 2000);
}
