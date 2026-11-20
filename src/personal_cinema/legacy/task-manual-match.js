(() => {
  'use strict';
  let active = null;
  const node = (tag, cls, text) => {
    const value = document.createElement(tag);
    if (cls) value.className = cls;
    if (text !== undefined) value.textContent = text;
    return value;
  };
  const requestId = () => {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
  };
  function styles() {
    if (document.getElementById('media-manual-match-style')) return;
    const style = node('style');
    style.id = 'media-manual-match-style';
    style.textContent = `
      .mm-dialog{box-sizing:border-box;width:min(680px,calc(100vw - 32px));max-height:calc(100dvh - 40px);padding:0;border:1px solid #ffffff24;border-radius:20px;color:var(--text,#f4f1ea);background:var(--surface,#1c2129);box-shadow:0 24px 90px #0008;font:inherit;color-scheme:dark;overflow:auto}
      .mm-dialog::backdrop{background:#090b10c9}
      .mm-dialog *{box-sizing:border-box}
      .mm-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:25px 26px 16px}
      .mm-head h2{margin:0;font-size:22px;line-height:1.4}
      .mm-sub{margin:6px 0 0;color:var(--muted,#a6afb9);font-size:14px;line-height:1.6;overflow-wrap:anywhere}
      .mm-dialog button,.mm-dialog input,.mm-dialog select{font:inherit}
      .mm-dialog button{min-height:44px;padding:9px 16px;border:1px solid #ffffff24;border-radius:12px;background:#ffffff0c;color:var(--text,#f4f1ea);cursor:pointer;line-height:1.4}
      .mm-dialog button:hover:not(:disabled){background:#ffffff18}
      .mm-dialog button:disabled{cursor:wait;opacity:.5}
      .mm-dialog button:focus-visible,.mm-dialog input:focus-visible,.mm-dialog select:focus-visible{outline:2px solid var(--accent,#e6dcc9);outline-offset:3px}
      .mm-dialog .mm-close{flex:none;width:38px;min-height:38px;padding:2px;border-radius:50%;font-size:24px;line-height:1}
      .mm-body{padding:0 26px 24px}
      .mm-step{display:block;margin:20px 0 10px;font-weight:600;line-height:1.5}
      .mm-search{display:grid;grid-template-columns:minmax(0,1fr) 100px auto;gap:8px}
      .mm-dialog input[type=search],.mm-dialog input[type=url],.mm-dialog select{width:100%;min-height:44px;padding:10px 12px;border:1px solid #ffffff29;border-radius:12px;background:var(--bg,#12151b);color:var(--text,#f4f1ea);outline-offset:3px}
      .mm-help{margin:8px 0;color:var(--muted,#a6afb9);font-size:13px;line-height:1.6}
      .mm-results{display:grid;gap:8px;margin:12px 0;max-height:250px;overflow:auto;overscroll-behavior:contain}
      .mm-choice{display:flex;align-items:flex-start;gap:10px;padding:12px;border:1px solid #ffffff1c;border-radius:12px;background:#ffffff04;cursor:pointer}
      .mm-choice:has(input:checked){border-color:#e6dcc980;background:#e6dcc910}
      .mm-choice input{flex:none;accent-color:var(--accent,#e6dcc9);margin:4px 0 0;width:16px;height:16px}
      .mm-choice strong{display:block;font-size:15px;line-height:1.5;overflow-wrap:anywhere}
      .mm-choice small{display:block;margin-top:3px;color:var(--muted,#a6afb9);font-size:13px;line-height:1.5;overflow-wrap:anywhere}
      .mm-note{min-height:22px;margin:12px 0 0;font-size:14px;color:var(--accent,#e6dcc9);line-height:1.6;overflow-wrap:anywhere}
      .mm-note[data-error=true]{color:#ffbfac}
      .mm-footer{display:flex;justify-content:flex-end;align-items:center;gap:10px;padding:17px 26px 23px;border-top:1px solid #ffffff15}
      .mm-dialog .mm-primary{background:var(--button,#f4f1ea);color:var(--bg,#12151b);border-color:transparent;font-weight:600}
      .mm-dialog .mm-primary:hover:not(:disabled){background:#fffdf8}
      .mm-dialog [hidden]{display:none!important}
      @media(max-width:500px){.mm-head{padding:20px 18px 12px}.mm-body{padding:0 18px 20px}.mm-footer{padding:16px 18px}.mm-search{grid-template-columns:minmax(0,1fr) 94px}.mm-search button{grid-column:1/-1}}
    `;
    document.head.append(style);
  }
  function open(group, options = {}) {
    if (!group || !(group.id || group.group_id) || typeof options.api !== 'function') return;
    if (active) active.close();
    styles();
    const previousFocus = document.activeElement;
    const dialog = node('dialog', 'mm-dialog');
    const headingId = 'mm-title-' + requestId();
    dialog.setAttribute('aria-labelledby', headingId);
    const head = node('header', 'mm-head');
    const heading = node('h2', '', '手动匹配作品'); heading.id = headingId;
    const subtitle = node('p', 'mm-sub', group.title || '当前作品');
    const titleBlock = node('div'); titleBlock.append(heading, subtitle);
    const close = node('button', 'mm-close', '×'); close.type = 'button'; close.setAttribute('aria-label', '关闭手动匹配');
    head.append(titleBlock, close);
    const form = node('form', 'mm-body'); form.noValidate = true;
    const searchLabel = node('label', 'mm-step', '1. 查找对应作品');
    const query = node('input'); query.type = 'search'; query.value = group.title || ''; query.maxLength = 200;
    query.id = headingId + '-query'; searchLabel.htmlFor = query.id; query.placeholder = '输入作品名，可加年份'; query.autocomplete = 'off';
    const kind = node('select'); kind.setAttribute('aria-label', '作品类型');
    for (const [value, text] of [['Movie', '电影'], ['Series', '剧集 / 番剧']]) {
      const option = node('option', '', text); option.value = value; kind.append(option);
    }
    kind.value = ['Series', 'Shows', 'TV', 'Anime'].includes(group.kind || group.type) ? 'Series' : 'Movie';
    const search = node('button', '', '搜索'); search.type = 'button';
    const searchRow = node('div', 'mm-search'); searchRow.append(query, kind, search);
    const current = {name: group.title || '当前作品'};
    const candidates = node('div', 'mm-results'); candidates.setAttribute('role', 'radiogroup'); candidates.setAttribute('aria-label', '选择匹配结果');
    const searchNote = node('p', 'mm-note'); searchNote.setAttribute('role', 'status'); searchNote.setAttribute('aria-live', 'polite');
    const urlLabel = node('label', 'mm-step', '2. 补充中文百科链接（可选）');
    const url = node('input'); url.type = 'url'; url.id = headingId + '-url'; urlLabel.htmlFor = url.id;
    url.placeholder = '粘贴百度百科或中文维基百科的作品页面'; url.maxLength = 2000; url.autocomplete = 'off';
    const help = node('p', 'mm-help', '选定作品后重新整理海报、简介与演职员。也可以保留当前作品，仅补充百科链接，或直接重试整理。');
    const status = node('p', 'mm-note'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    form.append(searchLabel, searchRow, candidates, searchNote, urlLabel, url, help, status);
    const footer = node('footer', 'mm-footer');
    const cancel = node('button', '', '取消'); cancel.type = 'button';
    const save = node('button', 'mm-primary', '保存并重新整理'); save.type = 'button';
    footer.append(cancel, save); dialog.append(head, form, footer); document.body.append(dialog);
    let alive = true, searching = false, saving = false, selected = null, generation = 0, attempt = null;
    const note = (target, text, error = false) => { target.textContent = text; target.dataset.error = String(error); };
    function controls() {
      search.disabled = searching || saving; query.disabled = saving; kind.disabled = true;
      url.disabled = saving; save.disabled = searching || saving;
      candidates.querySelectorAll('input').forEach(input => input.disabled = saving);
      search.textContent = searching ? '正在查找…' : '搜索';
      save.textContent = saving ? '正在提交…' : '保存并重新整理';
    }
    function choice(candidate) {
      const label = node('label', 'mm-choice'), radio = node('input'), text = node('span');
      radio.type = 'radio'; radio.name = headingId + '-choice'; radio.checked = candidate === null;
      if (candidate) {
        text.append(node('strong', '', candidate.name || candidate.original_name || '未命名作品'));
        text.append(node('small', '', [candidate.year, candidate.kind === 'Series' ? '剧集 / 番剧' : '电影', candidate.original_name && candidate.original_name !== candidate.name ? candidate.original_name : ''].filter(Boolean).join(' · ')));
      } else {
        text.append(node('strong', '', '保留当前作品'));
        text.append(node('small', '', current.name || group.title || '沿用现有匹配，仅重新整理资料'));
      }
      radio.addEventListener('change', () => { if (radio.checked) { selected = candidate; attempt = null; note(status, ''); } });
      label.append(radio, text); return label;
    }
    function renderCandidates(values) {
      selected = null; attempt = null; candidates.replaceChildren(choice(null));
      values.filter(item => item && typeof item.token === 'string' && item.token).slice(0, 20).forEach(item => candidates.append(choice(item)));
    }
    async function runSearch() {
      if (saving || searching || !alive) return;
      const keyword = query.value.trim();
      if (!keyword) { note(searchNote, '先输入作品名称。', true); query.focus(); return; }
      const serial = ++generation; searching = true; controls(); note(searchNote, '正在查找作品，请稍候…');
      try {
        const result = await options.api('/MediaCache/MatchSearch', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({group_id: group.id || group.group_id, query: keyword, kind: kind.value})});
        if (!alive || serial !== generation) return;
        if (result.current && typeof result.current === 'object') {
          Object.assign(current, result.current);
          if (!url.value && current.reference_url) url.value = current.reference_url;
        }
        const values = Array.isArray(result.candidates) ? result.candidates : [];
        renderCandidates(values);
        note(searchNote, values.length ? '请选择名称、年份都对应的作品。' : '暂未找到对应作品，可以换个名称搜索，或保留当前作品并补充百科链接。');
      } catch (error) {
        if (alive && serial === generation) note(searchNote, error.message || '暂时没有查到结果，请稍后重试。', true);
      } finally {
        if (alive && serial === generation) { searching = false; controls(); }
      }
    }
    async function submit() {
      if (!alive || searching || saving) return;
      const reference = url.value.trim();
      if (reference) {
        try {
          const parsed = new URL(reference);
          if (parsed.protocol !== 'https:' || parsed.username || parsed.password || !['baike.baidu.com', 'zh.wikipedia.org', 'zh.m.wikipedia.org'].includes(parsed.hostname)) throw new Error();
        } catch (_) { note(status, '请粘贴百度百科或中文维基百科的完整作品链接。', true); url.focus(); return; }
      }
      const payload = {group_id: group.id || group.group_id};
      if (selected) payload.candidate_token = selected.token;
      if (reference) payload.reference_url = reference;
      const signature = JSON.stringify(payload);
      if (!attempt || attempt.signature !== signature) attempt = {signature, request_id: requestId()};
      payload.request_id = attempt.request_id;
      saving = true; controls(); note(status, '正在保存选择并安排重新整理…');
      try {
        const result = await options.api('/MediaCache/Match', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
        if (!['accepted', 'queued', 'running', 'waiting', 'applied', 'complete', 'partial'].includes(result.state)) throw new Error(result.message || '暂未收到处理确认，请重试。');
        if (typeof options.onSaved === 'function') {
          try { Promise.resolve(options.onSaved({...result,request_payload:payload})).catch(() => {}); } catch (_) { /* The accepted task remains valid if a page refresh fails. */ }
        }
        if (!alive) return;
        form.replaceChildren(node('p', 'mm-step', result.state === 'partial' ? '选择已保存，部分资料待补齐' : ['applied','complete'].includes(result.state) ? '本作整理已完成' : '已提交重新整理'), node('p', 'mm-help', result.message || '后台会按本次选择继续处理。关闭窗口后，可在入库进度中查看结果。'));
        save.hidden = true; cancel.textContent = '完成'; cancel.focus();
      } catch (error) {
        if (alive) note(status, error.message || '提交暂未确认，请重试。', true);
      } finally {
        saving = false;
        if (alive && !save.hidden) controls();
      }
    }
    function dismiss() { if (dialog.open) dialog.close(); }
    dialog.addEventListener('keydown', event => {
      // Search inputs consume Escape to clear their value before the dialog's
      // native cancel event. Keep dismiss consistent with other app dialogs.
      if (event.key === 'Escape') { event.preventDefault(); dismiss(); }
    });
    close.addEventListener('click', dismiss); cancel.addEventListener('click', dismiss);
    search.addEventListener('click', runSearch); save.addEventListener('click', submit);
    form.addEventListener('submit', event => { event.preventDefault(); runSearch(); });
    url.addEventListener('input', () => { attempt = null; note(status, ''); });
    dialog.addEventListener('close', () => {
      alive = false; generation++; dialog.remove(); if (active === handle) active = null;
      if (previousFocus && previousFocus.isConnected && typeof previousFocus.focus === 'function') previousFocus.focus();
    }, {once: true});
    const handle = {close: dismiss}; active = handle;
    renderCandidates([]); dialog.showModal(); query.focus(); query.select();
    return handle;
  }
  window.MediaManualMatch = {open};
})();
