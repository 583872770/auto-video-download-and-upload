/* Server authorization remains authoritative; explain unavailable actions early. */
(() => {
  if(document.body.dataset.consoleRole!=='viewer')return;
  const writes=new Set(['start','pause','stop','archive','delete','unarchive','retry','append','remove-selected','clear']);
  function apply(){
    const buttons=document.querySelectorAll('form:not([method="dialog"]) button[type="submit"], #new-task, #download-queue-start, #download-queue-toggle, #download-job-pause, #download-job-stop, [data-policy-toggle], [data-policy-move], [data-system-resume], [data-remove], [data-action]');
    for(const button of buttons){
      if(button.dataset.action&&!writes.has(button.dataset.action))continue;
      if(!button.disabled)button.disabled=true;
      button.title='当前账户为只读';
    }
  }
  new MutationObserver(apply).observe(document.body,{childList:true,subtree:true});apply();
})();
