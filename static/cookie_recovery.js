'use strict';

let cookieRecoveryData = null;
let cookieRecoveryTicket = 0;
let cookieRecoveryAbort = null;

function cookieRecoverySessions() {
  return cookieRecoveryData?.cookie_pool?.sessions || [];
}
function cookieRecoveryCurrent() {
  return !!cookieRecoveryData?.online && Date.now() / 1000 - cookieRecoveryData.time <= 20;
}
function cookieRecoveryDuration(seconds) {
  return `${Math.floor(Math.max(0, seconds) / 60)} 分 ${Math.floor(Math.max(0, seconds) % 60)} 秒`;
}
function cookieRecoveryProgressHTML(session, data, current) {
  const p = session?.pacing;
  const stale = current ? '' : '<p class="cookie-recovery-warning">实例离线或采样已过期，以下为上次记录，不能确认当前进度。</p>';
  if (!p) return `${stale}<p>尚未取得该账号的恢复进度，请等待实例更新采样。</p>`;
  const cooling = p.cooling_until > data.time;
  const staged = p.phase === 'protection_stage';
  const reached = staged && p.target_reached;
  const phase = cooling ? '账号冷却中' : p.recovery ? '低频恢复观察' : reached ? '已达到设置目标'
    : staged ? '逐档提速观察' : '稳定后逐步缩短间隔';
  const time = stamp => escapeHTML(new Date(stamp * 1000).toLocaleString('zh-CN'));
  const observation = p.observed_since
    ? `从 ${time(p.observed_since)} 开始，已观察 ${cookieRecoveryDuration(p.observed_seconds)} / 至少 ${cookieRecoveryDuration(p.observation_seconds)}。${p.seconds_remaining > 0 ? `时间门槛还需 ${concurrencyCountdown(p.seconds_remaining, data.time)}；倒计时结束后仍须等待成功结算。` : '时间门槛已满足。'}`
    : p.recovery ? '尚未开始计时：冷却结束后，从首个获准启动的恢复任务开始计算。'
      : staged ? '尚未开始本档观察：下一次有效成功后建立观察窗口，历史成功不计入本档。'
      : '尚未开始计时：从下一次有效成功完成任务开始计算。';
  const next = p.recovery
    ? `两项同时达标后的成功结算，将退出低频恢复、恢复配置的账号并发（${Number(session.configured_limit)} 路）。当次启动间隔仍至少 ${Number(p.next_interval)} 秒；后续稳定成功才逐步缩短。`
    : staged ? reached
      ? `当前已达到设置的目标下限 ${Number(p.configured_minimum)} 秒，停止继续提速；实际启动仍受任务结束后休息、账号并发和滚动预算约束。`
      : `本档至少观察 ${cookieRecoveryDuration(p.observation_seconds)}，且有效成功至少 ${Number(p.success_target)} 次，达标后的有效成功才会从 ${Number(p.interval)} 秒降至 ${Number(p.next_interval)} 秒。每档最多减少 ${Number(p.step)} 秒；降档后重新计时和计数，不沿用历史成功。`
    : p.next_interval < p.interval
      ? `满足两项门槛后的下一次有效成功，预计把实际启动间隔从 ${Number(p.interval)} 秒降至 ${Number(p.next_interval)} 秒。稳定阶段每次减 ${Number(p.stable_step)} 秒；正常阶段累计成功至少 ${Number(p.mature_success_target)} 次且本轮观察满 ${cookieRecoveryDuration(p.mature_observation_seconds)} 后每次减 ${Number(p.mature_step)} 秒，实际步长按结算时状态决定。`
      : `当前实际间隔已受配置或保护下限约束；即使记录间隔继续下降，实际启动间隔暂时仍至少 ${Number(p.next_interval)} 秒。`;
  const failures = p.recovery
    ? '再次发生影响账号节奏的故障，会重置本轮成功数和观察起点，并按错误策略退避或冷却。'
    : staged ? '再次发生影响账号节奏的故障会清零本档观察并退避；明确限流会重新进入冷却和低频恢复。'
    : '影响账号节奏的故障会重置稳定观察，可能提高间隔或重新进入冷却。';
  const protection = p.recovery ? `低频恢复期间至少 ${Number(p.recovery_minimum)} 秒`
    : staged ? `本档至少 ${Number(p.protection_minimum)} 秒，满足观察条件后可逐档下降`
    : p.protection_minimum ? `暂时至少 ${Number(p.protection_minimum)} 秒，间隔降至 120 秒后开始逐档观察`
    : '无额外保护下限';
  return `${stale}<p class="cookie-recovery-phase">${phase}</p>
    <div class="cookie-recovery-metrics"><div><span>当前实际启动间隔</span><strong>至少 ${Number(p.interval)} 秒</strong></div><div><span>配置最小间隔</span><strong>${Number(p.configured_minimum)} 秒</strong></div><div><span>调度器记录间隔</span><strong>${Number(p.saved_interval)} 秒</strong></div></div>
    <p>恢复保护下限：${protection}。实际间隔取所有下限的最大值。</p>
    ${cooling ? `<p class="cookie-recovery-warning">先等待冷却：截止 ${time(p.cooling_until)} · ${concurrencyCountdown(p.cooling_until - data.time, data.time)}。冷却到期仅重新检查准入，不保证源站解除限制。</p>` : ''}
    ${reached ? '' : `<h3>下一步调整需要同时满足</h3>
    <div class="cookie-recovery-gate"><label>有效成功 ${Number(p.success_count)} / 至少 ${Number(p.success_target)} 次${p.successes_remaining ? ` · 还差 ${Number(p.successes_remaining)} 次` : ' · 次数门槛已满足'}</label><progress value="${Math.min(p.success_count, p.success_target)}" max="${p.success_target}" aria-label="有效成功进度"></progress></div>
    <div class="cookie-recovery-gate"><label>${observation}</label><progress value="${Math.min(p.observed_seconds, p.observation_seconds)}" max="${p.observation_seconds}" aria-label="观察时间进度"></progress></div>`}
    <p class="cookie-recovery-next">${next}</p>
    <p class="micro-copy">${failures}单个视频不可用等内容错误不会重置账号恢复进度；上传失败和本地资源等待也不作为账号故障。跳过已有视频不计有效成功。时间到不会独立触发调整，任务仍受启动间隔、预算和队列限制。</p>`;
}

function renderCookieRecovery(data = cookieRecoveryData) {
  cookieRecoveryData = data;
  const dialog = document.getElementById('cookie-recovery-dialog');
  if (!dialog?.open) return;
  if (dialog.dataset.instance !== cookieFilterScope() || dialog.dataset.epoch !== String(state.instanceEpoch || 0)) {
    dialog.close();
    return;
  }
  const sessions = cookieRecoverySessions();
  const selector = document.getElementById('cookie-recovery-account');
  // Preserve the native dropdown while new samples arrive.
  const signature = JSON.stringify(sessions.map(s => [s.id, s.name, s.state]));
  if (selector.dataset.signature !== signature) {
    const selected = selector.value;
    selector.replaceChildren(...sessions.map(s => new Option(`${s.name} · ${s.state === 'recovery' ? '低频恢复' : s.state === 'cooldown' ? '冷却中' : '稳定观察'}`, s.id)));
    if (sessions.some(s => s.id === selected)) selector.value = selected;
    selector.dataset.signature = signature;
    if (selected && selected !== selector.value) {
      // Never show the removed account's history alongside another account.
      cookieRecoveryAbort?.abort();
      cookieRecoveryTicket++;
      document.getElementById('cookie-recovery-history').textContent = '账号池成员已变化，请点击“刷新调整记录”读取当前账号。';
    }
  }
  const session = sessions.find(s => s.id === selector.value);
  const target = document.getElementById('cookie-recovery-progress');
  const html = cookieRecoveryProgressHTML(session, data, cookieRecoveryCurrent());
  if (capacityMarkupChanged(target, html)) target.innerHTML = html;
  document.getElementById('cookie-recovery-refresh').disabled = !session;
}

async function refreshCookieRecoveryHistory() {
  const dialog = document.getElementById('cookie-recovery-dialog');
  const list = document.getElementById('cookie-recovery-history');
  const id = document.getElementById('cookie-recovery-account').value;
  cookieRecoveryAbort?.abort();
  const ticket = ++cookieRecoveryTicket;
  const instance = cookieFilterScope();
  const epoch = state.instanceEpoch || 0;
  if (!dialog.open || !id) { list.textContent = '暂无可查看的账号。'; return; }
  cookieRecoveryAbort = new AbortController();
  list.textContent = '正在读取记录…';
  const valid = () => dialog.open && ticket === cookieRecoveryTicket && instance === cookieFilterScope()
    && epoch === (state.instanceEpoch || 0) && document.getElementById('cookie-recovery-account').value === id;
  try {
    const data = await api(`/api/youtube/cookies/${encodeURIComponent(id)}/timeline?from=${(Date.now() / 1000 - 604800).toFixed(3)}`, {signal: cookieRecoveryAbort.signal});
    if (!valid()) return;
    list.replaceChildren();
    if (!data.events.length) { list.textContent = '最近 7 天暂无调整或异常记录。'; return; }
    for (const event of data.events) {
      const article = document.createElement('article');
      article.className = `cookie-timeline-event ${event.event_type === 'failure' ? 'failure' : 'change'}`;
      const heading = document.createElement('h4');
      heading.textContent = `${new Date(event.occurred_at * 1000).toLocaleString('zh-CN')} · ${{interval_change: '启动间隔调整', recovery: '恢复阶段调整', failure: '异常与退避', auth_check: '登录检测'}[event.event_type] || '账号事件'}`;
      const message = document.createElement('p');
      message.textContent = event.message || event.kind || '';
      const detail = document.createElement('small');
      detail.textContent = [event.previous_interval != null && event.new_interval != null ? `记录间隔：${event.previous_interval} 秒 → ${event.new_interval} 秒` : '',
        event.cooldown_until ? `冷却截止：${new Date(event.cooldown_until * 1000).toLocaleString('zh-CN')}` : '',
        event.http_status ? `HTTP ${event.http_status}` : ''].filter(Boolean).join(' · ');
      article.append(heading, message, detail);
      list.append(article);
    }
  } catch (error) {
    if (valid() && error.name !== 'AbortError') list.textContent = `读取失败：${error.message}。可点击“刷新调整记录”重试。`;
  }
}

document.addEventListener('click', event => {
  const button = event.target.closest('[data-cookie-recovery]');
  if (!button) return;
  const dialog = document.getElementById('cookie-recovery-dialog');
  const sessions = cookieRecoverySessions();
  if (!dialog || !sessions.length) return;
  dialog.dataset.instance = cookieFilterScope();
  dialog.dataset.epoch = String(state.instanceEpoch || 0);
  if (!dialog.open) dialog.showModal();
  renderCookieRecovery();
  const selected = sessions.find(s => s.id === button.dataset.cookieRecovery)
    || sessions.find(s => s.state === 'recovery') || sessions.find(s => s.state === 'cooldown') || sessions[0];
  document.getElementById('cookie-recovery-account').value = selected.id;
  renderCookieRecovery();
  refreshCookieRecoveryHistory();
});
document.getElementById('cookie-recovery-account')?.addEventListener('change', () => {
  renderCookieRecovery();
  refreshCookieRecoveryHistory();
});
document.getElementById('cookie-recovery-refresh')?.addEventListener('click', refreshCookieRecoveryHistory);
document.getElementById('cookie-recovery-dialog')?.addEventListener('close', () => {
  cookieRecoveryAbort?.abort();
  cookieRecoveryTicket++;
});
