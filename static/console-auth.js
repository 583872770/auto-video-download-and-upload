'use strict';
(() => {
  const $=id=>document.getElementById(id),roles={admin:'管理员',operator:'操作员',viewer:'只读'};
  const message=text=>{$('access-message').textContent=text;};
  async function api(url,method='GET',data){
    const response=await fetch(url,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content},...(data?{body:JSON.stringify(data)}:{})});
    const value=await response.json();if(!response.ok)throw new Error(value.error||'请求失败');return value;
  }
  async function accounts(){
    if(!$('accounts'))return;
    const data=await api('/api/auth/users');$('accounts').replaceChildren();
    for(const user of data.items){const line=document.createElement('div');line.className='account-row';line.textContent=`${user.username} · ${roles[user.role]} · ${user.enabled?'启用':'停用'}`;$('accounts').append(line);}
  }
  async function submit(event,action){event.preventDefault();const button=event.target.querySelector('button');button.disabled=true;message('');try{await action(Object.fromEntries(new FormData(event.target)));}catch(error){message(error.message);}finally{button.disabled=false;}}
  $('login-form')?.addEventListener('submit',event=>submit(event,async data=>{const result=await api(event.target.dataset.setup==='true'?'/api/auth/setup':'/api/auth/login','POST',data);location.assign(result.redirect);}));
  $('logout')?.addEventListener('click',async()=>{try{const result=await api('/api/auth/logout','POST',{});location.assign(result.redirect);}catch(error){message(error.message);}});
  $('create-account')?.addEventListener('submit',event=>submit(event,async data=>{const result=await api('/api/auth/users','POST',data);event.target.reset();message(result.message);await accounts();}));
  $('update-account')?.addEventListener('submit',event=>submit(event,async data=>{const name=data.username;delete data.username;data.enabled=data.enabled==='true';if(!data.password)delete data.password;const result=await api('/api/auth/users/'+encodeURIComponent(name),'PUT',data);event.target.elements.password.value='';message(result.message);const status=await api('/api/auth/status');if(!status.user)location.assign('/login');else await accounts();}));
  accounts().catch(error=>message(error.message));
})();
