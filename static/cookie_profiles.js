'use strict';

// Live status is shared with the account list; membership drafts survive paging.
function updateCookiePoolLive(data) {
  const live = $('#youtube-cookie-pool-live');
  if (live) live.innerHTML = cookieAccountNoticesHTML(data.pool?.capacity_summary, Date.now()/1000);
  $('#youtube-cookie-pool-recover').hidden = !data.pool?.halted || !accountAdmin();
}
let cookiePoolPolling = false;
setInterval(async () => {
  if (location.hash !== '#accounts' || document.hidden || cookiePoolPolling || fleetEnabled && !state.instanceReady) return;
  cookiePoolPolling = true;
  try { await loadCookieProfiles(); }
  catch (_) { $('#youtube-cookie-pool-status').textContent = '账号池状态刷新失败，请点击刷新重试。'; }
  finally { cookiePoolPolling = false; }
}, 10000);
async function loadCookieProfiles({refreshAccounts=true} = {}) {
  const instance = state.instanceId || '';
  const data = await api('/api/youtube/cookies');
  if (instance !== (state.instanceId || '')) return;
  if (state.config) {
    state.config.youtube_cookie = data.cookie_path;
    state.config.youtube_cookie_pool = data.items.filter(item=>item.pooled).map(item=>item.id);
    renderDownloadMethod();
  }
  if (cookiePoolDraft && cookiePoolDraft.instance !== instance) cookiePoolDraft = null;
  if (cookiePoolDraft) cookiePoolDraft.ids = cookiePoolDraft.ids.filter(id=>data.items.some(item=>item.id===id));
  $('#youtube-cookie-pool-enabled').checked = cookiePoolDraft ? cookiePoolDraft.enabled : !!data.pool;
  renderCookiePoolSaveState(); updateCookiePoolLive(data);
  if (refreshAccounts && location.hash === '#accounts' && typeof loadAccounts === 'function') await loadAccounts();
}
function renderCookiePoolSaveState() {
  const status = $('#youtube-cookie-pool-status');
  if (!status) return;
  const count = cookiePoolDraft ? cookiePoolDraft.ids.length : state.config?.youtube_cookie_pool?.length || 0;
  status.textContent = cookiePoolDraft ? `未保存：${cookiePoolDraft.enabled ? '已勾选 '+count+' 份 Cookie' : '将关闭账号池'}。点击“保存账号池”后生效。`
    : count ? `账号池已启用，共 ${count} 份 Cookie。成员在下方清单勾选，节奏在“节奏设置”调整。`
    : '账号池未启用。勾选至少 2 份 Cookie 并启用账号池后，点击“保存账号池”。';
}
document.addEventListener('change', event => {
  const input = event.target;
  if (!input.matches('#youtube-cookie-pool-enabled, [data-cookie-pool-id]')) return;
  const ids = new Set(cookiePoolDraft?.ids || state.config?.youtube_cookie_pool || []);
  if (input.dataset.cookiePoolId) {
    if (input.checked) ids.add(input.dataset.cookiePoolId); else ids.delete(input.dataset.cookiePoolId);
  }
  cookiePoolDraft = {instance:state.instanceId,enabled:$('#youtube-cookie-pool-enabled').checked,ids:[...ids]};
  renderCookiePoolSaveState();
});
$('#youtube-cookie-pool-recover').addEventListener('click', event=>runButton(event.currentTarget,async()=>{
  const value = await api('/api/youtube/cookies/pool/recover',{method:'POST',body:{}});
  toast(value.message); await loadCookieProfiles();
}));

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
