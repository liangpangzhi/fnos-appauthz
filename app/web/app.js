'use strict';

/* ============================ 全局状态 ============================ */
const S = {
  state: null,
  tab: 'grant',
  sel: new Set(),
  paths: [''],       // 可一次授权多个文件夹
  perm: 'rw',
  recursive: false,
  restartAfter: false,
  browse: null,
  results: null,
  orphans: [],
  orphanCount: 0,
  diag: null,
  verify: {},        // "app|path" -> verify result
  filter: '',
  search: '',
  busy: false,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.prototype.slice.call(document.querySelectorAll(sel));

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function toast(msg, isError) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show' + (isError ? ' err' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.className = 'toast'; }, 3200);
}

/* 应用可能直接跑在 http://NAS:18080/，也可能挂在飞牛网关的 /appauthz/ 下。
   接口一律按当前页面路径相对解析，两种入口都能用。 */
const API_BASE = (() => {
  const path = location.pathname || '/';
  const cut = path.lastIndexOf('/');
  return cut >= 0 ? path.slice(0, cut + 1) : '/';
})();

async function api(path, body) {
  const init = { headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin' };
  if (body !== undefined) {
    init.method = 'POST';
    init.body = JSON.stringify(body);
  }
  const url = API_BASE + String(path).replace(/^\/+/, '');
  const res = await fetch(url, init);
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) {
    if (res.status === 401) { showLogin(data.error || '请先登录'); }
    const err = new Error(data.error || ('请求失败 HTTP ' + res.status));
    err.status = res.status;
    throw err;
  }
  return data;
}

/* 全局错误兜底：把 JS 异常显示出来，避免只看到“加载中” */
function fatal(msg) {
  const box = document.getElementById('main');
  if (box) box.insertAdjacentHTML('afterbegin',
    '<div class="notice">界面脚本出错：' + esc(msg) + '</div>');
}
window.addEventListener('error', (ev) => fatal(ev.message || '未知错误'));
window.addEventListener('unhandledrejection', (ev) => {
  const reason = ev.reason && ev.reason.message ? ev.reason.message : String(ev.reason || '');
  fatal(reason);
});

/* ============================ 启动 ============================ */
async function boot() {
  try {
    const ping = await api('/api/ping');
    $('#ver').textContent = 'v' + ping.version;
    if (ping.needs_setup) return renderSetup();
    if (!ping.logged_in) return showLogin();
    await loadState();
  } catch (e) {
    $('#main').innerHTML = '<div class="notice">无法连接后端服务：' + esc(e.message) + '</div>';
  }
}

function renderSetup() {
  $('#chips').innerHTML = '';
  $('#tabs').innerHTML = '';
  $('#main').innerHTML = `
    <div class="card"><div class="card-body">
      <h3>尚未初始化管理密码</h3>
      <p class="muted mt8">安装向导中设置的管理密码没有写入成功。请在 NAS 终端执行：</p>
      <pre class="log">/var/apps/appauthz/target/server/manage.py reset-password --password '新密码'</pre>
      <p class="muted">然后点击下面的按钮。</p>
      <button class="btn primary mt12" data-act="reload">重新检测</button>
    </div></div>`;
}

function showLogin(msg) {
  $('#btnLogout').classList.add('hidden');
  $('#tabs').innerHTML = '';
  $('#chips').innerHTML = '';
  $('#main').innerHTML = `
    <div class="login-wrap"><div class="login">
      <h2>应用授权管家</h2>
      <p>请输入安装时设置的管理密码</p>
      ${msg ? '<div class="notice">' + esc(msg) + '</div>' : ''}
      <label class="field"><span>管理密码</span>
        <input type="password" id="loginPwd" autocomplete="current-password" placeholder="••••••">
      </label>
      <button class="btn primary" style="width:100%" data-act="login">登录</button>
    </div></div>`;
  const input = $('#loginPwd');
  if (input) {
    input.focus();
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
  }
}

async function doLogin() {
  const input = $('#loginPwd');
  if (!input) return;
  try {
    await api('/api/login', { password: input.value });
    await loadState();
  } catch (e) {
    toast(e.message, true);
    input.select();
  }
}

async function loadState() {
  S.state = await api('/api/state');
  const hash = (location.hash || '').replace('#', '');
  if (['grant', 'overview', 'orphans', 'diag'].indexOf(hash) >= 0) S.tab = hash;
  $('#btnLogout').classList.remove('hidden');
  renderShell();
  render();
  loadOrphanCount();
}

function renderShell() {
  const st = S.state;
  if (!st) return;
  $('#ver').textContent = 'v' + st.version + (st.demo ? ' · 演示模式' : '');
  $('#chips').innerHTML = [
    `<span class="chip"><i class="dot ${st.db_ok ? 'ok' : 'err'}"></i>授权库 <b>${st.db_ok ? '正常' : '异常'}</b></span>`,
    `<span class="chip">ACL <b>${esc(st.acl_mode)}</b></span>`,
    `<span class="chip">已安装应用 <b>${st.apps.length}</b></span>`,
    (st.settings && st.settings.no_login)
      ? '<span class="chip" title="任何能访问本端口的人都能修改授权"><i class="dot err"></i>免登录</span>'
      : '',
  ].filter(Boolean).join('');
  const tabs = [['grant', '批量授权', 0], ['overview', '授权总览', 0],
                ['orphans', '失效清理', S.orphanCount], ['diag', '诊断与设置', 0]];
  $('#tabs').innerHTML = tabs.map(([key, label, count]) => `
    <button class="tab ${S.tab === key ? 'active' : ''}" data-act="tab" data-tab="${key}">
      ${label}${count ? `<span class="count">${count}</span>` : ''}
    </button>`).join('');
}

function render() {
  renderShell();
  if (!S.state) return;
  if (S.tab === 'grant') return renderGrant();
  if (S.tab === 'overview') return renderOverview();
  if (S.tab === 'orphans') return renderOrphans();
  if (S.tab === 'diag') return renderDiag();
}

function switchTab(tab) {
  S.tab = tab;
  try { history.replaceState(null, '', '#' + tab); } catch (e) { /* ignore */ }
  render();
  if (tab === 'orphans') loadOrphans();
  if (tab === 'diag') loadDiag();
}

async function loadOrphanCount() {
  try {
    const data = await api('/api/orphans');
    S.orphanCount = (data.items || []).length;
    if (S.tab === 'grant' || S.tab === 'overview') renderShell();
  } catch (e) { /* 忽略 */ }
}

/* ============================ 批量授权 ============================ */
function syncPathsFromDom() {
  const inputs = $$('#pathList .path-input');
  if (!inputs.length) return;
  S.paths = inputs.map((el) => el.value.trim());
}

function renderGrant() {
  if (!Array.isArray(S.paths) || !S.paths.length) S.paths = [''];
  const paths = S.paths;
  const st = S.state;
  const apps = st.apps.filter((a) => {
    const q = S.search.trim().toLowerCase();
    if (!q) return true;
    return (a.display + ' ' + a.name).toLowerCase().indexOf(q) >= 0;
  });
  const selectedCount = S.sel.size;

  $('#main').innerHTML = `
    ${st.db_ok ? '' : `<div class="notice">授权数据库不可用：${esc(st.db_error || '未知错误')}。授权只能写入文件 ACL，系统设置界面可能不同步。</div>`}
    <div class="grid-2">
      <div class="card">
        <div class="card-head">
          <h3>1. 选择应用</h3>
          <span class="sub" id="selCount">已选 ${selectedCount} 个</span>
          <div class="spacer"></div>
          <button class="btn sm" data-act="selAll">全选</button>
          <button class="btn sm" data-act="selNone">清空</button>
        </div>
        <div class="toolbar">
          <input type="text" id="appSearch" placeholder="搜索应用…" value="${esc(S.search)}">
        </div>
        <div class="app-list" id="appList">
          ${apps.length ? apps.map(appItem).join('') : '<div class="empty">没有匹配的应用</div>'}
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h3>2. 文件夹与权限</h3></div>
        <div class="card-body">
          <label class="field"><span>文件夹路径 <small class="muted">（可一次授权多个）</small></span>
            <div id="pathList">
              ${paths.map((p, i) => `
                <div class="row path-row" style="flex-wrap:nowrap;margin-bottom:8px">
                  <input type="text" class="path-input" data-idx="${i}" placeholder="/vol1/1000/媒体" value="${esc(p)}">
                  <button class="btn ghost" data-act="pathDel" data-idx="${i}" title="移除此行">✕</button>
                </div>`).join('')}
            </div>
            <div class="row" style="gap:8px;margin-top:4px">
              <button class="btn sm" data-act="pathAdd">＋ 添加路径</button>
              <button class="btn sm" data-act="browse">浏览…</button>
              <span class="muted" style="font-size:12px">共 ${paths.filter((x) => x.trim()).length} 个有效路径</span>
            </div>
          </label>
          <div class="muted" style="font-size:12px;margin:-2px 0 14px">
            每行一个文件夹，可一次提交「多应用 × 多目录」；路径需真实存在。常用位置：/vol1、/vol2 下的用户目录，外接存储与远程挂载在同级。
          </div>
          <label class="field"><span>权限</span>
            <div class="segmented" id="permSeg">
              <button data-act="perm" data-perm="rw" class="${S.perm === 'rw' ? 'on' : ''}">读写</button>
              <button data-act="perm" data-perm="ro" class="${S.perm === 'ro' ? 'on' : ''}">只读</button>
            </div>
          </label>
          <label class="switch mt8" style="display:flex;margin:4px 0 10px">
            <input type="checkbox" data-act="recursive" ${S.recursive ? 'checked' : ''}>
            <span class="track"></span>
            <span>递归应用到子文件夹（目录很大时较慢）</span>
          </label>
          <label class="switch" style="display:flex;margin:0 0 14px">
            <input type="checkbox" data-act="restartAfter" ${S.restartAfter ? 'checked' : ''}>
            <span class="track"></span>
            <span>授权后重启目标应用（立即加载新目录，会短暂中断）</span>
          </label>
          <div class="notice info mt16">
            <div>授权 = 写入 appcenter 授权记录 <b>+</b> 给应用系统账户添加文件 ACL，两者缺一不可。
            只读（ro）只给读与进入权限，应用无法创建或修改文件。</div>
          </div>
          <div class="row mt16">
            <button class="btn" data-act="preview">预览影响</button>
            <button class="btn primary" data-act="grant" ${selectedCount ? '' : 'disabled'}>执行授权</button>
          </div>
        </div>
      </div>
    </div>
    <div id="resultArea">${S.results ? resultsHtml(S.results) : ''}</div>`;
}

function appItem(a) {
  const count = (a.paths || []).length;
  return `
    <label class="app-item">
      <input type="checkbox" data-act="pick" data-app="${esc(a.name)}" ${S.sel.has(a.name) ? 'checked' : ''}>
      <div class="grow">
        <div class="name">${esc(a.display)}
          ${a.builtin ? '<span class="badge">内置</span>' : ''}
          ${a.no_authz ? '<span class="badge warn">不支持目录授权</span>' : ''}
          ${a.app_id ? '' : '<span class="badge warn">无 app_id</span>'}
        </div>
        <div class="meta">${esc(a.name)} · uid ${a.uid == null ? '—' : a.uid} · 已授权 ${count} 个目录</div>
      </div>
    </label>`;
}

function updateSelCount() {
  const el = $('#selCount');
  if (el) el.textContent = '已选 ' + S.sel.size + ' 个';
  const btn = document.querySelector('[data-act="grant"]');
  if (btn) btn.disabled = S.sel.size === 0;
}

function resultsHtml(data) {
  const rows = data.results || [];
  const okCount = data.ok_count || 0;
  const total = data.total || rows.length;
  const head = `
    <div class="card-head">
      <h3>${data.dry_run ? '预览结果' : '执行结果'}</h3>
      <span class="badge ${okCount === total ? 'ok' : 'warn'}">${okCount}/${total} 成功</span>
      ${(data.missing || []).length ? `<span class="badge err">未找到：${esc(data.missing.join(', '))}</span>` : ''}
      <div class="spacer"></div>
      <button class="btn ghost sm" data-act="clearResult">收起</button>
    </div>`;
  const restarts = (data.restarts || []).map((r) => `
    <div class="result-head" style="background:transparent;padding:6px 2px 0">
      <span class="badge ${r.ok ? 'ok' : 'warn'}">${r.ok ? '已重启' : '未重启'}</span>
      <span class="muted" style="font-size:12.5px">${esc(r.app)}：${esc(r.message || '')}</span>
    </div>`).join('');
  const body = rows.map((r) => {
    const cls = r.ok ? 'ok' : (r.skipped ? 'warn' : 'err');
    const label = r.ok ? (r.skipped ? '跳过' : '成功') : '失败';
    const steps = (r.steps || []).map((s) => {
      const cmd = s.kind === 'db' ? (s.sql || s.cmd) : s.cmd;
      const rc = s.rc === undefined ? '' : `退出码 ${s.rc}`;
      return `<pre class="log"><b>${s.kind === 'db' ? 'SQL' : 'ACL'}</b> ${esc(rc)}
${esc(cmd)}${s.err ? '\n' + esc(s.err) : ''}${s.out ? '\n' + esc(s.out) : ''}</pre>`;
    }).join('');
    return `
      <div class="result">
        <div class="result-head">
          <span class="badge ${cls}">${label}</span>
          <div class="grow">
            <b>${esc(r.display || r.app)}</b>
            <span class="muted mono-list">${esc(r.path || '')}</span>
          </div>
          ${r.perm ? `<span class="badge blue">${r.perm === 'rw' ? '读写' : '只读'}</span>` : ''}
        </div>
        <div class="result-body">
          <div class="muted" style="font-size:12.5px">${esc(r.message || '')}</div>
          ${steps ? `<details><summary>查看执行明细（${(r.steps || []).length} 步）</summary>${steps}</details>` : ''}
        </div>
      </div>`;
  }).join('');
  return `<div class="card">${head}<div class="card-body">${restarts}${body || '<div class="empty">没有结果</div>'}</div></div>`;
}

async function runGrant(dry) {
  const apps = Array.from(S.sel);
  syncPathsFromDom();
  const paths = [];
  S.paths.forEach((raw) => {
    const one = String(raw || '').trim();
    if (one && paths.indexOf(one) < 0) paths.push(one);
  });
  if (!apps.length) return toast('请先选择应用', true);
  if (!paths.length) return toast('请先选择或输入文件夹', true);
  const scope = `${apps.length} 个应用 × ${paths.length} 个目录 = ${apps.length * paths.length} 条授权`;
  if (!dry && !confirm(`即将执行 ${scope}：\n\n应用：${apps.join('、')}\n目录：\n${paths.map((p) => '· ' + p).join('\n')}\n权限：${S.perm === 'rw' ? '读写' : '只读'}\n\n操作前会自动备份授权表，是否继续？`)) return;
  try {
    const data = await api('/api/grant', {
      apps, paths, perm: S.perm, dry_run: !!dry, recursive: S.recursive,
      restart: S.restartAfter,
    });
    S.results = data;
    S.state = await api('/api/state');
    render();
    if (!dry) toast(`完成：${data.ok_count}/${data.total} 成功`, data.ok_count !== data.total);
  } catch (e) {
    toast(e.message, true);
  }
}

/* ============================ 授权总览 ============================ */
function renderOverview() {
  const st = S.state;
  const q = S.filter.trim().toLowerCase();
  const groups = [];
  st.apps.forEach((a) => {
    const paths = (a.paths || []).filter((p) => {
      if (!q) return true;
      return (a.display + ' ' + a.name + ' ' + p.path).toLowerCase().indexOf(q) >= 0;
    });
    if (paths.length) groups.push({ app: a, paths });
  });
  const totalPaths = groups.reduce((n, g) => n + g.paths.length, 0);

  $('#main').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>授权总览</h3>
        <span class="sub">${groups.length} 个应用 · ${totalPaths} 条授权</span>
        <div class="spacer"></div>
        <input type="text" id="ovFilter" placeholder="筛选应用或路径…" style="width:220px" value="${esc(S.filter)}">
        <button class="btn sm" data-act="reloadState">刷新</button>
      </div>
      <div class="card-body tight">
        ${groups.length ? groups.map(groupHtml).join('') : '<div class="empty"><div class="big">∅</div>没有匹配的授权记录</div>'}
      </div>
    </div>`;
}

function groupHtml(group) {
  const a = group.app;
  return `
    <div style="border-bottom:1px solid var(--border)">
      <div class="card-head" style="background:transparent">
        <b>${esc(a.display)}</b>
        <span class="muted mono-list">${esc(a.name)} · uid ${a.uid == null ? '—' : a.uid}</span>
        <div class="spacer"></div>
        <button class="btn sm" data-act="copy" data-app="${esc(a.name)}">复制授权到…</button>
        <button class="btn sm" data-act="verifyApp" data-app="${esc(a.name)}">实测权限</button>
        <button class="btn sm danger" data-act="revokeApp" data-app="${esc(a.name)}">全部撤销</button>
      </div>
      <table class="tbl">
        <tbody>
        ${group.paths.map((p) => pathRow(a, p)).join('')}
        </tbody>
      </table>
    </div>`;
}

function pathRow(a, p) {
  const key = a.name + '|' + p.path;
  const v = S.verify[key];
  const permBadge = p.perm === 'rw' ? '<span class="badge ok">读写</span>'
    : p.perm === 'ro' ? '<span class="badge blue">只读</span>'
    : `<span class="badge warn">${esc(p.perm)}</span>`;
  const exist = p.exists ? '' : '<span class="badge err">路径不存在</span>';
  const verify = v ? '<span class="muted" style="font-size:12px">' + (v.checks || []).map(
    (c) => `${esc(c.label)}${c.ok === true ? '✓' : c.ok === false ? '✗' : '?'}`).join(' ') + '</span>' : '';
  return `
    <tr>
      <td class="path">${esc(p.path)} ${exist}</td>
      <td style="width:90px">${permBadge}</td>
      <td style="width:120px">${verify}</td>
      <td style="width:150px;text-align:right">
        <button class="btn sm" data-act="verifyOne" data-app="${esc(a.name)}" data-path="${esc(p.path)}">校验</button>
        <button class="btn sm danger" data-act="revokeOne" data-app="${esc(a.name)}" data-path="${esc(p.path)}">撤销</button>
      </td>
    </tr>`;
}

async function revokePaths(appName, paths) {
  if (!paths.length) return;
  if (!confirm(`确认撤销 ${appName} 的 ${paths.length} 条授权？\n\n会同时删除授权记录与文件 ACL 条目。`)) return;
  try {
    const data = await api('/api/revoke', { apps: [appName], paths });
    S.state = await api('/api/state');
    render();
    toast(`已撤销 ${data.ok_count}/${data.total}`, data.ok_count !== data.total);
  } catch (e) { toast(e.message, true); }
}

async function verifyPaths(appName, paths) {
  if (!paths.length) return;
  try {
    const data = await api('/api/verify', { apps: [appName], paths });
    (data.results || []).forEach((r) => { S.verify[r.app + '|' + r.path] = r; });
    render();
    const bad = (data.results || []).filter((r) => (r.checks || []).some((c) => c.ok === false));
    if (bad.length) toast(`有 ${bad.length} 条路径实测不通过，请查看标记`, true);
    else toast('实测通过');
  } catch (e) { toast(e.message, true); }
}

/* ============================ 失效清理 ============================ */
async function loadOrphans() {
  $('#main').innerHTML = '<div class="empty">正在扫描授权记录…</div>';
  try {
    const data = await api('/api/orphans');
    S.orphans = data.items || [];
    S.orphanCount = S.orphans.length;
    renderShell();
    renderOrphanList();
  } catch (e) {
    $('#main').innerHTML = `<div class="notice">扫描失败：${esc(e.message)}</div>`;
  }
}

function renderOrphans() {
  $('#main').innerHTML = '<div class="empty">正在扫描授权记录…</div>';
  loadOrphans();
}

function renderOrphanList() {
  const items = S.orphans;
  const body = items.length ? `
    <table class="tbl">
      <thead><tr>
        <th style="width:36px"><input type="checkbox" data-act="orphanAll"></th>
        <th>路径</th><th style="width:110px">账户 / 应用</th>
        <th style="width:130px">原因</th><th style="width:150px">数据表</th>
      </tr></thead>
      <tbody>${items.map((it, idx) => `
        <tr>
          <td><input type="checkbox" data-act="orphanPick" data-idx="${idx}"></td>
          <td class="path">${esc(it.path)}</td>
          <td class="muted">${esc(it.uname || it.app_name || it.uid || '—')}</td>
          <td><span class="badge warn">${esc(it.reason)}</span></td>
          <td class="muted mono-list">${esc(it.table)}</td>
        </tr>`).join('')}
      </tbody>
    </table>` : '<div class="empty"><div class="big">✓</div>没有发现失效授权，很干净</div>';

  $('#main').innerHTML = `
    <div class="notice">
      <div>卷、网盘或外接存储卸载后，fnOS 不会自动清理这些指向已消失路径的授权记录，
      会造成应用中心持续报 <code>trim access stat error</code> 并让日志膨胀。清理前会自动备份授权表。</div>
    </div>
    <div class="card">
      <div class="card-head">
        <h3>失效授权（${items.length}）</h3>
        <div class="spacer"></div>
        <button class="btn sm" data-act="reloadOrphans">重新扫描</button>
        <button class="btn sm danger" data-act="purge" ${items.length ? '' : 'disabled'}>清理选中</button>
      </div>
      <div class="card-body tight">${body}</div>
    </div>`;
}

async function purgeOrphans() {
  const picked = $$('[data-act="orphanPick"]').filter((el) => el.checked)
    .map((el) => S.orphans[Number(el.dataset.idx)]).filter(Boolean);
  if (!picked.length) return toast('请先勾选要清理的记录', true);
  if (!confirm(`确认从数据库删除 ${picked.length} 条失效授权记录？\n\n（仅删除数据库记录，不影响任何文件）`)) return;
  try {
    await api('/api/orphans/purge', { items: picked });
    toast('已清理');
    await loadOrphans();
  } catch (e) { toast(e.message, true); }
}

/* ============================ 诊断与设置 ============================ */
async function loadDiag() {
  $('#main').innerHTML = '<div class="empty">正在检测…</div>';
  try {
    const data = await api('/api/diagnose');
    S.diag = data.data;
    renderDiagView();
  } catch (e) {
    $('#main').innerHTML = `<div class="notice">检测失败：${esc(e.message)}</div>`;
  }
}

function renderDiag() {
  $('#main').innerHTML = '<div class="empty">正在检测…</div>';
  loadDiag();
}

function renderDiagView() {
  const d = S.diag;
  if (!d) return;
  const st = S.state;
  const tools = d.tools || {};
  const toolNames = ['trimacl', 'setfacl', 'getfacl', 'psql', 'pg_dump', 'runuser', 'python3'];
  const sc = d.schema || {};
  const settings = d.settings || {};

  $('#main').innerHTML = `
    ${st.demo ? '<div class="notice info">当前为演示模式（未连接 fnOS 数据库与 ACL），界面行为与真实环境一致。</div>' : ''}
    ${(d.is_root || st.demo) ? '' : '<div class="notice">当前进程不是 root，写入授权与 ACL 会失败。</div>'}

    <div class="grid-2">
      <div class="card">
        <div class="card-head"><h3>运行环境</h3></div>
        <div class="card-body">
          <dl class="kv">
            <dt>平台</dt><dd>${esc(d.platform)} · root=${d.is_root}</dd>
            <dt>ACL 方式</dt><dd><b>${esc(d.acl_mode)}</b></dd>
            ${toolNames.map((name) => `
              <dt>${name}</dt>
              <dd class="mono-list">${tools[name] ? esc(tools[name]) : '<span class="badge err">未找到</span>'}</dd>`).join('')}
          </dl>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h3>授权数据库</h3>
          <span class="badge ${sc.ok ? 'ok' : 'err'}">${sc.ok ? '结构已识别' : '结构未识别'}</span>
        </div>
        <div class="card-body">
          ${sc.error ? `<div class="notice">${esc(sc.error)}</div>` : ''}
          <dl class="kv">
            <dt>应用授权表</dt><dd class="mono-list">${esc(sc.app_path_table || '—')}</dd>
            <dt>路径权限表</dt><dd class="mono-list">${esc(sc.path_table || '—')}</dd>
            <dt>应用信息表</dt><dd class="mono-list">${esc(sc.app_table || '—')}${sc.app_name_col ? '（' + esc(sc.app_name_col) + '）' : ''}</dd>
            <dt>perm 取值统计</dt>
            <dd>${(d.perm_stats || []).length ? (d.perm_stats || []).map(
              (p) => `<span class="badge ${p.label === 'rw' ? 'ok' : p.label === 'ro' ? 'blue' : 'warn'}">${esc(p.perm)}=${esc(p.label)}</span> × ${esc(p.count)}`).join(' ')
              : '<span class="muted">无数据</span>'}</dd>
            <dt>存储卷</dt>
            <dd>${(d.volumes || []).map((v) => `<span class="badge">${esc(v.path)} ${esc(v.free)} 可用</span>`).join(' ')}</dd>
          </dl>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h3>参数设置</h3><span class="sub">修改后立即生效（端口除外）</span></div>
      <div class="card-body">
        <div class="grid-2">
          <div>
            <label class="field"><span>ACL 工具</span>
              <select id="setAclMode">
                <option value="auto" ${settings.acl_mode === 'auto' ? 'selected' : ''}>自动（优先 trimacl）</option>
                <option value="trimacl" ${settings.acl_mode === 'trimacl' ? 'selected' : ''}>trimacl（fnOS 1.2+）</option>
                <option value="setfacl" ${settings.acl_mode === 'setfacl' ? 'selected' : ''}>setfacl（旧版 / POSIX）</option>
              </select>
            </label>
            <label class="field"><span>读写对应的 perm 值</span>
              <input type="number" id="setPermRw" value="${esc(settings.perm_rw)}">
            </label>
            <label class="field"><span>只读对应的 perm 值</span>
              <input type="number" id="setPermRo" value="${esc(settings.perm_ro)}">
            </label>
            <label class="field"><span>服务端口（重启后生效）</span>
              <input type="number" id="setPort" value="${esc(settings.port)}">
            </label>
          </div>
          <div>
            <label class="field"><span>允许操作的根目录（每行一个）</span>
              <textarea id="setRoots" rows="4" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--panel);color:var(--text);font-family:monospace;font-size:12.5px">${esc((settings.roots || []).join('\n'))}</textarea>
            </label>
            <label class="switch" style="display:flex;margin:4px 0 12px">
              <input type="checkbox" id="setRecursive" ${settings.recursive ? 'checked' : ''}>
              <span class="track"></span><span>默认递归套用 ACL</span>
            </label>
            <label class="switch" style="display:flex;margin:0 0 12px">
              <input type="checkbox" id="setBackup" ${settings.backup ? 'checked' : ''}>
              <span class="track"></span><span>写操作前自动备份授权表</span>
            </label>
            <label class="switch" style="display:flex;margin:0 0 8px">
              <input type="checkbox" id="setNoLogin" ${settings.no_login ? 'checked' : ''}>
              <span class="track"></span><span>免密码登录（打开界面不再要求输入管理密码）</span>
            </label>
            <div class="muted" style="font-size:12px;margin:0 0 16px">
              开启后，任何能访问本端口的人都能修改授权——本应用以 root 运行，
              请只在可信网络、或端口仅本机可访问时使用。
            </div>
            <button class="btn primary" data-act="saveSettings">保存设置</button>
          </div>
        </div>
        <div class="notice info mt16">
          <div>若界面里显示的权限与实际不符，多半是 <code>perm</code> 取值映射不同：上面两个数字可对照「perm 取值统计」调整
          （fnOS 常见：读写=6、只读=4）。</div>
        </div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-head"><h3>管理密码</h3>
          ${settings.no_login ? '<span class="badge warn">当前免登录</span>' : ''}
        </div>
        <div class="card-body">
          ${settings.no_login
            ? '<div class="notice info" style="margin:0 0 12px">当前为免登录模式，修改密码不需要输入原密码。设好密码后可随时在上方关闭「免密码登录」。</div>'
              + '<label class="field"><span>设置新密码（至少 6 位）</span><input type="password" id="pwdNew"></label>'
            : '<label class="field"><span>当前密码</span><input type="password" id="pwdOld"></label>'
              + '<label class="field"><span>新密码（至少 6 位）</span><input type="password" id="pwdNew"></label>'}
          <button class="btn" data-act="changePwd">${settings.no_login ? '设置密码' : '更新密码'}</button>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>运行日志</h3>
          <div class="spacer"></div>
          <button class="btn sm" data-act="reloadLog">刷新</button>
        </div>
        <div class="card-body">
          <pre class="log" id="logBox" style="max-height:240px">加载中…</pre>
        </div>
      </div>
    </div>`;
  loadLog();
}

async function loadLog() {
  try {
    const data = await api('/api/log?limit=200');
    const box = $('#logBox');
    if (box) box.textContent = (data.lines || []).slice(-200).join('\n') || '（暂无日志）';
  } catch (e) { /* 忽略 */ }
}

async function saveSettings() {
  const body = {
    acl_mode: $('#setAclMode').value,
    perm_rw: Number($('#setPermRw').value),
    perm_ro: Number($('#setPermRo').value),
    recursive: $('#setRecursive').checked,
    backup: $('#setBackup').checked,
    no_login: $('#setNoLogin').checked,
    port: Number($('#setPort').value),
    roots: $('#setRoots').value.split('\n').map((s) => s.trim()).filter(Boolean),
  };
  try {
    const data = await api('/api/settings', body);
    toast(data.message || '已保存');
    if (data.restart_required) return;
    S.state = await api('/api/state');
    loadDiag();
  } catch (e) { toast(e.message, true); }
}

async function changePwd() {
  const oldEl = $('#pwdOld');
  const oldPwd = oldEl ? oldEl.value : '';
  const newPwd = $('#pwdNew').value;
  if (newPwd.length < 6) return toast('新密码至少 6 位', true);
  try {
    if (!newPwd) return toast('请输入新密码', true);
    await api('/api/password', { old: oldPwd, new: newPwd });
    toast(oldEl ? '密码已更新，请重新登录' : '密码已设置（仍为免登录，可在上方关闭）');
    if (oldEl) setTimeout(() => location.reload(), 1200);
    else { S.state = await api('/api/state'); loadDiag(); }
  } catch (e) { toast(e.message, true); }
}

/* ============================ 文件夹选择器 ============================ */
function openBrowse(callback) {
  const root = $('#modalRoot');
  root.innerHTML = `
    <div class="modal-mask" data-act="maskClose">
      <div class="modal">
        <div class="modal-head"><h3>选择文件夹</h3>
          <div class="spacer"></div>
          <button class="btn ghost sm" data-act="closeModal">✕</button>
        </div>
        <div class="modal-body">
          <div class="row" style="flex-wrap:nowrap;margin-bottom:10px">
            <input type="text" id="browsePath" placeholder="/vol1/1000/媒体">
            <button class="btn" data-act="browseGo">跳转</button>
          </div>
          <div class="crumbs" id="browseCrumbs"></div>
          <div class="dir-list" id="browseList"><div class="empty">加载中…</div></div>
        </div>
        <div class="modal-foot">
          <button class="btn" data-act="closeModal">取消</button>
          <button class="btn primary" data-act="browsePick">选择当前文件夹</button>
        </div>
      </div>
    </div>`;
  browseLoad('', callback);
  S.browseCb = callback;
}

async function browseLoad(path) {
  try {
    const data = await api('/api/browse?path=' + encodeURIComponent(path || ''));
    S.browse = data.data;
    renderBrowse();
  } catch (e) {
    toast(e.message, true);
  }
}

function renderBrowse() {
  const b = S.browse;
  if (!b) return;
  const list = $('#browseList');
  const input = $('#browsePath');
  if (input) input.value = b.path || '';
  const crumbs = $('#browseCrumbs');
  if (crumbs) {
    if (!b.path) {
      crumbs.innerHTML = '<span class="muted">选择起点：</span>' +
        (b.roots || []).map((r) => `<a data-act="browseTo" data-path="${esc(r.path)}">${esc(r.label)}</a>`).join(' ');
    } else {
      const parts = b.path.split('/').filter(Boolean);
      let acc = '';
      const html = ['<a data-act="browseTo" data-path="">全部</a>'];
      parts.forEach((part) => {
        acc += '/' + part;
        html.push('<span class="muted">/</span>');
        html.push(`<a data-act="browseTo" data-path="${esc(acc)}">${esc(part)}</a>`);
      });
      crumbs.innerHTML = html.join('');
    }
  }
  if (!list) return;
  if (!b.path) {
    list.innerHTML = (b.roots || []).map((r) => `
      <div class="dir" data-act="browseTo" data-path="${esc(r.path)}">
        <span class="ico">▸</span><span>${esc(r.label)}</span>
        <div class="spacer" style="flex:1"></div>
        <button class="btn sm" data-act="browsePickAt" data-path="${esc(r.path)}">选择</button>
      </div>`).join('') || '<div class="empty">没有可用的存储空间</div>';
    return;
  }
  const parent = b.parent
    ? `<div class="dir" data-act="browseTo" data-path="${esc(b.parent)}"><span class="ico">↑</span><span class="muted">上级目录</span></div>`
    : '';
  const dirs = (b.dirs || []).map((d) => `
    <div class="dir" data-act="browseTo" data-path="${esc(d.path)}" title="点击进入">
      <span class="ico">📁</span><span>${esc(d.name)}</span>
      <div class="spacer" style="flex:1"></div>
      <button class="btn sm" data-act="browsePickAt" data-path="${esc(d.path)}">选择</button>
    </div>`).join('');
  list.innerHTML = parent + (dirs || '<div class="empty">该目录下没有子文件夹，可直接选择它</div>');
}

function closeModal() {
  $('#modalRoot').innerHTML = '';
  S.browseCb = null;
}

/* ============================ 复制授权 ============================ */
function openCopy(appName) {
  const st = S.state;
  const src = st.apps.find((a) => a.name === appName);
  if (!src) return;
  const others = st.apps.filter((a) => a.name !== appName);
  $('#modalRoot').innerHTML = `
    <div class="modal-mask" data-act="maskClose">
      <div class="modal">
        <div class="modal-head"><h3>把「${esc(src.display)}」的授权复制到…</h3></div>
        <div class="modal-body">
          <div class="notice info">会把这 ${(src.paths || []).length} 个目录及其权限一并复制给选中的应用（已存在的授权会被更新为目标权限）。</div>
          <div style="max-height:320px;overflow:auto;border:1px solid var(--border);border-radius:9px">
            ${others.map((a) => `
              <label class="app-item">
                <input type="checkbox" data-act="copyPick" data-app="${esc(a.name)}">
                <div class="grow">
                  <div class="name">${esc(a.display)}</div>
                  <div class="meta">${esc(a.name)} · 已授权 ${(a.paths || []).length} 个</div>
                </div>
              </label>`).join('') || '<div class="empty">没有其它应用</div>'}
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn" data-act="closeModal">取消</button>
          <button class="btn primary" data-act="copyRun" data-app="${esc(appName)}">开始复制</button>
        </div>
      </div>
    </div>`;
}

async function runCopy(appName) {
  const targets = $$('[data-act="copyPick"]').filter((el) => el.checked)
    .map((el) => el.dataset.app);
  if (!targets.length) return toast('请选择目标应用', true);
  if (!confirm(`确认把 ${appName} 的授权复制到 ${targets.length} 个应用？`)) return;
  try {
    const data = await api('/api/copy', { source: appName, targets });
    S.results = data;
    S.state = await api('/api/state');
    closeModal();
    S.tab = 'grant';
    render();
    toast(`复制完成：${data.ok_count}/${data.total} 成功`, data.ok_count !== data.total);
  } catch (e) { toast(e.message, true); }
}

/* ============================ 事件绑定 ============================ */
document.addEventListener('change', (e) => {
  const el = e.target;
  const act = el.dataset ? el.dataset.act : '';
  if (act === 'pick') {
    if (el.checked) S.sel.add(el.dataset.app); else S.sel.delete(el.dataset.app);
    updateSelCount();
  } else if (act === 'recursive') {
    S.recursive = el.checked;
  } else if (act === 'restartAfter') {
    S.restartAfter = el.checked;
  } else if (act === 'orphanAll') {
    $$('[data-act="orphanPick"]').forEach((box) => { box.checked = el.checked; });
  }
});

document.addEventListener('input', (e) => {
  const el = e.target;
  if (el.id === 'appSearch') {
    S.search = el.value;
    const list = $('#appList');
    const st = S.state;
    const q = S.search.trim().toLowerCase();
    const apps = st.apps.filter((a) => !q || (a.display + ' ' + a.name).toLowerCase().indexOf(q) >= 0);
    if (list) list.innerHTML = apps.length ? apps.map(appItem).join('') : '<div class="empty">没有匹配的应用</div>';
  } else if (el.id === 'ovFilter') {
    S.filter = el.value;
    const pos = el.selectionStart;
    renderOverview();
    const again = $('#ovFilter');
    if (again) { again.focus(); try { again.setSelectionRange(pos, pos); } catch (err) {} }
  }
});

document.addEventListener('click', async (e) => {
  const el = e.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act;
  const appName = el.dataset.app;
  const path = el.dataset.path;

  switch (act) {
    case 'tab': switchTab(el.dataset.tab); break;
    case 'reload': boot(); break;
    case 'login': doLogin(); break;
    case 'perm':
      S.perm = el.dataset.perm;
      $$('#permSeg button').forEach((b) => b.classList.toggle('on', b.dataset.perm === S.perm));
      break;
    case 'selAll': {
      const q = S.search.trim().toLowerCase();
      S.state.apps.forEach((a) => {
        if (!q || (a.display + ' ' + a.name).toLowerCase().indexOf(q) >= 0) S.sel.add(a.name);
      });
      renderGrant();
      break;
    }
    case 'selNone': S.sel.clear(); renderGrant(); break;
    case 'browse': {
      syncPathsFromDom();
      openBrowse((picked) => {
        closeModal();               // 先关窗，避免后续重渲染带来的时序问题
        const list = S.paths.slice();
        if (list.filter((x) => String(x || '').trim() === picked).length) {
          toast('该路径已在列表中');
          renderGrant();
          return;
        }
        const blank = list.findIndex((x) => !String(x || '').trim());
        if (blank >= 0) list[blank] = picked;
        else list.push(picked);
        S.paths = list;
        renderGrant();
      });
      break;
    }
    case 'pathAdd':
      syncPathsFromDom();
      S.paths.push('');
      renderGrant();
      {
        const inputs = $$('#pathList .path-input');
        if (inputs.length) inputs[inputs.length - 1].focus();
      }
      break;
    case 'pathDel': {
      syncPathsFromDom();
      const idx = Number(el.dataset.idx);
      if (S.paths.length <= 1) S.paths = [''];
      else S.paths.splice(idx, 1);
      renderGrant();
      break;
    }
    case 'browseTo': browseLoad(el.dataset.path); break;
    case 'browsePickAt': {
      const picked = el.dataset.path;
      if (!picked) return;
      const cb = S.browseCb;
      if (cb) cb(picked);
      closeModal();
      break;
    }
    case 'browseGo': browseLoad(($('#browsePath') || {}).value || ''); break;
    case 'browsePick':
      if (!S.browse || !S.browse.path) return toast('请先进入一个文件夹', true);
      if (S.browseCb) S.browseCb(S.browse.path);
      break;
    case 'closeModal': closeModal(); break;
    case 'maskClose': if (e.target === el) closeModal(); break;
    case 'preview': runGrant(true); break;
    case 'grant': runGrant(false); break;
    case 'clearResult': S.results = null; $('#resultArea').innerHTML = ''; break;
    case 'reloadState': S.state = await api('/api/state'); render(); break;
    case 'revokeOne': revokePaths(appName, [path]); break;
    case 'revokeApp': {
      const app = S.state.apps.find((a) => a.name === appName);
      revokePaths(appName, (app.paths || []).map((p) => p.path));
      break;
    }
    case 'verifyOne': verifyPaths(appName, [path]); break;
    case 'verifyApp': {
      const app = S.state.apps.find((a) => a.name === appName);
      verifyPaths(appName, (app.paths || []).map((p) => p.path));
      break;
    }
    case 'copy': openCopy(appName); break;
    case 'copyRun': runCopy(appName); break;
    case 'reloadOrphans': loadOrphans(); break;
    case 'purge': purgeOrphans(); break;
    case 'saveSettings': saveSettings(); break;
    case 'changePwd': changePwd(); break;
    case 'reloadLog': loadLog(); break;
    case 'logout': break;
    default: break;
  }
});

$('#btnLogout').addEventListener('click', async () => {
  try { await api('/api/logout', {}); } catch (e) { /* ignore */ }
  location.reload();
});

boot();
