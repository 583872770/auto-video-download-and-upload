'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = { empty:'空任务', paused:'待开始 / 已暂停', waiting:'等待执行条件', queued:'排队中', running:'执行中', draining:'暂停中', importing:'导入中', completed:'已完成', partial:'完成但有失败', archived:'已归档', deleted:'已删除' };
  const videoLabels = { queued:'待处理', running:'执行中', succeeded:'处理成功', failed:'失败', interrupted:'已中断', removed:'已移除' };
  const state = { id:null, job:null, tab:'overview', rows:[], cursor:null, history:[], next:null, videoCursor:null, videoHistory:[], videoNext:null, videoStatus:'', videoQuery:'', selected:new Set(), mode:'new', editorJob:null, page:1, epoch:0, busy:false };
  const size = value => `${(Number(value || 0) / 1e9).toLocaleString('zh-CN',{minimumFractionDigits:3,maximumFractionDigits:6})} GB`;
  const money = j => j.amount === null ? '待设置单价' : `¥ ${j.amount}`;
  const metric = (label, value, note='') => `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
  let toastTimer, searchTimer, refreshAbort;
  function toast(text) { $('toast').textContent=text; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,5000); }
  function error(id, text) { $(id).textContent=text || ''; $(id).hidden=!text; }
  function renderSystemNote() {
    const system=state.system, note=$('system-note');
    note.hidden=!state.resuming&&!state.resumeError&&(!system||(!system.paused&&!system.blockers.length));
    note.replaceChildren();
    note.append(document.createTextNode(state.resuming?'正在恢复系统领取，请稍候；单机模式会检查下载环境与 YouTube Cookie。':system?.message||''));
    if(state.resumeError)note.append(document.createTextNode(' '+state.resumeError));
    if(system?.paused||state.resuming){const button=document.createElement('button');button.dataset.systemResume='';button.textContent=state.resuming?'正在恢复…':'恢复系统领取';button.disabled=!!state.resuming;button.setAttribute('aria-busy',String(!!state.resuming));note.append(document.createTextNode(' '),button);}
    if(!state.resuming)for(const blocker of system?.blockers||[]){if(!blocker.href)continue;const link=document.createElement('a');link.href=blocker.href;link.textContent=blocker.label;note.append(document.createTextNode(' '),link);}
  }
  async function resumeSystem() {
    if(state.busy)return;
    state.busy=true;state.resuming=true;state.resumeError='';
    refreshAbort?.abort();++state.epoch;renderSystemNote();
    try{const result=await api('/api/jobs/system/resume',{method:'POST',signal:AbortSignal.timeout(90000)});state.system=result.execution;toast(result.message);}
    catch(e){state.resumeError=e.name==='TimeoutError'?'恢复请求等待超过 90 秒，结果尚未确认，请以下方最新状态为准。':e.message;toast(state.resumeError);}
    finally{state.resuming=false;state.busy=false;renderSystemNote();}
    await refresh();
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
      const policy=latest.policy||{},names={yt_dlp:'yt-dlp',yt5s:'yt5s',downmp3:'downmp3'};
      const methods=(policy.download_methods||[policy.download_method||'yt_dlp']).map(method=>names[method]||method).join(' → ');
      text.textContent=`服务器最新资料：客户 ${latest.customer}，名称 ${latest.name}，单价 ${latest.price_set?latest.price+' 元 / '+latest.unit:'待定'}，备注 ${latest.note||'无'}。下载顺序 ${methods}，downmp3 ${policy.downmp3_quality||'720'}p，yt5s ${policy.yt5s_quality||'360'}p，${policy.storage_mode==='test'?'测试模式':'正式保存'}。下方仍保留你的输入。请逐项核对并修改后，再确认使用当前输入。`;
      const button=document.createElement('button');button.type='button';button.textContent='已核对，保留当前输入继续编辑';
      button.onclick=()=>{form.dataset.version=latest.edit_version;panel.remove();error('settings-error','已采用最新编辑版本；请确认输入后再次保存。')};
      panel.append(text,button);form.prepend(panel);
    } catch(error) { toast(error.message); }
  }
  function context(j) {
    $('detail-heading').innerHTML=`<div class="heading"><div><h1>${esc(j.name || '未命名任务')}</h1><p>${esc(j.customer)} · ${esc(j.id.slice(0,12))} · <span class="badge">${labels[j.state]}</span></p></div><div class="task-actions"><button class="primary" data-action="start" ${j.archived || (!j.queued&&!j.importing) ? 'disabled':''}>开始 / 继续</button><button data-action="pause" ${j.archived||!j.released?'disabled':''}>完成当前后暂停</button><button data-action="stop" ${j.archived||(!j.released&&!j.running)?'disabled':''}>立即停止</button><button data-action="${j.archived?'unarchive':'archive'}">${j.archived?'恢复任务':'归档'}</button><button class="danger" data-action="delete">删除任务</button></div></div>`;
    $('task-context').textContent=`当前任务：${j.customer} / ${j.name} · ${j.waiting_reason || '本页概览、视频、统计与账单均独立。'}`;
    document.querySelectorAll('[data-tab]').forEach(b=>{ b.classList.toggle('active',b.dataset.tab===state.tab); b.setAttribute('aria-selected',String(b.dataset.tab===state.tab)); });
  }
  function totals(j) {
    return metric('当前有效视频',j.total,`累计移除 ${j.removed} 个条目`)+metric('处理完成率',j.progress_percent===null?'—':`${j.progress_percent}%`,`处理成功 ${j.succeeded} · 最终失败 ${j.failed}`)+metric('累计交付',size(j.file_bytes_exact),`${j.file_count} 个成品文件 · 历史累计`)+metric('本任务预计费用',money(j),j.price_set?`${j.price} 元 / ${j.unit}`:'单价与免费任务分别设置');
  }
  function overview(j) {
    const pct=j.progress_percent || 0;
    $('detail-content').innerHTML=`<div class="cards">${totals(j)}</div><div class="split"><div class="panel"><h2>视频处理进度</h2><progress max="100" value="${pct}" aria-label="处理完成率">${pct}%</progress><div class="statelist"><span>待处理<strong>${j.queued}</strong></span><span>执行中<strong>${j.running}</strong></span><span>处理成功<strong>${j.succeeded}</strong></span><span>失败<strong>${j.failed}</strong></span></div><p class="muted small-gap">${j.total?'处理完成率包含最终失败。正式交付、测试与跳过请以账本明细为准。':'当前清单为空；历史交付与费用仍保留。'}</p><button class="small-gap" data-switch="videos">管理本任务视频 →</button></div><div class="panel"><h2>交付与计费</h2><div class="details-line"><span>交付成品</span><span>${j.file_count} 个 · ${size(j.file_bytes_exact)}</span></div><div class="details-line"><span>测试成功</span><span>${j.test_succeeded} 个视频 · ${size(j.test_file_bytes_exact)}（不计费）</span></div><div class="details-line"><span>历史跳过</span><span>${j.skipped} 个视频</span></div><div class="details-line"><span>预计费用</span><strong>${money(j)}</strong></div></div></div><div class="panel"><h2>当前执行 · ${(j.transfer_speed/1e6).toFixed(2)} MB/s</h2>${j.running_videos.length?j.running_videos.map(v=>`<div class="details-line"><span>${esc(v.video_id)} · ${esc(v.progress?.stage || '准备中')}</span><span>${v.progress?.percent===null||v.progress?.percent===undefined?'—':esc(v.progress.percent)+'%'} · ${esc(v.instance_id || '本机')}</span></div>`).join(''):'<p class="muted">当前没有正在执行的视频。</p>'}</div><div class="panel"><h2>导入记录</h2>${importRows(j)}</div>`;
  }
  function importRows(j) {
    return j.imports.length?j.imports.map(r=>`<div class="details-line"><span>${esc(r.name)} · ${r.finished<0?'已取消':r.finished?'已完成':'导入中'}</span><span>新增 ${r.added} · 重复 ${r.duplicates} · 无效 ${r.invalid}</span></div>${r.invalid?`<p class="muted">${esc(JSON.parse(r.samples || '[]').join('；'))}</p>`:''}`).join(''):'<p class="muted">尚无文件导入记录，可在视频清单中追加 TXT / CSV。</p>';
  }
  function videos(j, data) {
    state.videoNext=data.next_cursor;
    $('detail-content').innerHTML=`<div class="panel"><div class="heading"><div><h2>视频清单 · ${j.total} 个有效视频</h2><p>同任务内去重；不同任务独立处理。</p></div><div class="tools"><button class="primary" data-action="append" ${j.archived?'disabled':''}>＋ 追加视频 / 文件</button><button data-action="retry" ${!j.failed||j.archived?'disabled':''}>重试失败</button><button data-action="remove-selected" ${j.archived?'disabled':''}>移除选中</button><button class="danger" data-action="clear" ${j.archived?'disabled':''}>清空清单</button></div></div><div class="tools"><input class="search" id="video-query" aria-label="搜索视频" placeholder="视频 ID 或标题" value="${esc(state.videoQuery)}"><select id="video-filter" class="unit" aria-label="视频状态"><option value="">全部有效视频</option>${Object.entries(videoLabels).map(([key,value])=>`<option value="${key}" ${state.videoStatus===key?'selected':''}>${value}</option>`).join('')}</select><button data-action="filter-videos">查询 / 刷新</button><a href="${endpoint(j.id)}/export">导出全部 ID</a><a href="${endpoint(j.id)}/export?kind=failed">导出失败 ID</a></div><div class="table-wrap"><table><thead><tr><th><input type="checkbox" id="select-page" aria-label="选择本页全部视频"></th><th>视频 ID / 标题</th><th>状态</th><th>执行阶段 / 实例</th><th>错误</th><th>操作</th></tr></thead><tbody>${data.items.length?data.items.map(v=>`<tr><td><input type="checkbox" data-select="${v.id}" aria-label="选择 ${esc(v.video_id)}" ${state.selected.has(v.id)?'checked':''}></td><td><a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.video_id)}</a><small>${esc(v.title)}</small></td><td><span class="badge">${v.result?.test_mode?'测试成功':v.result?.skipped?'已跳过':videoLabels[v.status]||v.status}</span></td><td>${esc(v.progress?.stage || '—')}<small>${esc(v.instance_id || '本机')}</small></td><td title="${esc(v.error)}">${esc((v.error || '').slice(0,60))}</td><td><button data-remove="${v.id}" ${v.status==='removed'||j.archived?'disabled':''}>移出清单</button><button data-log="${v.id}">日志</button></td></tr>`).join(''):'<tr><td colspan="6" class="empty">没有符合条件的视频。</td></tr>'}</tbody></table></div><div class="dialog-actions"><button data-video-page="prev" ${!state.videoHistory.length?'disabled':''}>上一页</button><span>第 ${state.videoHistory.length+1} 页 · 每页 50 条</span><button data-video-page="next" ${!state.videoNext?'disabled':''}>下一页</button></div></div>`;
    state.rows=data.items;
  }
  function statistics(j, data) {
    const fileRows=rows=>rows.length?rows.map(r=>`<div class="details-line"><span>${esc(r.title || r.video_id)}<small> · ${esc(r.video_id)}</small></span><span>${size(r.size)}</span></div>`).join(''):'<p class="muted empty-line">暂无文件登记。</p>';
    $('detail-content').innerHTML=`<div class="cards">${metric('历史交付成品',j.file_count,'清单移除不冲减历史交付')+metric('计费大小',size(j.file_bytes_exact),`${j.file_bytes_exact} 字节`)+metric('测试大小',size(j.test_file_bytes_exact),'测试文件不计费')+metric('整单预计费用',money(j),j.price_set?`${j.price} 元 / ${j.unit}`:'请在任务设置中填写单价')}</div><div class="panel"><h2>任务账单与导出</h2><p class="notice">按实际成品字节累计；暂停、重试和跨实例接管不另起账单。修改单价会重算整单预计费用；此处显示预估账单。</p><div class="tools"><a href="${endpoint(j.id)}/export?kind=bill">导出任务账单 CSV</a><a href="/api/statistics/lists/${j.id}/files/export">导出交付明细 CSV</a><a href="/api/statistics/lists/${j.id}/files/export?kind=test">导出测试明细 CSV</a></div></div><div class="panel"><h2>下载器执行统计</h2>${data.providers.length?data.providers.map(p=>`<div class="details-line"><span>${esc(p.method)} · ${esc(videoLabels[p.status] || p.status)}</span><span>${p.count} 条执行记录</span></div>`).join(''):'<p class="muted">尚无执行记录。下载器切换记录与视频数量分别统计。</p>'}</div><div class="split"><div class="panel"><h2>最近交付（最多 50 条）</h2>${fileRows(data.files)}</div><div class="panel"><h2>最近测试（最多 50 条）</h2>${fileRows(data.test_files)}</div></div>`;
  }
  function settings(j) {
    const p=j.policy || {};
    $('detail-content').innerHTML=`<div class="panel"><h2>任务信息与计费</h2><form id="settings-form" data-version="${j.edit_version}"><div class="form-grid"><label><span>客户 *</span><input name="customer" required maxlength="200" value="${esc(j.customer)}"></label><label><span>清单名称 *</span><input name="name" required maxlength="200" value="${esc(j.name || '未命名任务')}"></label><label><span>单价（元 / 单位，可留空）</span><input name="price" type="number" min="0" max="1000000" step="0.000001" value="${j.price_set?esc(j.price):''}"></label><label><span>计费单位</span><select name="unit"><option value="GB" ${j.unit==='GB'?'selected':''}>GB（10⁹ 字节）</option><option value="GiB" ${j.unit==='GiB'?'selected':''}>GiB（2³⁰ 字节）</option></select></label></div><label><span>备注</span><textarea name="note" maxlength="2000">${esc(j.note)}</textarea></label><p class="notice">单价或计费单位调整会重算本任务全部交付的预计费用。实际视频、其他任务和历史字节记录不变。</p><h2>独立下载策略</h2><div class="form-grid"><label><span>下载器顺序（逗号分隔）</span><input name="methods" value="${esc((p.download_methods || [p.download_method || 'yt_dlp']).join(', '))}" placeholder="downmp3, yt5s"><small>可选 yt_dlp、yt5s、downmp3，按填写顺序尝试。</small></label><label><span>downmp3 画质</span><select name="quality">${['360','480','720','1080'].map(q=>`<option value="${q}" ${(p.downmp3_quality || '720')===q?'selected':''}>${q}p</option>`).join('')}</select></label><label><span>yt5s 画质</span><select name="yt5s_quality"><option value="360" ${p.yt5s_quality!=='720'?'selected':''}>360p</option><option value="720" ${p.yt5s_quality==='720'?'selected':''}>720p</option></select></label><label><span>保存模式（开始执行后锁定）</span><select name="storage_mode"><option value="local" ${p.storage_mode!=='test'?'selected':''}>正式保存</option><option value="test" ${p.storage_mode==='test'?'selected':''}>测试模式，不计费</option></select></label></div><p class="muted">策略变更在后续执行尝试生效。Cookie、代理、浏览器与磁盘目录由执行实例提供。</p><p id="settings-error" class="form-error" role="alert" hidden></p><button class="primary small-gap" type="submit" ${j.archived?'disabled':''}>保存任务设置</button></form></div>`;
  }
  async function refresh() {
    const ticket=++state.epoch, id=state.id, tab=state.tab;
    refreshAbort?.abort(); refreshAbort=new AbortController();
    const opt={signal:refreshAbort.signal};
    try {
      if (!id) {
        const params=new URLSearchParams({q:$('search').value.trim(),lifecycle:$('state-filter').value,...(state.cursor?{cursor:state.cursor}:{})});
        const [data,summary,system]=await Promise.all([api('/api/jobs?'+params,opt),api('/api/jobs/summary',opt),api('/api/jobs/system/status',opt)]);
        if (ticket!==state.epoch) return;
        state.next=data.next_cursor;
        $('center-cards').innerHTML=metric('有效任务',summary.total,'不含归档 / 删除')+metric('执行中任务',summary.running,'各任务独立控制')+metric('历史交付',size(summary.file_bytes_exact),'包含归档 / 删除任务的历史交付')+metric('整单预计费用合计',`¥ ${summary.amount}`,`单价待设置 ${summary.unset} 单`);
        state.system=system;renderSystemNote();
        $('task-table').innerHTML=data.items.length?`<table><thead><tr><th>任务 / 客户</th><th>状态</th><th>有效 / 成功 / 失败</th><th>处理进度</th><th>交付大小</th><th>预计费用</th><th>操作</th></tr></thead><tbody>${data.items.map(j=>`<tr><td><button class="open-task" data-open="${j.id}">${esc(j.name || '未命名任务')}</button><small>${esc(j.customer)} · ${j.id.slice(0,12)}</small></td><td><span class="badge">${labels[j.state]}</span></td><td>${j.total} / ${j.succeeded} / ${j.failed}</td><td>${j.progress_percent===null?'—':j.progress_percent+'%'}</td><td>${size(j.file_bytes_exact)}</td><td>${money(j)}</td><td><button data-open="${j.id}">进入任务</button></td></tr>`).join('')}</tbody></table>`:'<div class="empty">还没有任务，点击右上角新建或导入清单。</div>';
        $('jobs-prev').disabled=!state.history.length; $('jobs-next').disabled=!state.next;
        $('jobs-page-label').textContent=`第 ${state.history.length+1} 页 · 每页 20 条`;
        error('page-error','');
      } else {
        const params=new URLSearchParams({q:state.videoQuery,...(state.videoStatus?{status:state.videoStatus}:{}),...(state.videoCursor?{cursor:state.videoCursor}:{})});
        const [j,extra,system]=await Promise.all([api(endpoint(id),opt),tab==='videos'?api(endpoint(id)+'/videos?'+params,opt):['billing','history'].includes(tab)?api(endpoint(id)+'/statistics',opt):Promise.resolve(null),api('/api/jobs/system/status',opt)]);
        if (ticket!==state.epoch || id!==state.id || tab!==state.tab) return;
        state.job=j;context(j);state.system=system;renderSystemNote();
        if (tab==='overview')overview(j);
        if (tab==='videos')videos(j,extra);
        if (tab==='billing')statistics(j,extra);
        if (tab==='settings')settings(j);
        if (tab==='history')$('detail-content').innerHTML=`<div class="panel"><h2>任务操作记录（最近 50 条）</h2>${extra.events.length?extra.events.map(e=>`<div class="details-line"><span>${esc(e.message)}</span><small>${esc(e.created_at)}</small></div>`).join(''):'<p class="muted">历史任务没有完整操作记录。</p>'}</div><div class="panel"><h2>当前执行 · ${(j.transfer_speed/1e6).toFixed(2)} MB/s</h2>${j.running_videos.length?j.running_videos.map(v=>`<div class="details-line"><span>${esc(v.video_id)} · ${esc(v.progress?.stage || '准备中')}</span><span>${v.progress?.percent===null||v.progress?.percent===undefined?'—':esc(v.progress.percent)+'%'} · ${esc(v.instance_id || '本机')}</span></div>`).join(''):'<p class="muted">当前没有正在执行的视频。</p>'}</div><div class="panel"><h2>导入记录</h2>${importRows(j)}</div>`;
      }
    } catch(e) { if (e.name==='AbortError'||ticket!==state.epoch)return; if(id)toast(e.message);else error('page-error',e.message); }
  }
  function route() { const hash=location.hash.slice(1);state.id=/^[a-f0-9]{32}$/.test(hash)?hash:null;state.tab='overview';state.selected.clear();state.videoCursor=null;state.videoHistory=[];$('center-view').hidden=!!state.id;$('detail-view').hidden=!state.id;refresh(); }
  function editor(mode) {
    state.mode=mode;state.editorJob=mode==='append'?state.id:null;
    $('editor-form').reset();delete $('editor-form').dataset.createdId;$('editor-form').dataset.requestId=Array.from(crypto.getRandomValues(new Uint8Array(16)), x=>x.toString(16).padStart(2,'0')).join('');error('editor-error','');
    $('basic-fields').hidden=mode==='append';for(const name of ['customer','name'])$('editor-form').elements[name].required=mode!=='append';
    $('editor-title').textContent=mode==='append'?'追加视频到当前任务':'新建客户任务';
    $('editor-caption').textContent=mode==='append'?`${state.job.customer} / ${state.job.name}`:'创建后保持待开始；不影响其他任务。';
    $('editor').showModal();
  }
  async function mutate(button, action) {
    if(state.busy)return;state.busy=true;button.disabled=true;
    try { await action();state.selected.clear();await refresh(); }
    catch(e){toast(e.message)} finally{state.busy=false;if(button.isConnected)button.disabled=false;}
  }
  document.addEventListener('click',event=>{
    const b=event.target.closest('button');if(!b||b.disabled)return;
    if(b.dataset.nav==='center'){location.hash='';return}
    if(b.dataset.open){location.hash=b.dataset.open;return}
    if(b.dataset.tab||b.dataset.switch){state.tab=b.dataset.tab||b.dataset.switch;refresh();return}
    if(b.dataset.videoPage){if(b.dataset.videoPage==='next'){state.videoHistory.push(state.videoCursor);state.videoCursor=state.videoNext}else state.videoCursor=state.videoHistory.pop()||null;state.selected.clear();refresh();return}
    if(b.hasAttribute('data-system-resume')){resumeSystem();return}
    if(b.dataset.log){mutate(b,async()=>{const result=await api(`/api/tasks/${b.dataset.log}/logs?offset=0`);$('log-content').textContent=result.text||'暂无日志。';$('log-dialog').showModal()});return}
    if(b.dataset.remove){mutate(b,()=>api(endpoint(state.id)+'/videos/remove',{method:'POST',body:{ids:[b.dataset.remove]}}));return}
    const a=b.dataset.action;if(!a)return;
    if(a==='append'){editor('append');return}
    if(a==='filter-videos'){state.videoQuery=$('video-query').value.trim();state.videoStatus=$('video-filter').value;state.videoCursor=null;state.videoHistory=[];state.selected.clear();refresh();return}
    if(a==='clear'||a==='delete'){$('clear-form').reset();$('clear-form').dataset.action=a;$('clear-form').dataset.jobId=state.job.id;$('clear-form').dataset.jobName=state.job.name;$('clear-dialog').querySelector('h2').textContent=a==='delete'?'删除当前任务':'清空当前任务视频清单';$('clear-form').querySelector('select').closest('label').hidden=a==='delete';$('confirm-label').textContent='输入任务名称确认：'+state.job.name;error('clear-error','');$('clear-dialog').showModal();return}
    if(a==='remove-selected'){if(!state.selected.size){toast('请先勾选视频条目');return}mutate(b,()=>api(endpoint(state.id)+'/videos/remove',{method:'POST',body:{ids:[...state.selected]}}));return}
    mutate(b,async()=>{const result=await api(endpoint(state.id)+'/control',{method:'POST',body:{action:a}});toast(result.warnings?.length?result.warnings.join('；'):a==='stop'?'停止指令已生效，执行进程将在下次心跳退出；其他任务继续。':'任务操作已保存。')});
  });
  document.addEventListener('change',e=>{if(e.target.dataset.select){e.target.checked?state.selected.add(e.target.dataset.select):state.selected.delete(e.target.dataset.select)}if(e.target.id==='select-page'){for(const v of state.rows)e.target.checked?state.selected.add(v.id):state.selected.delete(v.id);document.querySelectorAll('[data-select]').forEach(c=>c.checked=e.target.checked)}});
  $('editor-form').elements.file.addEventListener('change',e=>{const name=$('editor-form').elements.name;if(state.mode==='new'&&!name.value.trim()&&e.target.files[0])name.value=e.target.files[0].name.slice(0,200)});
  $('new-task').onclick=()=>editor('new');$('back').onclick=()=>location.hash='';
  $('cancel-editor').onclick=()=>$('editor').close();$('cancel-clear').onclick=()=>$('clear-dialog').close();
  $('search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.cursor=null;state.history=[];refresh()},300)};
  $('state-filter').onchange=()=>{state.cursor=null;state.history=[];refresh()};
  $('jobs-next').onclick=()=>{state.history.push(state.cursor);state.cursor=state.next;refresh()};$('jobs-prev').onclick=()=>{state.cursor=state.history.pop()||null;refresh()};
  $('editor-form').onsubmit=async e=>{
    e.preventDefault();const form=e.target,button=form.querySelector('[type=submit]');if(button.disabled)return;button.disabled=true;error('editor-error','');
    const f=new FormData(form),file=f.get('file');let summary='';
    try{
      if(state.mode==='append'&&!String(f.get('ids')).trim()&&!file.size)throw new Error('请粘贴视频 ID 或选择导入文件。');
      if(file.size>1024**3)throw new Error('导入文件最大 1 GB。');
      let id=state.editorJob||form.dataset.createdId;
      if(!id){const created=await api('/api/jobs',{method:'POST',body:{request_id:form.dataset.requestId,customer:f.get('customer'),name:f.get('name'),note:f.get('note'),...(f.get('price')!==''?{price:f.get('price')}:{}),urls:f.get('ids')}});id=created.id;form.dataset.createdId=id;}
      else if(String(f.get('ids')).trim()&&state.mode==='append'){const result=await api(endpoint(id)+'/videos',{method:'POST',body:{urls:f.get('ids')}});summary=`新增 ${result.added}，恢复 ${result.restored}，重复 ${result.duplicates}。`;}
      if(file.size){const upload=new FormData();upload.set('file',file);await api(endpoint(id)+'/imports',{method:'POST',body:upload})}
      $('editor').close();toast(summary || '已保存到独立任务；新任务需要手动开始。');if(location.hash==='#'+id){state.tab='videos';refresh()}else location.hash=id;
    }catch(e){error('editor-error',e.message)}finally{button.disabled=false}
  };
  $('clear-form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('[type=submit]');if(b.disabled)return;b.disabled=true;const f=new FormData(e.target),clearId=e.target.dataset.jobId;try{if(f.get('confirmation')!==e.target.dataset.jobName)throw new Error('请输入完整任务名称确认。');if(e.target.dataset.action==='delete'){await api(endpoint(clearId)+'/control',{method:'POST',body:{action:'delete',confirmation:f.get('confirmation')}});$('clear-dialog').close();location.hash='';toast('任务已删除；历史交付与磁盘文件保留。')}else{const result=await api(endpoint(clearId)+'/videos/remove',{method:'POST',body:{clear:true,scope:f.get('scope'),confirmation:f.get('confirmation')}});$('clear-dialog').close();state.videoCursor=null;state.videoHistory=[];state.selected.clear();await refresh();toast(`已移除 ${result.removed} 个视频；历史账本保留。`)}}catch(e){error('clear-error',e.message)}finally{b.disabled=false}};
  $('detail-content').addEventListener('submit',async e=>{if(e.target.id!=='settings-form')return;e.preventDefault();const b=e.target.querySelector('[type=submit]'),f=new FormData(e.target);b.disabled=true;error('settings-error','');try{await api(endpoint(state.id),{method:'PUT',body:{edit_version:Number(e.target.dataset.version),customer:f.get('customer'),name:f.get('name'),note:f.get('note'),price:f.get('price'),unit:f.get('unit'),policy:{download_methods:String(f.get('methods')).split(/[,，]/).map(x=>x.trim()).filter(Boolean),downmp3_quality:f.get('quality'),yt5s_quality:f.get('yt5s_quality'),storage_mode:f.get('storage_mode')}}});toast('任务信息和整单预计费用已更新。');await refresh()}catch(e){error('settings-error',e.message);if(e.status===409)await showConflict(b.form)}finally{if(b.isConnected)b.disabled=false}});
  window.addEventListener('hashchange',route);
  setInterval(()=>{if(!document.hidden&&!state.busy&&!$('editor').open&&!$('clear-dialog').open&&(!state.id||['overview','history','billing'].includes(state.tab)))refresh()},5000);
  route();
})();
