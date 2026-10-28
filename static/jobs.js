'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = { empty:'空任务', paused:'待开始 / 已暂停', waiting:'等待执行条件', queued:'排队中', running:'执行中', draining:'暂停中', importing:'导入中', completed:'已完成', partial:'完成但有失败', archived:'已归档', deleted:'已删除' };
  const videoLabels = { queued:'待处理', running:'执行中', succeeded:'处理成功', failed:'失败', interrupted:'已中断', removed:'已移除' };
  const state = { id:null, job:null, tab:'overview', jobs:[], rows:[], cursor:null, history:[], next:null, videoCursor:null, videoHistory:[], videoNext:null, videoStatus:'', videoQuery:'', selected:new Set(), mode:'new', editorJob:null, page:1, epoch:0, busy:false, summary:null };
  const size = value => `${(Number(value || 0) / 1e9).toLocaleString('zh-CN',{minimumFractionDigits:3,maximumFractionDigits:6})} GB`;
  const money = j => j.amount === null ? '待设置单价' : `¥ ${j.amount}`;
  const metric = (label, value, note='') => `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
  const badge = j => `<span class="badge status-${esc(j.state)}">${esc(labels[j.state] || j.state)}</span>`;
  function taskRows(items) {
    return items.map(j => `<tr>
      <td><button class="open-task" data-open="${j.id}">${esc(j.name || '未命名任务')}</button><small>${esc(j.customer)} · ${j.id.slice(0,8)}</small></td>
      <td>${badge(j)}</td>
      <td><div class="table-progress"><progress max="100" value="${Number(j.progress_percent)||0}" aria-label="${esc(j.name)}处理完成率"></progress><span>${j.progress_percent===null?'—':esc(j.progress_percent)+'%'}</span></div><small>${j.completed} / ${j.total} 已处理</small></td>
      <td><span class="result-success">${j.succeeded} 成功</span><span class="result-divider">/</span><button class="failure-link" data-open="${j.id}" data-failed="true" ${j.failed?'':'disabled'}>${j.failed} 失败</button></td>
      <td>${size(j.file_bytes_exact)}<small>${j.file_count} 个成品</small></td>
      <td class="amount-cell">${money(j)}<small>${j.price_set?esc(j.price)+' 元 / '+esc(j.unit):'请设置任务单价'}</small></td>
      <td><div class="row-actions"><button data-open="${j.id}">查看任务</button><a href="/?job=${j.id}#overview" class="secondary-link">前往下载 ↗</a><button class="danger" data-action="delete" data-job-id="${j.id}" aria-label="删除任务：${esc(j.name)}">删除</button></div></td>
    </tr>`).join('');
  }
  let toastTimer, searchTimer, refreshAbort;
  function toast(text) { $('toast').textContent=text; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,5000); }
  function error(id, text) { $(id).textContent=text || ''; $(id).hidden=!text; }
  function renderSystemNote() {
    const system=state.system, note=$('system-note');
    const blockers=(system?.blockers||[]).filter(item=>!['system_paused','jobs_not_started','no_pending_videos'].includes(item.code));
    note.hidden=!blockers.length;
    note.replaceChildren();
    if (!blockers.length) return;
    note.append(document.createTextNode(blockers.map(item=>item.message).join(' ')));
    const link=document.createElement('a');link.href=state.id?`/?job=${state.id}#overview`:'/#overview';link.textContent='选择任务并开始下载 ↗';note.append(document.createTextNode(' '),link);
    for(const blocker of blockers){if(!blocker.href)continue;const link=document.createElement('a');link.href=blocker.href;link.textContent=blocker.label;note.append(document.createTextNode(' '),link);}
  }
  function renderCenterSummary(summary) {
    $('center-cards').innerHTML=metric('有效任务',summary.total,'未归档的客户任务')+metric('执行中任务',summary.running,'按下载实例独立执行')+metric('累计交付',size(summary.file_bytes_exact),'含归档与删除任务的历史交付')+metric('预计费用合计',`¥ ${summary.amount}`,`单价待设置 ${summary.unset} 单`);
  }
  async function api(path, options={}) {
    const headers = { 'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content };
    if (options.body && !(options.body instanceof FormData)) { headers['Content-Type']='application/json'; options.body=JSON.stringify(options.body); }
    const response=await fetch(path,{...options,headers});
    const data=await response.json();
    if (response.status===401) { location.assign('/login'); throw new Error('请先登录管理账户。'); }
    if (!response.ok) { const error=new Error(data.error || `请求失败（${response.status}）`);error.status=response.status;throw error; }
    return data;
  }
  const endpoint = id => `/api/jobs/${id}`;
  async function showConflict(form) {
    try {
      const latest=await api(endpoint(state.id));
      form.querySelector('[data-conflict]')?.remove();
      const panel=document.createElement('div');panel.className='notice small-gap';panel.dataset.conflict='';
      const text=document.createElement('p');
      text.textContent=`服务器最新资料：客户 ${latest.customer}，名称 ${latest.name}，单价 ${latest.price_set?latest.price+' 元 / '+latest.unit:'待定'}，备注 ${latest.note||'无'}。下方仍保留你的输入。请核对后再保存。`;
      const button=document.createElement('button');button.type='button';button.textContent='已核对，保留当前输入继续编辑';
      button.onclick=()=>{form.dataset.version=latest.edit_version;panel.remove();error('settings-error','已采用最新编辑版本；请确认输入后再次保存。')};
      panel.append(text,button);form.prepend(panel);
    } catch(error) { toast(error.message); }
  }
  function context(j) {
    $('detail-heading').innerHTML=`<div class="heading detail-title"><div><div class="detail-customer">${esc(j.customer)} ${badge(j)}</div><h1>${esc(j.name || '未命名任务')}</h1></div><div class="task-actions"><button class="primary" data-action="append" ${j.archived?'disabled':''}>＋ 添加视频</button><a class="primary" href="/?job=${j.id}#overview">前往下载 ↗</a></div></div>`;
    $('task-context').textContent=`任务编号 ${j.id}${j.note?' · '+j.note:''}`;
    document.querySelectorAll('[data-tab]').forEach(b=>{ b.classList.toggle('active',b.dataset.tab===state.tab); b.setAttribute('aria-selected',String(b.dataset.tab===state.tab)); });
  }
  function totals(j) {
    return metric('当前有效视频',j.total,`累计移除 ${j.removed} 个条目`)+metric('处理完成率',j.progress_percent===null?'—':`${j.progress_percent}%`,`处理成功 ${j.succeeded} · 最终失败 ${j.failed}`)+metric('累计交付',size(j.file_bytes_exact),`${j.file_count} 个成品文件 · 历史累计`)+metric('本任务预计费用',money(j),j.price_set?`${j.price} 元 / ${j.unit}`:'单价与免费任务分别设置');
  }
  function overview(j) {
    const pct=j.progress_percent || 0;
    const processed=j.succeeded+j.failed;
    const rate=processed?`${(100*j.succeeded/processed).toFixed(1)}%`:'—';
    $('detail-content').innerHTML=`<div class="cards">${totals(j)}</div>
      <div class="split job-overview-grid"><div class="panel"><div class="section-title"><h2>视频处理进度</h2><button class="secondary-link" data-switch="videos">管理视频 →</button></div>
        <progress max="100" value="${pct}" aria-label="处理完成率">${pct}%</progress>
        <div class="statelist"><span>待处理<strong>${j.queued}</strong></span><span>执行中<strong>${j.running}</strong></span><span>处理成功<strong class="result-success">${j.succeeded}</strong></span><span>最终失败<strong class="result-failed">${j.failed}</strong></span></div>
        <div class="success-line"><span>已处理视频成功率 <strong>${rate}</strong></span><small>完成率包含最终失败</small></div>
        ${j.waiting_reason?`<p class="notice">${esc(j.waiting_reason)}</p>`:''}
        <p class="muted subtle">${j.total?'正式交付、测试与跳过，以账本明细为准。':'当前清单为空；历史交付与费用保留。'}</p>
      </div><div class="panel"><div class="section-title"><h2>当前执行</h2><span class="speed-label">${(j.transfer_speed/1e6).toFixed(2)} MB/s</span></div>
        <div class="running-video-list">${j.running_videos.length?j.running_videos.map(v=>`<div class="details-line"><span><strong>${esc(v.video_id)}</strong><small>${esc(v.progress?.stage || '准备中')} · ${esc(v.instance_id || '本机')}</small></span><span>${v.progress?.percent==null?'—':esc(v.progress.percent)+'%'}</span></div>`).join(''):'<div class="compact-empty">当前没有正在执行的视频<span>在下载工作台选择此任务后开始。</span></div>'}</div>
      </div><div class="panel"><div class="section-title"><h2>交付与计费</h2><button class="secondary-link" data-switch="billing">查看账单 →</button></div>
        <div class="details-line"><span>交付成品</span><span>${j.file_count} 个 · ${size(j.file_bytes_exact)}</span></div>
        <div class="details-line"><span>测试成功 · 不计费</span><span>${j.test_succeeded} 个 · ${size(j.test_file_bytes_exact)}</span></div>
        <div class="details-line"><span>历史跳过</span><span>${j.skipped} 个视频</span></div>
      </div><div class="panel"><div class="section-title"><h2>导入记录</h2><span class="muted subtle">${j.imports.length} 份文件</span></div><div class="import-records">${importRows(j)}</div></div></div>`;
  }
  function importRows(j) {
    return j.imports.length?j.imports.map(r=>`<div class="details-line"><span>${esc(r.name)} · ${r.finished<0?'已取消':r.finished?'已完成':'导入中'}</span><span>新增 ${r.added} · 重复 ${r.duplicates} · 无效 ${r.invalid}</span></div>${r.invalid?`<p class="muted">${esc(JSON.parse(r.samples || '[]').join('；'))}</p>`:''}`).join(''):'<p class="muted">尚无文件导入记录，可在视频清单中追加 TXT / CSV。</p>';
  }
  function videos(j, data) {
    state.videoNext=data.next_cursor;
    $('detail-content').innerHTML=`<div class="panel"><div class="heading"><div><h2>视频清单 · ${j.total} 个有效视频</h2><p>同任务内去重；不同任务独立处理。</p></div><div class="tools"><button data-action="retry" ${!j.failed||j.archived?'disabled':''}>重试失败</button><button data-action="remove-selected" ${j.archived?'disabled':''}>移除选中</button><button class="danger" data-action="clear" ${j.archived?'disabled':''}>清空清单</button></div></div><div class="tools"><input class="search" id="video-query" aria-label="搜索视频" placeholder="视频 ID 或标题" value="${esc(state.videoQuery)}"><select id="video-filter" class="unit" aria-label="视频状态"><option value="">全部有效视频</option>${Object.entries(videoLabels).map(([key,value])=>`<option value="${key}" ${state.videoStatus===key?'selected':''}>${value}</option>`).join('')}</select><button data-action="filter-videos">查询 / 刷新</button><a href="${endpoint(j.id)}/export">导出全部 ID</a><a href="${endpoint(j.id)}/export?kind=failed">导出失败 ID</a></div><div class="table-wrap"><table><thead><tr><th><input type="checkbox" id="select-page" aria-label="选择本页全部视频"></th><th>视频 ID / 标题</th><th>状态</th><th>执行阶段 / 实例</th><th>错误</th><th>操作</th></tr></thead><tbody>${data.items.length?data.items.map(v=>`<tr><td><input type="checkbox" data-select="${v.id}" aria-label="选择 ${esc(v.video_id)}" ${state.selected.has(v.id)?'checked':''}></td><td><a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.video_id)}</a><small>${esc(v.title)}</small></td><td><span class="badge">${v.result?.test_mode?'测试成功':v.result?.skipped?'已跳过':videoLabels[v.status]||v.status}</span></td><td>${esc(v.progress?.stage || '—')}<small>${esc(v.instance_id || '本机')}</small></td><td title="${esc(v.error)}">${esc((v.error || '').slice(0,60))}</td><td><button data-remove="${v.id}" ${v.status==='removed'||j.archived?'disabled':''}>移出清单</button><button data-log="${v.id}">日志</button></td></tr>`).join(''):`<tr><td colspan="6" class="empty">${!j.total?'该任务还没有视频。点击上方“添加视频”。':'没有符合筛选条件的视频。'}</td></tr>`}</tbody></table></div><div class="dialog-actions"><button data-video-page="prev" ${!state.videoHistory.length?'disabled':''}>上一页</button><span>第 ${state.videoHistory.length+1} 页 · 每页 50 条</span><button data-video-page="next" ${!state.videoNext?'disabled':''}>下一页</button></div></div>`;
    state.rows=data.items;
  }
  function statistics(j, data) {
    const fileRows=rows=>rows.length?rows.map(r=>`<div class="details-line"><span>${esc(r.title || r.video_id)}<small> · ${esc(r.video_id)}</small></span><span>${size(r.size)}</span></div>`).join(''):'<p class="muted empty-line">暂无文件登记。</p>';
    $('detail-content').innerHTML=`<div class="cards">${metric('历史交付成品',j.file_count,'清单移除不冲减历史交付')+metric('计费大小',size(j.file_bytes_exact),`${j.file_bytes_exact} 字节`)+metric('测试大小',size(j.test_file_bytes_exact),'测试文件不计费')+metric('整单预计费用',money(j),j.price_set?`${j.price} 元 / ${j.unit}`:'请在任务设置中填写单价')}</div><div class="panel"><h2>任务账单与导出</h2><p class="notice">按实际成品字节累计；暂停、重试和跨实例接管不另起账单。修改单价会重算整单预计费用；此处显示预估账单。</p><div class="tools"><a href="${endpoint(j.id)}/export?kind=bill">导出任务账单 CSV</a><a href="/api/statistics/lists/${j.id}/files/export">导出交付明细 CSV</a><a href="/api/statistics/lists/${j.id}/files/export?kind=test">导出测试明细 CSV</a></div></div><div class="panel"><h2>下载器执行统计</h2>${data.providers.length?data.providers.map(p=>`<div class="details-line"><span>${esc(p.method)} · ${esc(videoLabels[p.status] || p.status)}</span><span>${p.count} 条执行记录</span></div>`).join(''):'<p class="muted">尚无执行记录。下载器切换记录与视频数量分别统计。</p>'}</div><div class="split"><div class="panel"><h2>最近交付（最多 50 条）</h2>${fileRows(data.files)}</div><div class="panel"><h2>最近测试（最多 50 条）</h2>${fileRows(data.test_files)}</div></div>`;
  }
  function settings(j) {
    $('detail-content').innerHTML=`<div class="panel"><h2>任务信息与计费</h2><form id="settings-form" data-version="${j.edit_version}"><div class="form-grid"><label><span>客户 *</span><input name="customer" required maxlength="200" value="${esc(j.customer)}"></label><label><span>任务名称 *</span><input name="name" required maxlength="200" value="${esc(j.name || '未命名任务')}"></label><label><span>单价（元 / 单位，可留空）</span><input name="price" type="number" min="0" max="1000000" step="0.000001" value="${j.price_set?esc(j.price):''}"></label><label><span>计费单位</span><select name="unit"><option value="GB" ${j.unit==='GB'?'selected':''}>GB（10⁹ 字节）</option><option value="GiB" ${j.unit==='GiB'?'selected':''}>GiB（2³⁰ 字节）</option></select></label></div><label><span>备注</span><textarea name="note" maxlength="2000">${esc(j.note)}</textarea></label><p class="notice">单价或计费单位调整会重算本任务全部交付的预计费用。实际视频、其他任务和历史字节记录不变。</p><p class="notice">下载方式、画质和保存模式由执行实例决定。<a href="/#settings">前往实例设置 ↗</a></p><p id="settings-error" class="form-error" role="alert" hidden></p><button class="primary small-gap" type="submit" ${j.archived?'disabled':''}>保存任务设置</button></form></div><details class="panel task-maintenance"><summary>归档与删除</summary><p class="muted small-gap">归档会停止此任务并取消未完成导入。删除保留历史账单和已下载文件。</p><div class="tools small-gap"><button data-action="${j.archived?'unarchive':'archive'}">${j.archived?'恢复归档任务':'归档此任务'}</button><button class="danger" data-action="delete">删除此任务</button></div></details>`;
  }
  async function refresh() {
    const ticket=++state.epoch, id=state.id, tab=state.tab;
    refreshAbort?.abort(); refreshAbort=new AbortController();
    const opt={signal:refreshAbort.signal};
    try {
      if (!id) {
        const params=new URLSearchParams({q:$('search').value.trim(),lifecycle:$('state-filter').value,...(state.cursor?{cursor:state.cursor}:{})});
        // Render the task rows as soon as their query returns. The summary and
        // system status can scan more data and must not hold the first paint.
        const data=await api('/api/jobs?'+params,opt);
        if (ticket!==state.epoch) return;
        state.next=data.next_cursor;state.jobs=data.items;
        // Placeholders only before the first summary; auto-refresh keeps the last values visible.
        if (!state.summary) $('center-cards').innerHTML=metric('有效任务','读取中…','未归档的客户任务')+metric('执行中任务','读取中…','按下载实例独立执行')+metric('累计交付','读取中…','历史交付')+metric('预计费用合计','读取中…','正在计算费用');
        $('task-list-count').textContent=`本页 ${data.items.length} 项 · 自动更新`;
        $('task-table').innerHTML=data.items.length?`<table class="jobs-table"><thead><tr><th>任务 / 客户</th><th>状态</th><th>处理完成率</th><th>处理结果</th><th>实际交付</th><th>预计费用</th><th>操作</th></tr></thead><tbody>${taskRows(data.items)}</tbody></table>`:'<div class="empty"><strong>从第一个客户任务开始</strong><p>新建任务 → 添加视频清单 → 前往下载 → 核对交付与费用</p><button class="primary" data-action="new">＋ 新建任务</button></div>';
        if (!data.items.length && ($('search').value.trim() || $('state-filter').value==='archived')) $('task-table').innerHTML='<div class="empty"><strong>没有匹配的任务</strong><p>试试其他客户、任务名称或归档状态。</p></div>';
        $('jobs-prev').disabled=!state.history.length; $('jobs-next').disabled=!state.next;
        $('jobs-page-label').textContent=`第 ${state.history.length+1} 页 · 每页 20 条`;
        error('page-error','');
        const [summaryResult,systemResult]=await Promise.allSettled([api('/api/jobs/summary',opt),api('/api/jobs/system/status',opt)]);
        if (ticket!==state.epoch) return;
        if (summaryResult.status==='fulfilled') { state.summary=summaryResult.value;renderCenterSummary(state.summary); }
        else if (summaryResult.reason?.name!=='AbortError') toast(`任务汇总读取失败：${summaryResult.reason.message}`);
        if (systemResult.status==='fulfilled') { state.system=systemResult.value;renderSystemNote(); }
        else if (systemResult.reason?.name!=='AbortError') toast(`系统状态读取失败：${systemResult.reason.message}`);
      } else {
        const params=new URLSearchParams({q:state.videoQuery,...(state.videoStatus?{status:state.videoStatus}:{}),...(state.videoCursor?{cursor:state.videoCursor}:{})});
        const [j,extra,system]=await Promise.all([api(endpoint(id),opt),tab==='videos'?api(endpoint(id)+'/videos?'+params,opt):['billing','history'].includes(tab)?api(endpoint(id)+'/statistics',opt):Promise.resolve(null),api('/api/jobs/system/status',opt)]);
        if (ticket!==state.epoch || id!==state.id || tab!==state.tab) return;
        state.job=j;context(j);state.system=system;renderSystemNote();
        if (tab==='overview')overview(j);
        if (tab==='videos')videos(j,extra);
        if (tab==='billing')statistics(j,extra);
        if (tab==='settings')settings(j);
        if (tab==='history')$('detail-content').innerHTML=`<div class="panel"><h2>任务操作记录（最近 50 条）</h2>${extra.events.length?extra.events.map(e=>`<div class="details-line"><span>${esc(e.message)}</span><small>${esc(e.created_at)}</small></div>`).join(''):'<p class="muted">历史任务没有完整操作记录。</p>'}</div><div class="panel"><h2>导入记录</h2>${importRows(j)}</div>`;
      }
    } catch(e) { if (e.name==='AbortError'||ticket!==state.epoch)return; if(id)toast(e.message);else error('page-error',e.message); }
  }
  function route() { const hash=location.hash.slice(1);state.id=/^[a-f0-9]{32}$/.test(hash)?hash:null;state.tab=state.openTab||'overview';state.openTab=null;state.selected.clear();state.videoCursor=null;state.videoHistory=[];$('center-view').hidden=!!state.id;$('detail-view').hidden=!state.id;if(state.id){state.job=null;$('detail-heading').textContent='正在读取任务…';$('task-context').textContent='';$('detail-content').replaceChildren()}refresh(); }
  function editor(mode) {
    state.mode=mode;state.editorJob=mode==='append'?state.id:null;
    $('editor-form').reset();delete $('editor-form').dataset.createdId;$('editor-form').dataset.requestId=Array.from(crypto.getRandomValues(new Uint8Array(16)), x=>x.toString(16).padStart(2,'0')).join('');error('editor-error','');
    $('basic-fields').hidden=mode==='append';$('video-fields').hidden=mode!=='append';$('video-fields').querySelectorAll('input,textarea').forEach(input=>input.disabled=mode!=='append');for(const name of ['customer','name'])$('editor-form').elements[name].required=mode!=='append';
    $('editor-title').textContent=mode==='append'?'添加视频':'新建任务';
    $('editor-caption').textContent=mode==='append'?`${state.job.customer} / ${state.job.name}`:'先建立任务，再进入任务添加视频。创建后不会自动下载。';
    $('editor').showModal();
  }
  async function mutate(button, action) {
    if(state.busy)return;state.busy=true;button.disabled=true;
    try { await action();state.selected.clear();await refresh(); }
    catch(e){toast(e.name==='TimeoutError'?'操作请求等待超过 90 秒，结果尚未确认，请刷新查看最新状态。':e.message)} finally{state.busy=false;if(button.isConnected)button.disabled=false;if(state.job&&state.id)context(state.job);}
  }
  function confirmTaskAction(action, job) {
    if(!job){toast('任务信息已更新，请刷新后重试。');return}
    const form=$('clear-form'), dialog=$('clear-dialog'), deleting=action==='delete';
    form.reset();form.dataset.action=action;form.dataset.jobId=job.id;form.dataset.jobName=job.name;
    dialog.querySelector('h2').textContent=deleting?'删除任务':'清空当前任务视频清单';
    form.querySelector('select').closest('label').hidden=deleting;
    form.querySelector('[type=submit]').textContent=deleting?'确认删除任务':'确认清空视频';
    dialog.querySelector('.notice').textContent=deleting?'删除会停止此任务并取消未完成导入，历史账单和已下载文件保留。删除后无法在任务中心恢复。':'清空会暂停此任务并取消未完成导入，历史账单和磁盘文件保留。';
    $('confirm-label').textContent=`输入任务名称确认：${job.name}（客户：${job.customer}）`;
    error('clear-error','');dialog.showModal();
  }
  document.addEventListener('click',event=>{
    const center=event.target.closest('[data-nav="center"]');if(center){event.preventDefault();$('sidebar').classList.remove('mobile-open');location.hash='';return}
    const b=event.target.closest('button');if(!b||b.disabled)return;
    if(b.dataset.nav==='center'){location.hash='';return}
    if(b.dataset.open){if(b.dataset.failed){state.openTab='videos';state.videoStatus='failed';state.videoQuery=''}else{state.videoStatus='';state.videoQuery=''}location.hash=b.dataset.open;return}
    if(b.dataset.tab||b.dataset.switch){const next=b.dataset.tab||b.dataset.switch;if(next!==state.tab){state.tab=next;refresh()}return}
    if(b.dataset.videoPage){if(b.dataset.videoPage==='next'){state.videoHistory.push(state.videoCursor);state.videoCursor=state.videoNext}else state.videoCursor=state.videoHistory.pop()||null;state.selected.clear();refresh();return}
    if(b.dataset.log){mutate(b,async()=>{const result=await api(`/api/tasks/${b.dataset.log}/logs?offset=0`);$('log-content').textContent=result.text||result.message||'暂无日志。';$('log-dialog').showModal()});return}
    if(b.dataset.remove){mutate(b,()=>api(endpoint(state.id)+'/videos/remove',{method:'POST',body:{ids:[b.dataset.remove]}}));return}
    const a=b.dataset.action;if(!a)return;
    if(a==='new'){editor('new');return}
    if(a==='append'){editor('append');return}
    if(a==='filter-videos'){state.videoQuery=$('video-query').value.trim();state.videoStatus=$('video-filter').value;state.videoCursor=null;state.videoHistory=[];state.selected.clear();refresh();return}
    if(a==='clear'||a==='delete'){confirmTaskAction(a,b.dataset.jobId?state.jobs?.find(j=>j.id===b.dataset.jobId):state.job);return}
    if(a==='remove-selected'){if(!state.selected.size){toast('请先勾选视频条目');return}mutate(b,()=>api(endpoint(state.id)+'/videos/remove',{method:'POST',body:{ids:[...state.selected]}}));return}
    mutate(b,async()=>{const result=await api(endpoint(state.id)+'/control',{method:'POST',body:{action:a},signal:AbortSignal.timeout(90000)});toast(result.warnings?.length?result.warnings.join('；'):a==='stop'?'停止指令已生效，执行进程将在下次心跳退出；其他任务继续。':({archive:'任务已归档；账单和已下载文件保留。',unarchive:'任务已恢复，请前往下载工作台选择后开始。',retry:'失败视频已重新排队；暂停中的任务需前往工作台开始。'}[a]||'任务操作已保存。'))});
  });
  document.addEventListener('change',e=>{if(e.target.dataset.select){e.target.checked?state.selected.add(e.target.dataset.select):state.selected.delete(e.target.dataset.select)}if(e.target.id==='select-page'){for(const v of state.rows)e.target.checked?state.selected.add(v.id):state.selected.delete(v.id);document.querySelectorAll('[data-select]').forEach(c=>c.checked=e.target.checked)}});
  $('jobs-mobile-menu').onclick=()=>$('sidebar').classList.toggle('mobile-open');
  $('new-task').onclick=()=>editor('new');$('back').onclick=()=>location.hash='';
  $('cancel-editor').onclick=()=>$('editor').close();$('cancel-clear').onclick=()=>$('clear-dialog').close();
  $('search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.cursor=null;state.history=[];refresh()},300)};
  $('state-filter').onchange=()=>{state.cursor=null;state.history=[];refresh()};
  $('jobs-next').onclick=()=>{state.history.push(state.cursor);state.cursor=state.next;refresh()};$('jobs-prev').onclick=()=>{state.cursor=state.history.pop()||null;refresh()};
  $('editor-form').onsubmit=async e=>{
    e.preventDefault();const form=e.target,button=form.querySelector('[type=submit]');if(button.disabled)return;button.disabled=true;error('editor-error','');
    const f=new FormData(form),file=f.get('file')||{size:0};let summary='';
    try{
      if(state.mode==='append'&&!String(f.get('ids')||'').trim()&&!file.size)throw new Error('请粘贴视频 ID 或选择导入文件。');
      if(file.size>1024**3)throw new Error('导入文件最大 1 GB。');
      let id=state.editorJob||form.dataset.createdId;
      if(!id){const created=await api('/api/jobs',{method:'POST',body:{request_id:form.dataset.requestId,customer:f.get('customer'),name:f.get('name'),note:f.get('note'),...(f.get('price')!==''?{price:f.get('price')}:{})}});id=created.id;form.dataset.createdId=id;}
      else if(String(f.get('ids')||'').trim()&&state.mode==='append'){const result=await api(endpoint(id)+'/videos',{method:'POST',body:{urls:f.get('ids')}});summary=`新增 ${result.added}，恢复 ${result.restored}，重复 ${result.duplicates}。`;}
      if(file.size){const upload=new FormData();upload.set('file',file);await api(endpoint(id)+'/imports',{method:'POST',body:upload})}
      $('editor').close();toast(summary || (state.mode==='new'?'任务已创建，请添加视频。':'已提交到当前任务。'));if(location.hash==='#'+id){state.tab='videos';refresh()}else {state.openTab='videos';location.hash=id;}
    }catch(e){error('editor-error',e.message)}finally{button.disabled=false}
  };
  $('clear-form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('[type=submit]');if(b.disabled)return;b.disabled=true;const f=new FormData(e.target),clearId=e.target.dataset.jobId;try{if(f.get('confirmation')!==e.target.dataset.jobName)throw new Error('请输入完整任务名称确认。');if(e.target.dataset.action==='delete'){await api(endpoint(clearId)+'/control',{method:'POST',body:{action:'delete',confirmation:f.get('confirmation')}});$('clear-dialog').close();state.cursor=null;state.history=[];state.selected.clear();state.job=null;if(location.hash)location.hash='';else await refresh();toast('任务已删除；历史账单和已下载文件保留。')}else{const result=await api(endpoint(clearId)+'/videos/remove',{method:'POST',body:{clear:true,scope:f.get('scope'),confirmation:f.get('confirmation')}});$('clear-dialog').close();state.videoCursor=null;state.videoHistory=[];state.selected.clear();await refresh();toast(`已移除 ${result.removed} 个视频；历史账本保留。`)}}catch(e){error('clear-error',e.message)}finally{b.disabled=false}};
  $('detail-content').addEventListener('submit',async e=>{if(e.target.id!=='settings-form')return;e.preventDefault();const b=e.target.querySelector('[type=submit]'),f=new FormData(e.target);b.disabled=true;error('settings-error','');try{await api(endpoint(state.id),{method:'PUT',body:{edit_version:Number(e.target.dataset.version),customer:f.get('customer'),name:f.get('name'),note:f.get('note'),price:f.get('price'),unit:f.get('unit')}});toast('任务信息和整单预计费用已更新。');await refresh()}catch(e){error('settings-error',e.message);if(e.status===409)await showConflict(b.form)}finally{if(b.isConnected)b.disabled=false}});
  window.addEventListener('hashchange',route);
  setInterval(()=>{if(!document.hidden&&!state.busy&&!$('editor').open&&!$('clear-dialog').open&&(!state.id||['overview','history','billing'].includes(state.tab)))refresh()},5000);
  state.openTab=new URLSearchParams(location.search).get('tab')==='settings'?'settings':null;
  route();
})();
