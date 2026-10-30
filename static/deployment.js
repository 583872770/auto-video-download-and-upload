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
async function load() {
  current = await api('/api/deployment');
  for (const [key, value] of Object.entries(current.config)) if (form.elements[key]) form.elements[key].value = value;
  const hosts = new Set(form.elements.web_hosts.value.split(',').map(host => host.trim()).filter(Boolean));
  hosts.add(location.hostname);
  form.elements.web_hosts.value = [...hosts].join(',');
  renderClusterState();
  renderStatus();
  $('#unlock-panel').hidden = true; form.hidden = false;
}
function renderClusterState() {
  const state = current.cluster_state;
  $('#cluster-state-note').hidden = !state;
  if (state) $('#cluster-state-description').textContent = `已识别原实例 ID：${state.instance_id}。升级后统一提供管理与下载，原任务、统计与计费保留。`;
}
function restoreInstance() {
  if (!current.cluster_state) return;
  form.elements.instance_id.value = current.cluster_state.instance_id;
  feedback('已恢复原实例 ID，请检查配置后点击保存启动。');
}
function renderStatus() {
  $('#database-state').textContent = current.database_configured ? '已读取数据库启动配置，可测试连接。' : '数据库配置不完整，请补齐配置文件后重启服务。';
  renderDiagnostic(current.diagnostic);
  const statusMessage = current.starting ? `${current.stage || '正在初始化服务…'}（当前阶段 ${Math.floor(current.stage_elapsed)} 秒）` : current.pending_restart ? '已有新配置等待生效，请重启本机服务。' : current.diagnostic ? '' : current.error;
  $('#deployment-state').hidden = !statusMessage;
  $('#deployment-state').textContent = statusMessage || '';
  $('#back-link').hidden = !current.running;
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
      $('#enter-workbench').hidden = false;
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
  data.role = 'hybrid';
  return data;
}
$('#unlock-form').addEventListener('submit', event => { event.preventDefault(); run(async () => { await api('/api/deployment/unlock', {code:$('#access-code').value.trim()}); $('#access-code').value = ''; await load(); feedback('已打开部署设置。'); }); });
$('#restore-instance').addEventListener('click', restoreInstance);
$('#test-database').addEventListener('click', () => { if (form.reportValidity()) run(async () => { feedback('正在测试 MySQL 连接…'); feedback((await api('/api/deployment/test', values())).message); }); });
form.addEventListener('submit', event => { event.preventDefault(); run(async () => {
  feedback('正在验证并保存配置…');
  const value = await api('/api/deployment/save', values());
  await load();
  const message = value.pending_restart ? value.message : current.running ? '配置已保存，服务已启动。' : current.error || value.message;
  $('#setup-success').hidden = Boolean(current.starting || current.error);
  $('#success-message').textContent = message;
  $('#enter-workbench').hidden = current.starting || value.pending_restart;
  feedback(current.error ? '初始化未完成，请查看上方原因和处理建议。' : message, Boolean(current.error));
}); });
if ($('meta[name="deployment-unlocked"]').content === 'true') run(load);
