import {hosted, backendBase, accessToken, saveBackend, apiFetch} from './api-client.js';

const panel = document.createElement('dialog');
panel.id = 'backend-dialog';
panel.className = 'backend-dialog';
panel.innerHTML = `<form id="backend-form"><div class="dialog-top"><div><span class="eyebrow">ALPHA LAB · 服务连接</span><h2>连接你的研究工作区</h2></div><button type="button" class="icon-button" id="backend-close" aria-label="关闭服务连接">×</button></div>
<p class="dialog-intro">网站界面已就绪。连接 Alpha Lab 后端后，即可读取行情、运行回测并使用 Agent。</p>
<div class="info-box">GitHub Pages 仅托管前端。Python 后端需要单独部署；未连接时不会生成行情、收益或交易记录。</div>
<label class="field">后端服务地址<input id="backend-url" type="url" placeholder="https://your-alphalab.example.com" autocomplete="url"></label>
<label class="field">工作区访问口令<input id="backend-token" type="password" autocomplete="off" placeholder="后端的 ALPHALAB_ACCESS_TOKEN"></label>
<p class="field-help">这是工作区口令，不是模型 API Key。口令只在当前浏览器标签页会话中保存。</p>
<p id="backend-error" class="inline-error" role="alert" hidden></p>
<div class="dialog-footer"><a class="text-button" href="https://github.com/usbfl/suiyi.github.io/blob/main/docs/GITHUB_PAGES.md" target="_blank" rel="noopener">部署说明 ↗</a><button class="primary" type="submit">验证并连接 →</button></div></form>`;
document.body.append(panel);
let loaded = false;
function showConnection() {
  document.querySelector('#backend-url').value = backendBase() || (hosted ? '' : location.origin);
  document.querySelector('#backend-token').value = accessToken();
  document.querySelector('#backend-error').hidden = true;
  panel.showModal();
}
const connectButton = document.createElement('button');
connectButton.className = 'quiet'; connectButton.textContent = '服务连接'; connectButton.id = 'backend-button';
connectButton.onclick = showConnection;
document.querySelector('.top-actions').prepend(connectButton);
document.querySelector('#backend-close').onclick = () => panel.close();
if (hosted) {
  document.querySelector('.workspace-label div').innerHTML = '在线工作区<small>独立账户 · 模拟交易</small>';
  document.querySelector('.terminal-version').textContent = 'WEB';
  document.querySelector('.sidebar-note p').innerHTML = '前端：GitHub Pages<br>后端：独立 Python 服务';
  document.querySelector('footer > div > span:last-child').textContent = '模拟账户 · 在线工作区';
}
function waiting(message) {
  document.querySelector('#connection').textContent = '后端未连接';
  const box = document.querySelector('#initial-loading');
  box.replaceChildren();
  const title = document.createElement('h2'); title.textContent = 'Alpha Lab · 投资研究终端';
  const text = document.createElement('p'); text.textContent = message;
  const button = document.createElement('button'); button.className = 'primary'; button.textContent = '连接后端服务'; button.onclick = showConnection;
  box.append(title, text, button);
  document.querySelector('#new-button').disabled = true;
}
async function launch() {
  if (loaded) { location.reload(); return; }
  loaded = true;
  document.querySelector('.service a').href = (backendBase() || location.origin) + '/docs';
  await import('./app.js');
}
document.querySelector('#backend-form').onsubmit = async event => {
  event.preventDefault();
  const button = event.submitter, error = document.querySelector('#backend-error');
  button.disabled = true; error.hidden = true;
  try {
    const url = new URL(document.querySelector('#backend-url').value.trim());
    const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
    if (url.username || url.password || url.search || url.hash || url.pathname !== '/') throw Error('请输入后端根地址，不包含 /api、路径、参数或账号');
    if (url.protocol !== 'https:' && !(loopback && url.protocol === 'http:' && location.protocol === 'http:')) throw Error('在线工作区需要 HTTPS 后端地址');
    const base = url.origin, token = document.querySelector('#backend-token').value.trim();
    const response = await fetch(base + '/api/config', {headers: token ? {Authorization: 'Bearer ' + token} : {}, credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(20000)});
    if (response.status === 401) throw Error('访问口令不正确，请检查后端配置');
    if (!response.ok) throw Error('后端响应异常（' + response.status + '）');
    const config = await response.json();
    if (!Array.isArray(config.profiles) || !Array.isArray(config.presets)) throw Error('此地址不是兼容的 Alpha Lab 后端');
    saveBackend(base, token);
    panel.close();
    await launch();
  } catch (err) {
    error.textContent = err.name === 'TypeError' ? '无法连接。请检查 HTTPS 地址、服务是否启动，以及后端是否允许当前网站来源。' : err.message;
    error.hidden = false;
  } finally { button.disabled = false; }
};
if (hosted && !backendBase()) {
  waiting('行情、回测与 Agent 功能等待连接 Python 后端。请先部署后端，再填写服务地址。');
} else {
  try {
    const response = await apiFetch('/config', {signal: AbortSignal.timeout(20000)});
    if (!response.ok) throw Error('后端暂时不可用，或需要输入工作区访问口令。');
    await launch();
  } catch (err) { waiting('暂时无法连接后端，请检查服务地址与访问口令。'); showConnection(); }
}
// Before the application starts, keep the help/menu controls usable.
if (!loaded) {
  document.querySelector('#menu-button').onclick = () => document.querySelector('#sidebar').classList.toggle('open');
  document.querySelector('#help-button').onclick = showConnection;
  document.querySelector('#archives-button').onclick = showConnection;
  document.querySelectorAll('#navigation button').forEach(button => button.addEventListener('click', () => { if (!loaded) showConnection(); }));
}
