/* Session accounting stays separate from the live download dashboard. */
(() => {
  const stats = { cursor: null, history: [], next: null, selected: null, kind: 'files', detailCursor: null,
    detailHistory: [], detailNext: null, busy: false, pending: false, dirty: false, pricingDirty: false, detail: null };
  const sessionStatus = { active: '进行中', stopping: '正在暂停', paused: '已暂停', completed: '已结束' };
  const outcome = { running: '下载中', retrying: '等待重试', paused: '暂停 / 待续传', succeeded: '成功', failed: '失败', skipped: '已跳过', test_succeeded: '测试成功（不计费）' };
  const visible = () => !$('#statistics-view').hidden;
  const integer = value => BigInt(String(value || 0)).toLocaleString('zh-CN');
  const successes = value => BigInt(String(value.succeeded || 0)) + BigInt(String(value.test_succeeded || 0));
  const gb = bytes => `${(Number(bytes || 0) / 1e9).toLocaleString('zh-CN', { minimumFractionDigits: 3, maximumFractionDigits: 6 })} GB`;
  const money = session => Number(session.price) > 0 ? `¥ ${session.amount}` : '未设置单价';
  const card = (label, value, note = '') => `<div class="accounting-card"><span>${label}</span><strong>${escapeHTML(value)}</strong><small>${escapeHTML(note)}</small></div>`;
  const duration = session => {
    const seconds = Math.max(0, Math.floor((Date.parse(session.ended_at || new Date().toISOString()) - Date.parse(session.started_at)) / 1000));
    return `${Math.floor(seconds / 3600)}小时 ${Math.floor(seconds % 3600 / 60)}分 ${seconds % 60}秒`;
  };
  const fill = (form, values) => Object.entries(values).forEach(([name, value]) => {
    if (form.elements.namedItem(name)) form.elements.namedItem(name).value = value ?? '';
  });

  function renderMethods(target, rows) {
    $(target).innerHTML = (rows || []).map(row => `<tr><td><strong>${escapeHTML(row.label)}</strong><small>${row.method === 'unknown' ? '升级前未记录实际下载提供方' : row.fallback_tasks ? `包含 ${integer(row.fallback_tasks)} 个从其他下载器切换过来的任务` : '按实际处理该视频的下载提供方归类'}</small></td><td><b>${integer(row.attempted)}</b><small>进行中 ${integer(row.running)}</small></td><td><b>${integer(successes(row))} / ${integer(row.failed)}</b><small>含测试成功 ${integer(row.test_succeeded)} · 成功率 ${row.success_rate === null ? '—' : `${row.success_rate}%`}</small></td><td>${integer(row.skipped)} / ${integer(row.unfinished)}</td><td><b>${gb(row.file_bytes)}</b><small>${integer(row.file_count)} 个视频 · ${integer(row.file_bytes_exact)} 字节</small></td><td><b>${gb(row.test_file_bytes)}</b><small>${integer(row.test_file_count)} 个视频 · ${integer(row.test_file_bytes_exact)} 字节 · 不计费</small></td><td><b>${gb(row.downloaded_file_bytes)}</b><small>${integer(row.downloaded_file_count)} 个视频 · ${integer(row.downloaded_file_bytes_exact)} 字节</small></td><td>${integer(row.fallback_tasks)}</td></tr>`).join('');
  }

  function renderOverview(data) {
    const total = data.summary;
    $('#statistics-totals').innerHTML = card('累计计费视频大小', gb(total.file_bytes), `${integer(total.file_bytes_exact)} 字节 · 精确大小累计`)
      + card('累计交付视频', integer(total.file_count), `跳过 ${integer(total.skipped)} · 测试成功 ${integer(total.test_succeeded)}（不计费）`)
      + card('成功任务 / 失败任务', `${integer(successes(total))} / ${integer(total.failed)}`, `含测试成功 ${integer(total.test_succeeded)} · 重试未耗尽时不计失败`)
      + card('累计预计费用', `¥ ${total.amount}`, total.unpriced_bytes ? `${gb(total.unpriced_bytes)} 尚未设置单价` : '按各会话单价计算后汇总')
      + card('累计测试下载大小', gb(total.test_file_bytes), `${integer(total.test_file_count)} 个视频 · ${integer(total.test_file_bytes_exact)} 字节 · 已删除，不计费`)
      + card('累计完成视频大小', gb(total.downloaded_file_bytes), `交付 + 测试，共 ${integer(total.downloaded_file_count)} 个视频 · ${integer(total.downloaded_file_bytes_exact)} 字节`);
    $('#statistics-summary-export').href = `/api/statistics/export?${new URLSearchParams({ q: $('#statistics-search').value.trim() })}`;
    $('#statistics-methods-export').href = `/api/statistics/methods/export?${new URLSearchParams({ q: $('#statistics-search').value.trim() })}`;
    renderMethods('#statistics-methods', data.methods);
    $('#statistics-methods-since').textContent = `下载提供方统计起始于 ${formatDate(data.method_tracking_since, true)}。按 yt5s、yt-dlp、downmp3 分别统计；按配置顺序切换时，前一下载器计失败，下一下载器单独计尝试；同一会话中每个视频对同一下载器只计一次。旧记录显示为历史未分类。成功率 = 成功尝试（含测试）÷ 成功与失败尝试之和。`;
    $('#statistics-since').textContent = `计费大小统计起始于 ${formatDate(data.tracking_since, true)}；测试大小统计起始于 ${formatDate(data.test_tracking_since, true)}。历史任务未补算大小；金额按精确字节计算，按会话四舍五入到分。`;
    if (!stats.pricingDirty) fill($('#statistics-pricing-form'), data.pricing);
    const active = data.active;
    $('#statistics-active').hidden = !active;
    if (active) $('#statistics-active').innerHTML = `<span class="online-dot"></span><div><strong>${active.status === 'stopping' ? '正在结束本次会话' : '本次下载会话进行中'}</strong><span>已交付 ${integer(active.file_count)} 个视频 · ${gb(active.file_bytes)}；测试 ${integer(active.test_file_count)} 个视频 · ${gb(active.test_file_bytes)} · ${integer(active.running)} 个正在执行</span></div><button type="button" class="text-button" data-statistics-session="${active.id}">查看本次明细 ↗</button>`;
    $('#statistics-sessions').innerHTML = data.items.map(item => `<tr class="${item.id === stats.selected ? 'accounting-selected' : ''}"><td><button type="button" class="accounting-session-link" data-statistics-session="${item.id}"><strong>${escapeHTML(item.label || `下载会话 #${item.seq}`)}</strong><span>${escapeHTML(formatDate(item.started_at, true))}</span><span>至 ${escapeHTML(item.ended_at ? formatDate(item.ended_at, true) : '当前')}</span></button></td><td><span class="accounting-status ${item.status === 'active' ? 'is-active' : ''}">${sessionStatus[item.status] || item.status}</span><small>${duration(item)}</small></td><td><b>${integer(item.file_count)}</b><small>成功任务 ${integer(item.succeeded)} · 跳过 ${integer(item.skipped)}</small></td><td><b class="${item.failed ? 'accounting-failed' : ''}">${integer(item.failed)}</b><small>测试成功 ${integer(item.test_succeeded)}</small></td><td title="${integer(item.file_bytes_exact)} 字节"><b>${gb(item.file_bytes)}</b><small>${integer(item.file_bytes_exact)} 字节</small></td><td><b>${gb(item.test_file_bytes)}</b><small>测试 ${integer(item.test_file_count)} 个视频 · ${integer(item.test_file_bytes_exact)} 字节 · 不计费</small></td><td><b>${money(item)}</b><small>${escapeHTML(item.price)} 元 / ${item.unit}</small></td></tr>`).join('');
    $('#statistics-empty').hidden = !!data.items.length;
    if (!data.items.length) $('#statistics-empty').innerHTML = emptyState('还没有下载会话', '开始下载后自动记录；先设置单价，完成后即可查看本次交付大小和费用。');
    stats.next = data.next_cursor;
    $('#statistics-prev').disabled = !stats.history.length;
    $('#statistics-next').disabled = !stats.next;
    $('#statistics-page-label').textContent = `第 ${stats.history.length + 1} 页`;
  }

  function renderDetail(data) {
    const item = data.session;
    stats.detail = item;
    $('#statistics-detail').hidden = false;
    $('#statistics-detail-title').textContent = item.label || `下载会话 #${item.seq}`;
    $('#statistics-detail-period').textContent = `${formatDate(item.started_at, true)} 至 ${item.ended_at ? formatDate(item.ended_at, true) : '当前'} · ${sessionStatus[item.status]} · ${duration(item)}`;
    $('#statistics-detail-totals').innerHTML = card('参与任务', integer(item.attempted), `进行中 ${item.running} · 未完成 ${item.unfinished}`)
      + card('成功保存的视频', integer(item.file_count), `成功任务 ${item.succeeded} · 失败任务 ${item.failed}`)
      + card('本次计费大小', `${item.quantity} ${item.unit}`, `${integer(item.file_bytes_exact)} 字节`)
      + card('本次预计费用', money(item), `${item.price} 元 / ${item.unit}`)
      + card('本次测试下载大小', gb(item.test_file_bytes), `${integer(item.test_file_count)} 个视频 · ${integer(item.test_file_bytes_exact)} 字节 · 已删除，不计费`)
      + card('本次完成视频大小', gb(item.downloaded_file_bytes), `交付 + 测试，共 ${integer(item.downloaded_file_count)} 个视频 · ${integer(item.downloaded_file_bytes_exact)} 字节`);
    $('#statistics-exact-bytes').textContent = `计费公式：${integer(item.file_bytes_exact)} ÷ ${item.unit === 'GB' ? '1,000,000,000' : '1,073,741,824'} × ${item.price} = ${item.amount} 元。成功视频按成品文件计数，播放列表中的已完成文件也会保留登记。`;
    renderMethods('#statistics-detail-methods', data.methods);
    $('#statistics-detail-methods-export').href = `/api/statistics/sessions/${item.id}/export?kind=methods`;
    if (!stats.dirty) fill($('#statistics-session-form'), item);
    const files = ['files', 'test_files'].includes(stats.kind);
    const testing = stats.kind === 'test_files';
    $('#statistics-detail-head').innerHTML = files ? '<tr><th>视频 / 文件</th><th>成品大小</th><th>登记时间</th><th>任务</th></tr>' : '<tr><th>视频 ID</th><th>结果</th><th>时间 / 原因</th><th>任务</th></tr>';
    $('#statistics-detail-rows').innerHTML = data.items.map(row => files
      ? `<tr><td><strong>${escapeHTML(row.title)}</strong><small>${escapeHTML(row.video_id || '—')}${testing ? ' · 测试文件已自动删除，不计费' : ''}</small>${testing ? '' : `<small class="accounting-path">${escapeHTML(row.path)}</small>`}</td><td>${gb(row.size)}<small>${integer(row.size_exact || row.size)} 字节</small></td><td>${escapeHTML(formatDate(row.created_at, true))}</td><td><button type="button" class="text-button" data-task="${row.task_id}">查看日志 ↗</button></td></tr>`
      : `<tr><td>${escapeHTML(row.video_id || '播放列表 / 链接任务')}</td><td>${outcome[row.status] || row.status}</td><td>${escapeHTML(formatDate(row.finished_at || row.started_at, true))}<small class="accounting-failed">${escapeHTML(row.error || '')}</small></td><td><button type="button" class="text-button" data-task="${row.task_id}">查看日志 ↗</button></td></tr>`).join('');
    $('#statistics-detail-empty').hidden = !!data.items.length;
    stats.detailNext = data.next_cursor;
    $('#statistics-detail-prev').disabled = !stats.detailHistory.length;
    $('#statistics-detail-next').disabled = !stats.detailNext;
    $('#statistics-detail-page-label').textContent = `第 ${stats.detailHistory.length + 1} 页`;
    $('#statistics-export').href = `/api/statistics/sessions/${item.id}/export?kind=${stats.kind}`;
    $$('[data-statistics-kind]').forEach(button => {
      button.classList.toggle('button-dark', button.dataset.statisticsKind === stats.kind);
      button.classList.toggle('button-white', button.dataset.statisticsKind !== stats.kind);
    });
  }

  async function refreshStatistics() {
    if (stats.busy) { stats.pending = true; return; }
    stats.busy = true;
    const selected = stats.selected;
    const kind = stats.kind, detailCursor = stats.detailCursor, cursor = stats.cursor;
    const query = $('#statistics-search').value.trim();
    try {
      const [overview, details] = await Promise.all([
        api(`/api/statistics?${new URLSearchParams({ q: query, ...(cursor ? { cursor } : {}) })}`),
        selected ? api(`/api/statistics/sessions/${selected}?${new URLSearchParams({ kind, ...(detailCursor ? { cursor: detailCursor } : {}) })}`) : Promise.resolve(null)
      ]);
      if (stats.cursor === cursor && $('#statistics-search').value.trim() === query) renderOverview(overview);
      if (details && stats.selected === selected && stats.kind === kind && stats.detailCursor === detailCursor) renderDetail(details);
      $('#statistics-error').hidden = true;
    } catch (error) {
      $('#statistics-error').textContent = error.message;
      $('#statistics-error').hidden = false;
    } finally {
      stats.busy = false;
      if (stats.pending) { stats.pending = false; refreshStatistics(); }
    }
  }

  document.addEventListener('viewchange', event => { if (event.detail === 'statistics') refreshStatistics(); });
  $('#statistics-refresh').addEventListener('click', refreshStatistics);
  let searchTimer;
  $('#statistics-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      stats.cursor = null; stats.history = []; stats.selected = null; stats.dirty = false;
      $('#statistics-detail').hidden = true; refreshStatistics();
    }, 300);
  });
  $('#statistics-pricing-form').addEventListener('input', () => { stats.pricingDirty = true; });
  $('#statistics-pricing-form').addEventListener('submit', event => {
    event.preventDefault();
    runButton($('button[type="submit"]', event.target), async () => {
      const result = await api('/api/statistics/pricing', { method: 'PUT', body: Object.fromEntries(new FormData(event.target)) });
      stats.pricingDirty = false; toast(result.message); await refreshStatistics();
    });
  });
  $('#statistics-session-form').addEventListener('input', () => { stats.dirty = true; });
  $('#statistics-session-form').addEventListener('submit', event => {
    event.preventDefault();
    runButton($('button[type="submit"]', event.target), async () => {
      const result = await api(`/api/statistics/sessions/${stats.selected}`, { method: 'PUT', body: Object.fromEntries(new FormData(event.target)) });
      stats.dirty = false; toast(result.message); await refreshStatistics();
    });
  });
  $('#statistics-close-detail').addEventListener('click', () => { stats.selected = null; stats.dirty = false; $('#statistics-detail').hidden = true; });
  for (const detail of [false, true]) for (const direction of ['prev', 'next']) {
    $(`#statistics-${detail ? 'detail-' : ''}${direction}`).addEventListener('click', () => {
      const history = detail ? stats.detailHistory : stats.history;
      const cursor = detail ? 'detailCursor' : 'cursor';
      if (direction === 'next') { history.push(stats[cursor]); stats[cursor] = detail ? stats.detailNext : stats.next; }
      else stats[cursor] = history.pop() || null;
      refreshStatistics();
    });
  }
  document.addEventListener('click', async event => {
    const selected = event.target.closest('[data-statistics-session]');
    const tab = event.target.closest('[data-statistics-kind]');
    if (!selected && !tab) return;
    if (selected) { stats.selected = selected.dataset.statisticsSession; stats.dirty = false; }
    if (tab) stats.kind = tab.dataset.statisticsKind;
    stats.detailCursor = null; stats.detailHistory = [];
    await refreshStatistics();
    if (selected && !$('#statistics-detail').hidden) $('#statistics-detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  setInterval(() => { if (visible() && !document.hidden) refreshStatistics(); }, 5000);
  if (visible()) refreshStatistics();
})();
