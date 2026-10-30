'use strict';

const riskGuide = JSON.parse(document.getElementById('risk-guide-data')?.textContent || '[]');
const riskByCode = new Map(riskGuide.map(item => [item.code, item]));

function riskFactsHTML(data, compact = false) {
  const facts = data.risks || [];
  if (!facts.length) return '';
  const visible = compact ? facts.filter(item => item.severity === 'warning') : facts;
  return visible.map(fact => {
    const guide = riskByCode.get(fact.code) || riskByCode.get('unknown');
    if (!guide) return '';
    const checked = ['auth_missing', 'auth_unknown', 'auth_valid', 'bot_check'].includes(fact.code) && data.auth_checked_at ? ` 检查记录：${new Date(data.auth_checked_at * 1000).toLocaleString('zh-CN')}。` : '';
    return `<div class="capacity-notice ${fact.severity === 'warning' ? 'warning' : 'info'}" data-risk-code="${escapeHTML(guide.code)}"><span class="capacity-notice-icon" aria-hidden="true">${fact.severity === 'warning' ? '!' : 'i'}</span><div><strong>${escapeHTML(guide.title)}</strong><p>${escapeHTML(fact.detail + checked)}</p>${compact ? '' : `<p><b>影响：</b>${escapeHTML(guide.impact)} <b>恢复：</b>${escapeHTML(guide.recovery)}</p><p><b>操作：</b>${escapeHTML(guide.action)}</p>`}</div><a href="${escapeHTML(guide.href)}">前往查看 ↗</a></div>`;
  }).join('');
}

function riskGuideHTML(key) {
  const categories = [...new Set(riskGuide.map(item => item.category))];
  return `<details class="capacity-details risk-guide" data-capacity-details="${escapeHTML(key)}-risk-guide"><summary>风控、限制与故障说明 <span>${riskGuide.length} 种情况 · 按影响与恢复方式查阅</span></summary><p class="risk-disclaimer">以下是情况说明，不代表当前已发生。系统只能显示已记录的响应与本地限制，无法读取源站内部风控规则；没有告警也不保证账号、出口或视频可用。HTTP 403、429、Video unavailable 均不能单独证明封号或封 IP。</p><div class="risk-legend"><span>限流 / 退避：等待后检查</span><span>登录 / 权限：可能需要处理</span><span>额度 / 资源：等待或调整配置</span><span>未知 / 过期：需要重新确认</span></div>${categories.map((category, index) => `<details class="risk-category" data-capacity-details="${escapeHTML(key)}-risk-${index}"><summary>${escapeHTML(category)} <span>${riskGuide.filter(item => item.category === category).length} 项</span></summary><div class="risk-grid">${riskGuide.filter(item => item.category === category).map(item => `<article class="risk-guide-item" data-guide-code="${escapeHTML(item.code)}"><h3>${escapeHTML(item.title)}</h3><dl><dt>影响与判断</dt><dd>${escapeHTML(item.impact)}</dd><dt>恢复与重试</dt><dd>${escapeHTML(item.recovery)}</dd><dt>建议操作</dt><dd>${escapeHTML(item.action)} <a href="${escapeHTML(item.href)}">前往查看 ↗</a></dd></dl></article>`).join('')}</div></details>`).join('')}</details>`;
}

function riskResourcesHTML(data, key) {
  return `<details class="capacity-details" data-capacity-details="${escapeHTML(key)}-resources"><summary>全部准入资源明细 <span>传输、解析、入口准备及共享范围</span></summary><p class="capacity-footnote">这里显示系统配置与许可占用，不是源站公开配额。额度降低后已有任务可继续；冷却与恢复可能同时存在，具体范围逐行查看。</p><div class="capacity-table-wrap"><table class="capacity-table"><thead><tr><th>渠道 / 上游组</th><th>阶段与范围</th><th>许可占用 / 上限</th><th>冷却与恢复</th></tr></thead><tbody>${(data.channels || []).flatMap(row => (row.resources || []).map(resource => `<tr><td>${escapeHTML(row.label)}<small>${escapeHTML(row.group)}</small></td><td>${resource.phase === 'resolve' ? '解析 / 准备' : '传输'}<small>${escapeHTML(waitResourceNames[resource.scope] || resource.scope)}${resource.scope.startsWith('backend-') ? '' : ' / '+escapeHTML(resource.group || '')}</small></td><td>${resource.used} / ${resource.limit}${resource.effective !== resource.limit ? `<small>当前有效额度 ${resource.effective}</small>` : ''}</td><td>${resource.cooldown_seconds ? '冷却 '+concurrencyCountdown(resource.cooldown_seconds, data.time) : resource.recovery_seconds ? '恢复观察 '+concurrencyCountdown(resource.recovery_seconds, data.time) : '未记录冷却'}${resource.last_throttle_status ? `<small>HTTP ${Number(resource.last_throttle_status)} / ${escapeHTML({resolve:'解析',transfer:'传输',subtitles:'字幕'}[resource.last_throttle_kind] || '触发阶段未记录')}</small>` : ''}</td></tr>`)).join('')}</tbody></table></div></details>`;
}

function taskRiskHTML(task) {
  const risk = task.risk;
  if (!risk?.codes?.length) return '';
  const queued = task.status === 'queued';
  const retry = risk.retry_scheduled;
  const remaining = Math.max(0, Math.ceil((risk.available_at || 0) - Date.now() / 1000));
  const exhausted = task.status === 'failed' && risk.max_attempts > 0 && risk.attempts >= risk.max_attempts;
  const stateText = task.status === 'failed' ? '任务已失败，当前没有已安排的自动重试。' : task.status === 'interrupted' ? '任务已中断，以当前队列状态为准。' : retry ? `已安排退避重试，${remaining ? `约 ${remaining} 秒后可再次领取` : '已到可领取时间，仍需等待实例、额度及资源'}。` : queued ? '正在排队，等待可用实例与资源。' : task.status === 'running' ? '任务正在执行，最终结果尚未确认。' : '任务已结束，请查看结果。';
  return `<section class="task-risk"><h3>限制与处理建议</h3><p class="risk-task-state">${escapeHTML(stateText)} 当前任务已尝试 ${Number(risk.attempts || 0)} / ${Number(risk.max_attempts || 0)} 次。${exhausted ? '已达到最大尝试次数，请先处理原因再手动重试。' : ''}${task.action === 'subtitles' ? '字幕任务状态不会改变父视频成功及计费。' : ''}</p>${risk.codes.map(code => {
    const item = riskByCode.get(code);
    return item ? `<article data-risk-code="${escapeHTML(code)}"><strong>${escapeHTML(item.title)}</strong><p>${escapeHTML(item.impact)}</p><p><b>恢复：</b>${escapeHTML(item.recovery)}</p><p><b>操作：</b>${escapeHTML(item.action)} <a href="${escapeHTML(item.href)}">前往查看 ↗</a></p></article>` : '';
  }).join('')}<p class="capacity-footnote">分类依据当前任务已记录的信息，可能同时匹配多个原因；请结合下方错误与日志判断，不能据此认定封号或封 IP。</p></section>`;
}

document.addEventListener('click', event => {
  if (event.target.closest('#task-risk a[href^="/#"]')) document.getElementById('task-dialog')?.close();
});
