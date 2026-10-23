/**
 * test_sources.js — 批量测试 LX Music 音源可用性
 *
 * 用法：node test_sources.js <音源目录> [关键词]
 *
 * 对每个 .js 音源：
 *   1. 加载（模拟 lx 环境）
 *   2. 检查注册的 handler
 *   3. 尝试搜索关键词
 *   4. 尝试取歌词 + 音频 URL
 * 输出可用性报告
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const DIR = process.argv[2] || '';
const KEYWORD = process.argv[3] || '勇气';

if (!DIR || !fs.existsSync(DIR)) {
  console.error('用法: node test_sources.js <音源目录> [关键词]');
  process.exit(1);
}

const PROXY = process.env.LX_PROXY || process.env.HTTPS_PROXY || '';
let dispatcher = null;
if (PROXY) {
  try {
    dispatcher = new (require('undici').ProxyAgent)(PROXY);
  } catch (e) {}
}

async function myFetch(u, opt = {}) {
  if (dispatcher) opt.dispatcher = dispatcher;
  return fetch(u, opt);
}

function collectJsFiles(dir) {
  const out = [];
  function walk(d) {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.name.toLowerCase().endsWith('.js')) out.push(p);
    }
  }
  walk(dir);
  return out;
}

function makeLxEnv() {
  const handlers = {};
  const lx = {
    EVENT_NAMES: {
      request: 'request', inited: 'inited', updateAlert: 'updateAlert',
      error: 'error', log: 'log', apiAction: 'apiAction',
    },
    version: '2.0.0',
    env: 'desktop',
    currentScriptInfo: {},
    on: (n, h) => { (handlers[n] = handlers[n] || []).push(h); },
    off: () => {},
    send: (n, d) => {
      (handlers[n] || []).forEach(h => {
        try { h(d); } catch (e) {}
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
          signal: AbortSignal.timeout(o.timeout || 15000),
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
  return { lx, handlers };
}

function callHandler(handlers, action, info, timeoutMs = 20000) {
  return new Promise((resolve) => {
    const hs = handlers['request'] || [];
    if (!hs.length) return resolve({ err: '无 request handler' });
    let done = false;
    const timer = setTimeout(() => {
      if (!done) { done = true; resolve({ err: '超时' }); }
    }, timeoutMs);
    for (const h of hs) {
      try {
        const ret = h({ source: 'kw', action, info });
        if (ret && typeof ret.then === 'function') {
          ret.then(r => {
            if (!done && r) {
              done = true; clearTimeout(timer); resolve({ data: r.data });
            }
          }).catch(e => {
            if (!done) { done = true; clearTimeout(timer); resolve({ err: e && e.message }); }
          });
        }
      } catch (e) {}
    }
    setTimeout(() => {
      if (!done) { done = true; clearTimeout(timer); resolve({ err: '无响应' }); }
    }, timeoutMs);
  });
}

async function testOne(file) {
  const name = path.basename(file);
  const r = { name, ok: false, handlers: [], search: '', lyric: '', url: '' };
  const { lx, handlers } = makeLxEnv();
  const code = fs.readFileSync(file, 'utf8');
  const sandbox = {
    lx, console: { log() {}, warn() {}, error() {}, info() {} },
    setTimeout, clearTimeout, setInterval, clearInterval,
    Buffer, URL, URLSearchParams, fetch: myFetch, require,
    process: { env: {} }, global: {},
  };
  sandbox.globalThis = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);

  try {
    vm.runInContext(code, sandbox, { filename: file, timeout: 8000 });
  } catch (e) {
    r.search = '加载异常: ' + (e && e.message || e);
    return r;
  }
  r.handlers = Object.keys(handlers);
  if (!handlers['request']) {
    r.search = '无 request handler';
    return r;
  }

  // 触发 inited
  try {
    (handlers['inited'] || []).forEach(h => {
      try { h({ status: true, sources: { kw:{},tx:{},wy:{},kg:{},mg:{} } }); } catch (e) {}
    });
  } catch (e) {}

  // 1. 搜索
  const s = await callHandler(handlers, 'search', { keyword: KEYWORD, page: 1, limit: 5 });
  if (s.err) {
    r.search = '搜索失败: ' + s.err;
    return r;
  }
  const list = s.data && (s.data.list || s.data);
  if (!Array.isArray(list) || !list.length) {
    r.search = '搜索无结果';
    return r;
  }
  const first = list[0];
  const mid = first.songmid || first.songMid || first.id || first.hash;
  r.search = `OK (${list.length} 首, 首条=${first.name || '?'}, mid=${mid || '?'})`;
  if (!mid) return r;

  // 2. 歌词
  const l = await callHandler(handlers, 'lyric', { songmid: mid });
  if (l.err) {
    r.lyric = '失败: ' + l.err;
  } else {
    const lyr = l.data && (l.data.lyric || (typeof l.data === 'string' ? l.data : ''));
    r.lyric = lyr ? `OK (${lyr.split('\n').length} 行)` : '空';
  }

  // 3. 音频 URL
  const u = await callHandler(handlers, 'musicUrl', { songmid: mid, type: '128k' });
  if (u.err) {
    r.url = '失败: ' + u.err;
  } else {
    const link = u.data && (u.data.url || (typeof u.data === 'string' ? u.data : ''));
    r.url = link ? `OK (${String(link).slice(0, 60)}...)` : '空';
  }

  r.ok = r.search.startsWith('OK') && (r.lyric.startsWith('OK') || r.url.startsWith('OK'));
  return r;
}

(async () => {
  const files = collectJsFiles(DIR);
  console.log(`发现 ${files.length} 个音源，测试关键词: ${KEYWORD}\n`);
  const results = [];
  for (const f of files) {
    process.stdout.write(`测试 ${path.basename(f)} ... `);
    let r;
    try {
      r = await testOne(f);
    } catch (e) {
      r = { name: path.basename(f), ok: false, search: '异常: ' + (e && e.message) };
    }
    results.push(r);
    console.log(r.ok ? '✓' : '✗');
  }

  console.log('\n========== 结果汇总 ==========\n');
  const good = results.filter(r => r.ok);
  console.log(`可用音源: ${good.length} / ${results.length}\n`);
  for (const r of results) {
    console.log(`${r.ok ? '✓' : '✗'} ${r.name}`);
    if (r.search) console.log(`    搜索: ${r.search}`);
    if (r.lyric) console.log(`    歌词: ${r.lyric}`);
    if (r.url) console.log(`    音频: ${r.url}`);
    if (r.handlers.length) console.log(`    handler: ${r.handlers.join(', ')}`);
  }
  process.exit(0);
})();
