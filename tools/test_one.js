/**
 * test_one.js — 在独立进程里测试单个音源（崩溃不影响主进程）
 *
 * 用法：node test_one.js <音源.js> [关键词]
 * 输出：JSON 结果到 stdout
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const FILE = process.argv[2] || '';
const KEYWORD = process.argv[3] || '勇气';

if (!FILE || !fs.existsSync(FILE)) {
  console.log(JSON.stringify({ ok: false, err: '文件不存在' }));
  process.exit(0);
}

const PROXY = process.env.LX_PROXY || '';
let dispatcher = null;
if (PROXY) {
  try { dispatcher = new (require('undici').ProxyAgent)(PROXY); } catch (e) {}
}

async function myFetch(u, opt = {}) {
  if (dispatcher) opt.dispatcher = dispatcher;
  return fetch(u, opt);
}

// 屏蔽音源内部日志噪音
const silentConsole = {
  log() {}, warn() {}, error() {}, info() {}, debug() {},
};

const handlers = {};
const lx = {
  EVENT_NAMES: {
    request: 'request', inited: 'inited', updateAlert: 'updateAlert',
    error: 'error', log: 'log', apiAction: 'apiAction',
  },
  version: '2.0.0',
  env: 'desktop',
  currentScriptInfo: { name: 'test', version: '1' },
  on: (n, h) => { (handlers[n] = handlers[n] || []).push(h); },
  off: () => {},
  send: (n, d) => {
    (handlers[n] || []).forEach(h => {
      try { const r = h(d); if (r && r.catch) r.catch(() => {}); } catch (e) {}
    });
  },
  request: async (u, o = {}, cb) => {
    try {
      const method = (o.method || 'GET').toUpperCase();
      const headers = o.headers || {};
      let body = o.body;
      if (body && typeof body === 'object' && !Buffer.isBuffer(body)) {
        body = new URLSearchParams(body).toString();
        if (!headers['Content-Type']) {
          headers['Content-Type'] = 'application/x-www-form-urlencoded';
        }
      }
      const r = await myFetch(u, {
        method, headers, body: method === 'GET' ? undefined : body,
        signal: AbortSignal.timeout(o.timeout || 12000),
      });
      const t = await r.text();
      let d = t;
      try { d = JSON.parse(t); } catch (_) {}
      const out = { statusCode: r.status, headers: Object.fromEntries(r.headers), body: d };
      if (cb) cb(null, out);
      return out;
    } catch (e) {
      if (cb) cb(e, null);
      throw e;
    }
  },
  utils: {
    buffer: { from: (...a) => Buffer.from(...a),
              bufToString: (b, f) => Buffer.from(b).toString(f || 'utf8') },
    crypto: {
      md5: s => require('crypto').createHash('md5').update(s).digest('hex'),
      randomBytes: n => require('crypto').randomBytes(n),
      aesEncrypt: (buf, mode, key, iv) => {
        const c = require('crypto');
        const ci = c.createCipheriv(mode, key, iv);
        return Buffer.concat([ci.update(buf), ci.final()]);
      },
    },
    zlib: { inflate: b => require('zlib').inflateSync(b),
            deflate: b => require('zlib').deflateSync(b) },
  },
};

// 未捕获异常全部吞掉（音源内部问题不应杀进程）
process.on('unhandledRejection', () => {});
process.on('uncaughtException', () => {});

const code = fs.readFileSync(FILE, 'utf8');
const sandbox = {
  lx, console: silentConsole,
  setTimeout, clearTimeout, setInterval, clearInterval,
  Buffer, URL, URLSearchParams, fetch: myFetch, require,
  process: { env: {} }, global: {},
};
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
vm.createContext(sandbox);

try {
  vm.runInContext(code, sandbox, { filename: FILE, timeout: 8000 });
} catch (e) {
  console.log(JSON.stringify({ ok: false, err: '加载异常: ' + (e && e.message) }));
  process.exit(0);
}

function callHandler(action, info, timeoutMs = 15000) {
  return new Promise((resolve) => {
    const hs = handlers['request'] || [];
    if (!hs.length) return resolve({ err: '无 request handler' });
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    const timer = setTimeout(() => finish({ err: '超时' }), timeoutMs);
    for (const h of hs) {
      try {
        const ret = h({ source: 'kw', action, info });
        if (ret && typeof ret.then === 'function') {
          ret.then(r => { if (r) { clearTimeout(timer); finish({ data: r.data }); } })
             .catch(e => { clearTimeout(timer); finish({ err: e && e.message }); });
        }
      } catch (e) {}
    }
    setTimeout(() => { clearTimeout(timer); finish({ err: '无响应' }); }, timeoutMs);
  });
}

(async () => {
  const result = { ok: false, name: path.basename(FILE), handlers: Object.keys(handlers) };

  // 触发 inited
  try {
    (handlers['inited'] || []).forEach(h => {
      try { h({ status: true, sources: { kw:{},tx:{},wy:{},kg:{},mg:{} } }); } catch (e) {}
    });
  } catch (e) {}

  if (!handlers['request']) {
    result.err = '无 request handler（handler: ' + Object.keys(handlers).join(',') + '）';
    console.log(JSON.stringify(result));
    return;
  }

  // 搜索
  const s = await callHandler('search', { keyword: KEYWORD, page: 1, limit: 5 });
  if (s.err) {
    result.err = '搜索: ' + s.err;
    console.log(JSON.stringify(result));
    return;
  }
  const list = s.data && (s.data.list || s.data);
  if (!Array.isArray(list) || !list.length) {
    result.err = '搜索无结果';
    console.log(JSON.stringify(result));
    return;
  }
  const first = list[0];
  const mid = first.songmid || first.songMid || first.id || first.hash || first.copyrightId;
  result.search = { count: list.length, first: first.name || '?', mid: mid || '' };
  if (!mid) {
    result.err = '拿不到 songmid';
    console.log(JSON.stringify(result));
    return;
  }

  // 歌词
  const l = await callHandler('lyric', { songmid: mid });
  if (!l.err) {
    const lyr = l.data && (l.data.lyric || (typeof l.data === 'string' ? l.data : ''));
    result.lyric = lyr ? { lines: lyr.split('\n').length, preview: lyr.split('\n').slice(0, 2) } : { empty: true };
  } else {
    result.lyric = { err: l.err };
  }

  // 音频
  const u = await callHandler('musicUrl', { songmid: mid, type: '128k' });
  if (!u.err) {
    const link = u.data && (u.data.url || (typeof u.data === 'string' ? u.data : ''));
    result.url = link ? { ok: true, preview: String(link).slice(0, 80) } : { empty: true };
  } else {
    result.url = { err: u.err };
  }

  result.ok = !!(result.lyric && result.lyric.lines) || !!(result.url && result.url.ok);
  console.log(JSON.stringify(result));
  process.exit(0);
})();
