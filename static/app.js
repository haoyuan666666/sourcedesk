const $ = id => document.getElementById(id);
const state = {token: '', configured: false, busy: false, view: 0, importing: false, fileVersion: 0, document: null};
function el(tag, className, text) { const item = document.createElement(tag); item.className = className; if (text !== undefined) item.textContent = text; return item; }
function message(id, text, error = false) { $(id).textContent = text; $(id).hidden = !text; $(id).classList.toggle('error', error); }
async function api(path, data, method) {
  const response = await fetch(path, {method: method || (data ? 'POST' : 'GET'), headers: {'Content-Type': 'application/json', 'X-SourceDesk-Token': state.token}, ...(data ? {body: JSON.stringify(data)} : {})});
  const packet = await response.json();
  if (!response.ok) throw new Error(packet.error || '请求未完成，请重试。');
  return packet;
}
function busy(value) { state.busy = value; $('ask').disabled = value || !state.configured; $('search').disabled = value; $('question').disabled = value; }
function confirmAction(title, text) {
  const dialog = $('confirm-dialog');
  if (dialog.open) return Promise.resolve(false);
  $('confirm-title').textContent = title; $('confirm-text').textContent = text; dialog.returnValue = 'cancel'; dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), {once: true}));
}
function countContent() { $('content-count').textContent = `${$('content').value.length.toLocaleString()} 字`; }
function download(name, text) { const url = URL.createObjectURL(new Blob([text], {type: 'text/plain;charset=utf-8'})); const link = document.createElement('a'); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
function sources(rows) {
  const container = $('sources'); container.replaceChildren();
  if (!rows.length) return;
  container.append(el('h3', 'sources-heading', `${rows.length} 个相关片段 · 展开查看完整原文`));
  rows.forEach(row => { const detail = el('details', 'source'); detail.id = `source-${row.id}`; const summary = el('summary', ''); summary.append(el('span', 'source-tag', `#${row.id}`), el('span', 'source-title', row.title), el('span', 'source-index', `片段 ${row.chunk_index}`)); detail.append(summary, el('pre', 'source-text', row.chunk_text)); container.append(detail); });
}
function renderAnswer(data) {
  const container = $('answer'); container.replaceChildren();
  container.append(el('p', 'asked-question', data.question));
  if (!data.answerable) container.append(el('p', 'refusal', data.answer));
  else data.claims.forEach((claim, index) => {
    const block = el('div', 'claim'); block.append(el('span', 'claim-number', String(index + 1).padStart(2, '0')));
    const body = el('div', 'claim-body'); body.append(el('p', 'claim-text', claim.text));
    const quote = el('blockquote', 'quote', claim.quote); const link = el('a', 'citation', `核对原文 #${claim.chunk_id} ↗`); link.href = `#source-${claim.chunk_id}`;
    link.addEventListener('click', () => { const target = $(`source-${claim.chunk_id}`); if (target) target.open = true; });
    body.append(quote, link); block.append(body); container.append(block);
  });
  const tokenInfo = data.usage?.total_tokens === undefined ? '' : ` · ${data.usage.total_tokens} tokens`;
  const modelLabel = data.model === 'injected-test-provider' ? '测试提供器（非真实模型）' : data.model ? '真实模型回答' : '未调用模型';
  container.append(el('p', 'answer-meta', `${modelLabel}${tokenInfo} · ${new Date(data.created_at).toLocaleString('zh-CN')}`));
  container.append(el('p', 'evidence-note', '已核对引用编号与原文字面一致；这不代表语义一定正确，请结合上下文判断。'));
  sources(data.sources);
  $('export-actions').hidden = false;
  $('export-md').href = `/api/history/${data.id}/export?format=md`;
  $('export-json').href = `/api/history/${data.id}/export?format=json`;
}
async function refreshDocuments() {
  const rows = await api('/api/documents'); const list = $('document-list'); list.replaceChildren(); $('document-count').textContent = `${rows.length} 份`;
  if (!rows.length) list.append(el('p', 'empty-small', '还没有资料，从上方添加第一份。'));
  rows.forEach(row => { const button = el('button', 'document-item'); button.type = 'button'; const icon = el('span', 'document-icon', '≡'); const text = el('span', 'document-item-text'); text.append(el('strong', '', row.title), el('span', '', `${row.chunk_count} 个片段 · 点击查看`)); button.append(icon, text, el('span', 'document-arrow', '↗')); button.addEventListener('click', () => openDocument(row.id).catch(error => message('global-message', error.message, true))); list.append(button); });
}
let documentVersion = 0;
async function openDocument(id) {
  const token = ++documentVersion; const data = await api(`/api/documents/${id}`); if (token !== documentVersion) return;
  state.document = data; $('document-dialog-title').textContent = data.title; $('document-detail').textContent = data.content;
  $('document-detail-note').textContent = data.reconstructed ? '旧版资料：以下文字由原有片段重组，原始分段格式可能不同。' : `${data.chunks.length} 个检索片段 · 已保存导入的原文`;
  if (!$('document-dialog').open) $('document-dialog').showModal();
}
async function refreshHistory() {
  const records = await api('/api/history'); const list = $('history-list'); list.replaceChildren();
  if (!records.length) list.append(el('p', 'empty-small', '完成提问后，记录会保存在这里。'));
  records.forEach(record => { const button = el('button', 'history-item'); button.type = 'button'; button.append(el('span', '', record.question), el('time', '', new Date(record.created_at).toLocaleString('zh-CN'))); button.addEventListener('click', async () => { const token = ++state.view; try { const data = await api(`/api/history/${record.id}`); if (token !== state.view) return; renderAnswer(data); message('global-message', '正在查看历史回答，引用保留的是当时的资料。'); } catch (error) { if (token === state.view) message('global-message', error.message, true); } }); list.append(button); });
}
async function submitQuestion(useAI) {
  if (state.busy) return;
  if (!$('question').reportValidity()) return;
  const question = $('question').value.trim(); if (!question) return;
  const token = ++state.view; busy(true); $('export-actions').hidden = true; $('sources').replaceChildren();
  $('answer').replaceChildren(el('div', 'working', useAI ? '正在检索资料，并等待模型回答…' : '正在查找相关原文…'));
  message('global-message', useAI ? '通常需要几秒到45秒。请等待完成，没有自动重试。' : '只检索本机资料，不调用模型。');
  try {
    const data = await api(useAI ? '/api/ask' : '/api/search', {question});
    if (token === state.view) {
      if (useAI) renderAnswer(data);
      else { $('answer').replaceChildren(el('p', 'asked-question', question), el('p', data.sources.length ? 'search-result' : 'refusal', data.sources.length ? '找到以下相关原文，可直接核对，或继续生成 AI 回答。' : '资料中没有找到足够相关的片段。请补充资料或调整问题。')); sources(data.sources); }
      message('global-message', useAI ? '回答已保存，可下载或在最近问答中再次打开。' : '检索完成，未调用模型。');
    }
    if (useAI) await refreshHistory();
  } catch (error) { if (token === state.view) {
    const detail = useAI ? `${error.message} 服务可能已经完成并产生费用，请先查看“最近问答”；手动重试可能再次计费。` : error.message;
    $('answer').replaceChildren(el('p', 'error', detail)); message('global-message', detail, true);
    if (useAI) { try { await refreshHistory(); } catch { /* The original failure stays visible if the service is still unreachable. */ } }
  } }
  finally { busy(false); }
}
$('question-form').addEventListener('submit', event => { event.preventDefault(); submitQuestion(true); });
$('search').addEventListener('click', () => submitQuestion(false));
$('content').addEventListener('input', () => { state.fileVersion++; countContent(); });
$('title').addEventListener('input', () => { state.fileVersion++; });
$('file').addEventListener('change', async event => {
  const file = event.target.files[0]; event.target.value = ''; if (!file) return;
  const token = ++state.fileVersion;
  try {
    if (!/\.(txt|md)$/i.test(file.name)) throw new Error('请选择 TXT 或 Markdown 文件。');
    if (file.size > 400000) throw new Error('文件超过400 KB，请拆分后导入。');
    const text = new TextDecoder('utf-8', {fatal: true}).decode(await file.arrayBuffer());
    if (text.length > 100000) throw new Error('资料超过10万字，请拆分后导入。');
    if (token !== state.fileVersion) return;
    if (($('content').value || $('title').value) && !await confirmAction('替换待保存内容？', '当前输入还没有保存。请选择取消保留原内容，或确认载入文件。')) return;
    if (token !== state.fileVersion) return;
    $('content').value = text; $('title').value = file.name.replace(/\.(txt|md)$/i, '').slice(0, 120); countContent(); message('import-message', '已读取文件，点击保存后才会加入资料库。');
  } catch (error) { if (token === state.fileVersion) message('import-message', error instanceof TypeError ? '文件不是有效 UTF-8 编码，请转换后重试。' : error.message, true); }
});
$('sample').addEventListener('click', async () => {
  if (($('content').value || $('title').value) && !await confirmAction('载入虚构资料？', '会替换尚未保存的输入。')) return;
  state.fileVersion++; $('title').value = '虚构示例 · 星河差旅规则';
  $('content').value = '【虚构示例，仅用于功能体验】\n\n星河工作室差旅报销规则：员工应在返程后10个工作日内提交报销申请。报销材料包括电子发票、行程单和负责人审批记录。\n\n财务收到完整材料后，在5个工作日内完成审核。缺少材料时会退回补充，补充完成后重新计算审核时间。\n\n住宿报销上限：每人每晚400元。超出上限的部分不予报销。'; countContent(); message('import-message', '示例不会自动入库，也没有预设 AI 回答。点击保存后可提问。');
});
$('document-form').addEventListener('submit', async event => {
  event.preventDefault(); if (state.importing) return; state.importing = true;
  const title = $('title').value, content = $('content').value; const revision = ++state.fileVersion;
  $('save-document').disabled = true;
  try { const data = await api('/api/documents', {title, content}); if (revision === state.fileVersion) { $('title').value = ''; $('content').value = ''; countContent(); } message('import-message', `已保存，生成 ${data.chunk_count} 个片段。`); await refreshDocuments(); }
  catch (error) { message('import-message', error.message, true); }
  finally { state.importing = false; $('save-document').disabled = false; }
});
$('close-document').addEventListener('click', () => $('document-dialog').close());
$('download-document').addEventListener('click', () => { if (state.document) download(`sourcedesk-document-${state.document.id}.txt`, state.document.content); });
$('delete-document').addEventListener('click', async () => {
  const doc = state.document; if (!doc) return;
  if (!await confirmAction('删除这份资料？', `将删除“${doc.title}”及其检索片段。建议先下载原文。历史回答的引用快照会保留。`)) return;
  $('delete-document').disabled = true;
  try { await api(`/api/documents/${doc.id}`, null, 'DELETE'); $('document-dialog').close(); state.document = null; await refreshDocuments(); message('global-message', '资料已删除；已有历史回答仍保留当时引用。'); }
  catch (error) { message('global-message', error.message, true); }
  finally { $('delete-document').disabled = false; }
});
(async () => {
  try { const boot = await api('/api/bootstrap'); state.token = boot.token; state.configured = boot.configured; busy(false);
    $('connection').textContent = boot.model === 'injected-test-provider' ? '测试提供器 · 非真实模型' : boot.configured ? 'DeepSeek 已配置 · 本机资料库' : '未配置密钥 · 可导入与检索'; $('connection').classList.toggle('ready', boot.configured);
    if (!boot.configured) $('privacy-hint').textContent = '当前未配置 DEEPSEEK_API_KEY，AI 回答暂不可用。你仍可导入资料并检索原文。';
    await Promise.all([refreshDocuments(), refreshHistory()]);
  } catch (error) { message('global-message', `无法连接本地服务：${error.message}`, true); $('connection').textContent = '连接失败，请刷新'; }
})();
