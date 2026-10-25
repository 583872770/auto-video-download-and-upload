'use strict';
const $ = selector => document.querySelector(selector);
const form = $('#deployment-form');
let current = null;
let busy = false;
let pollTimer = null;
function updateButtons() {
  document.querySelectorAll('button').forEach(button => { button.disabled = busy || (Boolean(current?.starting) && Boolean(button.closest('#deployment-form'))); });
}
function feedback(message, error = false) { $('#setup-feedback').textContent = message; $('#setup-feedback').classList.toggle('error', error); }
async function api(path, data) {
  const response = await fetch(path, { method: data ? 'POST' : 'GET', headers: { 'X-CSRF-Token': $('meta[name="csrf-token"]').content, ...(data ? {'Content-Type':'application/json'} : {}) }, ...(data ? { body: JSON.stringify(data) } : {}) });
  const value = await response.json();
  if (!response.ok) {
    const error = new Error(value.error || '请求失败，请稍后重试。');
    error.diagnostic = value.diagnostic;
    throw error;
  }
  return value;
}
async function run(operation) {
  if (busy) return;
  busy = true;
  updateButtons();
  try { await operation(); } catch (error) { feedback(error.diagnostic ? '初始化未完成，请查看上方原因和处理建议。' : error.message, true); if (error.diagnostic) renderDiagnostic(error.diagnostic); }
  finally { busy = false; updateButtons(); }
}
function renderRole() {
  const role = $('#deployment-role').value;
  $('#instance-url-field').hidden = role !== 'worker';
  form.elements.instance_url.required = role === 'worker';
  if (role === 'worker' && !form.elements.instance_url.value) form.elements.instance_url.value = location.origin;
  $('#role-description').textContent = {manager:'管理后台只做管理与导入，视频由下载实例保存。整个集群使用一个管理后台。',worker:'这台机器只执行下载。配置完成后会自动注册到共享 MySQL，管理端即可看到。',all:'适合只用一台机器，同时提供管理页面与视频下载。'}[role];
  const converting = role === 'all' && Boolean(current?.cluster_state) && !current?.single_conversion;
  $('#single-transition-field').hidden = !converting;
  $('#single-transition-confirm').disabled = !converting;
  $('#single-transition-confirm').required = converting;
}
async function load() {
  current = await api('/api/deployment');
  for (const [key, value] of Object.entries(current.config)) if (form.elements[key]) form.elements[key].value = value;
  const hosts = new Set(form.elements.web_hosts.value.split(',').map(host => host.trim()).filter(Boolean));
  hosts.add(location.hostname);
  form.elements.web_hosts.value = [...hosts].join(',');
  $('#mysql-password').required = !current.password_saved;
  if (current.password_saved) { $('#mysql-password').placeholder = '已保存，留空保持原密码'; $('#password-note').textContent = '已保存密码不会回显。留空保持原密码，填写新密码则替换。'; }
  renderClusterState();
  if (current.cluster_state && form.elements.role.value === 'all' && !current.single_conversion) restoreInstance();
  renderStatus();
  $('#unlock-panel').hidden = true; form.hidden = false;
  renderRole();
}
function renderClusterState() {
  const state = current.cluster_state;
  $('#cluster-state-note').hidden = !state;
  if (state) $('#cluster-state-description').textContent = `这份状态目录原角色：${state.role === 'manager' ? '管理后台' : '下载实例'}；实例 ID：${state.instance_id}。可以选择单机模式并勾选转换确认，保存后重启。`;
}
function restoreInstance() {
  if (!current.cluster_state) return;
  form.elements.role.value = current.cluster_state.role;
  form.elements.instance_id.value = current.cluster_state.instance_id;
  renderRole();
  feedback('已恢复原角色和实例 ID，请检查配置后点击保存启动。');
}
function renderStatus() {
  renderDiagnostic(current.diagnostic);
  const statusMessage = current.starting ? `${current.stage || '正在初始化服务…'}（当前阶段 ${Math.floor(current.stage_elapsed)} 秒）` : current.pending_restart ? '已有新配置等待生效，请重启本机服务。' : current.diagnostic ? '' : current.error;
  $('#deployment-state').hidden = !statusMessage;
  $('#deployment-state').textContent = statusMessage || '';
  $('#back-link').hidden = !current.running || current.running_role === 'worker';
  $('#save-deployment').textContent = current.starting ? '服务正在初始化…' : current.running ? '保存配置（重启后生效）' : '保存并启动服务';
  $('#save-description').textContent = current.starting ? '设置页可以使用，初始化完成后即可进入工作台。' : current.running ? '保存后不会中断当前任务。' : '先测试连接，再保存部署配置。';
  updateButtons();
  if (current.starting && pollTimer === null) pollTimer = setTimeout(pollStartup, 1000);
}
function renderDiagnostic(diagnostic) {
  $('#deployment-error').hidden = !diagnostic;
  if (!diagnostic) return;
  $('#deployment-error-title').textContent = diagnostic.summary;
  $('#deployment-error-remedy').textContent = diagnostic.remedy;
  $('#deployment-error-details').textContent = [
    `失败阶段：${diagnostic.stage || '初始化'}`, `异常类型：${diagnostic.type}`,
    diagnostic.mysql_code ? `MySQL 错误码：${diagnostic.mysql_code}` : '', diagnostic.detail,
    ...(diagnostic.location || [])].filter(Boolean).join('\n');
}
async function pollStartup() {
  pollTimer = null;
  try {
    current = await api('/api/deployment');
    renderClusterState();
    renderStatus();
    if (!current.starting && current.running) {
      $('#setup-success').hidden = false;
      $('#success-message').textContent = '服务初始化完成。';
      $('#enter-workbench').hidden = current.running_role === 'worker';
      $('#worker-next').hidden = current.running_role !== 'worker';
      feedback('服务已就绪。');
    } else if (!current.starting && current.error) {
      feedback('初始化未完成，请查看上方原因和处理建议。', true);
    }
  } catch (error) {
    feedback(error.message, true);
    if (current?.starting) pollTimer = setTimeout(pollStartup, 2000);
  }
}
function values() {
  const data = Object.fromEntries(new FormData(form));
  data.mysql_port = Number(data.mysql_port);
  data.single_transition = !$('#single-transition-confirm').disabled && $('#single-transition-confirm').checked;
  return data;
}
$('#unlock-form').addEventListener('submit', event => { event.preventDefault(); run(async () => { await api('/api/deployment/unlock', {code:$('#access-code').value.trim()}); $('#access-code').value = ''; await load(); feedback('已打开部署设置。'); }); });
$('#deployment-role').addEventListener('change', renderRole);
$('#restore-instance').addEventListener('click', restoreInstance);
$('#test-database').addEventListener('click', () => { if (form.reportValidity()) run(async () => { feedback('正在测试 MySQL 连接…'); feedback((await api('/api/deployment/test', values())).message); }); });
form.addEventListener('submit', event => { event.preventDefault(); run(async () => {
  feedback('正在验证并保存配置…');
  const value = await api('/api/deployment/save', values());
  $('#mysql-password').value = '';
  await load();
  const message = value.pending_restart ? value.message : current.running ? '配置已保存，服务已启动。' : current.error || value.message;
  $('#setup-success').hidden = Boolean(current.starting || current.error);
  $('#success-message').textContent = message;
  $('#enter-workbench').hidden = current.starting || value.pending_restart || value.role === 'worker';
  $('#worker-next').hidden = current.starting || value.pending_restart || value.role !== 'worker';
  feedback(current.error ? '初始化未完成，请查看上方原因和处理建议。' : message, Boolean(current.error));
}); });
if ($('meta[name="deployment-unlocked"]').content === 'true') run(load);
