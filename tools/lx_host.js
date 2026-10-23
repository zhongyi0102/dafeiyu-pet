/**
 * lx_host.js — LX Music 音源插件宿主
 *
 * 在 Node.js 里模拟 LX Music 的 `lx` 全局对象，加载第三方音源脚本，
 * 暴露 HTTP 接口给桌宠（Python）调用。
 *
 * 用法：
 *   node lx_host.js <音源.js> [端口]
 *   默认端口 9882
 *
 * 接口：
 *   GET  /health              -> {"ok":true,"sources":[...]}
 *   GET  /search?q=歌名        -> {"ok":true,"data":[{songmid,name,singer}]}
 *   GET  /lyric?songmid=xxx   -> {"ok":true,"data":{"lyric":"...","tlyric":""}}
 *   GET  /url?songmid=xxx     -> {"ok":true,"data":"http://...mp3"}
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');

// ---------- 代理支持 ----------
// Node 的 fetch 不读 HTTP_PROXY，需要显式设置 dispatcher
const PROXY = process.env.LX_PROXY || process.env.HTTPS_PROXY || '';
let dispatcher = null;
if (PROXY) {
  try {
    const undici = require('undici');
    dispatcher = new undici.ProxyAgent(PROXY);
    console.log('[lx] 使用代理:', PROXY);
  } catch (e) {
    console.warn('[lx] undici 不可用，代理失效:', e && e.message);
  }
}

// 带代理的 fetch
async function myFetch(u, opt = {}) {
  if (dispatcher) opt.dispatcher = dispatcher;
  return fetch(u, opt);
}

const PLUGIN_PATH = process.argv[2] || '';
const PORT = parseInt(process.argv[3] || '9882', 10);

if (!PLUGIN_PATH || !fs.existsSync(PLUGIN_PATH)) {
  console.error('[lx] 用法: node lx_host.js <音源.js> [端口]');
  process.exit(1);
}

// ---------------- LX 事件系统 ----------------
const handlers = {};   // eventName -> [fn]

function on(eventName, handler) {
  if (!handlers[eventName]) handlers[eventName] = [];
  handlers[eventName].push(handler);
}

function off(eventName, handler) {
  if (!handlers[eventName]) return;
  handlers[eventName] = handlers[eventName].filter(h => h !== handler);
}

function send(eventName, data) {
  const hs = handlers[eventName] || [];
  for (const h of hs) {
    try {
      const ret = h(data);
      if (ret && typeof ret.then === 'function') ret.catch(e => {
        console.error('[lx] handler error:', eventName, e && e.message);
      });
    } catch (e) {
      console.error('[lx] handler throw:', eventName, e && e.message);
    }
  }
}

// LX 的 EVENT_NAMES（音源脚本会引用）
const EVENT_NAMES = {
  request: 'request',
  inited: 'inited',
  updateAlert: 'updateAlert',
  error: 'error',
  log: 'log',
  apiAction: 'apiAction',
};

// ---------------- lx 全局对象 ----------------
const lx = {
  EVENT_NAMES,
  version: '2.0.0',
  env: 'desktop',
  currentScriptInfo: {
    name: 'flower',
    description: '',
    version: '1',
    author: '',
    homepage: '',
    rawScript: '',
  },
  on,
  off,
  send,
  request: async (reqUrl, options = {}, callback) => {
    // 音源用 lx.request 发 HTTP 请求
    try {
      const method = (options.method || 'GET').toUpperCase();
      const headers = options.headers || {};
      let body = options.body;
      if (body && typeof body === 'object' && !Buffer.isBuffer(body)) {
        body = new url.URLSearchParams(body).toString();
        if (!headers['Content-Type']) {
          headers['Content-Type'] = 'application/x-www-form-urlencoded';
        }
      }
      const resp = await myFetch(reqUrl, {
        method,
        headers,
        body: method === 'GET' ? undefined : body,
        signal: AbortSignal.timeout(options.timeout || 20000),
      });
      const text = await resp.text();
      let data = text;
      try { data = JSON.parse(text); } catch (_) {}
      if (callback) {
        const out = {
          statusCode: resp.status,
          headers: Object.fromEntries(resp.headers),
          body: data,
        };
        callback(null, out);
      }
      return { statusCode: resp.status, body: data };
    } catch (e) {
      if (callback) callback(e, null);
      throw e;
    }
  },
  utils: {
    buffer: {
      from: (...a) => Buffer.from(...a),
      bufToString: (buf, fmt) => Buffer.from(buf).toString(fmt || 'utf8'),
    },
    crypto: {
      md5: (s) => require('crypto').createHash('md5').update(s).digest('hex'),
      randomBytes: (n) => require('crypto').randomBytes(n),
      aesEncrypt: (buf, mode, key, iv) => {
        const c = require('crypto');
        const cipher = c.createCipheriv(mode, key, iv);
        return Buffer.concat([cipher.update(buf), cipher.final()]);
      },
      rsaEncrypt: () => { throw new Error('rsaEncrypt 未实现'); },
    },
    zlib: {
      inflate: (buf) => require('zlib').inflateSync(buf),
      deflate: (buf) => require('zlib').deflateSync(buf),
    },
  },
  // 音源请求结果的事件回调名（LX 里是 request 事件）
  apiVersion: '2.0.0',
};

// ---------------- 加载音源 ----------------
console.log('[lx] 加载音源:', PLUGIN_PATH);
const code = fs.readFileSync(PLUGIN_PATH, 'utf8');

let loaded = false;
let loadError = '';

// 音源常见写法：一个 IIFE，内部用 lx.on(EVENT_NAMES.request, ...)
try {
  const vm = require('vm');
  const sandbox = {
    lx,
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    Buffer, URL, URLSearchParams,
    fetch: myFetch,
    require,
    process: { env: {} },
    global: {},
  };
  sandbox.globalThis = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: PLUGIN_PATH, timeout: 10000 });
  loaded = true;
  console.log('[lx] 音源脚本执行完成');
} catch (e) {
  loadError = e && e.message || String(e);
  console.error('[lx] 音源加载失败:', loadError);
}

// 触发 inited（有些音源需要）
try {
  send(EVENT_NAMES.inited, {
    status: true,
    openDevTools: false,
    sources: { kw: {}, tx: {}, wy: {}, kg: {}, mg: {}, local: {} },
  });
} catch (e) {
  console.error('[lx] inited 失败:', e && e.message);
}

// ---------------- 请求封装 ----------------
// 音源通过 on('request', handler) 注册，handler 收到 {source, action, info}，
// 返回 {source, action, data}
function requestSource(action, info) {
  return new Promise((resolve, reject) => {
    const hs = handlers[EVENT_NAMES.request] || [];
    if (!hs.length) {
      reject(new Error('音源没有注册 request handler'));
      return;
    }
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) { settled = true; reject(new Error('请求超时')); }
    }, 25000);

    for (const h of hs) {
      try {
        const ret = h({ source: 'kw', action, info });
        if (ret && typeof ret.then === 'function') {
          ret.then(r => {
            if (!settled && r) {
              settled = true;
              clearTimeout(timer);
              resolve(r);
            }
          }).catch(e => {
            if (!settled) {
              settled = true;
              clearTimeout(timer);
              reject(e);
            }
          });
        }
      } catch (e) {
        // 单个 handler 失败继续尝试下一个
      }
    }
    // 若所有 handler 都同步返回，可能永远等不到；给个兜底
    setTimeout(() => {
      if (!settled) { settled = true; clearTimeout(timer); reject(new Error('无响应')); }
    }, 25000);
  });
}

// ---------------- HTTP 服务 ----------------
const server = http.createServer(async (req, res) => {
  const u = url.parse(req.url, true);
  const q = u.query;
  const sendJson = (obj, code = 200) => {
    const body = JSON.stringify(obj);
    res.writeHead(code, {
      'Content-Type': 'application/json; charset=utf-8',
      'Content-Length': Buffer.byteLength(body),
    });
    res.end(body);
  };

  try {
    if (u.pathname === '/health') {
      return sendJson({
        ok: true,
        loaded,
        loadError,
        handlers: Object.keys(handlers).map(k => `${k}:${handlers[k].length}`),
      });
    }

    if (u.pathname === '/search') {
      const keyword = q.q || '';
      if (!keyword) return sendJson({ ok: false, error: 'missing q' }, 400);
      const r = await requestSource('search', {
        keyword, page: 1, limit: 10,
      });
      return sendJson({ ok: true, data: r && r.data });
    }

    if (u.pathname === '/lyric') {
      const songmid = q.songmid || '';
      if (!songmid) return sendJson({ ok: false, error: 'missing songmid' }, 400);
      const r = await requestSource('lyric', { songmid });
      return sendJson({ ok: true, data: r && r.data });
    }

    if (u.pathname === '/url') {
      const songmid = q.songmid || '';
      if (!songmid) return sendJson({ ok: false, error: 'missing songmid' }, 400);
      const r = await requestSource('musicUrl', { songmid, type: q.type || '128k' });
      return sendJson({ ok: true, data: r && r.data });
    }

    sendJson({ ok: false, error: 'not found' }, 404);
  } catch (e) {
    sendJson({ ok: false, error: e && e.message || String(e) }, 500);
  }
});

// 音源内部可能有未捕获的 Promise rejection（如版本检查失败），
// 不能让它杀掉整个服务
process.on('unhandledRejection', (e) => {
  console.warn('[lx] 未处理的 rejection（已忽略）:', e && e.message || e);
});
process.on('uncaughtException', (e) => {
  console.warn('[lx] 未捕获异常（已忽略）:', e && e.message || e);
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[lx] 监听 http://127.0.0.1:${PORT}`);
  console.log('[lx] 已注册 handler:', Object.keys(handlers).join(', ') || '(无)');
});
