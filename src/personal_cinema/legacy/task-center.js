(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const el = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=text; return n; };
  const gb = n => `${((Number(n)||0)/1e9).toLocaleString('zh-CN',{maximumFractionDigits:2})} GB`;
  const gib = n => `${((Number(n)||0)/1073741824).toLocaleString('zh-CN',{maximumFractionDigits:2})} GiB`;
  const clock = n => n ? new Date(n*1000).toLocaleString('zh-CN',{hour12:false}) : '尚无记录';
  const shortTime = n => n ? new Date(n*1000).toLocaleTimeString('zh-CN',{hour12:false}) : '尚无记录';
  const episode = i => i.episode!=null ? `S${String(i.season||0).padStart(2,'0')}E${String(i.episode).padStart(2,'0')}` : '';
  const title = i => [i.series,episode(i),i.title].filter(Boolean).join(' · ');
  const isQueued = i => ['waiting','downloading','retry'].includes(i.phase);
  const hasCache = i => i.phase!=='none'||i.bytes>0;
  const requestKey='ml-console-requests';
  let token=sessionStorage.getItem('ml-console-token')||'',data,busy=false,connected=false,epoch=0,limit=40,activeTab='cache',noticeTimer,seriesSignature='',visibleRows=[];
  const selected=new Set(),pictures=new Map(),pictureLoads=new Map(),pictureFailures=new Map(),chosenVersions=new Map(),requests=new Map(),nodes=new Map();
  try { if(token)for(const r of JSON.parse(sessionStorage.getItem(requestKey)||'[]'))if(r.request_id&&r.action)requests.set(r.request_id,{...r,recover:true,sending:false}); } catch(_) { sessionStorage.removeItem(requestKey); }
  const states={done:'已完成',no_photo:'已处理 · 暂无照片',waiting:'等待处理',retry:'等待重试',review:'可手动指定',error:'处理失败',complete:'已完成',partial:'部分资料待完成',missing:'已检索，暂缺可靠照片',indexing:'等待媒体索引',running:'处理中',stalled:'长时间未更新'};
  const phases={library:'正在整理影视库',organizing:'正在整理网盘',downloading:'后台缓存 · 整片下载',idle:'缓存待命',steam:'为 Steam 下载让路',paused:'缓存已暂停',playback:'播放优先',error:'缓存遇到问题',unavailable:'后台状态暂不可用'};
  const cacheStates={ready:'本地已就绪',downloading:'后台缓存',buffered:'播放缓冲',waiting:'已排队',retry:'等待重试',none:'尚未缓存'};
  function notice(text,error=false,scope='command',requestId=''){$('notice-text').textContent=text;$('notice').dataset.error=String(error);$('notice').dataset.scope=scope;$('notice').dataset.requestId=requestId;$('notice').hidden=false;clearTimeout(noticeTimer);if(!error)noticeTimer=setTimeout(()=>$('notice').hidden=true,6500);}
  function persist(){sessionStorage.setItem(requestKey,JSON.stringify([...requests.values()].map(({recover,sending,...r})=>r)));}
  function loggedIn(yes){$('workspace').hidden=!yes;$('login').hidden=yes;for(const id of ['logout','refresh','scan-actions'])$(id).hidden=!yes;$('system-settings').hidden=!yes||!data?.capabilities?.open_settings;}
  function clearSession(message=''){
    epoch++;token='';data=null;connected=false;sessionStorage.removeItem('ml-console-token');sessionStorage.removeItem(requestKey);selected.clear();requests.clear();chosenVersions.clear();nodes.clear();seriesSignature='';
    pictures.forEach(url=>URL.revokeObjectURL(url));pictures.clear();pictureLoads.clear();pictureFailures.clear();$('media-list').replaceChildren();loggedIn(false);$('login-error').textContent=message;$('connection').textContent=message?'需要重新登录':'已退出';$('connection-state').dataset.state='error';$('notice').hidden=true;
    if($('remove-dialog').open)$('remove-dialog').close('cancel');
  }
  function authError(e){if(e.status===401||e.status===403){clearSession(e.status===403?'当前账号没有工作台权限，请重新登录有权限的账号。':'登录已失效，请重新登录。');return true;}return false;}
  async function api(path,opts={}) {
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000),started=epoch,auth=token;
    try {
      const r=await fetch(path,{cache:'no-store',...opts,headers:{Authorization:`MediaBrowser Token="${auth}"`,...(opts.headers||{})},signal:controller.signal});
      const raw=await r.text();let v;try{v=JSON.parse(raw);}catch(_){v=null;}
      if(started!==epoch){const e=new Error('登录状态已变更');e.stale=true;throw e;}
      if(!r.ok){const e=new Error(r.status===401?'登录已失效，请重新登录。':r.status===403?'当前账号没有操作权限，请重新登录。':v?.error||'服务暂时不可用，请稍后刷新。');e.status=r.status;throw e;}
      if(!v)throw new Error('服务器暂未返回有效状态，请稍后刷新。');return v;
    } catch(e){if(e.name==='AbortError')throw new Error('服务器响应超时，请稍后刷新。');if(e instanceof TypeError)throw new Error('连接中断，请检查网络；已保留上次结果。');throw e;}finally{clearTimeout(timer);}
  }
  function uuid(){const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);return Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');}
  const isScan = action => ['scan','scan-new','start-automation'].includes(action);
  function pending(id){for(const r of requests.values())if(r.ids.includes(id))return r.action;return data?.items.find(i=>i.id===id)?.pending_action;}
  function refreshActions(){
    const scanning=data?.automation?.batch?.state==='running'||[...requests.values()].some(r=>isScan(r.action));
    $('scan').disabled=$('start-automation').disabled=!connected||scanning;
    $('start-automation').textContent=scanning?'检查进行中…':'扫描新入库';
    if(data)renderMedia();
  }
  async function sendRequest(r){
    if(r.sending||!token||(r.action==='manual-match'&&!r.payload))return;r.sending=true;const started=epoch;
    try{
      const result=await api(r.action==='manual-match'?'/MediaCache/Match':'/MediaCache/Commands',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(r.action==='manual-match'?r.payload:{action:r.action,ids:r.ids,request_id:r.request_id})});
      if(started!==epoch)return;r.uncertain=false;r.retry_at=0;r.failures=0;if($('notice').dataset.requestId===r.request_id)$('notice').hidden=true;
      if(['applied','complete','partial','error'].includes(result.state)){requests.delete(r.request_id);if(result.state!=='applied')notice(result.message||'操作未完成，请查看任务状态。',true);}
    }catch(e){if(e.stale||started!==epoch)return;if(authError(e))return;
      if(e.status&&e.status<500){requests.delete(r.request_id);notice(e.message,true);}else{r.uncertain=true;r.failures=(r.failures||0)+1;r.retry_at=Date.now()+Math.min(300000,5000*2**Math.min(r.failures-1,6));notice('暂未收到确认，将查询同一请求的回执。'+e.message,true,'command',r.request_id);}
    }finally{r.sending=false;if(started===epoch){persist();refreshActions();}}
  }
  async function command(action,ids=[]){
    if(!token||!connected)return;
    ids=[...new Set(ids)].slice(0,50);if(!isScan(action)&&!ids.length)return;
    if([...requests.values()].some(r=>isScan(action)&&isScan(r.action)||r.action===action&&JSON.stringify([...r.ids].sort())===JSON.stringify([...ids].sort())))return;
    const r={request_id:uuid(),action,ids,created_at:Date.now(),uncertain:false,recover:false,sending:false};requests.set(r.request_id,r);persist();ids.forEach(id=>selected.delete(id));refreshActions();
    notice(isScan(action)?action==='scan'?'已提交全局扫描，检查全部新旧目录。':'已提交新入库检查；本批完成后停止。':action==='prefer-version'?'已指定优选，后台将迁移记录并回收同作品旧版本。':action==='remove-cache'?'正在移除任务和电脑缓存，云盘源文件保留。':'已提交缓存请求，等待后台确认。');
    await sendRequest(r);await poll();
  }
  async function checkReceipts(){
    const pendingRequests=[...requests.values()].filter(r=>!r.sending);
    for(let offset=0;offset<pendingRequests.length;offset+=4)await Promise.all(pendingRequests.slice(offset,offset+4).map(async r=>{
      try{
        const receipt=await api('/MediaCache/Receipts/'+encodeURIComponent(r.request_id));
        if(['applied','complete','partial','error'].includes(receipt.state)){
          requests.delete(r.request_id);persist();if($('notice').dataset.requestId===r.request_id)$('notice').hidden=true;
          if(!['applied','complete'].includes(receipt.state))notice(receipt.message||'部分资料仍待完成，可继续指定来源。',receipt.state==='error');
          else if(r.action==='manual-match')notice('所选作品的匹配整理已完成。');
          else if(r.action==='remove-cache')notice('已移除所选缓存；云盘源文件保留。');
        }else if((r.recover||r.uncertain)&&Date.now()>=(r.retry_at||0)){r.recover=false;await sendRequest(r);}
      }catch(e){if(e.stale||authError(e))return;if(e.status===404&&(r.uncertain||r.recover)&&Date.now()>=(r.retry_at||0)){r.recover=false;await sendRequest(r);}}
    }));
    if(token)refreshActions();
  }
  async function poll(){
    if(busy||document.hidden)return;
    if(!token){loggedIn(false);$('connection').textContent='等待登录';return;}
    busy=true;$('refresh').disabled=true;const started=epoch;
    try{
      const result=await api('/MediaCache/Tasks');if(started!==epoch)return;data=result;connected=true;loggedIn(true);$('connection').textContent='已连接 · 实时更新';$('connection-state').dataset.state='connected';
      if($('notice').dataset.scope==='connection')$('notice').hidden=true;render();await checkReceipts();
    }catch(e){if(e.stale||started!==epoch)return;connected=false;if(!authError(e)){$('connection').textContent='连接暂时中断';$('connection-state').dataset.state='error';notice(e.message,true,'connection');refreshActions();}}
    finally{busy=false;$('refresh').disabled=false;}
  }
  function render(){
    const s=data.cache||{},c=s.cache||{},buffer=s.playback_buffer,bufferCurrent=buffer?.current,bufferActive=bufferCurrent&&['watching','paused','grace','error'].includes(bufferCurrent.phase),cur=s.phase==='playback'||bufferActive?null:s.current,queue=data.items.filter(isQueued),attention=data.ingestion.filter(g=>g.state!=='complete').length;
    $('usage').textContent=gb(c.bytes);$('cache-limit').textContent='上限 '+gb(c.limit);const capacity=Math.min(100,100*(c.bytes||0)/(c.limit||1));$('capacity').style.width=capacity+'%';$('capacity').parentElement.setAttribute('aria-valuenow',Math.round(capacity));
    $('ready').textContent=c.ready_items||0;$('attention').textContent=attention;$('scan-time').textContent='上次扫描 '+shortTime(data.last_scan_at);
    $('cache-count').textContent=queue.length?` · ${queue.length} 项任务`:` · ${c.ready_items||0} 个副本`;$('ingest-count').textContent=attention?` · ${attention} 项待处理`:' · 已完成';
    $('phase').textContent=phases[s.phase]||phases.unavailable;$('current').dataset.phase=s.phase||'unavailable';
    $('current-title').textContent=cur?title(cur):s.phase==='steam'?'后台缓存等待 Steam 下载结束':s.phase==='paused'?'后台下载保持暂停':s.phase==='unavailable'?'暂时无法读取后台状态':s.phase==='playback'?'优先保障当前播放':c.ready_items?`${c.ready_items} 个本地副本已就绪`:'暂无缓存任务';
    $('current-note').textContent=cur?`${cur.source} · ${gb(cur.downloaded_bytes)} / ${gb(cur.size)}`:s.phase==='unavailable'?'下方保留最近一次资料；连接恢复后自动更新。':s.phase==='paused'?'手动暂停仍然生效。':queue.length?`${queue.length} 项缓存任务等待后台调度。`:'在下方选择影片或具体集数，即可加入缓存。';
    if(s.phase==='playback')$('current-note').textContent='整片后台下载已暂停，网络优先供给当前播放；暂停影片也不会启动整片下载。';
    if(s.phase==='library'&&s.activity){$('current-title').textContent=`${s.activity.label} · ${s.activity.title}`;$('current-note').textContent=(s.activities||[s.activity]).map(a=>`${a.label}：${a.title}`).join('；')+'。其他后台缓存稍后继续。';}
    if(bufferActive){
      $('current').dataset.phase='playback';
      $('phase').textContent=bufferCurrent.phase==='paused'?'影片已暂停 · 顺序预读':bufferCurrent.phase==='grace'?'播放缓冲 · 等待继续':bufferCurrent.phase==='error'||!buffer.available?'播放缓冲 · 预读暂不可用':'播放缓冲 · 顺序预读';
      $('current-title').textContent=bufferCurrent.title||'当前影片';
      $('current-note').textContent=`${bufferCurrent.source?bufferCurrent.source+' · ':''}播放读取位置后方，已连续缓冲 ${gib(bufferCurrent.ahead_bytes)}。播放器内已有的缓冲另计。`;
    }
    let bufferNote=$('playback-buffer-pool');
    if(!bufferNote){bufferNote=el('p','media-sub');bufferNote.id='playback-buffer-pool';$('current-note').after(bufferNote);}
    bufferNote.hidden=!buffer;
    if(buffer){
      bufferNote.textContent=`E 盘播放缓冲：已用 ${gib(buffer.pool_bytes)} / 上限 ${gib(buffer.limit_bytes)}。空间用满后自动回收已读取的片段。${buffer.error?' '+buffer.error+'。':''}`;
    }
    const batch=data.automation?.batch;
    $('automation-note').textContent=data.automation?.manual_only?`定时扫描已关闭 · ${batch?.state==='running'?'本批检查进行中':batch?.state==='complete'?'上批检查已完成':batch?.state==='partial'?'上批有未完成项目，请查看入库任务':'批量添加资源后，点击“扫描新入库”'}`:'入库整理已开启 · 后台程序运行中';
    $('transfer').hidden=!cur;if(cur){const percent=Math.min(100,Math.max(0,Number(cur.percent)||0));$('download-fill').style.width=percent+'%';$('download-fill').parentElement.setAttribute('aria-valuenow',percent);$('transfer-note').textContent=`整片缓存 ${percent}% · 原始画质`;}
    $('heartbeat').textContent='后台更新 '+shortTime(s.updated_at);$('page-time').textContent=`页面更新 ${shortTime(data.generated_at)} · 每 5 秒更新状态`;
    const series=new Map(data.items.filter(i=>i.series_id).map(i=>[i.series_id,i.series]));const signature=JSON.stringify([...series].sort());
    if(signature!==seriesSignature){const previous=$('series').value;$('series').replaceChildren();[['','电影与剧集'],['movies','仅电影'],...[...series].sort((a,b)=>String(a[1]).localeCompare(String(b[1]),'zh-CN'))].forEach(([value,label])=>{const o=el('option','',label);o.value=value;$('series').append(o);});$('series').value=[...$('series').options].some(o=>o.value===previous)?previous:'';seriesSignature=signature;}
    refreshActions();if(activeTab==='ingest')renderIngest();
  }
  async function poster(img,id,tag){
    const started=epoch,key=id+'|'+(tag||'');img.posterKey=key;
    if(Date.now()<(pictureFailures.get(key)?.retry_at||0))return;
    if(!pictures.has(key)&&!pictureLoads.has(key)){
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
      const promise=(async()=>{try{
        const r=await fetch(`/Items/${encodeURIComponent(id)}/Images/Primary?maxWidth=100${tag?'&tag='+encodeURIComponent(tag):''}`,{headers:{Authorization:`MediaBrowser Token="${token}"`},signal:controller.signal});if(!r.ok)throw new Error('image');
        const blob=await r.blob();if(started!==epoch)return null;const url=URL.createObjectURL(blob);pictures.set(key,url);pictureFailures.delete(key);
        if(pictures.size>100){const oldest=pictures.keys().next().value;URL.revokeObjectURL(pictures.get(oldest));pictures.delete(oldest);}return url;
      }catch(_){if(started===epoch){const count=(pictureFailures.get(key)?.count||0)+1;pictureFailures.set(key,{count,retry_at:Date.now()+Math.min(60000,5000*2**Math.min(count-1,4))});if(pictureFailures.size>200)pictureFailures.delete(pictureFailures.keys().next().value);}return null;}finally{clearTimeout(timer);}})();
      pictureLoads.set(key,promise);promise.finally(()=>{if(pictureLoads.get(key)===promise)pictureLoads.delete(key);});
    }
    const url=pictures.get(key)||await pictureLoads.get(key);if(url&&started===epoch&&img.isConnected&&img.posterKey===key)img.src=url;
  }
  function createRow(key){
    const row=el('article','media-row');row.dataset.key=key;
    const select=el('label','select-control'),check=el('input');check.type='checkbox';select.append(check);row.append(select);
    const art=el('span','art'),main=el('div','media-main'),heading=el('div','media-title'),sub=el('div','media-sub'),versions=el('select','version-picker'),prefer=el('button','outline prefer-version','设为优选并替换旧版'),track=el('div','track mini-track'),fill=el('i'),badge=el('span','badge'),button=el('button');
    track.append(fill);main.append(heading,sub,versions,prefer,track);row.append(art,main,badge,button);row.parts={check,art,heading,sub,versions,prefer,track,fill,badge,button};
    check.addEventListener('change',()=>{const i=row.item;if(check.checked&&selected.size>=50){check.checked=false;notice('一次最多选择 50 个视频。');return;}check.checked?selected.add(i.id):selected.delete(i.id);updateSelection();});
    versions.addEventListener('change',()=>{const i=row.item;chosenVersions.set(i.item_id,versions.value);if(selected.delete(i.id))selected.add(versions.value);renderMedia();});
    prefer.addEventListener('click',()=>command('prefer-version',[row.item.id]));
    button.addEventListener('click',()=>hasCache(row.item)?removeCache([row.item.id]):command('queue',[row.item.id]));return row;
  }
  function patchRow(row,i){
    row.item=i;const p=row.parts,action=pending(i.id),cached=hasCache(i);row.dataset.selected=String(selected.has(i.id));p.check.checked=selected.has(i.id);p.check.disabled=!connected||Boolean(action);p.check.setAttribute('aria-label','选择 '+title(i));
    p.heading.textContent=title(i);p.sub.textContent=`${i.source} · ${gb(i.size)}${i.reason?' · '+i.reason:''}${i.retry_at?' · 重试 '+shortTime(i.retry_at):''}`;
    const posterKey=i.poster?i.item_id+'|'+(i.poster_tag||''):false;
    if(row.poster!==posterKey){row.poster=posterKey;p.art.replaceChildren();if(i.poster){const img=el('img');img.alt='';img.loading='lazy';p.art.append(img);}else p.art.textContent=(i.title||'影').slice(0,1);}
    const img=p.art.querySelector('img');if(img&&!img.getAttribute('src'))queueMicrotask(()=>poster(img,i.item_id,i.poster_tag));
    p.versions.hidden=i.variants.length<2;const sig=JSON.stringify(i.variants.map(v=>[v.id,v.source,v.size,v.phase,v.source_missing]));
    if(sig!==row.versionSignature){p.versions.replaceChildren();i.variants.forEach(v=>{const option=el('option','',`${v.source} · ${gb(v.size)}${v.phase==='ready'?' · 本地已缓存':v.source_missing?' · 源文件缺失':''}`);option.value=v.id;p.versions.append(option);});row.versionSignature=sig;}p.versions.value=i.id;p.versions.setAttribute('aria-label',i.title+' 的播放来源');p.versions.disabled=Boolean(action);
    p.prefer.hidden=i.variants.length<2||i.kind!=='Movies';p.prefer.disabled=!connected||Boolean(action)||i.source_missing;
    p.track.hidden=!(i.bytes>0&&i.phase!=='ready');p.fill.style.width=Math.min(100,Number(i.percent)||0)+'%';
    p.badge.className='badge '+i.phase;p.badge.textContent=action==='remove-cache'?'正在移除':action==='queue'?'请求已送达':action==='prefer-version'?'正在设置优选':i.cache_blocked&&i.phase==='none'?'已停用自动缓存':`${cacheStates[i.phase]||'读取状态'}${i.percent>0&&i.phase!=='ready'?' '+i.percent+'%':''}`;
    p.button.className=cached?'outline':'';p.button.textContent=i.phase==='ready'?'删除本地缓存':i.phase==='downloading'?'停止并删除缓存':cached?'移除缓存任务':i.source_missing?'源文件缺失':i.cache_blocked?'重新加入缓存':'加入缓存';p.button.disabled=!connected||Boolean(action)||!cached&&i.source_missing;
  }
  function renderMedia(){
    if(!data)return;const q=$('search').value.trim().toLocaleLowerCase(),series=$('series').value,filter=$('filter').value,grouped=new Map();
    data.items.forEach(i=>{const key=i.item_id||i.id;if(!grouped.has(key))grouped.set(key,[]);grouped.get(key).push(i);});
    const rows=[...grouped.values()].map(variants=>{
      variants.sort((a,b)=>Number(b.preferred)-Number(a.preferred)||Number(b.phase==='ready')-Number(a.phase==='ready')||Number(a.source_missing)-Number(b.source_missing)||(a.source==='夸克'?0:1)-(b.source==='夸克'?0:1)||b.size-a.size);
      const requested=chosenVersions.get(variants[0].item_id);const matches=i=>filter==='ready'?i.phase==='ready':filter==='queue'?isQueued(i):filter==='none'?!hasCache(i):true;
      const picked=variants.find(i=>i.id===requested&&matches(i))||variants.find(matches)||variants[0];return {...picked,variants};
    }).filter(i=>(!q||`${title(i)} 第${i.episode||''}集`.toLocaleLowerCase().includes(q))&&(!series||series==='movies'&&i.kind==='Movies'||series===i.series_id)&&(filter==='all'||filter==='ready'&&i.variants.some(v=>v.phase==='ready')||filter==='queue'&&i.variants.some(isQueued)||filter==='none'&&i.variants.some(v=>!hasCache(v))));
    rows.sort((a,b)=>Number(a.kind!=='Movies')-Number(b.kind!=='Movies')||String(a.series||'').localeCompare(String(b.series||''),'zh-CN')||(a.season||0)-(b.season||0)||(a.episode||0)-(b.episode||0)||a.title.localeCompare(b.title,'zh-CN'));
    const eligible=new Set(rows.map(i=>i.id));for(const id of selected)if(!eligible.has(id))selected.delete(id);
    visibleRows=rows.slice(0,limit);$('list-title').textContent={all:'选择缓存内容',ready:'电脑上的缓存',queue:'下载与排队',none:'尚未缓存的内容'}[filter];$('list-count').textContent=`${rows.length} 个影片 / 剧集${rows.length>limit?' · 已显示 '+limit+' 个':''}`;
    const list=$('media-list'),keys=new Set(visibleRows.map(i=>i.item_id||i.id));
    for(const [key,row] of nodes)if(!keys.has(key)){row.remove();nodes.delete(key);}list.querySelector('.empty')?.remove();
    visibleRows.forEach((i,index)=>{const key=i.item_id||i.id;let row=nodes.get(key);if(!row){row=createRow(key);nodes.set(key,row);}patchRow(row,i);if(list.children[index]!==row)list.insertBefore(row,list.children[index]||null);});
    if(!rows.length){const empty=el('div','empty');empty.append(el('strong','',q||series?'没有匹配的内容':filter==='queue'?'暂无下载任务':filter==='ready'?'还没有本地缓存':'暂无可显示的内容'),el('p','',q||series?'试试其他关键词，或清空作品筛选。':filter==='ready'?'在“全部内容”中选择影片并加入缓存。':filter==='queue'?'手动加入影片后，可在这里查看下载进度。':'添加资源后，点击“扫描新入库”。'));list.append(empty);}
    $('more').hidden=rows.length<=limit;$('select-page').disabled=!connected||!visibleRows.some(i=>!pending(i.id));updateSelection();
  }
  function selectedItems(){return data?.items.filter(i=>selected.has(i.id)&&!pending(i.id))||[];}
  function updateSelection(){
    const chosen=selectedItems(),queue=chosen.filter(i=>!hasCache(i)&&!i.source_missing),remove=chosen.filter(hasCache);$('selection-bar').hidden=selected.size===0;$('selection-count').textContent=`已选 ${selected.size} 项`;
    $('batch').disabled=!connected||!queue.length;$('batch').textContent=queue.length?`加入 ${queue.length} 项缓存`:'加入缓存';$('batch-remove').disabled=!connected||!remove.length;$('batch-remove').textContent=remove.length?`移除 ${remove.length} 项缓存`:'移除缓存';
    for(const row of nodes.values()){row.parts.check.checked=selected.has(row.item.id);row.dataset.selected=String(row.parts.check.checked);}
  }
  async function removeCache(ids){
    const targets=data.items.filter(i=>ids.includes(i.id)&&hasCache(i)&&!pending(i.id));if(!targets.length||!connected)return;
    if(targets.some(i=>i.bytes>0||i.phase==='ready')){
      const dialog=$('remove-dialog');if(dialog.open)return;dialog.returnValue='cancel';$('remove-note').textContent=`将移除 ${targets.length} 项缓存，共 ${gb(targets.reduce((sum,i)=>sum+(Number(i.bytes)||0),0))}。`;$('remove-list').replaceChildren();targets.forEach(i=>$('remove-list').append(el('li','',`${title(i)} · ${gb(i.bytes)}`)));
      const result=new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue),{once:true}));dialog.showModal();if(await result!=='confirm')return;
    }
    await command('remove-cache',targets.map(i=>i.id));
  }
  function renderIngest(){
    if(!data)return;const workers=$('workers');workers.replaceChildren();
    (data.events||[]).filter(e=>!['idle','complete'].includes(e.state)||(e.worker==='portraits'&&e.state==='idle'&&e.count>0)).forEach(e=>{const label={versions:'版本升级',organizer:'云盘整理',scan:'网盘扫描',credits:'中文演员资料',portraits:'演职员头像',artwork:'海报与简介'}[e.worker]||'影片识别';const n=el('div','worker',`${label} · ${e.title}${e.state==='idle'?'':' · '+(states[e.state]||e.state)}`);n.append(el('small','',`阶段更新 ${clock(e.updated_at)}${e.count!=null?' · '+(e.worker==='portraits'?(e.state==='idle'?'保留待办 ':'本轮处理 '):'已处理 ')+e.count+(e.total?' / '+e.total:'')+(e.worker==='portraits'?' 项':' 个文件'):''}${e.retry_at?' · 重试 '+clock(e.retry_at):''}`));workers.append(n);});
    if(data.source_errors)workers.append(el('div','worker',`${data.source_errors} 个网盘目录尚未读取完成，请查看本批状态。`));
    const filter=$('ingest-filter').value,rows=data.ingestion.filter(g=>filter==='all'||filter==='complete'&&g.state==='complete'||filter==='open'&&g.state!=='complete');rows.sort((a,b)=>Number(a.state==='complete')-Number(b.state==='complete')||(b.updated_at||0)-(a.updated_at||0));
    const list=$('ingest-list');list.replaceChildren();if(!rows.length){const empty=el('div','empty');empty.append(el('strong','',filter==='complete'?'暂无已完成记录':'当前没有待处理的入库任务'),el('p','',filter==='open'&&data.ingestion.length?`${data.ingestion.length} 个作品的资料已整理完成。`:'批量添加新资源后，点击“扫描新入库”。'));list.append(empty);}
    rows.forEach(g=>{const card=el('article','ingest-card'),top=el('div','ingest-top'),text=el('div');text.append(el('h3','',g.title),el('p','',`${g.source} · ${g.files} 个视频 · 最近检查 ${clock(g.updated_at)}`));const closedWithMissing=g.state==='complete'&&g.portraits?.missing>0;top.append(text,el('span','badge',closedWithMissing?'已完成 · 部分暂无照片':states[g.state]||g.state));card.append(top);const steps=el('div','steps');g.steps.forEach(s=>{const n=el('div','step',s.title);n.dataset.state=s.state==='no_photo'?'done':s.state;n.append(el('b','',states[s.state]||s.state));steps.append(n);});card.append(steps);if(g.note)card.append(el('p','ingest-note',g.note));if(g.retry_at&&g.state!=='complete')card.append(el('p','media-sub','下一次资料重试 '+clock(g.retry_at)));
      const match=el('button','outline manual-match-button','手动匹配 / 指定百科');match.disabled=!connected||g.id.startsWith('pending-');
      if(match.disabled)match.title='等待这部作品建立索引后即可指定';
      match.addEventListener('click',()=>window.MediaManualMatch.open(g,{api,onSaved:result=>{
        if(result.request_id){requests.set(result.request_id,{request_id:result.request_id,action:'manual-match',ids:[],payload:result.request_payload,created_at:Date.now(),uncertain:false,recover:false});persist();}
        notice(result.message||'已保存指定，后台将只重新整理这部作品。');poll();
      }}));card.append(match);list.append(card);});
    $('automation-results').replaceChildren();const versions=[...(data.versions||[])].sort((a,b)=>(b.updated_at||0)-(a.updated_at||0)).slice(0,8);$('version-count').textContent=versions.length?' · '+versions.length:'';versions.forEach(v=>$('automation-results').append(el('p','media-sub',`${v.title}：${v.note||v.status}`)));if(!versions.length)$('automation-results').append(el('p','media-sub','暂无版本升级记录。'));
    if(!$('rules').childElementCount)for(const r of data.rules?.rules||[]){const n=el('article','ingest-card');n.append(el('h3','',r.title),el('p','media-sub',r.detail));$('rules').append(n);}
  }
  $('login').addEventListener('submit',async e=>{e.preventDefault();$('login-error').textContent='';const button=$('login').querySelector('button');button.disabled=true;
    try{const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);let v;try{const r=await fetch('/Users/AuthenticateByName',{method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json',Authorization:`MediaBrowser Client="LibraryTasks", Device="Browser", DeviceId="task-center-${uuid()}", Version="1.0"`},body:JSON.stringify({Username:$('username').value,Pw:$('password').value})});if(!r.ok)throw new Error('登录失败，请核对账号和密码。');v=await r.json();}finally{clearTimeout(timer);}if(!v.AccessToken)throw new Error('登录服务暂时不可用。');token=v.AccessToken;epoch++;sessionStorage.setItem('ml-console-token',token);$('password').value='';await poll();}
    catch(e){$('login-error').textContent=e instanceof TypeError||e.name==='AbortError'?'暂时无法连接影视库，请检查网络后重试。':e instanceof SyntaxError?'登录服务暂时不可用，请稍后重试。':e.message;}finally{button.disabled=false;}
  });
  $('system-settings').addEventListener('click',async()=>{
    const button=$('system-settings');button.disabled=true;
    try{const result=await api('/MediaCache/Commands',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'open-settings',ids:[],request_id:uuid()})});notice(result.message);}
    catch(error){if(!error.stale)notice(error.message,true);}
    finally{button.disabled=false;}
  });
  $('start-automation').addEventListener('click',()=>command('scan-new'));$('scan').addEventListener('click',()=>command('scan'));$('refresh').addEventListener('click',poll);$('logout').addEventListener('click',()=>clearSession());$('dismiss-notice').addEventListener('click',()=>$('notice').hidden=true);
  $('batch').addEventListener('click',()=>command('queue',selectedItems().filter(i=>!hasCache(i)&&!i.source_missing).map(i=>i.id)));$('batch-remove').addEventListener('click',()=>removeCache(selectedItems().filter(hasCache).map(i=>i.id)));
  $('clear-selection').addEventListener('click',()=>{selected.clear();updateSelection();});$('select-page').addEventListener('click',()=>{for(const i of visibleRows)if(!pending(i.id)&&selected.size<50)selected.add(i.id);updateSelection();});
  ['search','series','filter'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',()=>{selected.clear();limit=40;renderMedia();}));$('more').addEventListener('click',()=>{limit+=40;renderMedia();});$('ingest-filter').addEventListener('change',renderIngest);
  const tabs=[...document.querySelectorAll('[data-tab]')];function chooseTab(button){activeTab=button.dataset.tab;tabs.forEach(b=>{b.setAttribute('aria-selected',String(b===button));b.tabIndex=b===button?0:-1;});$('cache-view').hidden=activeTab!=='cache';$('ingest-view').hidden=activeTab!=='ingest';activeTab==='cache'?renderMedia():renderIngest();}
  tabs.forEach((button,index)=>{button.addEventListener('click',()=>chooseTab(button));button.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(index+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;chooseTab(tabs[next]);tabs[next].focus();});});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});setInterval(poll,5000);poll();
})();
