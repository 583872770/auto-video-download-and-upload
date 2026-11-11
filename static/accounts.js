'use strict';

// Global account views never inherit the workbench's selected executor.
const accountApi = '/api/account-library/youtube';
const accountLoginLabels = {unlinked:'未关联 Cookie',unchecked:'待检测',valid:'最近验证通过',expired:'检测已过期',not_logged_in:'需要重新登录',invalid:'Cookie 无效 / 过期',verification_required:'需要网页验证',network_error:'暂时无法确认（网络）',rate_limited:'暂时无法确认（限流）',checking:'检测进行中',unconfirmed:'暂时无法确认',missing:'凭据缺失'};
const accountDownloadLabels = {inactive:'未参与下载',ready:'正常',cooldown:'账号冷却中',recovery:'低频恢复',manual:'需人工处理',unavailable:'Cookie 不可用',unknown:'状态待更新'};
let accountTab = 'library', accountPage = 1, credentialPage = 1, accountTicket = 0, accountDetailTicket = 0;
let accountData = null, credentialData = null, accountNodes = [], accountDetail = null, accountDetailCookie = null;
let accountPoolBaseline = {}, accountPoolInstance = '', accountPreview = null;
let accountDetailDirty = false, accountImportDirty = false, accountDetailBusy = false;
let accountDialogEpoch = 0, accountSecretEpoch = 0;
let accountPreviewEpoch = 0, accountImportBusy = false, accountUploadBusy = false;
let accountLoginItem = null, accountLoginStatus = null, accountLoginEpoch = 0, accountLoginBusy = false, accountLoginPolling = false;
const accountAdmin = () => document.body.dataset.consoleRole === 'admin';
function accountHasDraft() { return accountDetailDirty || accountImportDirty || uploadDirty || accountDetailBusy || accountImportBusy || accountUploadBusy; }
const accountTime = stamp => stamp ? new Date(stamp * 1000).toLocaleString('zh-CN', {hour12:false}) : '尚无记录';
const accountName = id => accountNodes.find(n => n.id === id)?.name || instanceName(id);
function accountOptions(selected = '', unbound = true) {
  return (unbound ? '<option value="">先不绑定</option>' : '') + accountNodes.map(n => `<option value="${escapeHTML(n.id)}"${selected === n.id ? ' selected' : ''}>${escapeHTML(n.name)} · ${n.online ? '在线' : '离线'}</option>`).join('');
}
function updateAccountNodes(nodes) {
  accountNodes = nodes;
  for (const id of ['account-instance','credential-instance']) {
    const input = document.getElementById(id), value = input.value;
    input.innerHTML = '<option value="">全部实例</option><option value="unbound">未绑定</option>' + accountOptions('',false);
    input.value = value;
  }
}
function accountError(id, error) {
  const target = document.getElementById(id);
  target.hidden = !error; target.textContent = error?.message || error || '';
}
function renderAccountScope() {
  const pool = accountTab === 'pool';
  document.getElementById('account-scope').textContent = pool ? `正在配置：${instanceName(state.instanceId)}` : '全局账号资料 · 全部实例';
  document.getElementById('account-pool-target').textContent = `正在配置：${instanceName(state.instanceId)}。保存仅影响此实例，其他实例的账号池和节奏独立。`;
  document.getElementById('account-pool-form').inert = fleetEnabled && !state.instanceReady;
}
function selectAccountTab(tab) {
  if (!['library','cookies','pool'].includes(tab)) return;
  accountTab = tab;
  for (const node of document.querySelectorAll('[data-account-tab]')) { const active = node.dataset.accountTab === tab; node.setAttribute('aria-selected',String(active)); node.tabIndex = active ? 0 : -1; }
  for (const node of document.querySelectorAll('[data-account-pane]')) node.hidden = node.dataset.accountPane !== tab;
  renderAccountScope(); renderInstanceScope();
  if (tab === 'pool') {
    if (!fleetEnabled || state.instanceReady) loadCookieProfiles().catch(error => toast(error.message,true));
    renderYouTubePacingRuntime();
  } else loadAccounts().catch(error => toast(error.message,true));
}
async function loadAccounts() {
  if (accountTab === 'pool') return;
  const tab = accountTab, ticket = ++accountTicket;
  const cookies = tab === 'cookies', prefix = cookies ? 'credential' : 'account';
  const params = new URLSearchParams({page:String(cookies ? credentialPage : accountPage),size:'50',q:document.getElementById(prefix+'-search').value,instance:document.getElementById(prefix+'-instance').value});
  if (cookies) { params.set('linked', document.getElementById('credential-linked').value); params.set('auth',document.getElementById('credential-auth').value); }
  else { params.set('auth',document.getElementById('account-auth').value); params.set('download',document.getElementById('account-download').value); }
  const data = await api(accountApi + (cookies ? '/cookies' : '') + '?' + params);
  if (ticket !== accountTicket || tab !== accountTab) return;
  updateAccountNodes(data.instances);
  if (cookies) credentialData = data; else accountData = data;
  const authLabel = value => escapeHTML(accountLoginLabels[value] || '待检测');
  const action = (id, label='详情 / 编辑') => `<button type="button" class="text-button" data-account-open="${escapeHTML(id)}">${label}</button>`;
  document.getElementById(prefix+'-rows').innerHTML = data.items.map(item => cookies
    ? `<tr><td><strong>${escapeHTML(item.email || '未关联账号资料')}</strong><small>${escapeHTML(item.name)}${item.pooled ? ' · 账号池成员' : ''}</small></td><td>${escapeHTML(item.instance_name)}<small>${item.online ? '在线' : '离线'}</small></td><td>${authLabel(item.auth.state)}<small>${accountTime(item.auth.checked_at)}</small></td><td>${escapeHTML(accountDownloadLabels[item.download_state])}</td><td><button type="button" class="text-button" data-credential-open="${escapeHTML(item.instance_id)}:${escapeHTML(item.id)}">详情 / 关联</button>${accountAdmin() ? `<button type="button" class="text-button" data-credential-check="${escapeHTML(item.instance_id)}:${escapeHTML(item.id)}"${!item.online || ['cooldown'].includes(item.download_state) ? ' disabled' : ''}>检测登录</button>` : ''}</td></tr>`
    : `<tr><td><strong>${escapeHTML(item.email)}</strong><small>${escapeHTML(item.note)}</small></td><td>${escapeHTML(item.instance_name || '未绑定')}<small>${item.instance_id ? item.online ? '在线' : '离线' : ''}</small></td><td>${item.cookie_count} 份${item.cookie_count ? `<small>${item.valid_count} / ${item.cookie_count} 份近期验证通过</small>` : ''}</td><td>${authLabel(item.auth_state)}</td><td>${escapeHTML(accountDownloadLabels[item.download_state])}</td><td>${accountAdmin() ? `<button type="button" class="text-button" data-account-login="${escapeHTML(item.id)}"${item.instance_id && !item.online ? ' disabled title="绑定实例离线，恢复后可登录"' : ''}>登录</button>` : ''}${action(item.id,accountAdmin() ? '详情 / 编辑' : '查看详情')}</td></tr>`).join('');
  document.getElementById(prefix+'-empty').hidden = !!data.items.length;
  document.getElementById(prefix+'-page-info').textContent = `共 ${data.total} ${cookies ? '份 Cookie' : '个账号'} · 第 ${data.page} / ${Math.max(1,Math.ceil(data.total/data.size))} 页`;
  for (const node of document.querySelectorAll(cookies ? '[data-credential-page]' : '[data-account-page]')) node.disabled = Number(node.dataset[cookies?'credentialPage':'accountPage']) < 0 ? data.page <= 1 : data.page * data.size >= data.total;
}
async function accountCapabilities() {
  const data = await api(accountApi+'/capabilities');
  const note = document.getElementById('account-encryption-note');
  note.hidden = data.encryption_ready;
  note.textContent = data.message;
  for (const id of ['account-new','account-import']) document.getElementById(id).disabled = !accountAdmin() || !data.encryption_ready;
  document.getElementById('credential-upload').disabled = !accountAdmin();
}
function fillAccountPoolConfig(config) {
  const form = document.getElementById('account-pool-form');
  if (!form || state.accountPoolDirty && accountPoolInstance === (state.instanceId || '')) return;
  accountPoolInstance = state.instanceId || '';
  accountPoolBaseline = {};
  for (const input of form.querySelectorAll('[name]')) {
    input.value = config[input.name] ?? '';
    accountPoolBaseline[input.name] = input.value;
  }
  state.accountPoolDirty = false;
  if (!state.accountSourceDirty) document.querySelector('#youtube-source-form [name]').value = config.youtube_source_address || '';
  renderAccountScope();
}
function accountPoolDirty() {
  state.accountPoolDirty = true;
  document.getElementById('account-pool-save-state').textContent = `「${instanceName(state.instanceId)}」有未保存修改，请点击“保存账号池与节奏”。`;
  renderYouTubePacingRuntime();
}
async function saveAccountPool() {
  if (accountPoolInstance !== (state.instanceId || '')) throw new Error('实例已切换，请重新读取。');
  const form = document.getElementById('account-pool-form');
  const data = Object.fromEntries([...new FormData(form)].filter(([k,v]) => v !== accountPoolBaseline[k]));
  if (cookiePoolDraft) {
    if (cookiePoolDraft.instance !== state.instanceId) throw new Error('账号池草稿不属于当前实例。');
    if (cookiePoolDraft.enabled && cookiePoolDraft.ids.length < 2) throw new Error('账号池至少需要选择 2 份 Cookie。');
    if (cookiePoolDraft.enabled && selectedProviders().some(p => p !== 'yt_dlp')) throw new Error('账号池仅支持单独使用 yt-dlp，请先保存下载策略。');
    data.youtube_cookie_pool = cookiePoolDraft.enabled ? cookiePoolDraft.ids : [];
  }
  form.inert = true;
  try {
    await api('/api/config',{method:'PUT',body:data});
    const saved = (await api('/api/config',{cache:'no-store'})).config;
    if (unconfirmedSettings(data,saved).length) throw new Error('服务器读回值与提交值不同，请重新确认；草稿已保留。');
    state.accountPoolDirty = false; cookiePoolDraft = null;
    fillConfig(saved); await loadCookieProfiles();
    document.getElementById('account-pool-save-state').textContent = `「${instanceName(state.instanceId)}」账号池与节奏已保存，新任务使用新策略；无需再保存系统设置。`;
    toast('账号池与节奏已保存。');
  } finally { form.inert = false; }
}
function selectAccountDetailTab(tab) {
  const current = document.querySelector('[data-account-detail-tab][aria-selected=true]')?.dataset.accountDetailTab;
  if ((accountDetailDirty || accountDetailBusy) && current !== tab) { toast('请先保存当前页签的修改，或关闭详情并放弃修改。',true); return; }
  for (const node of document.querySelectorAll('[data-account-detail-tab]')) { const active = node.dataset.accountDetailTab === tab; node.setAttribute('aria-selected',String(active)); node.tabIndex = active ? 0 : -1; }
  for (const node of document.querySelectorAll('[data-account-detail-pane]')) node.hidden = node.dataset.accountDetailPane !== tab;
  if (tab === 'schedule') loadAccountEvents().catch(error => accountError('account-detail-error',error));
}
function clearAccountSecret() {
  accountSecretEpoch++;
  document.getElementById('account-secret-value').value = '';
  document.getElementById('account-secret-box').hidden = true;
  document.getElementById('account-open-url').removeAttribute('href');
}
function accountMemberHTML(p) {
  const session = p.session;
  return `<article class="account-cookie-row"><strong>${escapeHTML(p.name)}</strong><p>${escapeHTML(p.instance_name)} · ${p.online ? '在线' : '离线'} · ${p.pooled ? '在账号池中' : '未加入账号池'}</p><p>登录：${escapeHTML(accountLoginLabels[p.auth.state] || '待检测')} · ${accountTime(p.auth.checked_at)}${p.auth.connection ? ' · '+escapeHTML(p.auth.connection) : ''}</p>${p.auth.last_valid_at ? `<p class="micro-copy">上次验证通过：${accountTime(p.auth.last_valid_at)}</p>` : ''}<p>${escapeHTML(p.auth.message || '')}</p><p>下载：${escapeHTML(accountDownloadLabels[p.download_state])}${session?.interval ? ` · 启动间隔至少 ${Number(session.interval)} 秒` : ''}${session?.cooldown_until > Date.now()/1000 ? ` · 冷却到 ${accountTime(session.cooldown_until)}` : ''}</p>${accountAdmin() ? `<button type="button" class="button button-white" data-credential-check="${escapeHTML(p.instance_id)}:${escapeHTML(p.id)}"${!p.online || p.download_state === 'cooldown' ? ' disabled' : ''}>检测登录</button>` : ''}<button type="button" class="button button-white" data-pool-owner="${escapeHTML(p.instance_id)}">账号池与节奏 ↗</button></article>`;
}
async function openAccountDetails(identity, {cookie=null, tab='profile'} = {}) {
  if (accountDetailBusy) return;
  if (accountDetailDirty && !await confirmDialog('放弃当前未保存的账号修改？')) return;
  const ticket = ++accountDetailTicket;
  const item = identity ? (await api(accountApi+'/'+encodeURIComponent(identity))).item : null;
  if (ticket !== accountDetailTicket) return;
  const creating = !item && !cookie;
  accountDetail = item; accountDetailCookie = cookie; accountDetailDirty = false; clearAccountSecret();
  accountError('account-detail-error','');
  const form = document.getElementById('account-edit-form'); form.reset(); form.hidden = !!cookie && !item;
  for (const input of form.querySelectorAll('[name]')) input.disabled = !accountAdmin();
  document.getElementById('account-detail-title').textContent = item?.email || cookie?.name || '新增账号';
  form.elements.email.value = item?.email || '';
  form.elements.note.value = item?.note || '';
  form.elements.password.required = creating;
  form.elements.instance_id.innerHTML = accountOptions(item?.instance_id || '');
  form.elements.instance_id.disabled = !!item?.cookie_count || !accountAdmin();
  document.getElementById('account-secret-actions').hidden = !item || !accountAdmin();
  document.getElementById('account-delete').hidden = !item || !accountAdmin();
  form.querySelector('[type=submit]').disabled = !accountAdmin();
  for (const node of document.querySelectorAll('[data-account-detail-tab]')) node.hidden = creating && node.dataset.accountDetailTab !== 'profile';
  const members = item?.cookies || (cookie ? [cookie] : []);
  document.getElementById('account-associated-cookies').innerHTML = members.map(accountMemberHTML).join('') || '<p>未关联 Cookie。保存绑定实例后，可上传或关联已有 Cookie。</p>';
  document.getElementById('account-link-form').hidden = !accountAdmin() || !item?.instance_id;
  document.getElementById('account-browser-login').disabled = !accountAdmin() || !item?.instance_id || !item?.online;
  document.getElementById('account-link-choices').replaceChildren();
  document.getElementById('account-link-confirmed').checked = false;
  const dialog = document.getElementById('account-detail-dialog');
  if (!dialog.open) dialog.showModal();
  selectAccountDetailTab(creating ? 'profile' : tab);
  if (item?.instance_id) {
    const values = await api(accountApi+'/cookies?size=100&instance='+encodeURIComponent(item.instance_id));
    if (ticket !== accountDetailTicket || !dialog.open) return;
    document.getElementById('account-link-choices').innerHTML = values.items.map(p => `<label><input type="checkbox" value="${escapeHTML(p.id)}"${p.account_id === item.id ? ' checked' : ''}${p.account_id && p.account_id !== item.id ? ' disabled' : ''}>${escapeHTML(p.name)}${p.account_id && p.account_id !== item.id ? '（已关联其他账号）' : ''}</label>`).join('') || '<p>此实例尚无 Cookie，请先上传。</p>';
  } else if (cookie && accountAdmin()) {
    const choices = await api(accountApi+'/choices');
    if (ticket !== accountDetailTicket || !dialog.open) return;
    document.getElementById('account-associated-cookies').insertAdjacentHTML('beforeend', `<label>选择要关联的账号<select id="account-unlinked-choice"><option value="">请选择账号</option>${choices.items.filter(a => !a.instance_id || a.instance_id === cookie.instance_id).map(a => `<option value="${escapeHTML(a.id)}">${escapeHTML(a.email)}</option>`).join('')}</select></label><button type="button" class="button button-dark" id="account-unlinked-link">选择账号并确认关联</button><p class="micro-copy">没有账号资料时先新增；现有 Cookie 可继续下载。</p>`);
  }
}
async function openAccountCookie(scope, profile, tab='cookies') {
  const data = await api(accountApi+'/cookies?profile='+encodeURIComponent(profile)+'&instance='+encodeURIComponent(scope || ''));
  const p = data.items.find(p => p.id === profile);
  if (!p) throw new Error('此 Cookie 已删除或不在该实例。');
  updateAccountNodes(data.instances);
  return openAccountDetails(p.account_id, {cookie:p,tab});
}
async function loadAccountEvents() {
  const epoch = ++accountDialogEpoch;
  const members = accountDetail?.cookies || (accountDetailCookie ? [accountDetailCookie] : []);
  const schedule = document.getElementById('account-schedule'), events = document.getElementById('account-events');
  schedule.innerHTML = members.map(p => {
    const session = p.session;
    const progress = typeof cookieRecoveryProgressHTML === 'function' && session ? cookieRecoveryProgressHTML(session,{time:Date.now()/1000},p.online) : '';
    return `<h3>${escapeHTML(p.name)}</h3>${progress || accountMemberHTML(p)}`;
  }).join('') || '<p>尚未关联 Cookie，没有调度记录。</p>';
  events.textContent = members.length ? '正在读取最近记录…' : '';
  const values = await Promise.all(members.map(p => api(`${accountApi}/cookies/${encodeURIComponent(p.instance_id)}/${encodeURIComponent(p.id)}/timeline`)));
  if (epoch !== accountDialogEpoch || !document.getElementById('account-detail-dialog').open) return;
  const rows = values.flatMap(v => v.events).sort((a,b) => b.occurred_at-a.occurred_at).slice(0,100);
  const filter = document.getElementById('account-event-type').value;
  events.innerHTML = '<h3>最近异常与间隔调整</h3>' + (rows.filter(e => !filter || e.event_type === filter).map(e => `<article class="account-event"><small>${accountTime(e.occurred_at)} · ${escapeHTML(e.profile_name || '')}</small><p>${escapeHTML(e.message || e.kind || '已记录')}${e.previous_interval != null && e.new_interval != null ? ` · ${Number(e.previous_interval)} → ${Number(e.new_interval)} 秒` : ''}</p></article>`).join('') || '<p>暂无符合条件的记录。</p>');
}
async function manageAccountInstance(target, tab='pool') {
  if (target && target !== state.instanceId) {
    if (instanceHasDraft() || accountDetailDirty || state.accountPoolDirty) throw new Error('请先保存当前实例和账号的未提交修改，再切换。');
    if (state.instanceWrites || state.instanceLoading) throw new Error('正在执行实例操作，请稍后切换。');
    state.instanceId = target;
    localStorage.setItem('frameflow-instance:'+location.origin,target);
    await loadSelectedInstance();
  }
  setView('accounts'); selectAccountTab(tab);
}
function guardAccountDialog(dialog, dirty, clear, busy=() => accountDetailBusy) {
  let confirming = false;
  const before = event => {
    if (busy()) { event.preventDefault(); return; }
    if (!dirty()) return;
    event.preventDefault();
    if (confirming) return;
    confirming = true;
    confirmDialog('存在未保存输入，确认放弃并关闭？').then(ok => { if (ok) { clear(); dialog.close(); } }).finally(() => { confirming = false; });
  };
  dialog.addEventListener('dialogbeforeclose',before);
  dialog.addEventListener('cancel',event => { event.preventDefault(); closeWorkspaceDialog(dialog); });
}

document.addEventListener('viewchange', event => {
  if (event.detail === 'accounts') { renderAccountScope(); selectAccountTab(accountTab); accountCapabilities().catch(error => toast(error.message,true)); }
});
document.addEventListener('keydown',event => {
  const tab = event.target.closest('[data-account-tab],[data-account-detail-tab]');
  if (!tab || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
  event.preventDefault();
  const detail = tab.hasAttribute('data-account-detail-tab');
  const tabs = [...tab.parentElement.querySelectorAll('button')].filter(n => !n.hidden);
  const index = tabs.indexOf(tab);
  const next = tabs[event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length-1 : (index + (event.key === 'ArrowRight' ? 1 : -1)+tabs.length)%tabs.length];
  if (detail) selectAccountDetailTab(next.dataset.accountDetailTab); else selectAccountTab(next.dataset.accountTab);
  if (next.getAttribute('aria-selected') === 'true') next.focus();
});
document.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.accountTab) selectAccountTab(button.dataset.accountTab);
  if (button.dataset.accountDetailTab) selectAccountDetailTab(button.dataset.accountDetailTab);
  if (button.hasAttribute('data-account-refresh')) loadAccounts().catch(error => toast(error.message,true));
  if (button.dataset.accountOpen) openAccountDetails(button.dataset.accountOpen).catch(error => toast(error.message,true));
  if (button.dataset.accountLogin) runButton(button,() => openAccountLogin(button.dataset.accountLogin));
  if (button.dataset.credentialOpen) { const [scope,id] = button.dataset.credentialOpen.split(':'); openAccountCookie(scope,id).catch(error => toast(error.message,true)); }
  if (button.dataset.credentialCheck) runButton(button,async () => {
    const [scope,id] = button.dataset.credentialCheck.split(':');
    const value = await api(accountApi+'/cookie-action',{method:'POST',body:{action:'check',instance_id:scope,profile_id:id}});
    toast(value.message); await loadAccounts();
    if (document.getElementById('account-detail-dialog').open) await openAccountCookie(scope,id);
  });
  if (button.dataset.poolOwner) runButton(button,async () => {
    if (accountDetailDirty || accountDetailBusy) throw new Error('请先保存账号修改，再进入账号池。');
    await manageAccountInstance(button.dataset.poolOwner);
    document.getElementById('account-detail-dialog').close();
  });
  if (button.dataset.manageAccountInstance) runButton(button,async () => {
    if (!accountNodes.length) updateAccountNodes((await api(accountApi+'?size=1')).instances);
    document.getElementById('account-instance').value = button.dataset.manageAccountInstance;
    accountPage = 1; setView('accounts'); selectAccountTab('library');
  });
  if (button.hasAttribute('data-account-page')) { accountPage += Number(button.dataset.accountPage); loadAccounts().catch(error => toast(error.message,true)); }
  if (button.hasAttribute('data-credential-page')) { credentialPage += Number(button.dataset.credentialPage); loadAccounts().catch(error => toast(error.message,true)); }
  if (button.dataset.accountSecret && accountDetail) runButton(button,async () => {
    const identity = accountDetail.id, ticket = accountDetailTicket;
    const secretEpoch = ++accountSecretEpoch;
    const value = await api(accountApi+'/'+identity+'/secrets',{method:'POST',body:{field:button.dataset.accountSecret}});
    if (!document.getElementById('account-detail-dialog').open || ticket !== accountDetailTicket || secretEpoch !== accountSecretEpoch) return;
    document.getElementById('account-secret-box').hidden = false;
    document.getElementById('account-secret-value').value = value.value;
    const url = document.getElementById('account-open-url');
    url.hidden = button.dataset.accountSecret !== 'code_url' || !/^https?:\/\//.test(value.value);
    if (!url.hidden) url.href = value.value;
  });
  if (button.id === 'account-unlinked-link') runButton(button,async () => {
    const id = document.getElementById('account-unlinked-choice').value;
    if (!id) throw new Error('请选择账号。');
    await openAccountDetails(id,{tab:'cookies'});
    toast('请勾选 Cookie，并确认属于此账号后保存关联。');
  });
});
for (const [id,labels] of [['account-auth',accountLoginLabels],['credential-auth',accountLoginLabels],['account-download',accountDownloadLabels]]) document.getElementById(id).innerHTML = `<option value="">${id === 'account-download' ? '全部下载状态' : '全部登录状态'}</option>` + Object.entries(labels).filter(([k]) => id !== 'credential-auth' || k !== 'unlinked').map(([k,v]) => `<option value="${k}">${v}</option>`).join('');
let accountSearchTimer;
for (const id of ['account-search','credential-search','account-instance','credential-instance','account-auth','credential-auth','account-download','credential-linked']) document.getElementById(id).addEventListener(id.includes('search') ? 'input' : 'change',() => {
  accountPage = credentialPage = 1; clearTimeout(accountSearchTimer); accountSearchTimer = setTimeout(() => loadAccounts().catch(error => toast(error.message,true)),250);
});
document.getElementById('account-new').addEventListener('click',() => openAccountDetails(null).catch(error => toast(error.message,true)));
document.getElementById('account-pool-form').addEventListener('input',accountPoolDirty);
document.getElementById('account-pool-form').addEventListener('change',accountPoolDirty);
document.getElementById('account-pool-form').addEventListener('submit',event => { event.preventDefault(); runButton(event.submitter,saveAccountPool); });
document.getElementById('account-pool-form').addEventListener('invalid',event => {
  for (let node=event.target.parentElement; node && node !== event.currentTarget; node=node.parentElement) {
    if (node.tagName === 'DETAILS') node.open = true;
  }
},true);
document.getElementById('account-edit-form').addEventListener('input',() => { accountDetailDirty = true; });
document.getElementById('account-link-form').addEventListener('change',() => { accountDetailDirty = true; });
document.getElementById('account-edit-form').addEventListener('submit',event => {
  event.preventDefault(); runButton(event.submitter,async () => {
    if (accountDetailBusy) return;
    const f = event.target, data = {email:f.elements.email.value,password:f.elements.password.value,note:f.elements.note.value,instance_id:f.elements.instance_id.value};
    if (!accountDetail || f.elements.change_extra.checked) data.extra = f.elements.extra.value;
    if (!accountDetail || f.elements.change_code_url.checked) data.code_url = f.elements.code_url.value;
    if (accountDetail) data.version = accountDetail.version;
    accountDetailBusy = true;
    try {
      const result = await api(accountApi+(accountDetail?'/'+accountDetail.id:''),{method:accountDetail?'PATCH':'POST',body:data});
      const identity = accountDetail?.id || result.id;
      accountDetailDirty = false; accountDetailBusy = false; toast(result.message); await openAccountDetails(identity); await loadAccounts();
    } catch(error) { accountError('account-detail-error',error); throw error; }
    finally { accountDetailBusy = false; }
  });
});
document.getElementById('account-link-form').addEventListener('submit',event => {
  event.preventDefault(); runButton(event.submitter,async () => {
    if (accountDetailBusy) return;
    const item = accountDetail;
    const ids = [...document.querySelectorAll('#account-link-choices input:checked')].map(n => n.value);
    accountDetailBusy = true;
    try {
      await api(accountApi+'/'+item.id+'/cookies',{method:'PUT',body:{version:item.version,instance_id:item.instance_id,confirmed:document.getElementById('account-link-confirmed').checked,profile_ids:ids}});
      accountDetailDirty = false; accountDetailBusy = false; await openAccountDetails(item.id,{tab:'cookies'}); await loadAccounts(); toast('关联已保存，未改变账号池。');
    } catch(error) { accountError('account-detail-error',error); throw error; }
    finally { accountDetailBusy = false; }
  });
});
document.getElementById('account-delete').addEventListener('click',event => runButton(event.currentTarget,async () => {
  const item = accountDetail;
  if (!await confirmDialog(`删除 ${item.email} 的资料及已保存密钥？关联 ${item.cookie_count} 份 Cookie，Cookie 本身和实例归属会保留；仍在使用时会拒绝删除。`,{title:'删除账号资料',confirmText:'确认删除'})) return;
  if (accountDetailBusy || accountDetail?.id !== item.id) return;
  accountDetailBusy = true;
  try {
    await api(accountApi+'/'+item.id,{method:'DELETE',body:{version:item.version}});
    accountDetailDirty = false; document.getElementById('account-detail-dialog').close(); await loadAccounts(); toast('账号资料已删除。');
  } finally { accountDetailBusy = false; }
}));
document.getElementById('account-hide-secret').addEventListener('click',clearAccountSecret);
document.getElementById('account-event-type').addEventListener('change',() => loadAccountEvents().catch(error => accountError('account-detail-error',error)));
document.getElementById('account-events-refresh').addEventListener('click',event => runButton(event.currentTarget,() => accountDetail
  ? openAccountDetails(accountDetail.id,{tab:'schedule'})
  : openAccountCookie(accountDetailCookie.instance_id,accountDetailCookie.id,'schedule')));
document.getElementById('account-copy-secret').addEventListener('click',async () => { try { await navigator.clipboard.writeText(document.getElementById('account-secret-value').value); toast('已复制。'); } catch { toast('请手动选择并复制。',true); } });
document.getElementById('account-detail-dialog').addEventListener('close',() => {
  accountDetailTicket++; accountDialogEpoch++; accountDetailDirty = false; clearAccountSecret();
  document.getElementById('account-edit-form').reset(); accountDetail = accountDetailCookie = null;
});
guardAccountDialog(document.getElementById('account-detail-dialog'),() => accountDetailDirty,() => { accountDetailDirty = false; });
document.getElementById('account-browser-login').addEventListener('click',event => runButton(event.currentTarget,async () => {
  if (accountDetailDirty || accountDetailBusy) throw new Error('请先保存账号修改后登录。');
  const identity = accountDetail.id;
  document.getElementById('account-detail-dialog').close();
  await openAccountLogin(identity);
}));

function renderAccountLogin() {
  const status = accountLoginStatus;
  const target = document.getElementById('account-login-instance');
  target.disabled = accountLoginBusy || !!accountLoginItem?.instance_id || !!status?.active;
  document.getElementById('account-login-automatic').disabled = accountLoginBusy || !!status?.active;
  document.getElementById('account-login-start').disabled = accountLoginBusy || !status || !!status.active || !target.value;
  document.getElementById('account-login-start').textContent = status?.active ? '登录进行中…' : status?.state === 'succeeded' ? '重新登录' : '开始登录';
  document.getElementById('account-login-cancel').hidden = !status?.can_cancel;
  document.getElementById('account-login-cancel').disabled = accountLoginBusy;
  document.getElementById('account-login-status').textContent = status?.message || '正在读取此账号的登录状态…';
  document.getElementById('account-login-target').textContent = `浏览器位置：${accountName(target.value) || '请选择实例'}${status?.connection ? ' · '+status.connection : ''}`;
}
async function openAccountLogin(identity) {
  if (!accountAdmin()) return;
  if (accountLoginBusy) throw new Error('正在提交登录操作，请稍后再打开其他账号。');
  const epoch = ++accountLoginEpoch;
  const item = (await api(accountApi+'/'+identity)).item;
  if (epoch !== accountLoginEpoch) return;
  if (!accountNodes.length) updateAccountNodes((await api(accountApi+'?size=1')).instances);
  accountLoginItem = item; accountLoginStatus = null;
  document.getElementById('account-login-title').textContent = '登录：'+item.email;
  const target = document.getElementById('account-login-instance');
  target.innerHTML = '<option value="">请选择绑定实例</option>'+accountNodes.map(node => `<option value="${escapeHTML(node.id)}"${node.id === item.instance_id ? ' selected' : ''}${!node.online ? ' disabled' : ''}>${escapeHTML(node.name)} · ${node.online ? '在线' : '离线'}</option>`).join('');
  document.getElementById('account-login-automatic').checked = true;
  accountError('account-login-error','');
  document.getElementById('account-login-dialog').showModal();
  renderAccountLogin();
  if (item.instance_id) await refreshAccountLogin();
  else { accountLoginStatus = {state:'idle',active:false,message:'选择绑定实例后开始登录；后续 Cookie 只在此实例保存。'}; renderAccountLogin(); }
}
async function refreshAccountLogin() {
  const dialog = document.getElementById('account-login-dialog');
  if (!dialog.open || !accountLoginItem?.instance_id || accountLoginPolling || accountLoginBusy) return;
  const epoch = accountLoginEpoch, identity = accountLoginItem.id;
  accountLoginPolling = true;
  try {
    const result = await api(accountApi+'/'+identity+'/login');
    if (epoch !== accountLoginEpoch || !dialog.open) return;
    const changed = result.state === 'succeeded' && (accountLoginStatus?.id !== result.id || accountLoginStatus?.state !== 'succeeded');
    accountLoginStatus = result; accountError('account-login-error',''); renderAccountLogin();
    if (changed) {
      const latest = (await api(accountApi+'/'+identity)).item;
      if (epoch !== accountLoginEpoch || !dialog.open) return;
      accountLoginItem = latest; await loadAccounts();
    }
  } catch(error) { if (epoch === accountLoginEpoch && dialog.open) accountError('account-login-error',error); }
  finally { accountLoginPolling = false; }
}
document.getElementById('account-login-instance').addEventListener('change',renderAccountLogin);
async function accountBrowserLoginAction(cancel) {
  if (accountLoginBusy || !accountLoginItem) return;
  const item = accountLoginItem, epoch = accountLoginEpoch;
  accountLoginBusy = true; accountError('account-login-error',''); renderAccountLogin();
  try {
    const data = cancel ? {session_id:accountLoginStatus.id} : {version:item.version,
      instance_id:document.getElementById('account-login-instance').value,
      automatic:document.getElementById('account-login-automatic').checked};
    const result = await api(accountApi+'/'+item.id+'/login'+(cancel?'/cancel':''),{method:'POST',body:data});
    if (epoch !== accountLoginEpoch) return;
    accountLoginStatus = result;
    if (result.account) accountLoginItem = {...item,instance_id:result.account.instance_id,version:result.account.version};
    await loadAccounts();
  } catch(error) {
    if (epoch === accountLoginEpoch) {
      accountError('account-login-error',error);
      // Reconcile a response timeout before offering another start.
      try {
        const latest = (await api(accountApi+'/'+item.id)).item;
        const status = latest.instance_id ? await api(accountApi+'/'+item.id+'/login') : {state:'idle',active:false,message:'请选择绑定实例后开始登录。'};
        if (epoch === accountLoginEpoch) { accountLoginItem = latest; accountLoginStatus = status; }
      } catch { if (epoch === accountLoginEpoch) accountLoginStatus = null; }
    }
  } finally { accountLoginBusy = false; if (epoch === accountLoginEpoch) renderAccountLogin(); }
}
document.getElementById('account-login-start').addEventListener('click',() => accountBrowserLoginAction(false));
document.getElementById('account-login-cancel').addEventListener('click',() => accountBrowserLoginAction(true));
document.getElementById('account-login-dialog').addEventListener('close',() => { accountLoginEpoch++; accountLoginItem = accountLoginStatus = null; });
setInterval(() => { if (!document.hidden) refreshAccountLogin(); },2000);
document.getElementById('youtube-source-form').addEventListener('input',() => { state.accountSourceDirty = true; });
document.getElementById('youtube-source-form').addEventListener('submit',event => {
  event.preventDefault(); runButton(event.submitter,async () => {
    const source = event.target.querySelector('[name]').value;
    await api('/api/config',{method:'PUT',body:{youtube_source_address:source}});
    const saved = (await api('/api/config')).config;
    if (unconfirmedSettings({youtube_source_address:source},saved).length) throw new Error('读回源地址与提交值不同，请重新确认；输入已保留。');
    state.accountSourceDirty = false; fillConfig(saved); toast('直连源地址已保存。');
  });
});

// Import text only lives in the open dialog. A signed preview fences changes.
const importDialog = document.getElementById('account-import-dialog');
document.getElementById('account-import').addEventListener('click',() => {
  document.getElementById('account-import-form').reset();
  document.getElementById('account-import-instance').innerHTML = accountOptions();
  document.getElementById('account-import-preview').replaceChildren(); accountPreview = null; accountImportDirty = false;
  document.getElementById('account-import-confirm').disabled = true; accountError('account-import-error',''); importDialog.showModal();
});
const invalidatePreview = () => { accountPreviewEpoch++; accountPreview = null; accountImportDirty = true; document.getElementById('account-import-confirm').disabled = true; document.getElementById('account-import-preview').replaceChildren(); };
document.getElementById('account-import-form').addEventListener('input',invalidatePreview);
document.getElementById('account-import-form').addEventListener('change',event => { if (event.target.id !== 'account-import-file') invalidatePreview(); });
document.getElementById('account-import-file').addEventListener('change',async event => {
  try {
    const file = event.target.files[0]; if (!file) return;
    invalidatePreview();
    const epoch = accountPreviewEpoch;
    if (file.size > 1024*1024) throw new Error('文本最大 1 MB。');
    const value = new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());
    if (!importDialog.open || epoch !== accountPreviewEpoch) return;
    document.getElementById('account-import-text').value = value;
    invalidatePreview();
  } catch(error) { accountError('account-import-error',error); }
});
document.getElementById('account-import-form').addEventListener('submit',event => {
  event.preventDefault(); runButton(event.submitter,async () => {
    const text = document.getElementById('account-import-text').value;
    const epoch = ++accountPreviewEpoch;
    const result = await api(accountApi+'/import/preview',{method:'POST',body:{text,mode:document.getElementById('account-import-mode').value,instance_id:document.getElementById('account-import-instance').value}});
    if (!importDialog.open || epoch !== accountPreviewEpoch) return;
    accountPreview = {token:result.token,text};
    const labels = {create:'新增',update:'更新资料',skip:'跳过',error:'格式错误'};
    document.getElementById('account-import-preview').innerHTML = `<p>共 ${result.items.length} 行 · 错误 ${result.errors} 行</p><table class="account-table"><thead><tr><th>行号</th><th>账号</th><th>密码</th><th>结果</th></tr></thead><tbody>${result.items.map(r => `<tr><td>${r.line}</td><td>${escapeHTML(r.email || '—')}</td><td>${r.password ? '••••••' : '—'}</td><td>${escapeHTML(r.error || labels[r.action])}</td></tr>`).join('')}</tbody></table>`;
    document.getElementById('account-import-confirm').disabled = !!result.errors;
  });
});
document.getElementById('account-import-confirm').addEventListener('click',event => runButton(event.currentTarget,async () => {
  if (!accountPreview) throw new Error('请先预览。');
  accountImportBusy = true; document.getElementById('account-import-form').inert = true;
  try {
    const result = await api(accountApi+'/import',{method:'POST',body:accountPreview});
    accountImportDirty = false; importDialog.close(); await loadAccounts();
    toast(`新增 ${result.created}，更新 ${result.updated}，跳过 ${result.skipped}。`);
  } finally { accountImportBusy = false; document.getElementById('account-import-form').inert = false; }
}));
guardAccountDialog(importDialog,() => accountImportDirty,() => { accountImportDirty = false; },() => accountImportBusy);
importDialog.addEventListener('close',() => { accountPreviewEpoch++; document.getElementById('account-import-form').reset(); accountPreview = null; accountImportDirty = false; document.getElementById('account-import-preview').replaceChildren(); });

const uploadDialog = document.getElementById('account-upload-dialog');
let uploadAccounts = [], uploadDirty = false;
document.getElementById('credential-upload').addEventListener('click',event => runButton(event.currentTarget,async () => {
  uploadAccounts = (await api(accountApi+'/choices')).items.filter(a => a.instance_id);
  if (!uploadAccounts.length) throw new Error('请先新增账号并绑定下载实例，再上传 Cookie。');
  document.getElementById('account-upload-form').reset(); uploadDirty = false;
  document.getElementById('account-upload-account').innerHTML = uploadAccounts.map(a => `<option value="${escapeHTML(a.id)}">${escapeHTML(a.email)} · ${escapeHTML(accountName(a.instance_id))}</option>`).join('');
  accountError('account-upload-error',''); uploadDialog.showModal();
}));
document.getElementById('account-upload-form').addEventListener('input',() => { uploadDirty = true; });
document.getElementById('account-upload-form').addEventListener('submit',event => {
  event.preventDefault(); runButton(event.submitter,async () => {
    const item = uploadAccounts.find(a => a.id === document.getElementById('account-upload-account').value);
    const data = new FormData(); data.append('cookie',document.getElementById('account-upload-file').files[0]);
    data.append('version',item.version); data.append('name',document.getElementById('account-upload-name').value || item.email);
    data.append('confirmed',String(document.getElementById('account-upload-confirmed').checked));
    accountUploadBusy = true; event.target.inert = true;
    try { const result = await api(accountApi+'/'+item.id+'/upload',{method:'POST',body:data}); uploadDirty = false; uploadDialog.close(); await loadAccounts(); toast(result.message); }
    catch(error) { accountError('account-upload-error',error); throw error; }
    finally { accountUploadBusy = false; event.target.inert = false; }
  });
});
guardAccountDialog(uploadDialog,() => uploadDirty,() => { uploadDirty = false; },() => accountUploadBusy);
uploadDialog.addEventListener('close',() => { document.getElementById('account-upload-form').reset(); uploadAccounts = []; uploadDirty = false; });
setInterval(() => { if (!document.hidden && location.hash === '#accounts' && accountTab !== 'pool') loadAccounts().catch(() => {}); },30000);
window.addEventListener('beforeunload',event => { if (accountDetailDirty || accountImportDirty || uploadDirty || state.accountPoolDirty) { event.preventDefault(); event.returnValue = ''; } });
if (state.config) fillAccountPoolConfig(state.config);
if (location.hash === '#accounts') { selectAccountTab('library'); accountCapabilities().catch(error => toast(error.message,true)); }
