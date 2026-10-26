'use strict';

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
async function loadFleet() {
  if (!['manager', 'hybrid'].includes(serviceRole)) return;
  const data = await api('/api/instances');
  state.fleet = data;
  const workers = data.items.filter(item => ['worker', 'hybrid'].includes(item.role));
  const previous = state.instanceId;
  if (!workers.some(item => item.id === state.instanceId)) {
    state.instanceId = workers.find(item => item.id === data.instance_id)?.id || workers.find(item => item.online)?.id || workers[0]?.id || '';
  }
  if (state.instanceId && state.instanceId !== previous && state.config && !settingsDirty) {
    resetPage('media'); state.media = []; state.mediaTotal = 0; state.mediaDirectory = '';
    fillConfig((await api('/api/config')).config);
  }
  const selector = $('#fleet-selection');
  const options = workers.map(item => `<option value="${escapeHTML(item.id)}">${escapeHTML(item.name)}${item.id === data.instance_id ? '（本机）' : ''} · ${item.online ? item.paused ? '已暂停领取' : '在线' : '离线'}</option>`).join('');
  if (selector.innerHTML !== options) selector.innerHTML = options;
  selector.value = state.instanceId || '';
  $('#fleet-selection-note').textContent = workers.length ? '策略与开始/暂停作用于整个集群；目录、Cookie、代理和素材库作用于所选实例。' : '还没有下载实例，请部署管理与下载一体或纯下载实例。';
  if (!state.fleetControlBusy && !state.fleetFormDirty && !document.activeElement?.matches('#instances-view input')) {
    $('#fleet-global-limit').value = data.global_limit;
    $('#fleet-instances').innerHTML = workers.length ? workers.map(item => `<article class="fleet-instance panel"><form data-instance-form="${escapeHTML(item.id)}"><div class="panel-heading"><div><h2>${escapeHTML(item.name)}${item.id === data.instance_id ? '（本机）' : ''}</h2><p class="micro-copy">${escapeHTML(item.id)} · ${escapeHTML(item.host)}</p></div><span class="count-pill">${item.online ? '在线' : '离线'}${item.paused ? ' · 已暂停领取' : ''}</span></div><div class="fleet-metrics"><span>执行中 <b>${item.running}</b></span><span>传输中 <b>${item.transferring}</b></span><span>总速度 <b>${bytes(item.speed)}/s</b></span></div>${instanceStatistics(item)}<p class="micro-copy">${escapeHTML(item.directory)}</p>${item.error ? `<p class="task-error">${escapeHTML(item.error)}</p>` : ''}<div class="fleet-fields"><label>显示名称<input name="name" value="${escapeHTML(item.name)}" required maxlength="100"></label><label>下载并发<input name="worker_count" type="number" min="1" max="16" value="${item.worker_count}" required></label><label>浏览器并发<input name="yt5s_browser_count" type="number" min="1" max="16" value="${item.yt5s_browser_count}" required></label><label>磁盘预留 GB<input name="min_free_gb" type="number" min="0" max="10000" value="${item.min_free_gb}" required></label></div><div class="media-library-actions"><button type="submit" class="button button-white">保存实例配置</button><button type="button" class="button button-white" data-instance-action="resume" data-instance="${escapeHTML(item.id)}">恢复领取</button><button type="button" class="button button-white" data-instance-action="drain" data-instance="${escapeHTML(item.id)}">完成当前后暂停</button><button type="button" class="button button-danger" data-instance-action="stop" data-instance="${escapeHTML(item.id)}">立即停止</button></div><p class="micro-copy">降低并发时让当前任务先完成；离线实例的停止指令会在恢复连接后处理。</p></form></article>`).join('') : emptyState('还没有下载实例', '部署管理与下载一体或纯下载实例，连接同一个 MySQL 后会自动注册。');
  }
}
async function initializeFleet() {
  if (!['manager', 'hybrid'].includes(serviceRole)) return;
  $('#fleet-nav').hidden = false;
  $('#fleet-selection-bar').hidden = false;
  state.instanceId = localStorage.getItem('frameflow-instance:' + location.origin) || '';
  $('#instances-view').addEventListener('input', () => { state.fleetFormDirty = true; });
  await loadFleet();
  $('#fleet-selection').addEventListener('change', async event => {
    if (settingsDirty || providerDraft && downloadMethodBusy) {
      event.target.value = state.instanceId || '';
      toast('请先保存当前修改，再切换实例。', true);
      return;
    }
    state.instanceId = event.target.value;
    localStorage.setItem('frameflow-instance:' + location.origin, state.instanceId);
    resetPage('media'); state.media = [];
    try { fillConfig((await api('/api/config')).config); await refresh(); }
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
        refresh().catch(error => toast(error.message, true));
      }
    });
  });
  setInterval(() => { if (!document.hidden) loadFleet().catch(() => {}); }, 5000);
}
