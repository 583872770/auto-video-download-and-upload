/* Customer ledgers span multiple start/pause sessions. Preserve open editors. */
(() => {
  const ledger = { cursor: null, history: [], next: null, busy: false, pending: false, rows: [], dirty: new Set() };
  const visible = () => !$('#statistics-view').hidden;
  const integer = value => BigInt(String(value || 0)).toLocaleString('zh-CN');
  const metric = (label, value, note = '') => `<div><span>${label}</span><strong>${escapeHTML(value)}</strong><small>${escapeHTML(note)}</small></div>`;
  const formMarkup = row => `<details class="list-editor"><summary>客户、名称与整单单价</summary><form data-list-form="${row.id}" class="accounting-form"><label>客户<input name="customer" maxlength="200"></label><label>清单名称<input name="name" maxlength="200"></label><label>单价（元 / 单位）<input name="price" type="number" min="0" max="1000000" step="0.000001" required></label><label>计费单位<select name="unit"><option value="GB">GB（10⁹ 字节）</option><option value="GiB">GiB（2³⁰ 字节）</option></select></label><button class="button button-dark" type="submit">保存整单设置</button><p class="micro-copy" data-list-save>按整张清单的成品字节计算，不随暂停重新计费。</p></form></details>`;
  function renderRow(element, row) {
    $('[data-list-title]', element).textContent = row.name;
    $('[data-list-meta]', element).textContent = `${row.customer || '未填写客户'} · ${formatDate(row.created_at, true)} · ${row.total} 个视频任务 · ${row.id.slice(0, 8)}`;
    $('[data-list-status]', element).textContent = !row.released && row.queued ? '待启动' : row.running ? '执行中' : row.queued ? '待处理' : row.failed ? '含失败任务' : '已处理';
    $('[data-list-totals]', element).innerHTML = metric('待处理 / 执行中', `${integer(row.queued)} / ${integer(row.running)}`)
      + metric('成功 / 失败', `${integer(row.succeeded)} / ${integer(row.failed)}`, `测试成功 ${integer(row.test_succeeded)} · 跳过 ${integer(row.skipped)}`)
      + metric('计费成品大小', row.tracked ? decimalGB(row.file_bytes) : '未补算', row.tracked ? `${integer(row.file_bytes_exact)} 字节 · ${integer(row.file_count)} 个文件` : '历史清单无完整独立账本')
      + metric('累计测试大小', row.tracked ? decimalGB(row.test_file_bytes) : '未补算', row.tracked ? `${integer(row.test_file_bytes_exact)} 字节 · 不计费` : '请查看原会话统计')
      + metric('整单预计费用', row.tracked ? Number(row.price) ? `¥ ${row.amount}` : '未设置单价' : '—', row.tracked ? `${row.price} 元 / ${row.unit} · 与会话费用不可叠加` : '请查看原会话账单');
    $('[data-list-import]', element).textContent = row.processed === null ? '' : `文件导入${row.import_finished ? '已完成' : '处理中'}：读取 ${integer(row.processed)} 行，新增 ${integer(row.added)}，重复 ${integer(row.duplicates)}，无效 ${integer(row.invalid)}。`;
    if (row.tracked && !ledger.dirty.has(row.id)) {
      const form = $('form', element);
      for (const name of ['customer', 'name', 'price', 'unit']) form.elements.namedItem(name).value = row[name] ?? '';
    }
  }
  function render(data) {
    ledger.rows = data.items;
    const container = $('#customer-lists');
    const ids = data.items.map(row => row.id).join();
    if (container.dataset.ids !== ids && ledger.dirty.size) return;
    if (container.dataset.ids !== ids) {
      container.dataset.ids = ids;
      ledger.dirty.clear();
      container.innerHTML = data.items.length ? data.items.map(row => `<article class="customer-list" data-customer-list="${row.id}"><div class="customer-list-heading"><div><h3 data-list-title></h3><p data-list-meta></p></div><span class="count-pill" data-list-status></span></div><div class="list-totals" data-list-totals></div><p class="micro-copy" data-list-import></p><div class="list-exports"><a class="text-button" href="/api/statistics/lists/${row.id}/files/export" download>导出交付文件 CSV ↗</a><a class="text-button" href="/api/statistics/lists/${row.id}/files/export?kind=test" download>导出测试文件 CSV ↗</a></div>${row.tracked ? formMarkup(row) : '<p class="micro-copy">升级前清单保留任务记录；独立账本从新清单开始累计。</p>'}</article>`).join('') : emptyState('还没有客户清单', '添加清单时填写客户和名称，开始下载后按成品大小累计。');
    }
    for (const row of data.items) renderRow($(`[data-customer-list="${row.id}"]`, container), row);
    ledger.next = data.next_cursor;
    $('#lists-prev').disabled = !ledger.history.length;
    $('#lists-next').disabled = !ledger.next;
    $('#lists-page-label').textContent = `第 ${ledger.history.length + 1} 页`;
  }
  async function refreshLists() {
    if (ledger.busy) { ledger.pending = true; return; }
    ledger.busy = true;
    const cursor = ledger.cursor, query = $('#lists-search').value.trim();
    const params = new URLSearchParams({ q: query, ...(cursor ? { cursor } : {}) });
    $('#lists-export').href = `/api/statistics/lists/export?${new URLSearchParams({ q: query })}`;
    try {
      const data = await api(`/api/statistics/lists?${params}`);
      if (ledger.cursor === cursor && $('#lists-search').value.trim() === query) render(data);
      $('#lists-error').hidden = true;
    } catch (error) {
      $('#lists-error').textContent = readableError(error.message);
      $('#lists-error').hidden = false;
    } finally {
      ledger.busy = false;
      if (ledger.pending) { ledger.pending = false; refreshLists(); }
    }
  }
  $('#customer-lists').addEventListener('input', event => {
    const form = event.target.closest('[data-list-form]');
    if (form) { ledger.dirty.add(form.dataset.listForm); $('[data-list-save]', form).textContent = '有未保存修改，请点击保存整单设置。'; }
  });
  $('#customer-lists').addEventListener('submit', event => {
    const form = event.target.closest('[data-list-form]');
    if (!form) return;
    event.preventDefault();
    runButton($('button[type="submit"]', form), async () => {
      const result = await api(`/api/statistics/lists/${form.dataset.listForm}`, { method: 'PUT', body: Object.fromEntries(new FormData(form)) });
      ledger.dirty.delete(form.dataset.listForm);
      $('[data-list-save]', form).textContent = '已保存，费用按整张清单累计。';
      toast(result.message);
      await refreshLists();
    });
  });
  for (const direction of ['prev', 'next']) $('#lists-' + direction).addEventListener('click', () => {
    ledger.dirty.clear();
    if (direction === 'next') { ledger.history.push(ledger.cursor); ledger.cursor = ledger.next; }
    else ledger.cursor = ledger.history.pop() || null;
    refreshLists();
  });
  let searchTimer;
  $('#lists-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { ledger.dirty.clear(); ledger.cursor = null; ledger.history = []; refreshLists(); }, 300);
  });
  document.addEventListener('viewchange', event => { if (event.detail === 'statistics') refreshLists(); });
  $('#statistics-refresh').addEventListener('click', refreshLists);
  setInterval(() => { if (visible() && !document.hidden) refreshLists(); }, 5000);
  if (visible()) refreshLists();
})();
