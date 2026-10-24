#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
班级随机点名 · 后台 API（纯标准库，Python 3.8+）

路由：
    GET  /api/health                     健康检查
    GET  /api/admin                      管理页（未登录显示登录框）
    POST /api/admin/login                token -> 会话 Cookie
    POST /api/admin/logout               退出
    GET  /api/admin/state                读取状态（需登录）
    POST /api/admin/save                 保存概率（需登录，X-RC 头防 CSRF）
    POST /api/rollcall/sync?k=..&n=..    客户端同步（共享 key）

数据文件：
    data/state.json   {"class","names","weights","updated","last_sync","sync_count"}
    secret.json       {"token_sha256","sync_key","cookie_secret"}   600 权限
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
LOG_DIR = os.path.join(BASE, "logs")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
NAMES_FILE = os.path.join(DATA_DIR, "names.json")
SECRET_FILE = os.path.join(BASE, "secret.json")
LOG_FILE = os.path.join(LOG_DIR, "api.log")

PORT = 8120
SESSION_TTL = 12 * 3600
MAX_WEIGHT = 100.0
MAX_STUDENTS = 128

_lock = threading.Lock()
_login_fail = {}          # ip -> [count, first_ts]
_login_fail_lock = threading.Lock()

DEFAULT_STATE = {
    "class": "高一2618班",
    "names": [],
    "weights": [],
    "updated": 0,
    "last_sync": 0,
    "sync_count": 0,
}


# ---------------------------------------------------------------- 文件工具

def ensure_dirs():
    for d in (DATA_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def state_read():
    st = load_json(STATE_FILE, None)
    if not isinstance(st, dict):
        st = dict(DEFAULT_STATE)
    for k, v in DEFAULT_STATE.items():
        st.setdefault(k, v)
    # 权重数量必须与名单一致
    n = len(st.get("names") or [])
    w = st.get("weights") or []
    if len(w) < n:
        w = list(w) + [1.0] * (n - len(w))
    st["weights"] = w[:n] if n else w
    return st


def state_write(st):
    save_json(STATE_FILE, st)


def log_line(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


# ---------------------------------------------------------------- 鉴权

def load_secret():
    s = load_json(SECRET_FILE, None)
    if not isinstance(s, dict) or not s.get("token_sha256"):
        raise SystemExit("secret.json missing or invalid")
    return s


SECRET = load_secret()
COOKIE_SECRET = SECRET.get("cookie_secret", "").encode()
SYNC_KEY = SECRET.get("sync_key", "")
TOKEN_SHA = SECRET.get("token_sha256", "")


def make_session():
    exp = int(time.time()) + SESSION_TTL
    sig = hmac.new(COOKIE_SECRET, str(exp).encode(), hashlib.sha256).hexdigest()
    return "%d.%s" % (exp, sig)


def check_session(value):
    if not value or "." not in value:
        return False
    exp_s, sig = value.rsplit(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    want = hmac.new(COOKIE_SECRET, exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


def check_token(token):
    got = hashlib.sha256(token.encode()).hexdigest()
    return hmac.compare_digest(got, TOKEN_SHA)


# ---------------------------------------------------------------- 页面

LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>点名后台 · 登录</title>
<style>
  :root{--bg:#fafaf8;--fg:#121315;--sub:#8c8c86;--line:#e6e4de;--dark:#181a1f}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:var(--bg);color:var(--fg);
       font:15px/1.6 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
  .card{width:340px;padding:34px 30px 28px;border:1px solid var(--line);border-radius:14px;background:#fff}
  h1{margin:0 0 4px;font-size:17px;font-weight:600}
  p{margin:0 0 22px;color:var(--sub);font-size:12.5px}
  input{width:100%;height:42px;padding:0 12px;border:1px solid var(--line);border-radius:9px;
        font:inherit;letter-spacing:.5px;outline:none}
  input:focus{border-color:#c9c6bd}
  button{width:100%;height:42px;margin-top:12px;border:0;border-radius:9px;background:var(--dark);
         color:#fff;font:inherit;cursor:pointer}
  button:hover{background:#000}
  .err{margin-top:12px;color:#c0392b;font-size:12.5px;min-height:18px}
</style></head><body>
<form class="card" method="post" action="/api/admin/login">
  <h1>点名后台</h1>
  <p>输入管理 Token 进入</p>
  <input name="token" type="password" autocomplete="current-password" autofocus placeholder="Token">
  <button type="submit">进入</button>
  <div class="err">__ERROR__</div>
</form></body></html>"""

PANEL_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>点名后台</title>
<style>
  :root{--bg:#fafaf8;--fg:#121315;--sub:#8c8c86;--line:#e6e4de;--dark:#181a1f;--green:#189452}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.55 "Microsoft YaHei","PingFang SC",system-ui,sans-serif;padding-bottom:78px}
  header{padding:22px 20px 14px;max-width:760px;margin:0 auto}
  h1{margin:0;font-size:16px;font-weight:600;display:flex;align-items:center;gap:8px}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--green)}
  .meta{margin-top:6px;color:var(--sub);font-size:12px}
  main{max-width:760px;margin:0 auto;padding:0 20px}
  .row{display:grid;grid-template-columns:46px 1fr 86px 74px 40px;gap:10px;align-items:center;
       padding:9px 4px;border-bottom:1px solid #f0eee9}
  .row.head{color:var(--sub);font-size:12px;border-bottom:1px solid var(--line);position:sticky;top:0;
       background:var(--bg);z-index:2}
  .idx{color:var(--sub);font-size:12.5px;font-variant-numeric:tabular-nums}
  .name{font-size:15px}
  input[type=number]{width:100%;height:34px;padding:0 9px;border:1px solid var(--line);border-radius:8px;
       font:inherit;text-align:right;font-variant-numeric:tabular-nums;background:#fff;outline:none}
  input[type=number]:focus{border-color:#c9c6bd}
  .pct{text-align:right;color:var(--sub);font-size:12.5px;font-variant-numeric:tabular-nums;padding-right:6px}
  footer{position:fixed;left:0;right:0;bottom:0;background:rgba(250,250,248,.94);
       border-top:1px solid var(--line);padding:12px 20px;display:flex;gap:10px;align-items:center;
       max-width:760px;margin:0 auto;backdrop-filter:blur(6px)}
  button{height:40px;padding:0 20px;border:0;border-radius:9px;background:var(--dark);color:#fff;
       font:inherit;cursor:pointer}
  button.ghost{background:transparent;color:var(--sub);border:1px solid var(--line)}
  button:hover{opacity:.9}
  .spacer{flex:1}
  .toast{position:fixed;left:50%;bottom:86px;transform:translateX(-50%);background:var(--dark);color:#fff;
       padding:9px 16px;border-radius:9px;font-size:13px;opacity:0;transition:opacity .2s;pointer-events:none}
  .toast.show{opacity:1}
  .empty{padding:36px 0;color:var(--sub);text-align:center;font-size:13px}
  .addbar{display:flex;gap:8px;margin:4px 0 16px}
  .addbar input{flex:1;height:38px;padding:0 12px;border:1px solid var(--line);border-radius:9px;
       font:inherit;background:#fff;outline:none}
  .addbar input:focus{border-color:#c9c6bd}
  .addbar button{height:38px;padding:0 16px}
  .del{height:26px;padding:0 8px;background:transparent;color:var(--sub);border:1px solid var(--line);
       border-radius:7px;font-size:13px;line-height:1}
  .del:hover{color:#c0392b;border-color:#e5b8b2}
</style></head><body>
<header>
  <h1><span class="dot"></span>__CLASS__</h1>
  <div class="meta">名单 __COUNT__ 人 · 上次同步 __LAST__</div>
  <div class="meta">权重 0 = 本轮不抽，数值越大越容易被抽中；改完点「保存修改」</div>
</header>
<main>
  <div class="addbar">
    <input id="newname" maxlength="12" placeholder="输入姓名，回车或点添加">
    <button id="addbtn">添加</button>
  </div>
  <div class="row head"><div>序号</div><div>姓名</div><div style="text-align:right">概率权重</div><div>占比</div><div></div></div>
  <div id="list"></div>
</main>
<footer>
  <button id="save">保存修改</button>
  <button class="ghost" id="reset">全部恢复 1.0</button>
  <div class="spacer"></div>
  <button class="ghost" id="logout">退出</button>
</footer>
<div class="toast" id="toast"></div>

<script>
const NAMES = __NAMES__;
const WEIGHTS = __WEIGHTS__;
const list = document.getElementById('list');
const toast = document.getElementById('toast');

function showToast(t){ toast.textContent=t; toast.classList.add('show');
  clearTimeout(showToast._t); showToast._t=setTimeout(()=>toast.classList.remove('show'),1600); }

function pct(){ 
  const ws=[...list.querySelectorAll('input')].map(i=>Math.max(0,parseFloat(i.value)||0));
  const total=ws.reduce((a,b)=>a+b,0);
  [...list.querySelectorAll('.pct')].forEach((el,i)=>{ el.textContent = total>0 ? (ws[i]/total*100).toFixed(1)+'%' : '—'; });
}

function row(i,name,w){
  const d=document.createElement('div'); d.className='row';
  d.innerHTML = '<div class="idx">'+String(i+1).padStart(2,'0')+'</div>'+
                '<div class="name"></div>'+
                '<div><input type="number" min="0" max="100" step="0.1" value="'+w+'"></div>'+
                '<div class="pct"></div>'+
                '<div style="text-align:right"><button class="del" title="删除">×</button></div>';
  d.querySelector('.name').textContent = name;
  d.querySelector('input').addEventListener('input', pct);
  d.querySelector('.del').onclick = ()=>removeName(i);
  list.appendChild(d);
}

NAMES.forEach((n,i)=>row(i,n,WEIGHTS[i]));
pct();

async function postRoster(names){
  return fetch('/api/admin/roster',{method:'POST',headers:{'Content-Type':'application/json','X-RC':'1'},
    body:JSON.stringify({names})});
}

async function removeName(i){
  if(NAMES.length<=1){ showToast('至少要留一个人'); return; }
  if(!confirm('从名单里删除「'+NAMES[i]+'」？')) return;
  const names=NAMES.slice(); names.splice(i,1);
  const r=await postRoster(names);
  if(r.ok){ location.reload(); } else { showToast('删除失败：'+r.status); }
}

async function addName(){
  const el=document.getElementById('newname');
  const v=(el.value||'').trim();
  if(!v){ showToast('请输入姓名'); return; }
  if(v.indexOf('|')>=0){ showToast('姓名里不能有 | 符号'); return; }
  if(NAMES.indexOf(v)>=0){ showToast('名单里已经有这个人'); return; }
  const r=await postRoster(NAMES.concat([v]));
  if(r.ok){ location.reload(); } else { showToast('添加失败：'+r.status); }
}

document.getElementById('addbtn').onclick=addName;
document.getElementById('newname').addEventListener('keydown',e=>{ if(e.key==='Enter') addName(); });

document.getElementById('save').onclick=async ()=>{
  const weights=[...list.querySelectorAll('input')].map(i=>{
     let v=parseFloat(i.value); if(!isFinite(v)||v<0) v=0; if(v>100) v=100; return v; });
  const r=await fetch('/api/admin/save',{method:'POST',headers:{'Content-Type':'application/json','X-RC':'1'},
     body:JSON.stringify({weights})});
  if(r.ok){ showToast('已保存，客户端下次启动生效'); }
  else { showToast('保存失败：'+r.status); }
};

document.getElementById('reset').onclick=()=>{
  list.querySelectorAll('input').forEach(i=>i.value='1');
  pct(); showToast('已全部设为 1.0，记得保存');
};

document.getElementById('logout').onclick=async ()=>{
  await fetch('/api/admin/logout',{method:'POST',headers:{'X-RC':'1'}});
  location.reload();
};
</script></body></html>"""


def admin_html(st):
    names = st.get("names") or []
    weights = st.get("weights") or []
    last = st.get("last_sync") or 0
    last_s = time.strftime("%m-%d %H:%M", time.localtime(last)) if last else "从未"
    html = PANEL_HTML
    html = html.replace("__CLASS__", escape_html(st.get("class") or "班级"))
    html = html.replace("__COUNT__", str(len(names)))
    html = html.replace("__LAST__", last_s)
    html = html.replace("__NAMES__", json.dumps(names, ensure_ascii=False).replace("</", "<\\/"))
    html = html.replace("__WEIGHTS__", json.dumps([round(float(x), 2) for x in weights]))
    return html


def escape_html(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "RollCall/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------
    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", extra)

    def _redirect(self, to, extra=None):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()

    def _cookies(self):
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _authed(self):
        return check_session(self._cookies().get("rc_sess", ""))

    def _body(self, limit=65536):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if n <= 0 or n > limit:
            return b""
        return self.rfile.read(n)

    def _client_ip(self):
        fwd = self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For") or ""
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    def log_message(self, fmt, *args):
        log_line("%s %s" % (self._client_ip(), fmt % args))

    # -- routing ---------------------------------------------------------
    def _force_https(self, path):
        """管理页只走 HTTPS：Cookie 带 Secure，走 http 会看起来像没登录。"""
        if path.startswith("/api/admin") and self.headers.get("X-Forwarded-Proto") == "http":
            host = self.headers.get("Host", "example.com").split(":")[0]
            self._redirect("https://%s%s" % (host, path))
            return True
        return False

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)

        if self._force_https(path):
            return

        if path == "/api/health":
            return self._send(200, "ok")

        if path == "/api/rollcall/version":
            return self.version()

        if path in ("/api/admin", "/api/admin/"):
            if not self._authed():
                return self._send(200, LOGIN_HTML.replace("__ERROR__", ""), "text/html; charset=utf-8")
            try:
                st = state_read()
            except Exception:
                st = dict(DEFAULT_STATE)
            return self._send(200, admin_html(st), "text/html; charset=utf-8")

        if path == "/api/admin/state":
            if not self._authed():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            return self._json(200, state_read())

        return self._send(404, "not found")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)

        if self._force_https(path):
            return

        if path == "/api/rollcall/sync":
            return self.sync(q)

        if path == "/api/rollcall/version":
            return self.version()

        if path == "/api/admin/login":
            return self.login()

        if path == "/api/admin/logout":
            return self._redirect("/api/admin", [("Set-Cookie", "rc_sess=; Max-Age=0; Path=/api")])

        if path == "/api/admin/save":
            return self.save()

        if path == "/api/admin/roster":
            return self.roster()

        return self._send(404, "not found")

    # -- endpoints -------------------------------------------------------
    def login(self):
        ip = self._client_ip()
        now = time.time()
        with _login_fail_lock:
            rec = _login_fail.get(ip)
            if rec and now - rec[1] < 600 and rec[0] >= 5:
                return self._send(429, LOGIN_HTML.replace("__ERROR__", "尝试次数过多，请 10 分钟后再试"),
                                  "text/html; charset=utf-8")
            if rec and now - rec[1] >= 600:
                _login_fail.pop(ip, None)

        body = self._body(4096).decode("utf-8", "replace")
        form = urllib.parse.parse_qs(body)
        token = (form.get("token") or [""])[0].strip()

        if not token or not check_token(token):
            with _login_fail_lock:
                rec = _login_fail.setdefault(ip, [0, now])
                rec[0] += 1
            return self._send(200, LOGIN_HTML.replace("__ERROR__", "Token 不正确"),
                              "text/html; charset=utf-8")

        with _login_fail_lock:
            _login_fail.pop(ip, None)
        cookie = "rc_sess=%s; Path=/api; Max-Age=%d; HttpOnly; Secure; SameSite=Lax" % (
            make_session(), SESSION_TTL)
        return self._redirect("/api/admin", [("Set-Cookie", cookie)])

    def save(self):
        if not self._authed():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if self.headers.get("X-RC") != "1":
            return self._json(403, {"ok": False, "error": "csrf"})
        try:
            payload = json.loads(self._body().decode("utf-8"))
            weights = payload["weights"]
        except (ValueError, KeyError):
            return self._json(400, {"ok": False, "error": "bad json"})

        with _lock:
            st = state_read()
            n = len(st.get("names") or [])
            if not isinstance(weights, list) or len(weights) != n:
                return self._json(400, {"ok": False, "error": "weights length mismatch",
                                        "expect": n, "got": len(weights) if isinstance(weights, list) else -1})
            clean = []
            for w in weights:
                try:
                    v = float(w)
                except (TypeError, ValueError):
                    v = 1.0
                if v < 0:
                    v = 0.0
                if v > MAX_WEIGHT:
                    v = MAX_WEIGHT
                clean.append(round(v, 2))
            st["weights"] = clean
            st["updated"] = int(time.time())
            state_write(st)
        log_line("weights saved: %s" % (clean[:5],))
        return self._json(200, {"ok": True, "updated": st["updated"]})

    def version(self):
        """热更新清单：data/version.txt 原样返回（无缓存头）。"""
        p = os.path.join(DATA_DIR, "version.txt")
        try:
            with open(p, "r", encoding="utf-8") as f:
                body = f.read()
        except OSError:
            return self._send(404, "no version")
        return self._send(200, body, "text/plain; charset=utf-8")

    def sync(self, q):
        key = (q.get("k") or [""])[0]
        if not SYNC_KEY or not hmac.compare_digest(key, SYNC_KEY):
            return self._send(403, "forbidden")
        try:
            n = int((q.get("n") or ["0"])[0])
        except ValueError:
            n = 0

        with _lock:
            st = state_read()
            names = st.get("names") or []
            weights = st.get("weights") or []
            # 以服务器名单为准；只有当服务器没有名单时，才按客户端人数补齐
            if not names and n and len(weights) != n:
                if len(weights) < n:
                    weights = list(weights) + [1.0] * (n - len(weights))
                else:
                    weights = weights[:n]
                st["weights"] = weights
            elif names:
                st["weights"] = weights[:len(names)]
            st["last_sync"] = int(time.time())
            st["sync_count"] = int(st.get("sync_count") or 0) + 1
            state_write(st)

        body = "RC1\nv=%d\nw=%s\n" % (
            int(time.time()),
            ",".join("%.4g" % float(w) for w in (st.get("weights") or [])))
        if st.get("names"):
            body += "n=%s\n" % "|".join(st["names"])
        return self._send(200, body, "text/plain; charset=utf-8")

    def roster(self):
        """整份替换名单：新增 / 删除都走这里；同名的人保留原权重。"""
        if not self._authed():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if self.headers.get("X-RC") != "1":
            return self._json(403, {"ok": False, "error": "csrf"})
        try:
            payload = json.loads(self._body().decode("utf-8"))
            names = payload["names"]
        except (ValueError, KeyError):
            return self._json(400, {"ok": False, "error": "bad json"})

        if not isinstance(names, list) or not names:
            return self._json(400, {"ok": False, "error": "empty roster"})
        if len(names) > MAX_STUDENTS:
            return self._json(400, {"ok": False, "error": "too many students"})

        clean = []
        for nm in names:
            if not isinstance(nm, str):
                return self._json(400, {"ok": False, "error": "bad name"})
            nm = nm.strip()
            if not nm or len(nm) > 12 or "|" in nm:
                return self._json(400, {"ok": False, "error": "bad name"})
            clean.append(nm)

        with _lock:
            st = state_read()
            old_names = st.get("names") or []
            old_w = st.get("weights") or []
            by_name = {n: w for n, w in zip(old_names, old_w)}
            st["names"] = clean
            st["weights"] = [float(by_name.get(n, 1.0)) for n in clean]
            st["updated"] = int(time.time())
            state_write(st)

        log_line("roster updated: %d students" % len(clean))
        return self._json(200, {"ok": True, "count": len(clean)})


def apply_roster():
    """把部署时生成的 data/names.json 作为**种子**合进状态。
    names.json 是 {"class": ..., "names": [...], "force": bool}（也兼容旧的纯数组）。
    只有状态里还没有名单时才写入（首次部署）；之后名单以后台增删为准，
    force=true 时才强制覆盖（部署脚本 -ForceRoster 用）。"""
    raw = load_json(NAMES_FILE, None)
    cls = None
    force = False
    names = raw
    if isinstance(raw, dict):
        names = raw.get("names")
        cls = raw.get("class")
        force = bool(raw.get("force"))
    if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
        return

    st = state_read()
    if st.get("names") and not force:
        if cls:
            st["class"] = cls
            state_write(st)
        return

    old_names = st.get("names") or []
    old_weights = st.get("weights") or []
    by_name = {}
    for i, nm in enumerate(old_names):
        if i < len(old_weights):
            by_name[nm] = old_weights[i]

    st["names"] = names
    st["weights"] = [float(by_name.get(nm, 1.0)) for nm in names]
    if cls:
        st["class"] = cls
    state_write(st)


def main():
    ensure_dirs()
    if os.path.exists(NAMES_FILE):
        apply_roster()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    httpd.daemon_threads = True
    log_line("listening on 127.0.0.1:%d" % PORT)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
