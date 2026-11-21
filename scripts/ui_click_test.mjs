// 用真实浏览器（CDP）点击验证界面交互，避免"看起来对、点不动"。
//
//   node scripts/ui_click_test.mjs 9223 http://localhost:18080/#grant
//
// 前置：Edge/Chrome 以 --headless=new --remote-debugging-port=9223 启动。
// 覆盖三件事：
//   A. 选择弹窗里点目录行能进入、点行内「选择」能回填并关窗
//   B. 「浏览…」→ 进入目录 → 「选择当前文件夹」能回填
//   C. 多路径：加第二行路径 + 选两个应用，预览结果条数 = 应用数 × 路径数
const PORT = process.argv[2] || '9223';
const APP = process.argv[3] || 'http://localhost:18080/#grant';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const list = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
const page = list.find((t) => t.type === 'page');
if (!page) throw new Error('没有可用的页面目标');

const ws = new WebSocket(page.webSocketDebuggerUrl);
const pending = new Map();
let id = 0;
const call = (method, params) => new Promise((res, rej) => {
  const my = ++id;
  pending.set(my, { res, rej });
  ws.send(JSON.stringify({ id: my, method, params: params || {} }));
  setTimeout(() => {
    if (pending.has(my)) { pending.delete(my); rej(new Error('超时 ' + method)); }
  }, 20000);
});

await new Promise((resolve, reject) => {
  ws.onopen = resolve;
  ws.onerror = () => reject(new Error('WebSocket 连接失败'));
});
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const p = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? p.rej(new Error(msg.error.message)) : p.res(msg.result);
  }
};

const ev = async (expression) => {
  const r = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || '执行出错');
  return r.result.value;
};
const pathsOf = () => ev(
  "JSON.stringify([].slice.call(document.querySelectorAll('#pathList .path-input')).map(function(e){return e.value}))");

await call('Page.enable');
await call('Runtime.enable');
await call('Network.enable');
await call('Network.setCacheDisabled', { cacheDisabled: true });
await call('Page.navigate', { url: APP });

let ready = false;
for (let i = 0; i < 40; i++) {
  await sleep(300);
  ready = await ev("!!document.querySelector('#pathList .path-input')");
  if (ready) break;
}
if (!ready) throw new Error('界面未就绪（找不到路径输入框）');
console.log('✓ 界面已加载');

/* ---------------- A. 弹窗内点击 ---------------- */
await ev("openBrowse(function(p){ window.__picked = p; }); 'ok'");
await sleep(1100);
const rows = await ev("document.querySelectorAll('#browseList .dir').length");
if (!rows) throw new Error('弹窗里没有目录行');

const entered = await ev(`(function () {
  var rows = [].slice.call(document.querySelectorAll('#browseList .dir'));
  var t = rows.filter(function (r) { return r.dataset.path === '/vol1'; })[0] || rows[0];
  t.click();
  return t.dataset.path;
})()`);
await sleep(1000);
const nowPath = await ev("document.getElementById('browsePath').value");
if (!nowPath) throw new Error('点击目录行没有进入（路径仍为空）');
console.log(`A. 点目录行「${entered}」→ 当前路径「${nowPath}」`);

const pickedA = await ev(`(function () {
  var b = document.querySelector('#browseList .dir .btn[data-act=browsePickAt]');
  if (!b) return null;
  var v = b.dataset.path;
  b.click();
  return v;
})()`);
if (!pickedA) throw new Error('行内没有「选择」按钮');
await sleep(700);
if (await ev("!!document.querySelector('.modal')")) throw new Error('点「选择」后弹窗没关');
if ((await ev("window.__picked||null")) !== pickedA) throw new Error('点「选择」没有回传路径');
console.log(`A. 点行内「选择」→ 回传 ${pickedA}，弹窗已关闭`);

/* ---------------- B. 浏览 → 进入 → 选择当前文件夹 ---------------- */
await ev("document.querySelector('[data-act=browse]').click(); 'x'");
await sleep(1100);
if (!(await ev("!!document.querySelector('.modal')"))) throw new Error('点「浏览…」没有打开弹窗');
await ev(`(function () {
  var rows = [].slice.call(document.querySelectorAll('#browseList .dir'));
  var t = rows.filter(function (r) { return r.dataset.path === '/vol1'; })[0] || rows[0];
  t.click();
})()`);
await sleep(900);
await ev("document.querySelector('[data-act=browsePick]').click(); 'x'");
await sleep(700);
const afterB = JSON.parse(await pathsOf());
if (!afterB.filter(Boolean).length) throw new Error('「选择当前文件夹」没有回填路径');
if (await ev("!!document.querySelector('.modal')")) throw new Error('「选择当前文件夹」后弹窗没关');
console.log(`B. 浏览→选择当前文件夹 → 路径列表 ${JSON.stringify(afterB)}`);

/* ---------------- C. 多路径 × 多应用 ---------------- */
await ev("document.querySelector('[data-act=pathAdd]').click(); 'x'");
await sleep(400);
await ev(`(function () {
  var xs = [].slice.call(document.querySelectorAll('#pathList .path-input'));
  xs[0].value = '/vol1/1000';
  xs[xs.length - 1].value = '/vol1/@appconf';
  return xs.length;
})()`);
const filled = JSON.parse(await pathsOf());
if (filled.length < 2) throw new Error('没有添加出第二行路径');

// 选两个支持目录授权的应用
await ev("(function(){var a=document.querySelector('input[data-act=pick][data-app=pan115]'); if(a&&!a.checked)a.click()})()");
await sleep(400);
await ev("(function(){var b=document.querySelector('input[data-act=pick][data-app=fnos-scraper]'); if(b&&!b.checked)b.click()})()");
await sleep(500);
const appCount = await ev("document.querySelectorAll('input[data-act=pick]:checked').length");
if (appCount < 2) throw new Error('没有选中两个应用');

await ev("document.querySelector('[data-act=preview]').click(); 'x'");
await sleep(2500);
const preview = JSON.parse(await ev(`(function(){
  var rows = [].slice.call(document.querySelectorAll('#resultArea .result'));
  return JSON.stringify({
    count: rows.length,
    pairs: rows.map(function (r) {
      var b = r.querySelector('.grow b'), m = r.querySelector('.grow .mono-list');
      return (b ? b.textContent.trim() : '') + ' → ' + (m ? m.textContent.trim() : '');
    })
  });
})()`));
const validPaths = filled.filter((x) => x.trim()).length;
const expectCount = appCount * validPaths;
console.log(`C. ${appCount} 应用 × ${validPaths} 目录 → 结果 ${preview.count} 条：${JSON.stringify(preview.pairs)}`);
if (preview.count !== expectCount) {
  throw new Error(`结果条数不对：期望 ${expectCount}，实际 ${preview.count}`);
}

const shot = await call('Page.captureScreenshot', { format: 'png' });
const fs = await import('node:fs');
fs.writeFileSync(process.env.SHOT || '/tmp/ui-click.png', Buffer.from(shot.data, 'base64'));
console.log('✓ 全部通过，截图已保存');
ws.close();
