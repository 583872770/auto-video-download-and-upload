'use strict';

// Keep one settings form and all existing controls; group by the user's task.
(() => {
  const form = document.querySelector('#settings-form');
  if (!form) return;
  const tabs = [...document.querySelectorAll('.settings-tabs [role="tab"]')];
  const panes = new Map();
  const save = form.querySelector('.settings-save');
  for (const tab of tabs) {
    const pane = document.createElement('section');
    pane.id = tab.getAttribute('aria-controls');
    pane.className = 'settings-category';
    pane.setAttribute('role', 'tabpanel');
    pane.setAttribute('aria-labelledby', tab.id);
    pane.dataset.settingsCategory = tab.dataset.settingsTarget;
    pane.hidden = tab.dataset.settingsTarget !== 'download';
    form.insertBefore(pane, save);
    panes.set(tab.dataset.settingsTarget, pane);
  }
  const move = (node, key) => { if (node) panes.get(key).append(node); };
  const layout = form.querySelector('.settings-layout');
  const [locationPanel, loginPanel, resourcePanel] = [...layout.children];
  move(form.querySelector('.task-defaults-panel'), 'download');
  move(loginPanel, 'accounts');
  move(resourcePanel, 'performance');
  move(form.querySelector('#channel-settings'), 'performance');
  move(locationPanel, 'storage');
  move(form.querySelector('#disk-panel'), 'storage');
  move(form.querySelector('.service-strip'), 'diagnostics');
  move(form.querySelector('[data-concurrency-panel="settings"]'), 'diagnostics');
  layout.remove();

  function moveFields(names, destination) {
    for (const name of names) {
      const label = form.querySelector(`[name="${name}"]`)?.closest('label');
      if (label) destination.append(label);
    }
  }
  // Keep concurrency controls together, and directory/file controls together.
  moveFields(['fragment_concurrency'], resourcePanel.querySelector('.scale-fields'));
  moveFields(['shard_downloads'], locationPanel);
  moveFields(['min_free_gb', 'max_log_mb', 'log_retention_days'], form.querySelector('#disk-panel .scale-fields'));
  moveFields(['subtitle_mode', 'subtitle_languages'], form.querySelector('.task-defaults-panel .scale-fields'));
  for (const name of ['subtitle_mode', 'subtitle_languages']) {
    form.querySelector(`[name="${name}"]`).closest('label').dataset.providerSetting = 'yt_dlp';
  }
  const modePanel = document.createElement('section');
  modePanel.className = 'panel settings-panel control-mode-panel';
  modePanel.innerHTML = '<div class="panel-heading"><div><h2>运行模式与并发上限</h2><p class="panel-subtitle">自动调节和自定义都遵守会话、渠道及冷却限制；渠道选择策略单独设置。</p></div></div><div class="scale-fields"></div><p id="control-mode-note" class="settings-category-note" role="status"></p>';
  moveFields(['throughput_mode', 'worker_count', 'bandwidth_mbps'], modePanel.querySelector('.scale-fields'));
  panes.get('performance').insertBefore(modePanel, resourcePanel);
  const modeSelect = form.querySelector('[name="throughput_mode"]');
  modeSelect.closest('label').firstChild.textContent = '运行模式';
  modeSelect.querySelector('[value="auto"]').textContent = '自动调节';
  modeSelect.querySelector('[value="manual"]').textContent = '自定义';
  modeSelect.closest('label').querySelector('small').textContent = '自动调节在最高值内探索；自定义按固定设置执行，保护性限制始终有效。';
  form.querySelector('[name="bandwidth_mbps"]').closest('label').querySelector('small').textContent = '仅自动调节使用；0 表示不设目标。以下载器记录的媒体接收吞吐判断，不用整机网卡流量加速。';
  const channelStrategy = form.querySelector('[name="scheduler_mode"]').closest('label');
  channelStrategy.firstChild.textContent = '渠道选择策略';
  const channelNote = document.createElement('small');
  channelNote.textContent = '决定用哪个渠道，不改变运行模式；只启用 yt-dlp 时通常无需调整。';
  channelStrategy.append(channelNote);

  // Login actions come before less frequently changed request policy fields.
  const policy = loginPanel.querySelector('[data-provider-setting="yt_dlp"]');
  if (policy) {
    const details = document.createElement('details');
    details.className = 'settings-policy-details';
    const summary = document.createElement('summary');
    summary.textContent = '出口与请求节奏（高级）';
    details.append(summary, policy);
    loginPanel.append(details);
  }
  const channelPanel = form.querySelector('#channel-settings');
  channelPanel.querySelector('h2').textContent = '渠道与连接预算';
  resourcePanel.querySelector('h2').textContent = '浏览器与分片预算';
  move(form.querySelector('#database-backend'), 'diagnostics');
  form.querySelector('[name="worker_count"]').closest('label').firstChild.textContent = '实例并发上限';
  loginPanel.querySelector('h2').textContent = 'Cookie 与 YouTube 登录';
  loginPanel.querySelector('.youtube-setting > small').textContent = '浏览器登录会更新原有凭据；新增 Cookie 独立保存，需要在账号管理中选择使用。检测登录不会自动恢复下载。';
  const diagnosticLink = document.createElement('button');
  diagnosticLink.type = 'button';
  diagnosticLink.className = 'button button-white settings-diagnostic-link';
  diagnosticLink.dataset.settingsTarget = 'diagnostics';
  diagnosticLink.textContent = '查看实时额度、限流与风控说明 ↗';
  channelPanel.prepend(diagnosticLink);

  const descriptions = {
    download: '选择下载方式、画质和重试策略。保存方式为测试模式时，下载完成的文件会自动删除。',
    accounts: '先确认下载出口，再检测 Cookie。账号名称只是备注；相同核心会话共享额度，登录通过也不代表视频一定可下载。',
    performance: '设置值是上限，实际并发还受 Cookie 会话、启动间隔、渠道和连接预算约束。修改后可在运行诊断查看实际限制。',
    storage: '素材目录决定正式文件位置；临时预算、暂停水位和日志期限决定空间管理方式。',
    diagnostics: '这里显示已保存配置的实时运行结果。限流、验证码、账号失效和资源等待分别处理，详细提示与排查说明均保留。'
  };
  for (const [key, pane] of panes) {
    const note = document.createElement('p');
    note.className = 'settings-category-note';
    note.textContent = descriptions[key];
    pane.prepend(note);
  }
  const select = (key, focus = false) => {
    if (!panes.has(key)) return;
    for (const tab of tabs) {
      const active = tab.dataset.settingsTarget === key;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      panes.get(tab.dataset.settingsTarget).hidden = !active;
      if (active && focus) tab.focus();
    }
  };
  document.querySelector('.settings-tabs').hidden = false;
  document.addEventListener('click', event => {
    const target = event.target.closest('[data-settings-target]');
    if (target) select(target.dataset.settingsTarget);
  });
  document.querySelector('.settings-tabs').addEventListener('keydown', event => {
    const index = tabs.indexOf(document.activeElement);
    if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 :
      (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    select(tabs[next].dataset.settingsTarget, true);
  });
  // Native validation must reveal a required field even in another category.
  let revealingInvalid = false;
  form.addEventListener('invalid', event => {
    if (revealingInvalid) return;
    revealingInvalid = true;
    setTimeout(() => { revealingInvalid = false; }, 0);
    const pane = event.target.closest('[data-settings-category]');
    if (pane) select(pane.dataset.settingsCategory);
    for (let node = event.target.parentElement; node && node !== form; node = node.parentElement) {
      if (node.tagName === 'DETAILS') node.open = true;
    }
  }, true);
  // A disk shortcut always lands in storage, even after visiting another tab.
  document.querySelector('#workspace-disk-status').dataset.settingsTarget = 'storage';
  // Display current task control before the full live diagnostics.
  const workbench = document.querySelector('.workbench-grid');
  const capacity = document.querySelector('[data-concurrency-panel="overview"]');
  if (workbench && capacity) workbench.after(capacity);
  // Connection selection precedes the optional subscription maintenance.
  const network = document.querySelector('.clash-top-grid');
  if (network?.children.length === 2) network.prepend(network.lastElementChild);
  renderDownloadMethod();
})();
