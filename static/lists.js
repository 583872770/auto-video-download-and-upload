/* Read-only customer ledger. Edits belong to versioned job settings. */
(() => {
  const ledger={cursor:null,history:[],next:null,busy:false};
  const integer=value=>BigInt(String(value||0)).toLocaleString('zh-CN');
  const metric=(label,value,note='')=>`<div><span>${label}</span><strong>${escapeHTML(value)}</strong><small>${escapeHTML(note)}</small></div>`;
  async function refreshLists(){
    if(ledger.busy)return;ledger.busy=true;
    const cursor=ledger.cursor,query=$('#lists-search').value.trim();
    $('#lists-export').href=`/api/statistics/lists/export?${new URLSearchParams({q:query})}`;
    try{
      const data=await api(`/api/statistics/lists?${new URLSearchParams({q:query,...(cursor?{cursor}:{})})}`);
      if(cursor!==ledger.cursor||query!==$('#lists-search').value.trim())return;
      $('#customer-lists').innerHTML=data.items.length?data.items.map(row=>`<article class="customer-list"><div class="customer-list-heading"><div><h3>${escapeHTML(row.name)}</h3><p>${escapeHTML(row.customer||'历史未分类')} · ${escapeHTML(formatDate(row.created_at,true))} · ${integer(row.total)} 个有效视频</p></div><span class="count-pill">${row.deleted?'已删除 · 保留账本':row.archived?'已归档':row.running?'执行中':row.queued?(row.released?'等待执行':'待开始'):'已处理'}</span></div><div class="list-totals">${metric('待处理 / 执行中',`${integer(row.queued)} / ${integer(row.running)}`)+metric('成功 / 失败',`${integer(row.succeeded)} / ${integer(row.failed)}`,`测试成功 ${integer(row.test_succeeded)} · 跳过 ${integer(row.skipped)}`)+metric('累计交付',row.tracked?decimalGB(row.file_bytes):'历史未补算',`${integer(row.file_count)} 个文件 · ${integer(row.file_bytes_exact)} 字节`)+metric('累计测试',decimalGB(row.test_file_bytes),'不计费')+metric('任务预计费用',!row.tracked?'—':row.amount===null?'待设置单价':`¥ ${row.amount}`,row.price_set?`${row.price} 元 / ${row.unit}`:'留空为待定；0 为免费')}</div><div class="list-exports">${row.tracked&&!row.deleted?`<a class="text-button" href="/jobs#${row.id}">进入任务 / 修改设置 ↗</a>`:''}<a class="text-button" href="/api/statistics/lists/${row.id}/files/export" download>导出交付 CSV ↗</a><a class="text-button" href="/api/statistics/lists/${row.id}/files/export?kind=test" download>导出测试 CSV ↗</a></div></article>`).join(''):emptyState('暂无客户任务','在任务中心建立任务，再进入任务添加视频。');
      ledger.next=data.next_cursor;
      $('#lists-prev').disabled=!ledger.history.length;$('#lists-next').disabled=!ledger.next;
      $('#lists-page-label').textContent=`第 ${ledger.history.length+1} 页`;$('#lists-error').hidden=true;
    }catch(error){$('#lists-error').textContent=error.message;$('#lists-error').hidden=false;}
    finally{ledger.busy=false;}
  }
  for(const direction of ['prev','next'])$('#lists-'+direction).addEventListener('click',()=>{
    if(direction==='next'){ledger.history.push(ledger.cursor);ledger.cursor=ledger.next;}
    else ledger.cursor=ledger.history.pop()||null;refreshLists();
  });
  let timer;
  $('#lists-search').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>{ledger.cursor=null;ledger.history=[];refreshLists();},300);});
  document.addEventListener('viewchange',event=>{if(event.detail==='statistics')refreshLists();});
  $('#statistics-refresh').addEventListener('click',refreshLists);
  setInterval(()=>{if(!document.hidden&&!$('#statistics-view').hidden)refreshLists();},5000);
})();
