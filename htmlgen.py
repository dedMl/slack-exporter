# -*- coding: utf-8 -*-
"""Generate an offline-browsable static HTML viewer from exported JSON
(output/viewer/index.html).

Data is injected as JS files (fetch is CORS-restricted under file://, so
<script> loading is used instead):
  viewer/data.js        users / channel index / stats
  viewer/ch/<id>.js     per-channel messages (loaded on demand)
Attachments live in output/files/, referenced relatively as ../files/.
"""
import json
import re
import time
from pathlib import Path

import requests

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Slack Archive Viewer</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin: 0; display: flex; font: 15px/1.5 -apple-system, "Segoe UI",
         "Microsoft YaHei", "PingFang SC", sans-serif; color: #1d1c1d; }
  #side { width: 290px; flex-shrink: 0; background: #350d36; color: #cfd3d7;
          display: flex; flex-direction: column; overflow: hidden; }
  #ws { padding: 16px 16px 10px; font-weight: 700; color: #fff; font-size: 16px;
        border-bottom: 1px solid rgba(255,255,255,.12); }
  #stats { padding: 8px 16px; font-size: 12px; opacity: .75; }
  #side .search { margin: 6px 12px; }
  #side .search input { width: 100%; padding: 5px 10px; border: 0; border-radius: 6px;
        background: rgba(255,255,255,.15); color: #fff; outline: none; font-size: 13px; }
  #side .search input::placeholder { color: rgba(255,255,255,.5); }
  #chan-list { overflow-y: auto; padding-bottom: 24px; flex: 1; }
  .group-h { padding: 14px 16px 4px; font-size: 11px; letter-spacing: .06em; opacity: .65; }
  .chan { padding: 4px 16px; cursor: pointer; white-space: nowrap; overflow: hidden;
          text-overflow: ellipsis; font-size: 14px; }
  .chan:hover { background: rgba(255,255,255,.08); }
  .chan.active { background: #1164a3; color: #fff; }
  .chan .cnt { float: right; opacity: .55; font-size: 12px; margin-left: 6px; }
  .chan.archived { opacity: .5; }
  .chan .sava { width: 20px; height: 20px; border-radius: 4px; object-fit: cover;
        vertical-align: -5px; margin-right: 3px; }

  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; background: #fff; }
  #head { border-bottom: 1px solid #ddd; padding: 10px 20px; display: flex;
          align-items: baseline; gap: 16px; flex-wrap: wrap; }
  #ch-title { font-weight: 800; font-size: 17px; }
  #ch-meta { color: #777; font-size: 12.5px; flex: 1; min-width: 200px; }
  #head input { border: 1px solid #ccc; border-radius: 6px; padding: 5px 10px;
          font-size: 13px; outline: none; width: 220px; }
  #head input:focus { border-color: #1264a3; }
  #msgs { overflow-y: auto; padding: 12px 20px 40px; flex: 1; }
  /* canvas doc embed: fill the content area, scroll inside the doc */
  #msgs.docview { padding: 0; }
  .doc-frame { display: block; width: 100%; height: 100%; border: 0;
               background: #fff; }

  .day { display: flex; align-items: center; margin: 20px 0 10px; color: #555; font-size: 12px; }
  .day:before, .day:after { content: ''; flex: 1; height: 1px; background: #e4e4e4; }
  .day span { padding: 2px 14px; background: #f1f1f1; border-radius: 10px; }
  .sysmsg { color: #999; font-size: 12.5px; text-align: center; margin: 10px 0; }
  .msg { display: flex; gap: 10px; margin-top: 12px; }
  .msg.compact { margin-top: 2px; }
  .ava { width: 36px; height: 36px; border-radius: 4px; flex-shrink: 0; display: flex;
         align-items: center; justify-content: center; color: #fff; font-weight: 600;
         font-size: 14px; user-select: none; overflow: hidden; }
  .ava img { width: 100%; height: 100%; object-fit: cover; border-radius: 4px;
         display: block; }
  .msg.compact .ava { visibility: hidden; height: 10px; }
  .c { flex: 1; min-width: 0; }
  .m-head .n { font-weight: 800; }
  .m-head .t { color: #999; font-size: 11.5px; margin-left: 8px; font-weight: 400; }
  .msg.compact .t { color: #bbb; font-size: 10.5px; margin-left: 6px; }
  .body { word-wrap: break-word; overflow-wrap: anywhere; }
  .body a { color: #1264a3; }
  pre { background: #f6f6f6; padding: 8px 10px; border-radius: 6px; overflow: auto;
        font-size: 13px; margin: 4px 0; }
  code { background: #f2f2f2; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
  .files img, .files video { max-width: 380px; max-height: 300px; border-radius: 8px;
        display: block; margin-top: 6px; }
  .fcard { display: inline-block; border: 1px solid #ddd; padding: 8px 12px; border-radius: 8px;
        margin: 6px 8px 0 0; text-decoration: none; color: #1264a3; background: #fafafa; font-size: 13.5px; }
  .fcard:hover { background: #f0f0f0; }
  .fcard span { color: #999; margin-left: 8px; font-size: 12px; }
  .reacts { margin-top: 4px; }
  .react { display: inline-block; background: #f2f2f2; border: 1px solid #e0e0e0;
        border-radius: 12px; padding: 1px 9px; font-size: 12px; margin: 0 6px 4px 0; }
  .thread-btn { color: #1264a3; font-size: 12.5px; cursor: pointer; margin-top: 4px;
        font-weight: 600; display: inline-block; }
  .thread-btn:hover { text-decoration: underline; }
  .thread { border-left: 2px solid #e0e0e0; margin: 4px 0 6px 10px; padding-left: 14px; }
  mark { background: #ffe58a; padding: 0 1px; }
  #welcome { padding: 60px 40px; color: #666; }
  #welcome h1 { color: #350d36; }
</style>
</head>
<body>
<div id="side">
  <div id="ws">Slack Archive Viewer</div>
  <div id="stats"></div>
  <div class="search"><input id="ch-filter" placeholder="Filter channels…"></div>
  <div id="chan-list"></div>
</div>
<div id="main">
  <div id="head">
    <div id="ch-title">No channel selected</div>
    <div id="ch-meta"></div>
    <input id="msg-search" placeholder="Search in this channel…">
  </div>
  <div id="msgs"><div id="welcome"><h1>Slack Export Archive</h1><p>Click a channel on the left to start browsing.</p></div></div>
</div>
<script src="data.js"></script>
<script>
/* UI language: follows the browser locale (Chinese for zh*, English
   otherwise); override with ?lang=en or ?lang=zh */
const LANG = (new URLSearchParams(location.search).get('lang') ||
  (navigator.language || 'en')).toLowerCase();
const ZH = LANG.startsWith('zh');
const L = (zh, en) => ZH ? zh : en;
const LOC = ZH ? 'zh-CN' : undefined;
document.documentElement.lang = ZH ? 'zh' : 'en';
document.title = L('Slack 记录浏览器', 'Slack Archive Viewer');

const D = window.SLACK_DATA || {users: {}, channels: []};
const U = D.users || {};
const SYS = new Set(['channel_join','channel_leave','channel_topic','channel_purpose',
  'channel_name','channel_archive','channel_unarchive','group_topic','tombstone','pinned_item']);
let current = null;

const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function uname(id){ const u = U[id] || {}; return u.real_name || u.display_name || u.name || (L('用户', 'user') + id); }
function color(k){ const cs = ['#e01e5a','#67c830','#2b75cb','#ecb22e','#de4fba','#4ec0d6','#a359d6','#e8912c'];
  let h = 0; for (const c of String(k)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return cs[h % cs.length]; }
function initials(n){ return (String(n || '?').replace(/[^\p{L}\p{N} ]/gu, '').trim()
  .split(/\s+/).slice(0, 2).map(w => w[0] || '').join('').toUpperCase()) || '?'; }
function fmtTime(ts){ return new Date(ts * 1000).toLocaleTimeString(LOC, {hour: '2-digit', minute: '2-digit'}); }
function fmtDate(ts){ const d = new Date(ts * 1000), now = new Date();
  const a = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const b = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diff = Math.round((a - b) / 86400000);
  if (diff === 0) return L('今天', 'Today'); if (diff === 1) return L('昨天', 'Yesterday');
  return d.toLocaleDateString(LOC, {year: 'numeric', month: 'long', day: 'numeric'}); }
function fmtShort(ts){ return new Date(ts * 1000).toLocaleDateString(LOC); }
function fmtSize(n){ if (!n && n !== 0) return ''; const u = ['B','KB','MB','GB'];
  let i = 0; while (n >= 1024 && i < u.length - 1){ n /= 1024; i++; } return n.toFixed(n < 10 && i ? 1 : 0) + ' ' + u[i]; }

function md(text){
  let s = esc(text || '');
  s = s.replace(/```([\s\S]*?)```/g, (_, c) => '<pre>' + c.replace(/^\n/, '') + '</pre>');
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/\*([^*\n]+)\*/g, '<b>$1</b>');
  s = s.replace(/(?<![\w\/])_([^_\n]+)_(?![\w])/g, '<i>$1</i>');
  s = s.replace(/~([^~\n]+)~/g, '<s>$1</s>');
  s = s.replace(/&lt;#[A-Z0-9]+\|([^&]+)&gt;/g, '<b>#$1</b>');
  s = s.replace(/&lt;@(U[A-Za-z0-9]+|W[A-Za-z0-9]+)&gt;/g, (_, id) => '<b>@' + esc(uname(id)) + '</b>');
  s = s.replace(/&lt;(https?:\/\/[^\s|&]+)(?:\|([^\n]*?))?&gt;/g,
    (_, u, t) => '<a href="' + u + '" target="_blank">' + (t || u) + '</a>');
  s = s.replace(/&lt;!((?:here|channel|everyone)[^&]*)&gt;/g, '<b>@$1</b>');
  s = s.replace(/\n/g, '<br>');
  return s;
}

function avaHtml(key, name, ava){
  const ini = esc(initials(name)), bg = ' style="background:' + color(key) + '"';
  if (!ava)
    return '<div class="ava"' + bg + '>' + ini + '</div>';
  return '<div class="ava"' + bg + ' data-fb="' + ini + '">' +
    '<img loading="lazy" src="' + esc(ava) + '" alt="" onerror="avaFail(this)"></div>';
}
function avaFail(img){
  const d = img.parentElement; if (!d) return;
  img.remove(); d.textContent = d.dataset.fb || '?';
}

function sysText(m){
  const who = m.user ? uname(m.user) : L('有人', 'someone');
  switch (m.subtype){
    case 'channel_join': return who + L(' 加入了频道', ' joined the channel');
    case 'channel_leave': return who + L(' 离开了频道', ' left the channel');
    case 'channel_topic': return who + L(' 更新了主题：', ' updated the topic: ') + (m.topic || '');
    case 'channel_purpose': return who + L(' 更新了说明：', ' updated the description: ') + (m.purpose || '');
    case 'channel_name': return who + L(' 将频道重命名为 ', ' renamed the channel to ') + (m.name || '');
    case 'tombstone': return L('此消息已被删除', 'This message was deleted');
    default: return m.text || '';
  }
}

function renderFiles(m){
  const fs = m.files || []; if (!fs.length) return '';
  return '<div class="files">' + fs.map(f => {
    const label = esc(f.name || f.title || L('文件', 'file')), size = fmtSize(f.size);
    if (f.local_path){
      const p = '../' + f.local_path, mt = f.mimetype || '';
      if (mt.startsWith('image/'))
        return '<a href="' + p + '" target="_blank"><img loading="lazy" src="' + p + '"></a>';
      if (mt.startsWith('video/')) return '<video controls preload="metadata" src="' + p + '"></video>';
      if (mt.startsWith('audio/')) return '<audio controls preload="metadata" src="' + p + '"></audio>';
      if (mt === 'application/vnd.slack-docs')
        return '<a class="fcard" href="javascript:void 0" data-p="' + esc(p) +
          '" data-t="' + esc(label) + '" onclick="openDocByPath(this.dataset.p,' +
          'this.dataset.t)">📝 ' + label +
          ' <span>' + L('画板文档 · 页内查看', 'Canvas doc · view inline') + '</span></a>';
      return '<a class="fcard" href="' + p + '" target="_blank">📄 ' + label +
        (size ? ' <span>' + size + '</span>' : '') + '</a>';
    }
    const url = f.permalink || f.url_private || '#';
    return '<a class="fcard" href="' + esc(url) + '" target="_blank">🔗 ' + label +
      (size ? ' <span>' + size + '</span>' : '') + '</a>';
  }).join('') + '</div>';
}

function renderReactions(m){
  const rs = m.reactions || []; if (!rs.length) return '';
  return '<div class="reacts">' + rs.map(r => {
    const names = (r.users || []).map(uname).join(ZH ? '、' : ', ');
    return '<span class="react" title="' + esc(names) + '">:' + esc(r.name) + ': ' +
      (r.count || 1) + '</span>';
  }).join('') + '</div>';
}

function row(m, compact){
  const uid = m.user || '';
  const name = m.username || (U[uid] ? uname(uid) :
    (m.bot_profile && m.bot_profile.name) || L('应用', 'app'));
  const t = new Date(m.ts * 1000).toLocaleString(LOC);
  let h = '<div class="msg' + (compact ? ' compact' : '') + '" data-ts="' + m.ts + '">';
  h += avaHtml(uid || name, name, (U[uid] || {}).avatar);
  h += '<div class="c">';
  h += compact
    ? '<div class="body" title="' + esc(t) + '">' + md(m.text) + renderFiles(m) + renderReactions(m) + '</div>'
    : '<div class="m-head"><span class="n">' + esc(name) + '</span>' +
      '<span class="t">' + esc(t) + '</span></div>' +
      '<div class="body">' + md(m.text) + renderFiles(m) + renderReactions(m) + '</div>';
  h += '</div></div>';
  return h;
}

function toggleThread(btn){
  const t = btn.nextElementSibling, show = t.style.display === 'none';
  t.style.display = show ? 'block' : 'none';
  btn.textContent = (show ? '▾ ' : '▸ ') + btn.dataset.n + L(' 条回复', ' replies');
}

function match(m, q){
  return (m.text || '').toLowerCase().includes(q) ||
    (m.user && uname(m.user).toLowerCase().includes(q));
}

function renderChannel(id){
  current = id;
  const box0 = document.getElementById('msgs');
  box0.classList.remove('docview');
  document.getElementById('msg-search').style.visibility = 'visible';
  const ch = D.channels.find(c => c.id === id) || {};
  document.getElementById('ch-title').textContent = ch.title || id;
  const meta = [];
  if (ch.topic) meta.push(ch.topic);
  meta.push((ch.msg_count || 0) + L(' 条消息', ' messages'));
  if (ch.first_ts) meta.push(fmtShort(ch.first_ts) + ' ~ ' + fmtShort(ch.last_ts));
  if (ch.is_archived) meta.push(L('已归档', 'Archived'));
  document.getElementById('ch-meta').textContent = meta.join(' · ');

  const msgs = (window.__CH && window.__CH[id]) || [];
  const q = (document.getElementById('msg-search').value || '').trim().toLowerCase();
  const top = [], threads = {};
  for (const m of msgs){
    if (m.thread_ts && m.thread_ts !== m.ts)
      (threads[m.thread_ts] = threads[m.thread_ts] || []).push(m);
    else top.push(m);
  }
  const list = q ? top.filter(m => match(m, q) ||
    (threads[m.ts] || []).some(r => match(r, q))) : top;

  let h = '', lastDay = null, lastU = null, lastT = 0;
  for (const m of list){
    const day = fmtDate(m.ts);
    if (day !== lastDay){ h += '<div class="day"><span>' + day + '</span></div>';
      lastDay = day; lastU = null; }
    if (SYS.has(m.subtype)){ h += '<div class="sysmsg">' + esc(sysText(m)) + '</div>';
      lastU = null; continue; }
    const compact = m.user && m.user === lastU && (m.ts - lastT) < 300 &&
      !m.reactions && !(m.files && m.files.length);
    h += row(m, compact);
    const reps = threads[m.ts];
    if (reps && reps.length)
      h += '<div class="msg"><div class="c"><span class="thread-btn" data-n="' +
        reps.length + '" onclick="toggleThread(this)">▸ ' + reps.length +
        L(' 条回复', ' replies') + '</span><div class="thread" style="display:none">' +
        reps.map(r => row(r, false)).join('') + '</div></div></div>';
    lastU = m.user; lastT = +m.ts;
  }
  if (!list.length) h = '<div class="sysmsg">' + L('没有消息', 'No messages') + '</div>';
  const box = document.getElementById('msgs');
  box.innerHTML = h; box.scrollTop = 0;
  if (q) highlight(q);
  if (pendingJump){   // canvas message link: jump after rendering completes
    const t = pendingJump; pendingJump = null;
    setTimeout(() => jumpToTs(t), 50);
  }
}

function highlight(q){
  const rx = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'ig');
  const walker = document.createTreeWalker(document.getElementById('msgs'),
    NodeFilter.SHOW_TEXT, { acceptNode: n => n.parentNode.closest('pre,code,a,style,script')
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes){
    rx.lastIndex = 0;
    if (!rx.test(n.nodeValue)) continue;
    rx.lastIndex = 0;
    const span = document.createElement('span');
    span.innerHTML = esc(n.nodeValue).replace(rx, m => '<mark>' + m + '</mark>');
    n.parentNode.replaceChild(span, n);
  }
}

function openChannel(id){
  document.querySelectorAll('.chan').forEach(e =>
    e.classList.toggle('active', e.dataset.id === id));
  if (window.__CH && window.__CH[id]){ renderChannel(id); return; }
  document.getElementById('msgs').innerHTML =
    '<div class="sysmsg">' + L('加载中…', 'Loading…') + '</div>';
  const s = document.createElement('script');
  s.src = 'ch/' + encodeURIComponent(id) + '.js';
  s.onload = () => renderChannel(id);
  s.onerror = () => { document.getElementById('msgs').innerHTML =
    '<div class="sysmsg">' + L('数据加载失败', 'Failed to load data') + '</div>'; };
  document.head.appendChild(s);
}

const GROUPS = [
  ['public_channel', L('频道', 'Channels'), '#'],
  ['private_channel', L('私有频道', 'Private channels'), '🔒'],
  ['mpim', L('群组', 'Group DMs'), '👥'],
  ['im', L('私信', 'Direct messages'), '✉'],
];

const U_BY_NAME = {};
for (const id in U){ const n = U[id].name; if (n) U_BY_NAME[n] = id; }
function imAva(title){
  const u = U[U_BY_NAME[title]] || {};
  return u.avatar || '';
}

function buildSidebar(){
  const f = (document.getElementById('ch-filter').value || '').trim().toLowerCase();
  let h = '';
  for (const [type, label, icon] of GROUPS){
    const cs = D.channels.filter(c => c.type === type &&
      (!f || String(c.title || '').toLowerCase().includes(f)));
    if (!cs.length) continue;
    h += '<div class="group-h">' + label + '</div>';
    for (const c of cs){
      let lead = icon + ' ';
      if (type === 'im' && imAva(c.title))
        lead = '<img class="sava" loading="lazy" src="' + esc(imAva(c.title)) + '" alt=""> ';
      h += '<div class="chan' + (c.is_archived ? ' archived' : '') + '" data-id="' + c.id +
        '" title="' + esc(c.title) + '" onclick="openChannel(this.dataset.id)">' +
        lead + esc(c.title) + '<span class="cnt">' + (c.msg_count || 0) + '</span></div>';
    }
  }
  // canvas docs (Slack Docs): click to open the offline HTML
  if (D.docs && D.docs.length){
    const ds = D.docs.filter(d => !f ||
      String(d.title || '').toLowerCase().includes(f) ||
      String(d.channel || '').toLowerCase().includes(f));
    if (ds.length){
      h += '<div class="group-h">' + L('画板文档', 'Canvas docs') + '</div>';
      for (const d of ds){
        const tip = esc((d.channel ? L('来自 ', 'from ') + d.channel + ' · ' : '') +
          (d.updated ? new Date(d.updated * 1000).toLocaleDateString(LOC) : ''));
        h += '<div class="chan"' + (d.path ? '' : ' style="opacity:.45"') +
          ' title="' + tip + '" onclick="openDoc(this.dataset.p, this.dataset.id)"' +
          ' data-p="' + esc(d.path || '') + '" data-id="' + esc(d.id || '') + '">' +
          '📝 ' + esc(d.title) +
          (d.channel ? '<span class="cnt">' + esc(d.channel) + '</span>' : '') +
          '</div>';
      }
    }
  }
  document.getElementById('chan-list').innerHTML = h ||
    '<div class="sysmsg">' + L('无匹配频道', 'No matching channels') + '</div>';
}

function openDoc(path, id){
  const d = (D.docs || []).find(x => x.id === id) || {};
  if (!path){ alert(L('该画板文档尚未导出（运行 python main.py export --scope docs）',
    'This canvas doc has not been exported yet (run python main.py export --scope docs)')); return; }
  document.querySelectorAll('.chan').forEach(e =>
    e.classList.toggle('active', e.dataset.id === id));
  openDocByPath('../' + path, d.title || L('画板文档', 'Canvas doc'), id, d.channel);
}

/* canvas docs render inside the right content area (iframe loads the
   offline HTML; the doc scrolls internally; no new tab) */
function openDocByPath(path, title, id, chName){
  current = null;   // not in a channel view
  document.getElementById('ch-title').textContent = title || L('画板文档', 'Canvas doc');
  const meta = [];
  if (chName) meta.push(L('来自 ', 'from ') + chName);
  meta.push(L('Slack 画板文档 · 页内渲染', 'Slack canvas doc · rendered inline'));
  document.getElementById('ch-meta').textContent = meta.join(' · ');
  document.getElementById('msg-search').style.visibility = 'hidden';
  const box = document.getElementById('msgs');
  box.classList.add('docview');
  box.innerHTML = '<iframe class="doc-frame" src="' + esc(path) +
    '" title="' + esc(title || '') + '"></iframe>';
  box.scrollTop = 0;
}

/* canvas/external anchor routing: #c=<channelID>&t=<ts> -- open the channel
   and scroll to the message */
let pendingJump = null;
function jumpToTs(t){
  const el = document.querySelector('[data-ts="' + t + '"]');
  if (el){
    el.scrollIntoView({block: 'center'});
    el.style.transition = 'background 2s';
    el.style.background = '#fff3c2';
    setTimeout(() => el.style.background = '', 2000);
  }
}
function routeFromHash(){
  const h = location.hash.slice(1);
  if (!h || h.indexOf('c=') === -1) return;
  const p = new URLSearchParams(h);
  const c = p.get('c');
  if (!c) return;
  openChannel(c);
  const t = p.get('t');
  pendingJump = t || null;   // executed after renderChannel finishes
}

(function init(){
  if (D.workspace) document.getElementById('ws').textContent = D.workspace;
  document.getElementById('stats').textContent =
    (D.total_channels || 0) + L(' 个频道', ' channels') + ' · ' +
    (D.total_messages || 0) + L(' 条消息', ' messages') +
    L(' · 导出于 ', ' · exported at ') + (D.generated_at || '');
  document.getElementById('ch-filter').placeholder = L('筛选频道…', 'Filter channels…');
  document.getElementById('ch-title').textContent = L('未选择频道', 'No channel selected');
  document.getElementById('msg-search').placeholder = L('在当前频道中搜索…', 'Search in this channel…');
  const w = document.getElementById('welcome');
  if (w) w.innerHTML = '<h1>' + L('Slack 导出记录', 'Slack Export Archive') +
    '</h1><p>' + L('点击左侧频道开始浏览。', 'Click a channel on the left to start browsing.') + '</p>';
  document.getElementById('ch-filter').addEventListener('input', buildSidebar);
  document.getElementById('msg-search').addEventListener('input',
    () => current && renderChannel(current));
  window.addEventListener('hashchange', routeFromHash);
  buildSidebar();
  if (location.hash && location.hash.indexOf('c=') !== -1){
    routeFromHash();   // incoming jump from a canvas channel/message link
    return;
  }
  const first = D.channels.find(c => (c.msg_count || 0) > 0) || D.channels[0];
  if (first) openChannel(first.id);
})();
</script>
</body>
</html>
"""


def _js_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _avatar_hires(url: str) -> str:
    """Swap 72px avatar URLs for 192px (displayed at 36px, sharper on HiDPI)."""
    if "avatars.slack-edge.com" in url and url.endswith("_72.png"):
        return url[:-7] + "_192.png"
    if "gravatar.com" in url:
        return re.sub(r"([?&])s=72\b", r"\1s=192", url)
    return url


def _download_avatars(users: dict, viewer: Path):
    """Download user avatars into viewer/ava/ and rewrite the avatar field
    to a local relative path on success. Avatars are public CDN resources
    needing no login; on failure keep the original URL (visible online).
    """
    from concurrent.futures import ThreadPoolExecutor

    ava_dir = viewer / "ava"
    ava_dir.mkdir(parents=True, exist_ok=True)
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 Chrome/120 Safari/537.36")

    todo = []
    for uid, u in users.items():
        url = u.get("avatar") or ""
        if not url or url.startswith("ava/"):
            continue
        todo.append((uid, url))

    def fetch(item):
        uid, url = item
        dest = ava_dir / f"{uid}.png"
        if dest.exists():
            return uid, dest.name, True
        try:
            r = requests.get(_avatar_hires(url), timeout=20,
                             headers={"User-Agent": UA})
            if r.status_code == 200 and r.content:
                dest.write_bytes(r.content)
                return uid, dest.name, True
        except requests.RequestException:
            pass
        return uid, None, False

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        for uid, name, good in ex.map(fetch, todo):
            if good:
                users[uid]["avatar"] = f"ava/{name}"
                ok += 1
            else:
                fail += 1
    print(f"User avatars: {ok} localized, {fail} downloads failed "
          "(online URLs kept)")


def run_html(out_dir: Path):
    raw = out_dir / "raw"
    viewer = out_dir / "viewer"
    ch_dir = viewer / "ch"
    ch_dir.mkdir(parents=True, exist_ok=True)

    users = json.loads((raw / "users.json").read_text(encoding="utf-8"))
    idx = {}
    if (raw / "channels.json").exists():
        for c in json.loads((raw / "channels.json").read_text(encoding="utf-8")):
            if isinstance(c, dict) and c.get("id"):
                idx[c["id"]] = c
    meta = {}
    if (raw / "meta.json").exists():
        meta = json.loads((raw / "meta.json").read_text(encoding="utf-8"))

    _download_avatars(users, viewer)

    # Single pass over raw/*.json: write ch/<id>.js, merge channels missing
    # from the index (possible when exporting is in progress / crashed
    # midway), and collect the canvas doc list for the sidebar
    SKIP = {"users.json", "meta.json", "channels.json", "_channels_cache.json"}
    n = 0
    docs = []
    seen_doc = set()
    for f in raw.glob("*.json"):
        if f.name in SKIP:
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue  # export process is writing; skip
        if not (isinstance(d, dict) and d.get("channel")):
            continue
        cmeta = d["channel"]
        cid = cmeta.get("id")
        if not cid:
            continue
        if cid not in idx:
            idx[cid] = cmeta
        for m in d.get("messages") or []:
            for fl in m.get("files") or []:
                if (fl.get("mimetype") == "application/vnd.slack-docs"
                        and fl.get("id") not in seen_doc):
                    seen_doc.add(fl.get("id"))
                    lp = fl.get("local_path") or ""
                    docs.append({
                        "id": fl.get("id"),
                        "title": fl.get("title") or fl.get("name") or "画板",
                        "path": lp,
                        "channel": cmeta.get("title") or cid,
                        "updated": fl.get("updated") or 0,
                    })
        (ch_dir / f"{cid}.js").write_text(
            "window.__CH=window.__CH||{};window.__CH[" +
            json.dumps(cid) + "]=" + _js_json(d.get("messages") or []) + ";",
            encoding="utf-8")
        n += 1

    channels = sorted(idx.values(),
                      key=lambda c: (-(c.get("msg_count") or 0),
                                     c.get("title") or ""))
    data = {
        "workspace": meta.get("name") or "Slack",
        "generated_at": meta.get("exported_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_channels": len(channels),
        "total_messages": sum(c.get("msg_count") or 0 for c in channels),
        "users": users,
        "channels": channels,
        "docs": sorted(docs, key=lambda x: -(x.get("updated") or 0)),
    }
    (viewer / "data.js").write_text(
        "window.SLACK_DATA = " + _js_json(data) + ";", encoding="utf-8")

    (viewer / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    print(f"HTML viewer generated: {viewer / 'index.html'} ({n} channels)")
    return viewer / "index.html"


if __name__ == "__main__":
    run_html(Path(__file__).resolve().parent / "output")
