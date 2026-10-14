// One transport for the local app and the separately hosted Pages frontend.
const scope = location.pathname.replace(/[^/]*$/, '');
const storageKey = 'alphalab:backend:' + scope;
export const hosted = document.documentElement.dataset.hosting === 'github-pages';
export function backendBase() {
  return (localStorage.getItem(storageKey) || window.ALPHALAB_CONFIG?.apiBase || '').replace(/\/+$/, '');
}
const tokenKey = base => storageKey + ':token:' + base;
export function accessToken() { return sessionStorage.getItem(tokenKey(backendBase())) || ''; }
export function saveBackend(base, token) {
  const previous = backendBase();
  sessionStorage.removeItem(tokenKey(previous));
  localStorage.setItem(storageKey, base);
  if (token) sessionStorage.setItem(tokenKey(base), token);
}
export function apiURL(path) { return backendBase() + '/api' + path; }
function authHeaders(headers) {
  const result = new Headers(headers);
  if (accessToken()) result.set('Authorization', 'Bearer ' + accessToken());
  return result;
}
export async function apiFetch(path, options = {}) {
  if (hosted && !backendBase()) throw Error('请先连接 Alpha Lab 后端服务');
  return fetch(apiURL(path), {...options, headers: authHeaders(options.headers), credentials: 'omit', redirect: 'error'});
}
export async function requestJSON(path, method = 'GET', body) {
  const response = await apiFetch(path, {method,
    headers: body === undefined ? undefined : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(120000)});
  const result = await response.json().catch(() => null);
  if (!response.ok) throw Error(response.status === 401 ? '访问口令无效或已过期，请通过“服务连接”重新登录' :
    (typeof result?.detail === 'string' ? result.detail : result?.detail?.[0]?.msg || '服务请求失败（' + response.status + '）'));
  if (result === null) throw Error('后端没有返回有效 JSON，请检查服务地址');
  return result;
}
// Fetch-based SSE keeps the access token in a header, never in URLs or logs.
export function subscribe(handlers, status) {
  const controller = new AbortController();
  let retry;
  async function connect() {
    try {
      const response = await apiFetch('/stream', {signal: controller.signal, headers: {Accept: 'text/event-stream'}});
      if (!response.ok) throw Error(String(response.status));
      if (!response.headers.get('content-type')?.includes('text/event-stream')) throw Error('Invalid event stream');
      status('服务已连接');
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = '';
      try {
        while (!controller.signal.aborted) {
          const {value, done} = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, {stream: true});
          let match;
          while ((match = /\r?\n\r?\n/.exec(buffer))) {
            const block = buffer.slice(0, match.index);
            buffer = buffer.slice(match.index + match[0].length);
            let event = 'message'; const data = [];
            for (const line of block.split(/\r?\n/)) {
              if (line.startsWith('event:')) event = line.slice(6).trim();
              if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
            }
            if (data.length && handlers[event]) handlers[event](JSON.parse(data.join('\n')));
          }
        }
      } finally { await reader.cancel().catch(() => {}); }
      throw Error('Stream closed');
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error.message === '401') { status('访问口令已失效'); return; }
      status('正在重新连接');
      retry = setTimeout(connect, 5000);
    }
  }
  connect();
  return () => { clearTimeout(retry); controller.abort(); };
}
