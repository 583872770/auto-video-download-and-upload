'use strict';
let deploymentRead = 0;
function resetInstanceDeployment() {
  ++deploymentRead;
  state.deployment = null; state.deploymentDirty = false;
  $('#instance-deployment-form').reset();
  $('#instance-deployment-form').hidden = true;
  $('#instance-deployment-state').textContent = '正在读取所选实例部署配置…';
  $('#instance-deployment-result').textContent = '';
  $('#instance-deployment-discard').hidden = true;
}
async function loadInstanceDeployment() {
  if (state.deploymentDirty) return;
  const ticket = ++deploymentRead;
  const form = $('#instance-deployment-form');
  form.inert = true;
  try {
    const data = await api('/api/instance-deployment');
    if (ticket !== deploymentRead) return;
    if (fleetEnabled && data.instance_id !== state.instanceId) throw new Error('返回的部署配置不属于所选实例，请检查实例地址。');
    state.deployment = data;
    for (const [key, value] of Object.entries(data.config)) {
      const field = form.elements[key];
      if (!field) continue;
      if (field.type === 'checkbox') field.checked = !!value;
      else field.value = value;
    }
    state.deploymentDirty = false;
    $('#instance-deployment-discard').hidden = true;
    form.hidden = false;
    $('#instance-deployment-state').textContent = `当前管理：${instanceName(state.instanceId)}。` +
      (data.pending_restart ? '部署配置已保存，等待重启该实例服务生效。' : '配置已读取；保存仅影响当前所选实例。');
    $('#instance-deployment-redis').textContent = data.redis_address ? `已保存：${data.redis_address}（认证信息已隐藏）` : '当前未配置 Redis。';
    form.elements.mysql_password.placeholder = data.mysql_password_configured ? '已设置，留空保留原密码' : '尚未设置，请填写密码';
  } catch (error) {
    if (ticket !== deploymentRead || error.name === 'AbortError') return;
    form.hidden = true;
    $('#instance-deployment-state').textContent = error.message;
  } finally { if (ticket === deploymentRead) form.inert = false; }
}
document.addEventListener('DOMContentLoaded', () => {
  const form = $('#instance-deployment-form');
  let busy = false;
  form.addEventListener('input', () => { state.deploymentDirty = true; $('#instance-deployment-discard').hidden = false; });
  $('#instance-deployment-discard').addEventListener('click', () => {
    if (busy) return;
    state.deploymentDirty = false;
    loadInstanceDeployment();
  });
  $('#instance-deployment-reload').addEventListener('click', () => {
    if (busy) return;
    if (state.deploymentDirty) { toast('请先保存部署配置，或点击“放弃修改并重新读取”。', true); return; }
    loadInstanceDeployment();
  });
  async function submit(test) {
    if (busy || !state.deployment || !form.reportValidity()) return;
    busy = true; form.inert = true;
    const data = Object.fromEntries(new FormData(form));
    data.mysql_port = Number(data.mysql_port);
    data.redis_enabled = form.elements.redis_enabled.checked;
    data.revision = state.deployment.revision;
    const result = $('#instance-deployment-result');
    const target = instanceName(state.instanceId);
    result.textContent = test ? `正在从「${target}」测试 MySQL / Redis 连接…` : `正在验证连接并保存「${target}」的部署配置…`;
    try {
      const response = await api('/api/instance-deployment' + (test ? '/test' : ''), {method: test ? 'POST' : 'PUT', body: data});
      result.textContent = `「${target}」：${response.message}`;
      if (!test) { state.deploymentDirty = false; await loadInstanceDeployment(); }
    } catch (error) { result.textContent = `操作未完成：${error.message}；输入已保留。`; }
    finally { busy = false; form.inert = false; }
  }
  $('#instance-deployment-test').addEventListener('click', () => submit(true));
  form.addEventListener('submit', event => { event.preventDefault(); submit(false); });
});
