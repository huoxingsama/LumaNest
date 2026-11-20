/* LumaNest. Local assets only; never store credentials in browser storage. */
(function () {
  'use strict';
  function filterFields(fields, section, query) {
    const q = (query || '').trim().toLocaleLowerCase();
    return fields.filter(f => (!section || f.section === section) && (!q || [f.key,f.label,f.help].join(' ').toLocaleLowerCase().includes(q)));
  }
  function parseValue(field, raw) {
    if (field.kind === 'bool') return {value: !!raw};
    if (field.kind === 'int' || field.kind === 'float') {
      if (String(raw).trim() === '') return {error:'请填写数值。'};
      const value = Number(raw);
      if (!Number.isFinite(value) || (field.kind === 'int' && !Number.isInteger(value))) return {error:field.kind === 'int' ? '请填写整数。' : '请填写有效数值。'};
      if (field.min != null && value < field.min) return {error:`不能小于 ${field.min}。`};
      if (field.max != null && value > field.max) return {error:`不能大于 ${field.max}。`};
      return {value};
    }
    if (field.kind === 'list') {
      try { const value = JSON.parse(raw); return Array.isArray(value) ? {value} : {error:'请填写 JSON 数组。'}; }
      catch (_) { return {error:'列表格式不正确，请使用 JSON 数组。'}; }
    }
    if (field.kind === 'select' && field.options && !field.options.some(o => (typeof o === 'object' ? o.value : o) === raw)) return {error:'请选择一个可用选项。'};
    return {value:String(raw)};
  }
  function effectiveValues(config, scope = 'global', drafts = {}) {
    const values = Object.assign({}, config.values || {}, scope === 'global' ? {} : (config.overrides || {})[scope] || {});
    for (const [key, value] of Object.entries(drafts)) {
      if (value === null) values[key] = (config.values || {})[key]; else values[key] = value;
    }
    return values;
  }
  function sourceEntry(input) {
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(input.id || '')) return {error:'来源 ID 需以字母或数字开头，使用 1–64 位字母、数字、下划线或连字符。'};
    if (!String(input.name || '').trim()) return {error:'请填写来源名称。'};
    const path = String(input.path || '').trim();
    if (!path) return {error:'请选择媒体目录。'};
    if (!['local','openlist'].includes(input.kind)) return {error:'请选择支持的来源类型。'};
    if (input.kind === 'local' && !/^(?:[A-Za-z]:[\\/]|\\\\|\/)/.test(path)) return {error:'本地目录需要绝对路径。'};
    if (input.kind !== 'local' && (!path.startsWith('/') || path.split(/[\\/]/).includes('..'))) return {error:'远程目录以 / 开头，不能包含上级目录跳转。'};
    if (/^[A-Za-z]:[\\/]?$/.test(path) || path === '/') return {error:'请选择具体媒体目录，不要选择整个磁盘或挂载根目录。'};
    const value = {id:input.id,name:input.name.trim(),kind:input.kind,path,enabled:input.enabled !== false,allow_write:!!input.allow_write};
    for (const key of ['url','credential','driver','mount_path','native_root','library_type']) if (input[key]) value[key] = input[key];
    return {value};
  }
  function errorMessage(value) { return typeof value?.error?.message === 'string' ? value.error.message : '操作未完成，请重试。'; }
  const helpers = {filterFields,parseValue,effectiveValues,sourceEntry,errorMessage};
  if (typeof module !== 'undefined') module.exports = helpers;
  if (typeof document === 'undefined') return;

  const $ = id => document.getElementById(id);
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const state = {schema:{sections:[],fields:[]},config:{values:{},overrides:{}},status:{integrations:[],runtime:{},setup:{}},credentials:{},draft:{},invalid:{},scope:'global',section:'foundation',query:'',advanced:false,csrf:'',wizard:false,step:0,busy:false,modalSubmit:null,tasks:null,organizing:null,backups:null};
  const labels = {foundation:'基础服务',sources:'媒体来源',organizing:'整理与入库',metadata:'识别与资料',artwork:'海报与人物',ratings:'评分',playback:'播放与画面',cache:'缓存与存储',network:'网络与远程访问',appearance:'界面与交互',automation:'自动化与工作台',maintenance:'账户与维护'};
  const icons = {
    foundation:'<rect x="4" y="3" width="16" height="7" rx="2"/><rect x="4" y="14" width="16" height="7" rx="2"/><path d="M8 6.5h.01M8 17.5h.01M12 6.5h4M12 17.5h4"/>',
    sources:'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
    organizing:'<path d="M5 5h14M5 12h10M5 19h6M18 11v9m-3-3 3 3 3-3"/>',
    metadata:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
    artwork:'<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 16 5-5 5 5 4-4 4 4"/>',
    ratings:'<path d="m12 3 2.8 5.8 6.4.9-4.6 4.5 1.1 6.3-5.7-3-5.7 3 1.1-6.3-4.6-4.5 6.4-.9Z"/>',
    playback:'<path d="m8 4 12 8-12 8Z"/>',
    cache:'<path d="m5 4-2 12v2a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-2L19 4Z"/><path d="M3 15h18M16 17.5h.01M18 17.5h.01"/>',
    network:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
    appearance:'<path d="M4 7h4m4 0h8M4 17h8m4 0h4"/><circle cx="10" cy="7" r="2"/><circle cx="14" cy="17" r="2"/>',
    automation:'<path d="M20 6v6h-6M4 18v-6h6M6.2 6.2A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.8 5.8"/>',
    maintenance:'<circle cx="12" cy="12" r="3"/><path d="m10 3-.6 2.3-2 .9L5.3 5 3 9l1.8 1.5v3L3 15l2.3 4 2.1-1.2 2 .9L10 21h4l.6-2.3 2-.9 2.1 1.2 2.3-4-1.8-1.5v-3L21 9l-2.3-4-2.1 1.2-2-.9L14 3Z"/>'
  };
  const descriptions = {foundation:'先让媒体库和播放器就绪，再按需要添加其他能力。',sources:'只管理你选择的目录。播放读取与云端整理分别授权。',organizing:'预先设定整理方式，具体作品可以单独覆盖。',metadata:'来源、身份和人工确认分开记录，匹配失败时可继续处理。',artwork:'清晰、正确的图片优先；没有资料时使用整齐的占位。',ratings:'保留真实来源与评分范围，缺少某个平台不影响观看。',playback:'调整这台设备的输出、声音与字幕，按标注时机生效。',cache:'顺序播放缓冲、离线影片和图片缓存分别管理。',network:'普通网络即可使用。指定出口和远程访问按需开启。',appearance:'统一页面密度、文字与交互，返回时保留浏览位置。',automation:'选择何时处理哪些任务，并保留可重试的结果。',maintenance:'保存配置、管理凭据、查看诊断及维护运行环境。'};
  const effects = {immediate:'立即生效',next_page:'重新进入页面',next_playback:'下次播放',next_batch:'下批任务',restart:'重启后生效'};
  const wizardSteps = [{label:'基础服务',section:'foundation',title:'连接媒体库与影院',description:'连接 Jellyfin，检测影院内置的 MPV 引擎。仅需使用其他播放器时选择外置模式；已有媒体库只连接，不默认接管。'}, {label:'媒体来源',section:'sources',title:'选择要使用的媒体',description:'已有 Jellyfin 媒体库可以先跳过新增来源。需要网盘时再配置 OpenList 与所选目录。'}, {label:'缓存与网络',section:'cache',title:'安排本机存储',description:'选择缓存目录与容量，并保留足够磁盘空间。校园网等出口策略可稍后在网络设置配置。'}, {label:'就绪检查',section:'foundation',title:'检查后进入影院',description:'检测 Jellyfin 登录与所选播放引擎。完成向导不会自动启动全库整理；可在工作台选择处理范围。'}];

  async function api(path, options = {}) {
    const method = options.method || 'GET';
    const headers = Object.assign({'Accept':'application/json'}, options.headers || {});
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (method !== 'GET' && method !== 'HEAD' && state.csrf) headers['X-Cinema-CSRF'] = state.csrf;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeout || 60000);
    try {
      const response = await fetch(path, {method,headers,body:options.body === undefined ? undefined : JSON.stringify(options.body),credentials:'same-origin',cache:'no-store',signal:controller.signal});
      let data; try { data = await response.json(); } catch (_) { data = {}; }
      if (!response.ok) { const error = new Error(errorMessage(data)); error.status = response.status; throw error; }
      return data;
    } catch (error) { if (error.name === 'AbortError') throw new Error('请求超时。操作可能仍在完成，请先重新读取状态，再决定是否重试。'); throw error; }
    finally { clearTimeout(timeout); }
  }
  function notify(message, kind = '') { const el=$('status-message');el.textContent=message;el.className='status-message '+kind;el.hidden=false; }
  function fieldValue(field) {
    if (Object.hasOwn(state.draft,field.key)) {
      if (state.draft[field.key] !== null) return state.draft[field.key];
      return state.scope === 'global' ? field.default : state.config.values[field.key];
    }
    return (state.config.effective_values || effectiveValues(state.config,state.scope))[field.key] ?? field.default;
  }
  function eligible(field) {
    if (!field.scope) return state.scope === 'global';
    const scopes = Array.isArray(field.scope) ? field.scope : [field.scope];
    const base = state.scope.split(':')[0];
    return scopes.some(s => s === state.scope || s === base || s === base+':ID' || s === '*');
  }
  function setDraft(key,value) {
    state.draft[key]=value;
    const original=(state.config.effective_values || effectiveValues(state.config,state.scope))[key];
    if (value !== null && JSON.stringify(original) === JSON.stringify(value)) delete state.draft[key];
    updateSavebar();
  }
  function updateSavebar() {
    const count=Object.keys(state.draft).length, errors=Object.keys(state.invalid).length;
    $('savebar').hidden=false;
    $('save-count').textContent=errors?`${errors} 项需要修正`:count?`${count} 项修改尚未保存`:'设置已与本机同步';
    $('save-note').textContent=state.scope==='global'?'保存规则后，根据提示应用到运行环境。':`当前范围：${state.scope}；未覆盖的值继续继承。`;
    $('save-button').disabled=!count || !!errors || state.busy;
  }
  function renderNavigation() {
    $('navigation').innerHTML=state.schema.sections.map(s=>`<button type="button" class="nav-button" data-section="${esc(s.id)}" ${state.section===s.id?'aria-current="page"':''}><span class="nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${icons[s.id]||'<circle cx="12" cy="12" r="3"/>'}</svg></span><span>${esc(s.label||labels[s.id]||s.id)}</span></button>`).join('');
    alignMobileNavigation();
  }
  function alignMobileNavigation() {
    if (!window.matchMedia('(max-width:720px)').matches) return;
    const nav=$('navigation'),selected=nav.querySelector('[aria-current="page"]');
    if (selected) {
      const container=nav.getBoundingClientRect(),current=selected.getBoundingClientRect();
      nav.scrollLeft+=current.left-container.left-(container.width-current.width)/2;
    }
  }
  window.matchMedia('(max-width:720px)').addEventListener('change',alignMobileNavigation);
  function renderWizard() {
    const complete=state.status.setup?.complete || state.status.setup?.completed;
    if (!state.wizard) { $('banner').innerHTML=complete?'':`<div class="onboarding-banner"><div><h2>从基础配置开始</h2><p>先连接 Jellyfin 与 MPV，再选择媒体来源和缓存方式。</p></div><button class="primary" type="button" data-action="wizard">开始配置</button></div>`;return; }
    const step=wizardSteps[state.step];
    $('banner').innerHTML=`<section class="wizard" aria-label="首次启动向导"><div class="wizard-steps">${wizardSteps.map((s,i)=>`<button type="button" data-wizard-step="${i}" class="${i===state.step?'current':''}" ${i===state.step?'aria-current="step"':''}>${i+1} · ${s.label}</button>`).join('')}</div><h2>${step.title}</h2><p>${step.description}</p><div class="wizard-footer"><button type="button" class="text-button" data-action="wizard-close">稍后继续</button><div class="button-row">${state.step?'<button type="button" data-action="wizard-back">上一步</button>':''}${state.step===3?'<button type="button" data-action="wizard-check">检查基础服务</button><button type="button" class="primary" data-action="wizard-complete">完成初始配置</button>':'<button type="button" class="primary" data-action="wizard-next">保存并继续</button>'}</div></div></section>`;
  }
  function fieldHtml(field) {
    const id='f-'+field.key.replace(/[^a-zA-Z0-9_-]/g,'-'), value=fieldValue(field), inherited=state.scope!=='global'&&!Object.hasOwn(state.config.overrides?.[state.scope]||{},field.key), effect=effects[field.effect]||field.effect||'保存后应用';
    let input='';
    if (field.kind==='bool') input=`<label class="switch-control"><span class="value-boolean">${value?'已开启':'已关闭'}</span><input id="${id}" type="checkbox" class="switch" role="switch" data-key="${esc(field.key)}" ${value?'checked':''} aria-labelledby="${id}-label"></label>`;
    else if (field.kind==='select') input=`<select id="${id}" data-key="${esc(field.key)}">${(field.options||[]).map(o=>{const v=typeof o==='object'?o.value:o,l=typeof o==='object'?o.label:o;return `<option value="${esc(v)}" ${v===value?'selected':''}>${esc(l)}</option>`;}).join('')}</select>`;
    else if (field.kind==='list') {
      if (Array.isArray(value) && value.every(v=>typeof v==='string') && field.key!=='sources.entries') input=`<textarea id="${id}" data-key="${esc(field.key)}" data-lines="true" rows="3">${esc(value.join('\n'))}</textarea><span class="small-label">每行一项，按从上到下的顺序使用</span>`;
      else input=`<textarea id="${id}" data-key="${esc(field.key)}" class="mono" rows="3">${esc(JSON.stringify(value,null,2))}</textarea>`;
    } else {
      const numeric=field.kind==='int'||field.kind==='float';
      input=`<input id="${id}" type="${numeric?'number':'text'}" data-key="${esc(field.key)}" value="${esc(value)}" ${numeric?`step="${field.kind==='int'?1:field.step||'any'}" ${field.min!=null?`min="${field.min}"`:''} ${field.max!=null?`max="${field.max}"`:''}`:''} ${field.kind==='path'?'spellcheck="false"':''} autocomplete="off">`;
    }
    const locked=field.available===false||field.readonly===true||field.invariant===true;
    if(locked)input=input.replace(/<(input|select|textarea)\b/g,'<$1 disabled');
    return `<div class="setting-row ${state.invalid[field.key]?'invalid':''}" data-field="${esc(field.key)}"><div><label class="setting-label" id="${id}-label" for="${id}">${esc(field.label)} ${field.advanced?'<span class="advanced-tag">高级</span>':''}</label><p class="setting-help" id="${id}-help">${esc(field.help||'')}</p><div class="field-meta"><span class="effect ${field.effect==='restart'?'pending':''}">${esc(field.invariant?'固定正确性规则':locked?'当前版本只读':effect)}</span><span class="inherited">${inherited?'继承系统规则':state.scope==='global'?'系统规则':'当前范围覆盖'}</span></div></div><div class="field-control">${input}${locked?`<p class="form-hint">${esc(field.reason||(field.invariant?'始终保持这项正确性保证。':'当前版本尚未接入此设置。'))}</p>`:''}<p class="field-error" ${state.invalid[field.key]?'':'hidden'}>${esc(state.invalid[field.key]||'')}</p><button type="button" class="field-reset" data-reset-key="${esc(field.key)}" ${locked?'disabled':''}>${state.scope==='global'?'恢复推荐默认值':'恢复继承'}</button></div></div>`;
  }
  const statusLabels={unconfigured:'待配置',not_configured:'待配置',configured:'配置已就绪',source_configured:'已启用 · 待读取验证',needs_setup:'等待首次配置',missing:'缺少组件',disabled:'未启用',ready:'可用',connected:'已连接',running:'运行中',authenticated:'已授权',stopped:'未启动',offline:'离线',error:'连接失败',failed:'连接失败',degraded:'能力受限',limited:'能力受限',unsupported:'暂不支持',needs_auth:'需要授权',unauthorized:'需要授权',expired:'授权过期',incompatible:'版本不兼容',available:'已检测到'};
  function badge(status) {const good=['ready','connected','running','authenticated','available'].includes(status),bad=['failed','error','unauthorized','expired'].includes(status),warn=['limited','degraded','incompatible','unsupported','needs_auth'].includes(status);return `<span class="status-badge ${good?'good':bad?'bad':warn?'warn':''}">${esc(statusLabels[status]||status||'未知')}</span>`;}
  function integrationHtml(service) {
    const actionLabels={probe:'检测连接',login:'登录授权',plan:'配置计划',apply:'应用配置',install_plan:'下载与安装',install_apply:'安装组件',start:'启动',stop:'停止',browse:'浏览目录',drivers:'查看驱动',libraries:'查看媒体库'};
    const actions=(service.actions||[]).map(a=>typeof a==='string'?{id:a,label:actionLabels[a]||a}:a).filter(a=>!['apply','install_apply'].includes(a.id));
    return `<section class="integration-card ${service.required?'':'optional'}"><div class="integration-head"><div><h3>${esc(service.name||service.id)}</h3><small>${service.required?'影院基础':'按需使用'} · ${({managed:'本程序托管',disabled:'未启用',embedded:'内置 libMPV',external:'外置 MPV'})[service.mode]||'连接已有服务'}</small></div>${badge(service.state)}</div><p>${esc(service.detail||'配置后检测实际连接状态。')}</p>${service.download_page&&/^https:\/\//i.test(service.download_page)?`<a class="inline-link" href="${esc(service.download_page)}" target="_blank" rel="noopener noreferrer">官方下载与校验值 ↗</a>`:''}<div class="button-row">${actions.map(a=>`<button type="button" class="small-button" data-service="${esc(service.id)}" data-service-action="${esc(a.id)}">${esc(a.label||actionLabels[a.id]||a.id)}</button>`).join('')}</div></section>`;
  }
  function renderIntegrations(ids) { const items=(state.status.integrations||[]).filter(i=>ids.includes(i.id));return items.length?`<div class="integration-grid">${items.map(integrationHtml).join('')}</div>`:''; }
  function sourcesHtml() {
    const field=state.schema.fields.find(f=>f.key==='sources.entries');if(!field||!eligible(field))return '';
    const entries=fieldValue(field)||[];
    return `<section class="setting-card"><div class="card-heading"><div><h2>我的媒体来源</h2><p>选择具体媒体目录；新来源默认只读。</p></div><button type="button" class="primary" data-action="source-add">添加来源</button></div>${entries.length?`<div class="sources-list">${entries.map((e,i)=>`<div class="source-row"><div><div class="source-title"><strong>${esc(e.name||e.id)}</strong>${badge(e.enabled===false?'disabled':'source_configured')}</div><p>${esc(({local:'本地目录',openlist:'OpenList',webdav:'WebDAV'})[e.kind]||e.kind)} · ${e.allow_write?'允许整理':'只读'}</p><p class="mono">${esc(e.path)}</p></div><div class="button-row"><button type="button" class="small-button" data-source-edit="${i}">编辑</button><button type="button" class="small-button subtle" data-source-remove="${i}">移除连接</button></div></div>`).join('')}</div>`:'<div class="empty">还没有添加媒体来源<p>已连接的 Jellyfin 媒体库仍可直接使用。</p></div>'}</section>`;
  }
  function credentialHtml() {
    const known=Object.assign({'jellyfin.token':false,'openlist.token':false,'ratings.mdblist':false},state.credentials);
    for(const field of state.schema.fields)if(field.key.endsWith('_credential')||field.key.endsWith('.credential')||field.key.endsWith('.credential_ref')){const key=fieldValue(field);if(typeof key==='string'&&key)known[key]=!!state.credentials[key];}
    return `<section class="setting-card"><div class="card-heading"><div><h2>安全凭据</h2><p>仅显示是否配置。密码和密钥不会从服务端读回。</p></div><button type="button" data-action="credential-add">添加凭据</button></div>${Object.entries(known).map(([name,set])=>`<div class="credential-row"><div><strong class="mono">${esc(name)}</strong><small>${set?'已保存在本机凭据存储':'尚未配置'}</small></div><div class="button-row"><button type="button" class="small-button" data-credential="${esc(name)}">${set?'更换':'设置'}</button>${set?`<button type="button" class="small-button subtle" data-credential-delete="${esc(name)}">移除</button>`:''}</div></div>`).join('')}</section>`;
  }
  function runtimeHtml() {
    const runtime=state.status.runtime||{};
    return `<section class="runtime-strip"><div><h3>运行环境 ${badge(runtime.state||runtime.status||'stopped')}</h3><p>${esc(runtime.detail||runtime.message||'设置保存后，先应用到独立运行环境，再启动服务或影院。')}</p></div><div class="button-row"><button type="button" data-runtime="apply">应用运行配置</button><button type="button" data-runtime="start">启动服务</button><button type="button" data-runtime="launch-player" class="primary">打开影院</button><button type="button" data-runtime="stop" class="subtle">停止托管服务</button></div></section>`;
  }
  function tasksHtml() {
    const tasks=state.tasks||{},items=tasks.items||[];
    const link=tasks.workbench_url&&/^https?:\/\//i.test(tasks.workbench_url)?`<a class="inline-link" target="_blank" rel="noopener noreferrer" href="${esc(tasks.workbench_url)}">打开完整工作台 ↗</a>`:'';
    return `<section class="setting-card"><div class="card-heading"><div><h2>入库工作台</h2><p>只处理所选范围；资料缺失与连接失败分别记录。</p></div><button type="button" data-action="tasks-refresh">刷新任务</button></div><div class="button-row" style="padding:18px 24px">${(tasks.actions||[]).map(a=>`<button type="button" data-task-action="${esc(a.id)}">${esc(a.label||a.id)}</button>`).join('')}${link}</div>${items.length?`<div class="task-list">${items.slice(0,50).map(t=>`<div class="task-row"><div><strong>${esc(t.name||t.title||t.id)}</strong><p>${esc(t.detail||t.message||'')}</p></div>${badge(t.state||t.status)}</div>`).join('')}</div>`:`<div class="empty">${state.tasks?'当前没有待处理任务':'点击刷新，读取当前任务状态'}<p>连接并启动入库运行环境后，会显示可用的扫描和修复操作。</p></div>`}</section>`;
  }
  function organizingHtml(){
    const items=state.organizing?.items||(state.config.values['sources.entries']||[]).filter(s=>s.kind==='openlist'&&s.enabled!==false).map(s=>({source_id:s.id,name:s.name,source_root:s.path,supported:['Quark','BaiduNetdisk'].includes(s.driver),allow_write:s.allow_write}));
    return `<section class="setting-card"><div class="card-heading"><div><h2>整理计划</h2><p>先完成入库识别，再生成变更预览；执行许可只对应当前计划和当前来源。</p></div><button type="button" data-action="organizing-refresh">刷新计划</button></div>${items.length?items.map(p=>`<div class="source-row"><div><strong>${esc(p.name||p.source_id)}</strong><p class="mono">${esc(p.source_root)}</p><p>${p.supported?`${p.operation_count||0} 项移动或改名 · ${p.review_count||0} 项待确认`:'此驱动仅只读播放；暂不支持安全整理'}</p></div><div class="button-row"><button type="button" data-organizing="plan" data-source-id="${esc(p.source_id)}" ${p.supported?'':'disabled'}>生成预览</button>${p.plan_id?`<button type="button" data-organizing="review" data-source-id="${esc(p.source_id)}">查看计划</button>`:''}</div></div>`).join(''):'<div class="empty">先在媒体来源中添加云盘目录</div>'}</section>`;
  }
  function maintenanceHtml(){
    const rows=state.backups?.items;
    return `<section class="setting-card"><div class="card-heading"><div><h2>配置与诊断</h2><p>分享配置与本机恢复备份分开管理。</p></div></div>
      <div class="setting-row"><div><h3>可分享的配置</h3><p class="setting-help">导出不含密钥、账号、地址和来源目录；导入时保留这台设备已有连接。</p></div><div class="button-row"><button type="button" data-action="export">导出配置</button><button type="button" data-action="import">导入配置</button></div></div>
      <div class="setting-row"><div><h3>运行诊断</h3><p class="setting-help">查看服务与运行状态，导出报告用于排查。</p></div><div class="button-row"><button type="button" data-action="diagnostics">查看诊断</button><button type="button" data-action="maintenance-prune">清理过期日志</button></div></div>
      <div class="setting-row"><div><h3>Windows 登录自启</h3><p class="setting-help">按下方“登录后启动后台”开关应用当前账户的启动规则。</p></div><div class="button-row"><button type="button" data-action="maintenance-autostart">应用自启规则</button></div></div></section>
      <section class="setting-card"><div class="card-heading"><div><h2>本机恢复备份</h2><p>包含私有配置和加密凭据；仅供原 Windows 账户恢复，请勿作为公开文件分享。</p></div><div class="button-row"><button type="button" data-action="backups-refresh">刷新列表</button><button type="button" class="primary" data-action="backup-create">创建备份</button></div></div>
      ${rows?.length?rows.map(row=>`<div class="source-row"><div><strong>${esc(new Date(row.created_at).toLocaleString())}</strong><p class="mono">${esc(row.id)}</p><p>${Math.round((Number(row.size)||0)/1024)} KiB · ${row.includes_credentials?'包含本机加密凭据':'仅配置'}</p></div><button type="button" data-backup-restore="${esc(row.id)}">恢复这份备份</button></div>`).join(''):`<div class="empty">${rows?'尚无本机备份':'点击刷新读取本机备份'}<p>备份不会复制影片与播放缓存。</p></div>`}</section>`;
  }
  function render() {
    renderNavigation();renderWizard();
    $('section-title').textContent=state.query?'搜索设置':labels[state.section]||state.section;
    $('section-description').textContent=state.query?`正在搜索“${state.query}”，包含所有分组。`:descriptions[state.section]||'';
    let html='';
    if(!state.query&&state.scope==='global') {
      if(state.section==='foundation')html+=renderIntegrations(['jellyfin','mpv','ffprobe'])+runtimeHtml();
      if(state.section==='sources')html+=renderIntegrations(['openlist']);
      if(state.section==='network')html+=renderIntegrations(['hypomux']);
      if(state.section==='sources')html+=sourcesHtml();
      if(['ratings','maintenance'].includes(state.section))html+=credentialHtml();
      if(state.section==='automation')html+=tasksHtml();
      if(state.section==='organizing')html+=organizingHtml();
      if(state.section==='maintenance')html+=maintenanceHtml()+runtimeHtml();
    }
    let fields=filterFields(state.schema.fields,state.query?null:state.section,state.query).filter(f=>eligible(f)&&f.key!=='sources.entries'&&(state.advanced||!f.advanced||state.query));
    const backend=state.schema.fields.find(f=>f.key==='foundation.mpv_backend');
    if(!state.query&&backend&&fieldValue(backend)!=='external') fields=fields.filter(f=>f.key!=='foundation.mpv_path');
    if(fields.length){
      const groups=state.query?state.schema.sections.filter(s=>fields.some(f=>f.section===s.id)):[{id:state.section,label:'偏好与规则'}];
      for(const group of groups){const fs=fields.filter(f=>f.section===group.id);if(!fs.length)continue;html+=`<section class="setting-card"><div class="card-heading"><h2>${esc(group.label||labels[group.id])}</h2><button type="button" class="text-button" data-reset-section="${esc(group.id)}">${state.scope==='global'?'恢复本组默认':'恢复本组继承'}</button></div>${fs.map(fieldHtml).join('')}</section>`;}
    }
    if(!html)html='<div class="empty">这个范围没有匹配的设置<p>可以更换搜索词、打开高级设置或切换作用范围。</p></div>';
    $('panel').innerHTML=html;
    document.querySelectorAll('[data-key]').forEach(el=>{const f=state.schema.fields.find(f=>f.key===el.dataset.key);el.setAttribute('aria-describedby',el.id+'-help');if(f?.help)el.title=f.help;});
    updateSavebar();
  }
  async function refreshStatus(){state.status=await api('/api/status');state.credentials=(await api('/api/credentials')).configured||{};$('connection').textContent='本机设置服务已连接';}
  async function loadConfig(){state.config=await api('/api/config?scope='+encodeURIComponent(state.scope));}
  async function save(){
    if(Object.keys(state.invalid).length)throw new Error('请先修正标出的设置。');
    if(!Object.keys(state.draft).length)return;
    try {
      const result=await api('/api/config',{method:'POST',body:{revision:state.config.revision,changes:state.draft,scope:state.scope}});
      state.config=result;state.draft={};await loadConfig();await refreshStatus();render();
      notify('设置已保存。需要更新运行环境的项目，请点击“应用运行配置”；下次播放或下批任务生效的项目不会打断当前观看。');
    }catch(error){if(error.status===409)notify('设置已被其他窗口修改。你的改动仍保留；请先撤销并重新读取最新值，再修改保存。','warning');throw error;}
  }
  function modal(title,content,onSubmit,submitLabel='确认'){
    $('modal-title').textContent=title;$('modal-content').innerHTML=content;$('modal-error').hidden=true;
    $('modal-buttons').innerHTML=`<button type="button" data-action="close-modal">取消</button>${onSubmit?`<button type="submit" class="primary">${esc(submitLabel)}</button>`:'<button type="button" class="primary" data-action="close-modal">关闭</button>'}`;
    state.modalSubmit=onSubmit;if(!$('modal').open)$('modal').showModal();
    requestAnimationFrame(()=>($('modal-content').querySelector('input,select,textarea')||$('modal-buttons').querySelector('button')).focus());
  }
  function inputField(label,name,value='',type='text',help=''){return `<label class="modal-field"><span>${esc(label)}</span><input name="${esc(name)}" type="${type}" value="${esc(value)}" autocomplete="${type==='password'?'new-password':'off'}">${help?`<small>${esc(help)}</small>`:''}</label>`;}
  function closeModal(){ $('modal').close();$('modal-content').querySelectorAll('input[type=password]').forEach(e=>e.value='');state.modalSubmit=null; }
  function credentialDialog(name=''){
    modal(name?'更新凭据':'添加凭据',`<p class="dialog-note">凭据仅保存于本机安全存储，不会显示已有值。服务登录请优先使用“基础服务”或“媒体来源”中的授权按钮。</p>${inputField('凭据名称','name',name)}${inputField('新密钥或令牌','value','','password')}`,async form=>{
      const key=form.elements.name.value.trim(),value=form.elements.value.value;if(!key||!value)throw new Error('名称和凭据不能为空。');
      try{await api('/api/credentials',{method:'POST',body:{name:key,value}});}finally{form.elements.value.value='';}
      await refreshStatus();render();notify('凭据已保存。请检测对应服务，确认授权有效。');
    },'保存到本机');
  }
  function sourceDialog(index=-1){
    const f=state.schema.fields.find(f=>f.key==='sources.entries'),entries=[...(fieldValue(f)||[])],entry=entries[index]||{id:'',name:'',kind:'openlist',path:'',enabled:true,allow_write:false};
    modal(index<0?'添加媒体来源':'编辑媒体来源',`<p class="dialog-note">选择具体媒体目录。本地来源只读；WebDAV 请先作为 OpenList 的驱动挂载，再添加挂载目录。</p>
      <div class="modal-grid">${inputField('来源 ID','id',entry.id,'text','固定标识，只使用字母、数字、_ 或 -。')}${inputField('显示名称','name',entry.name)}</div>
      <label class="modal-field"><span>来源类型</span><select name="kind">${[['openlist','OpenList 挂载'],['local','本地目录（只读）']].map(([v,l])=>`<option value="${v}" ${entry.kind===v?'selected':''}>${l}</option>`).join('')}</select></label>
      ${inputField('媒体目录','path',entry.path,'text','OpenList 例如 /我的网盘/电影；本地填写完整目录路径。')}
      <div data-cloud-source ${entry.kind==='local'?'hidden':''}>
        <label class="modal-field"><span>云盘管理驱动</span><select name="driver">${[['','只读 / 不指定'],['Quark','夸克'],['BaiduNetdisk','百度网盘']].map(([v,l])=>`<option value="${v}" ${entry.driver===v?'selected':''}>${l}</option>`).join('')}</select></label>
        ${inputField('OpenList 挂载点','mount_path',entry.mount_path||'','text','例如 /我的网盘。必须与 OpenList 中的挂载点完全一致。')}
        ${inputField('云盘原生根目录（可选）','native_root',entry.native_root||'','text','夸克填写具体文件夹 ID；百度填写云端绝对目录。留空时从明确选择的挂载点与媒体路径解析。')}
        ${inputField('独立管理凭据引用（可选）','credential',entry.credential||'','text','留空时仅在内存中复用该 OpenList 挂载的授权。填写凭据名称，不在此粘贴密码。')}
      </div>
      <label class="check-label"><input name="enabled" type="checkbox" ${entry.enabled!==false?'checked':''}>启用这个来源</label><p></p>
      <label class="check-label"><input name="allow_write" type="checkbox" ${entry.allow_write&&entry.kind==='openlist'?'checked':''} ${entry.kind==='local'?'disabled':''}>允许在该目录内整理、移动和重命名</label>
      <p class="form-hint">写入还需启用整理，并按规则预览确认计划。其他驱动可只读播放，不会被自动接管。</p>`,async form=>{
      const input=Object.assign({},entry,{id:form.elements.id.value.trim(),name:form.elements.name.value,kind:form.elements.kind.value,path:form.elements.path.value,enabled:form.elements.enabled.checked,allow_write:form.elements.kind.value==='openlist'&&form.elements.allow_write.checked});
      for(const key of ['url','credential','driver','mount_path','native_root'])delete input[key];
      if(input.kind==='openlist')for(const key of ['credential','driver','mount_path','native_root'])if(form.elements[key].value.trim())input[key]=form.elements[key].value.trim();
      if(input.allow_write&&!['Quark','BaiduNetdisk'].includes(input.driver))throw new Error('请先选择支持整理的云盘驱动；其他驱动可保持只读。');
      const parsed=sourceEntry(input);if(parsed.error)throw new Error(parsed.error);
      if(entries.some((e,i)=>i!==index&&e.id===parsed.value.id))throw new Error('来源 ID 已存在，请使用另一个 ID。');
      if(index<0)entries.push(parsed.value);else entries[index]=parsed.value;
      setDraft('sources.entries',entries);render();notify('来源修改已加入待保存设置。点击底部“保存设置”后生效。');
    },'加入待保存设置');
    $('modal-content').querySelector('[name=kind]').addEventListener('change',event=>{const local=event.target.value==='local',write=$('modal-content').querySelector('[name=allow_write]');write.disabled=local;if(local)write.checked=false;$('modal-content').querySelector('[data-cloud-source]').hidden=local;});
  }
  async function serviceAction(provider,action){
    if(Object.keys(state.draft).length)await save();
    if(action==='install_plan')return modal('准备组件安装',`<p class="dialog-note">填写官方发布页提供的 ZIP 下载地址和 SHA-256。下载会先校验内容，再安装到本实例的独立目录；不会自动启动服务。</p>${inputField('ZIP 下载地址','url','','url')}${inputField('SHA-256 校验值','sha256')}${inputField('版本号','version')}${inputField('ZIP 内程序路径（可选）','executable','','text','例如 bin/mpv.exe；留空由组件安装器定位。')}`,async form=>{
      const payload={url:form.elements.url.value.trim(),sha256:form.elements.sha256.value.trim(),version:form.elements.version.value.trim()};
      if(form.elements.executable.value.trim())payload.executable=form.elements.executable.value.trim();
      const result=await api(`/api/integrations/${provider}/install_plan`,{method:'POST',body:payload});setTimeout(()=>showPlan(provider,result,'install_apply'),0);
    },'预览安装计划');
    if(action==='login')return modal(`登录 ${provider==='jellyfin'?'Jellyfin':'OpenList'}`,`<p class="dialog-note">向已配置的服务地址发送登录请求，成功后只保存凭据引用。密码不会留在页面中。</p>${inputField('用户名','username',state.config.values[provider==='jellyfin'?'foundation.jellyfin_username':'sources.openlist_username']||'')}${inputField('密码','password','','password')}`,async form=>{
      let result;try{result=await api(`/api/integrations/${provider}/login`,{method:'POST',body:{username:form.elements.username.value,password:form.elements.password.value}});}finally{form.elements.password.value='';}
      await refreshStatus();await loadConfig();render();notify(result.detail||'登录已完成，请检测连接。');
    },'登录并保存授权');
    if(action==='plan'){
      const mode=state.config.values[provider==='jellyfin'?'foundation.jellyfin_mode':'sources.openlist_mode'];
      const usernameKey=provider==='jellyfin'?'foundation.jellyfin_username':'sources.openlist_username';
      const fields=['jellyfin','openlist'].includes(provider)&&mode==='managed'?`<p class="dialog-note">首次初始化需要设置管理员。已有实例不会覆盖管理员或密码。</p>${inputField('新实例管理员用户名','username',state.config.values[usernameKey]||'')}${inputField('新实例管理员密码','password','','password')}`:'';
      return modal('准备配置计划',`<p class="dialog-note">先读取现状并生成变更清单。已有服务不会被默认重置；只处理明确属于本程序的资源。</p>${fields}`,async form=>{
        const payload={};if(form.elements.username)payload.username=form.elements.username.value;if(form.elements.password)payload.password=form.elements.password.value;
        let result;try{result=await api(`/api/integrations/${provider}/plan`,{method:'POST',body:payload});}finally{if(form.elements.password)form.elements.password.value='';}
        setTimeout(()=>showPlan(provider,result),0);
      },'生成计划');
    }
    if(action==='browse')return modal('浏览 OpenList 目录',inputField('目录路径','path','/'),async form=>{const result=await api(`/api/integrations/${provider}/browse`,{method:'POST',body:{path:form.elements.path.value}});setTimeout(()=>modal('目录结果',`<pre class="diagnostics">${esc(JSON.stringify(result,null,2))}</pre>`,null),0);},'读取目录');
    const result=await api(`/api/integrations/${provider}/${action}`,{method:'POST',body:{}});
    await refreshStatus();render();
    if(action==='drivers'||action==='libraries')modal(action==='drivers'?'可用驱动':'媒体库列表',`<pre class="diagnostics">${esc(JSON.stringify(result,null,2))}</pre>`,null);
    else notify(result.detail||'操作已返回，请查看服务状态。', ['error','failed','unsupported'].includes(result.state)?'warning':'');
  }
  function showPlan(provider,result,applyAction='apply'){
    const changes=result.changes||[];
    const canApply=!!result.plan_id&&!['manual','manual_required','unsupported'].includes(result.state);
    modal('配置变更预览',`<p class="dialog-note">${esc(result.detail||'请确认下面的变更。应用后会再次验证实际状态。')}</p><ol class="change-list">${changes.map(c=>`<li>${esc(typeof c==='string'?c:JSON.stringify(c))}</li>`).join('')}</ol>${result.requires_restart?'<p class="dialog-note">此计划需要重启相应托管服务。</p>':''}`,canApply?async()=>{
      if(applyAction==='install_apply')notify('正在下载、校验并安装组件，完成后会更新程序路径。');
      const applied=await api(`/api/integrations/${provider}/${applyAction}`,{method:'POST',body:{plan_id:result.plan_id},timeout:applyAction==='install_apply'?600000:60000});await loadConfig();await refreshStatus();render();notify(applied.detail||'配置计划已执行，请查看验证结果。');
    }:null,'应用此计划');
  }
  function download(name,data){const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  function restoreScopeControl(){const parts=state.scope.split(':');$('scope').value=parts[0];$('scope-id-label').hidden=parts.length===1;if(parts.length>1)$('scope-id').value=parts.slice(1).join(':');}
  async function changeScope(scope){
    if(Object.keys(state.draft).length||Object.keys(state.invalid).length){restoreScopeControl();throw new Error('请先保存或撤销当前修改，再切换作用范围。');}
    const config=await api('/api/config?scope='+encodeURIComponent(scope));state.scope=scope;state.config=config;state.invalid={};restoreScopeControl();render();
  }
  async function action(name){
    if(name==='save')return save();
    if(name==='discard'){state.draft={};state.invalid={};await loadConfig();render();notify('已重新读取最新设置。');return;}
    if(name==='wizard'){if(state.scope!=='global'){await changeScope('global');$('scope').value='global';$('scope-id-label').hidden=true;}state.wizard=true;state.step=0;state.section='foundation';state.query='';$('search').value='';render();return;}
    if(name==='wizard-close'){state.wizard=false;renderWizard();return;}
    if(name==='wizard-next'){await save();state.step=Math.min(3,state.step+1);state.section=wizardSteps[state.step].section;render();return;}
    if(name==='wizard-back'){state.step=Math.max(0,state.step-1);state.section=wizardSteps[state.step].section;render();return;}
    if(name==='wizard-check'){await save();await Promise.all(['jellyfin','mpv'].map(p=>api(`/api/integrations/${p}/probe`,{method:'POST',body:{}})));await refreshStatus();render();return;}
    if(name==='wizard-complete'){await save();const result=await api('/api/setup/complete',{method:'POST',body:{}});await refreshStatus();state.wizard=false;render();notify(result.detail||'初始配置已完成。可以打开影院，或继续调整设置。');return;}
    if(name==='scope-apply'){const kind=$('scope').value,id=$('scope-id').value.trim();if(!id)throw new Error('请填写对象 ID。');return changeScope(kind+':'+id);}
    if(name==='source-add')return sourceDialog();
    if(name==='credential-add')return credentialDialog();
    if(name==='close-modal')return closeModal();
    if(name==='export'){download('personal-cinema-settings.json',await api('/api/config/export'));notify('已导出不含密钥的配置。');return;}
    if(name==='import'){$('import-file').click();return;}
    if(name==='diagnostics'){const data=await api('/api/diagnostics');modal('运行诊断',`<p class="dialog-note">查看本机报告；导出内容不包含凭据值。</p><pre class="diagnostics">${esc(JSON.stringify(data,null,2))}</pre>`,async()=>download('personal-cinema-diagnostics.json',data),'导出诊断');return;}
    if(name==='backups-refresh'){state.backups=await api('/api/maintenance/backups');render();return;}
    if(name==='backup-create'){await save();const result=await api('/api/maintenance/backup',{method:'POST',body:{}});state.backups=await api('/api/maintenance/backups');render();notify('本机备份已创建：'+result.id);return;}
    if(name==='maintenance-autostart'){await save();const result=await api('/api/maintenance/autostart',{method:'POST',body:{}});notify(result.enabled?'已启用此 Windows 账户的影院后台自启。':'已关闭此 Windows 账户的影院后台自启。');return;}
    if(name==='maintenance-prune')return modal('清理过期日志',`<p class="dialog-note">按设置的诊断保留天数，删除此实例管理目录中的过期日志与诊断文件。影片、封面、配置和播放缓存不参与清理。</p>`,async()=>{await save();const result=await api('/api/maintenance/prune',{method:'POST',body:{}});notify(`日志清理已完成：删除 ${Array.isArray(result.removed)?result.removed.length:Number(result.removed)||0} 项，跳过 ${Array.isArray(result.skipped)?result.skipped.length:Number(result.skipped)||0} 项。`);},'按保留期清理');
    if(name==='organizing-refresh'){state.organizing=await api('/api/organizing/plans');render();return;}
    if(name==='tasks-refresh'){state.tasks=await api('/api/tasks');render();return;}
  }
  async function guarded(fn,button){
    if(button?.disabled)return;const original=button?.textContent;if(button)button.disabled=true;
    try{await fn();}catch(error){notify(error.message||'操作未完成。','error');}
    finally{if(button?.isConnected){button.disabled=false;if(original)button.textContent=original;}updateSavebar();}
  }
  document.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button||button.type==='submit')return;
    guarded(async()=>{
      if(button.dataset.section){state.section=button.dataset.section;state.query='';$('search').value='';state.wizard=false;history.replaceState(null,'','#'+state.section);render();return;}
      if(button.dataset.wizardStep!==undefined){state.step=Number(button.dataset.wizardStep);state.section=wizardSteps[state.step].section;render();return;}
      if(button.dataset.serviceAction)return serviceAction(button.dataset.service,button.dataset.serviceAction);
      if(button.dataset.runtime){await save();const result=await api('/api/runtime/'+button.dataset.runtime,{method:'POST',body:{}});await refreshStatus();render();notify(result.detail||result.message||'操作已完成，请查看运行状态。');return;}
      if(button.dataset.backupRestore){
        if(Object.keys(state.draft).length||Object.keys(state.invalid).length)throw new Error('请先保存或撤销当前修改，再恢复备份。');
        const id=button.dataset.backupRestore,revision=state.config.revision;
        return modal('恢复本机配置与凭据',`<p class="dialog-note">将使用 <span class="mono">${esc(id)}</span> 覆盖本实例当前配置与加密凭据。不会移动、替换或删除影片。</p><p class="dialog-note">请先退出播放器并停止所有托管服务。服务端会验证当前 Windows 账户可解密备份，并在覆盖前保存一份当前状态的安全备份。</p>`,async()=>{
          const result=await api('/api/maintenance/restore',{method:'POST',body:{id,revision}});state.draft={};state.invalid={};await loadConfig();await refreshStatus();state.backups=await api('/api/maintenance/backups');render();notify((result.message||'配置与凭据已恢复，请重新应用运行配置。')+(result.safety_backup?' 安全备份：'+result.safety_backup:''));
        },'覆盖配置并恢复');
      }
      if(button.dataset.organizing){
        await save();const source_id=button.dataset.sourceId;
        if(button.dataset.organizing==='plan'){await api('/api/organizing/plan',{method:'POST',body:{source_id}});notify('已请求生成计划。完成后点击“刷新计划”查看具体变更。');return;}
        const plan=state.organizing?.items.find(p=>p.source_id===source_id);if(!plan?.plan_id)throw new Error('请先刷新计划。');
        return modal('确认此来源的整理计划',`<p class="dialog-note">${esc(plan.name||plan.source_id)} · ${plan.operation_count} 项操作。批准绑定当前设置；修改来源或规则后需重新确认。</p><ol class="change-list">${(plan.changes||[]).map(c=>`<li><span class="mono">${esc(c.source)}</span><br>→ <span class="mono">${esc(c.target)}</span></li>`).join('')}</ol>`,plan.allow_write&&plan.phase!=='complete'?async()=>{await api('/api/organizing/apply',{method:'POST',body:{source_id,plan_id:plan.plan_id}});notify('此计划已提交执行。可在工作台和计划列表查看实际进度。');}:null,'批准并执行');
      }
      if(button.dataset.taskAction){const result=await api('/api/tasks/'+button.dataset.taskAction,{method:'POST',body:{}});state.tasks=await api('/api/tasks');render();notify(result.detail||'任务已加入队列，可在工作台查看进度。');return;}
      if(button.dataset.resetKey){setDraft(button.dataset.resetKey,null);delete state.invalid[button.dataset.resetKey];render();return;}
      if(button.dataset.resetSection){for(const f of state.schema.fields.filter(f=>f.section===button.dataset.resetSection&&eligible(f)&&f.available!==false&&!f.readonly&&!f.invariant)){setDraft(f.key,null);delete state.invalid[f.key];}render();return;}
      if(button.dataset.credential!==undefined)return credentialDialog(button.dataset.credential);
      if(button.dataset.credentialDelete)return modal('移除凭据',`<p class="dialog-note">移除 ${esc(button.dataset.credentialDelete)} 后，对应服务可能需要重新授权。不会删除媒体。</p>`,async()=>{await api('/api/credentials/'+encodeURIComponent(button.dataset.credentialDelete),{method:'DELETE'});await refreshStatus();render();},'移除凭据');
      if(button.dataset.sourceEdit!==undefined)return sourceDialog(Number(button.dataset.sourceEdit));
      if(button.dataset.sourceRemove!==undefined){const f=state.schema.fields.find(f=>f.key==='sources.entries'),entries=[...(fieldValue(f)||[])],i=Number(button.dataset.sourceRemove);return modal('移除来源连接',`<p class="dialog-note">将移除 ${esc(entries[i]?.name||'这个来源')} 的配置。不会删除原文件或远程目录。</p>`,async()=>{entries.splice(i,1);setDraft('sources.entries',entries);render();},'加入待保存修改');}
      if(button.id==='refresh'){await refreshStatus();render();notify('已更新服务状态；待保存的修改仍保留。');return;}
      if(button.dataset.action)return action(button.dataset.action);
    },button);
  });
  document.addEventListener('input',event=>{
    const el=event.target;if(!el.dataset.key)return;const field=state.schema.fields.find(f=>f.key===el.dataset.key);if(!field)return;
    let result;if(el.dataset.lines)result={value:el.value.split('\n').map(s=>s.trim()).filter(Boolean)};else result=parseValue(field,field.kind==='bool'?el.checked:el.value);
    const row=el.closest('.setting-row'),error=row?.querySelector('.field-error');
    if(result.error){state.invalid[field.key]=result.error;row?.classList.add('invalid');if(error){error.textContent=result.error;error.hidden=false;}el.setAttribute('aria-invalid','true');}
    else{delete state.invalid[field.key];row?.classList.remove('invalid');if(error)error.hidden=true;el.removeAttribute('aria-invalid');setDraft(field.key,result.value);if(field.kind==='bool'){const label=row?.querySelector('.value-boolean');if(label)label.textContent=result.value?'已开启':'已关闭';}}
    updateSavebar();
    if(field.key==='foundation.mpv_backend'&&!result.error){render();document.querySelector('[data-key="foundation.mpv_backend"]')?.focus({preventScroll:true});}
  });
  $('search').addEventListener('input',event=>{state.query=event.target.value.trim();render();});
  $('advanced').addEventListener('change',event=>{state.advanced=event.target.checked;render();});
  $('scope').addEventListener('change',event=>{
    const value=event.target.value;$('scope-id-label').hidden=['global','device'].includes(value);
    if(['global','device'].includes(value))guarded(()=>changeScope(value));
  });
  $('modal-form').addEventListener('submit',async event=>{
    event.preventDefault();if(!state.modalSubmit)return;const submit=event.currentTarget.querySelector('[type=submit]');if(submit.disabled)return;submit.disabled=true;$('modal-error').hidden=true;
    const handler=state.modalSubmit;
    try{await handler(event.currentTarget);if(state.modalSubmit===handler)closeModal();}
    catch(error){$('modal-error').textContent=error.message||'操作未完成。';$('modal-error').hidden=false;}
    finally{if(submit.isConnected)submit.disabled=false;}
  });
  $('modal').addEventListener('cancel',()=>{state.modalSubmit=null;$('modal-content').querySelectorAll('input[type=password]').forEach(e=>e.value='');});
  $('import-file').addEventListener('change',async event=>{
    const file=event.target.files[0];event.target.value='';if(!file)return;
    await guarded(async()=>{if(file.size>1024*1024)throw new Error('配置文件不能超过 1 MiB。');let data;try{data=JSON.parse(await file.text());}catch(_){throw new Error('不是有效的 JSON 配置文件。');}
      if(Object.keys(state.draft).length)throw new Error('请先保存或撤销当前修改，再导入配置。');
      modal('导入配置预览',`<p class="dialog-note">文件：${esc(file.name)}<br>配置版本：${esc(data.version??data.schema_version??'未标明')}<br>包含 ${Object.keys(data.values||{}).length} 项系统设置。服务端会验证所有字段、版本和路径；凭据需在这台设备重新授权。</p><p class="dialog-note">导入会修改本实例配置，随后仍需应用运行配置。已有媒体文件不会随配置导入而移动。</p>`,async()=>{await api('/api/config/import',{method:'POST',body:{revision:state.config.revision,config:data}});state.draft={};await loadConfig();render();notify('配置已导入。请核对本机路径与服务授权，再应用运行配置。');},'验证并导入');
    });
  });
  window.addEventListener('beforeunload',event=>{if(Object.keys(state.draft).length){event.preventDefault();event.returnValue='';}});
  async function boot(){
    try{
      const params=new URLSearchParams(location.hash.replace(/^#/,'')),token=params.get('token');
      if(token){history.replaceState(null,'',location.pathname+location.search);const session=await api('/api/session',{method:'POST',body:{token}});state.csrf=session.csrf;}
      else state.csrf=(await api('/api/session')).csrf;
      state.schema=await api('/api/schema');await loadConfig();await refreshStatus();
      const hash=location.hash.slice(1);if(labels[hash])state.section=hash;
      $('toolbar').hidden=false;$('app').setAttribute('aria-busy','false');render();
    }catch(error){
      $('app').setAttribute('aria-busy','false');$('connection').textContent='尚未连接';$('section-title').textContent='连接本机设置';$('section-description').textContent='从 Personal Cinema 启动器重新打开设置页，完成本机会话验证。';
      $('panel').innerHTML=`<section class="locked"><h2>暂时无法打开设置</h2><p>${esc(error.message||'本机会话未建立。')}</p><p>如果刚重启了配置服务，请使用启动器提供的新地址；不需要在聊天中发送任何凭据。</p><button type="button" id="retry-boot">重新连接</button></section>`;
      $('retry-boot').addEventListener('click',boot);
    }
  }
  boot();
})();
