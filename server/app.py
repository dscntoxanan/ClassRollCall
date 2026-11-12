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
CLASS_DIR = os.path.join(DATA_DIR, "classes")   # 一个班一个 JSON 文件
STATE_FILE = os.path.join(DATA_DIR, "state.json")  # 老版本单班级状态，只用于迁移
NAMES_FILE = os.path.join(DATA_DIR, "names.json")
SECRET_FILE = os.path.join(BASE, "secret.json")
LOG_FILE = os.path.join(LOG_DIR, "api.log")

PORT = 8120
SESSION_TTL = 12 * 3600
MAX_WEIGHT = 100.0
MAX_STUDENTS = 128
MAX_STREAMS = 16          # 同时允许的 SSE 连接数
STREAM_MAX_SECONDS = 1800 # 单条长连接最长存活时间，到点让客户端重连
MAX_CLASSES = 60          # 最多开多少个班级
MASTER_ID = "@master"     # 管理员会话的虚拟班级 id

_lock = threading.Lock()
_login_fail = {}          # ip -> [count, first_ts]
_stream_lock = threading.Lock()
_stream_count = [0]       # 当前 SSE 长连接数
_login_fail_lock = threading.Lock()

DEFAULT_STATE = {
    "class": "高一2618班",
    "names": [],
    "weights": [],
    "updated": 0,
    "last_sync": 0,
    "sync_count": 0,
    "rev": 0,
    "round_seconds": 300,     # 防重复窗口多久清空一次（秒）
    "hist_draws": 3,          # 最近几次抽取不重复；0 = 关闭
    "max_pick": 5,            # 一次最多抽几人
    "default_count": 1,       # 新机器第一次打开时默认抽几人
    "weight_max": 100,        # 权重上限
    "next_name": "",          # 指定下一位（空 = 不指定）
    "next_id": 0,             # 指定编号：每设一次 +1，客户端据此判断"是新的一次"
}


def _default_state():
    """返回一份全新的状态字典（列表不能共用，否则各个班级会串）。"""
    return json.loads(json.dumps(DEFAULT_STATE))


# ---------------------------------------------------------------- 文件工具

def ensure_dirs():
    for d in (DATA_DIR, LOG_DIR, CLASS_DIR):
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


# ---------------------------------------------------------------- 状态规范化

def _state_normalize(st):
    for k, v in DEFAULT_STATE.items():
        st.setdefault(k, v)

    # 权重数量必须与名单一致
    n = len(st.get("names") or [])
    w = st.get("weights") or []
    if len(w) < n:
        w = list(w) + [1.0] * (n - len(w))
    w = w[:n] if n else w
    clean = []
    for x in w:
        try:
            clean.append(float(x))
        except (TypeError, ValueError):
            clean.append(1.0)
    st["weights"] = clean

    # 自定义项：夹到合理范围，坏值直接回默认
    try:
        rs = int(st.get("round_seconds", 300))
    except (TypeError, ValueError):
        rs = 300
    st["round_seconds"] = max(10, min(86400, rs))
    try:
        hd = int(st.get("hist_draws", 3))
    except (TypeError, ValueError):
        hd = 3
    st["hist_draws"] = max(0, min(20, hd))
    try:
        mp = int(st.get("max_pick", 5))
    except (TypeError, ValueError):
        mp = 5
    st["max_pick"] = max(1, min(20, mp))
    try:
        dc = int(st.get("default_count", 1))
    except (TypeError, ValueError):
        dc = 1
    st["default_count"] = max(1, min(20, dc))
    try:
        wm = int(st.get("weight_max", 100))
    except (TypeError, ValueError):
        wm = 100
    st["weight_max"] = max(1, min(1000, wm))
    if not isinstance(st.get("next_name"), str):
        st["next_name"] = ""
    try:
        st["next_id"] = int(st.get("next_id") or 0)
    except (TypeError, ValueError):
        st["next_id"] = 0
    return st


# ---------------------------------------------------------------- 班级（多租户）
#
# 一个班 = data/classes/<cid>.json：
#   {"id","name","token_sha256","sync_key","created","state":{...}}
# 后台用 token 认班级，客户端用 sync_key 认班级 —— 两边都只能碰到自己那一个。

def _class_path(cid):
    return os.path.join(CLASS_DIR, cid + ".json")


def _cid_ok(cid):
    if not cid or len(cid) > 32:
        return False
    return all((ch.isalnum() or ch in "-_") for ch in cid)


def class_read(cid):
    if not _cid_ok(cid):
        return None
    rec = load_json(_class_path(cid), None)
    if not isinstance(rec, dict):
        return None
    rec["id"] = cid
    st = rec.get("state")
    rec["state"] = _state_normalize(st if isinstance(st, dict) else _default_state())
    rec.setdefault("name", rec["state"].get("class") or cid)
    return rec


def class_write(rec):
    save_json(_class_path(rec["id"]), rec)


def class_all():
    out = []
    try:
        files = sorted(os.listdir(CLASS_DIR))
    except OSError:
        return out
    for fn in files:
        if not fn.endswith(".json"):
            continue
        rec = class_read(fn[:-5])
        if rec:
            out.append(rec)
    return out


def class_by_token(sha):
    if not sha:
        return None
    for rec in class_all():
        if hmac.compare_digest(str(rec.get("token_sha256") or ""), sha):
            return rec
    return None


def class_by_key(key):
    if not key:
        return None
    for rec in class_all():
        if hmac.compare_digest(str(rec.get("sync_key") or ""), key):
            return rec
    return None


def class_create(name, names=None):
    """新建班级，返回 (记录, 明文 token)。token 只在这里返回一次。"""
    if len(class_all()) >= MAX_CLASSES:
        return None, None
    cid = "c" + os.urandom(3).hex()
    while os.path.exists(_class_path(cid)):
        cid = "c" + os.urandom(3).hex()
    token = os.urandom(8).hex()
    rec = {
        "id": cid,
        "name": name,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "sync_key": os.urandom(16).hex(),
        "created": int(time.time()),
        "state": _default_state(),
    }
    rec["state"]["class"] = name
    if names:
        rec["state"]["names"] = list(names)[:MAX_STUDENTS]
        rec["state"]["weights"] = [1.0] * len(rec["state"]["names"])
    class_write(rec)
    return rec, token


def class_rotate_token(cid):
    """换一把新 token，旧的立刻失效；返回明文（只此一次）。"""
    rec = class_read(cid)
    if not rec:
        return None
    token = os.urandom(8).hex()
    rec["token_sha256"] = hashlib.sha256(token.encode()).hexdigest()
    class_write(rec)
    return token


def state_read(cid):
    rec = class_read(cid)
    if not rec:
        return _default_state()
    return rec["state"]


def state_write(cid, st):
    rec = class_read(cid)
    if not rec:
        rec = {"id": cid, "name": st.get("class") or cid, "token_sha256": "",
               "sync_key": os.urandom(16).hex(), "created": int(time.time())}
    rec["state"] = st
    class_write(rec)


def log_line(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


# ---------------------------------------------------------------- 鉴权

def load_secret():
    s = load_json(SECRET_FILE, None)
    if not isinstance(s, dict) or not (s.get("master_token_sha256") or s.get("token_sha256")):
        raise SystemExit("secret.json missing or invalid")
    return s


SECRET = load_secret()
COOKIE_SECRET = (SECRET.get("cookie_secret") or os.urandom(32).hex()).encode()
MASTER_SHA = SECRET.get("master_token_sha256") or ""


def make_session(cid):
    exp = int(time.time()) + SESSION_TTL
    msg = "%s.%d" % (cid, exp)
    sig = hmac.new(COOKIE_SECRET, msg.encode(), hashlib.sha256).hexdigest()
    return "%s.%s" % (msg, sig)


def check_session(value):
    """Cookie 有效则返回班级 id（管理员是 @master），否则 None。"""
    parts = (value or "").split(".")
    if len(parts) != 3:
        return None
    cid, exp_s, sig = parts
    try:
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < int(time.time()):
        return None
    want = hmac.new(COOKIE_SECRET, ("%s.%s" % (cid, exp_s)).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig):
        return None
    if cid == MASTER_ID:
        return cid
    return cid if _cid_ok(cid) and os.path.exists(_class_path(cid)) else None


def check_token(token):
    """token 有效则返回对应班级 id（管理员是 @master），否则 None。"""
    if not token:
        return None
    got = hashlib.sha256(token.encode()).hexdigest()
    if MASTER_SHA and hmac.compare_digest(got, MASTER_SHA):
        return MASTER_ID
    rec = class_by_token(got)
    return rec["id"] if rec else None


def migrate_legacy():
    """老版本只有一个 secret.json + state.json：把它变成第一个班级 c1。"""
    if class_all():
        return
    st = load_json(STATE_FILE, None)
    tok = SECRET.get("token_sha256") or ""
    key = SECRET.get("sync_key") or ""
    if not isinstance(st, dict) and not tok:
        return
    if not isinstance(st, dict):
        st = _default_state()
    rec = {
        "id": "c1",
        "name": st.get("class") or "班级 1",
        "token_sha256": tok,
        "sync_key": key or os.urandom(16).hex(),
        "created": int(time.time()),
        "state": _state_normalize(st),
    }
    class_write(rec)
    log_line("migrated legacy state.json -> c1")


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
  .cfg{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;padding:13px 4px;
       border-bottom:1px solid var(--line);color:var(--sub);font-size:13px}
  .cfg label{display:flex;align-items:center;gap:7px}
  .cfg input{width:66px;height:32px;text-align:center;border:1px solid var(--line);border-radius:8px;
       font:inherit;background:#fff;outline:none;font-variant-numeric:tabular-nums}
  .cfg input:focus{border-color:#c9c6bd}
  .cfg select{height:32px;padding:0 8px;border:1px solid var(--line);border-radius:8px;font:inherit;
       background:#fff;outline:none;max-width:180px}
  .cfg .meta{font-size:12px}
  .cfg b{color:var(--fg);font-weight:600}
  .del{height:26px;padding:0 8px;background:transparent;color:var(--sub);border:1px solid var(--line);
       border-radius:7px;font-size:13px;line-height:1}
  .del:hover{color:#c0392b;border-color:#e5b8b2}
</style></head><body>
<header>
  <h1><span class="dot"></span>__CLASS__</h1>
  <div class="meta">名单 __COUNT__ 人 · 上次同步 __LAST__ · 班级编号 __CID__</div>
  <div class="meta">权重 0 = 不参与常规抽取，数值越大越容易被抽中；改完点「保存修改」</div>
  <div class="meta">被抽到的人 30 分钟内权重自动 −0.2（最低 1.0，本来 ≤1 的不变）；客户端重启即恢复</div>
</header>
<main>
  <div class="cfg">
    <label>最近 <input id="cfg_draws" type="number" min="0" max="20" step="1" value="__HD__"> 次抽取不重复</label>
    <label>每 <input id="cfg_round" type="number" min="1" max="1440" step="1" value="__RSM__"> 分钟清空窗口</label>
  </div>
  <div class="cfg">
    <label>每次最多抽 <input id="cfg_mp" type="number" min="1" max="20" step="1" value="__MP__"> 人</label>
    <label>默认 <input id="cfg_dc" type="number" min="1" max="20" step="1" value="__DC__"> 人</label>
    <label>权重上限 <input id="cfg_wm" type="number" min="1" max="1000" step="1" value="__WM__"></label>
  </div>
  <div class="cfg">
    <label>班级名 <input id="cfg_class" type="text" maxlength="40" value="__CLASS_EDIT__" style="width:180px;text-align:left"></label>
  </div>
  <div class="cfg">
    <label>指定下一位
      <select id="cfg_next"><option value="">（不指定）</option></select>
    </label>
    <button class="ghost" id="nextset" style="height:32px;padding:0 14px">设为下一位</button>
    <button class="ghost" id="nextclear" style="height:32px;padding:0 14px">取消指定</button>
    <span class="meta" id="nextnow"></span>
  </div>
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
const NEXT_NAME = __NEXT__;
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
  const cfgD=document.getElementById('cfg_draws');
  const cfgR=document.getElementById('cfg_round');
  let hd=parseInt(cfgD.value,10); if(!isFinite(hd)) hd=3; hd=Math.max(0,Math.min(20,hd));
  let rm=parseInt(cfgR.value,10); if(!isFinite(rm)) rm=5; rm=Math.max(1,Math.min(1440,rm));
  const num=(id,dflt,lo,hi)=>{
    let v=parseInt((document.getElementById(id)||{}).value,10);
    if(!isFinite(v)) v=dflt; return Math.max(lo,Math.min(hi,v)); };
  const mp=num('cfg_mp',5,1,20), dc=num('cfg_dc',1,1,20), wm=num('cfg_wm',100,1,1000);
  const cls=(document.getElementById('cfg_class')||{}).value||'';
  const r=await fetch('/api/admin/save',{method:'POST',headers:{'Content-Type':'application/json','X-RC':'1'},
     body:JSON.stringify({weights, hist_draws:hd, round_seconds:rm*60,
                          max_pick:mp, default_count:dc, weight_max:wm, class:cls})});
  if(r.ok){ showToast('已保存，客户端立刻生效'); }
  else { showToast('保存失败：'+r.status); }
};

const selNext=document.getElementById('cfg_next');
NAMES.forEach(n=>{ const o=document.createElement('option'); o.value=n; o.textContent=n; selNext.appendChild(o); });
if(NEXT_NAME) selNext.value=NEXT_NAME;
document.getElementById('nextnow').textContent = NEXT_NAME ? ('当前：'+NEXT_NAME) : '';

async function setNext(name){
  const r=await fetch('/api/admin/next',{method:'POST',headers:{'Content-Type':'application/json','X-RC':'1'},
     body:JSON.stringify({name})});
  if(r.ok){ showToast(name?('下一位：'+name):'已取消指定'); setTimeout(()=>location.reload(),500); }
  else { showToast('设置失败：'+r.status); }
}
document.getElementById('nextset').onclick=()=>{
  const v=selNext.value; if(!v){ showToast('先选一个人'); return; } setNext(v); };
document.getElementById('nextclear').onclick=()=>setNext('');

document.getElementById('reset').onclick=()=>{
  list.querySelectorAll('input').forEach(i=>i.value='1');
  pct(); showToast('已全部设为 1.0，记得保存');
};

document.getElementById('logout').onclick=async ()=>{
  await fetch('/api/admin/logout',{method:'POST',headers:{'X-RC':'1'}});
  location.reload();
};
</script></body></html>"""


def admin_html(rec):
    st = rec.get("state") or {}
    names = st.get("names") or []
    weights = st.get("weights") or []
    last = st.get("last_sync") or 0
    last_s = time.strftime("%m-%d %H:%M", time.localtime(last)) if last else "从未"
    html = PANEL_HTML
    html = html.replace("__CID__", escape_html(rec.get("id") or ""))
    html = html.replace("__CLASS__", escape_html(st.get("class") or "班级"))
    html = html.replace("__COUNT__", str(len(names)))
    html = html.replace("__LAST__", last_s)
    html = html.replace("__NAMES__", json.dumps(names, ensure_ascii=False).replace("</", "<\\/"))
    html = html.replace("__WEIGHTS__", json.dumps([round(float(x), 2) for x in weights]))
    html = html.replace("__MP__", str(int(st.get("max_pick") or 5)))
    html = html.replace("__DC__", str(int(st.get("default_count") or 1)))
    html = html.replace("__WM__", str(int(st.get("weight_max") or 100)))
    html = html.replace("__CLASS_EDIT__", escape_html(st.get("class") or ""))
    html = html.replace("__NEXT__", json.dumps(st.get("next_name") or "", ensure_ascii=False).replace("</", "<\\/"))
    html = html.replace("__HD__", str(int(st.get("hist_draws") or 0)))
    html = html.replace("__RSM__", str(max(1, int(st.get("round_seconds") or 300) // 60)))
    return html


MASTER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>班级管理</title>
<style>
  :root{--bg:#fafaf8;--fg:#121315;--sub:#8c8c86;--line:#e6e4de;--dark:#181a1f}
  *{box-sizing:border-box}
  body{margin:0;padding:34px 22px 60px;background:var(--bg);color:var(--fg);
       font:14px/1.6 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
  .wrap{max-width:780px;margin:0 auto}
  h1{margin:0 0 3px;font-size:18px;font-weight:600}
  .meta{color:var(--sub);font-size:12.5px}
  .card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px 18px 10px;margin-top:18px}
  .row{display:flex;align-items:center;gap:9px;padding:11px 0;border-bottom:1px solid var(--line);flex-wrap:wrap}
  .row:last-child{border-bottom:0}
  .nm{font-weight:600}
  button{border:1px solid var(--line);background:#fff;border-radius:8px;padding:6px 11px;
         font:inherit;font-size:12.5px;color:var(--fg);cursor:pointer}
  button:hover{background:#f2f0ea}
  button.dark{background:var(--dark);color:#fff;border-color:var(--dark)}
  input{height:38px;padding:0 11px;border:1px solid var(--line);border-radius:9px;font:inherit;outline:none}
  input:focus{border-color:#c9c6bd}
  textarea{width:100%;min-height:88px;padding:10px;border:1px solid var(--line);border-radius:9px;
           font:13px/1.5 ui-monospace,Consolas,monospace;outline:none;resize:vertical}
  .grid{display:flex;gap:9px;margin:12px 0}
  .grid input{flex:1}
  .out{margin-top:14px;padding:12px 14px;border:1px dashed #c9c6bd;border-radius:10px;
       background:#fbfaf6;font:12.5px/1.9 ui-monospace,Consolas,monospace;display:none;word-break:break-all}
  .toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:var(--dark);color:#fff;
         padding:9px 18px;border-radius:9px;font-size:13px;opacity:0;transition:.2s;pointer-events:none}
  .toast.on{opacity:1}
</style></head><body><div class="wrap">
  <h1>班级管理</h1>
  <div class="meta">每个班级有独立的 token 和独立的数据，互相看不见。</div>
  <div class="card" id="list">__ROWS__</div>
  <div class="card">
    <div class="meta" style="margin-bottom:10px">新建班级</div>
    <div class="grid">
      <input id="cname" placeholder="班级名，例如 高一2618班" maxlength="40">
      <button class="dark" id="mk">创建</button>
    </div>
    <div class="meta" style="margin-bottom:6px">名单（可选，每行一个名字）</div>
    <textarea id="cnames" placeholder="张三&#10;李四"></textarea>
    <div class="out" id="out"></div>
  </div>
  <div style="margin-top:18px"><button id="logout">退出</button></div>
</div>
<div class="toast" id="toast"></div>
<script>
const toast=(m)=>{const t=document.getElementById('toast');t.textContent=m;t.classList.add('on');
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('on'),1800);};
const show=(h)=>{const o=document.getElementById('out');o.innerHTML=h;o.style.display='block';};
async function post(url,body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-RC':'1'},
    body:JSON.stringify(body||{})});
  if(!r.ok) throw new Error(r.status);
  return r.json();
}
document.querySelectorAll('[data-enter]').forEach(b=>b.onclick=async ()=>{
  try{ await post('/api/admin/enter',{id:b.dataset.enter}); location.href='/api/admin'; }
  catch(e){ toast('进入失败'); }});
document.querySelectorAll('[data-tok]').forEach(b=>b.onclick=async ()=>{
  if(!confirm('换掉后旧 token 立刻失效，确定？')) return;
  try{ const r=await post('/api/admin/newtoken',{id:b.dataset.tok});
       show('班级 '+b.dataset.tok+' 的新 token（只显示这一次）：<br><b>'+r.token+'</b>'); }
  catch(e){ toast('失败'); }});
document.querySelectorAll('[data-key]').forEach(b=>b.onclick=()=>{
  show('班级 '+b.dataset.key+' 的同步密钥（写到客户端 class.ini 里）：<br><b>'+b.dataset.k+'</b>'); });

document.getElementById('mk').onclick=async ()=>{
  const name=document.getElementById('cname').value.trim();
  if(!name){ toast('先写班级名'); return; }
  const names=document.getElementById('cnames').value.split(/\\r?\\n/).map(s=>s.trim()).filter(Boolean);
  try{
    const r=await post('/api/admin/classes',{name,names});
    show('班级已创建：<b>'+r.id+'</b><br>token（只显示这一次）：<b>'+r.token+'</b><br>'
        +'同步密钥：<b>'+r.sync_key+'</b><br>把密钥写进客户端目录的 class.ini：<br>key='+r.sync_key);
    toast('已创建，记下 token');
    document.getElementById('cname').value='';
    document.getElementById('cnames').value='';
  }catch(e){ toast('创建失败：'+e.message); }
};
document.getElementById('logout').onclick=async()=>{
  await fetch('/api/admin/logout',{method:'POST',headers:{'X-RC':'1'}});
  location.reload();
};
</script></body></html>"""


def classes_json():
    out = []
    for rec in class_all():
        st = rec["state"]
        out.append({
            "id": rec["id"],
            "name": rec.get("name") or st.get("class") or rec["id"],
            "class": st.get("class") or "",
            "count": len(st.get("names") or []),
            "rev": int(st.get("rev") or 0),
            "last_sync": int(st.get("last_sync") or 0),
            "created": int(rec.get("created") or 0),
        })
    return {"ok": True, "classes": out}


def master_html(_unused=None):
    rows = []
    for rec in class_all():
        st = rec["state"]
        last = st.get("last_sync") or 0
        last_s = time.strftime("%m-%d %H:%M", time.localtime(last)) if last else "从未"
        rows.append(
            '<div class="row"><div style="flex:1;min-width:200px">'
            '<div class="nm">%s</div>'
            '<div class="meta">编号 %s · 名单 %d 人 · 上次同步 %s</div></div>'
            '<button data-enter="%s">进入</button>'
            '<button data-tok="%s">换 token</button>'
            '<button data-key="%s" data-k="%s">看密钥</button></div>' % (
                escape_html(st.get("class") or rec.get("name") or rec["id"]),
                escape_html(rec["id"]),
                len(st.get("names") or []),
                last_s,
                escape_html(rec["id"]),
                escape_html(rec["id"]),
                escape_html(rec["id"]),
                escape_html(rec.get("sync_key") or "")))
    if not rows:
        rows = ['<div class="meta">还没有班级，下面新建一个。</div>']
    return MASTER_HTML.replace("__ROWS__", "".join(rows))


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
            return self.version(q)

        if path == "/api/rollcall/stream":
            return self.stream(q)

        if path == "/api/rollcall/rev":
            return self.rev(q)

        if path in ("/api/admin", "/api/admin/"):
            cid = self._authed()
            if not cid:
                return self._send(200, LOGIN_HTML.replace("__ERROR__", ""), "text/html; charset=utf-8")
            if cid == MASTER_ID:
                return self._send(200, master_html(None), "text/html; charset=utf-8")
            rec = class_read(cid)
            if not rec:
                return self._redirect("/api/admin/logout")
            return self._send(200, admin_html(rec), "text/html; charset=utf-8")

        if path == "/api/admin/state":
            cid = self._authed()
            if not cid:
                return self._json(401, {"ok": False, "error": "unauthorized"})
            if cid == MASTER_ID:
                return self._json(200, classes_json())
            return self._json(200, state_read(cid))

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
            return self.version(q)

        if path == "/api/admin/login":
            return self.login()

        if path == "/api/admin/logout":
            return self._redirect("/api/admin", [("Set-Cookie", "rc_sess=; Max-Age=0; Path=/api")])

        if path == "/api/admin/save":
            return self.save()

        if path == "/api/admin/roster":
            return self.roster()

        if path == "/api/admin/next":
            return self.next_person()

        if path == "/api/admin/classes":
            return self.classes()

        if path == "/api/admin/enter":
            return self.enter()

        if path == "/api/admin/newtoken":
            return self.newtoken()

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

        cid = check_token(token)
        if not cid:
            with _login_fail_lock:
                rec = _login_fail.setdefault(ip, [0, now])
                rec[0] += 1
            return self._send(200, LOGIN_HTML.replace("__ERROR__", "Token 不正确"),
                              "text/html; charset=utf-8")

        with _login_fail_lock:
            _login_fail.pop(ip, None)
        cookie = "rc_sess=%s; Path=/api; Max-Age=%d; HttpOnly; Secure; SameSite=Lax" % (
            make_session(cid), SESSION_TTL)
        log_line("login ok: %s (%s)" % (cid, ip))
        return self._redirect("/api/admin", [("Set-Cookie", cookie)])

    def save(self):
        cid = self._authed()
        if not cid or cid == MASTER_ID:
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if self.headers.get("X-RC") != "1":
            return self._json(403, {"ok": False, "error": "csrf"})
        try:
            payload = json.loads(self._body().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, KeyError):
            return self._json(400, {"ok": False, "error": "bad json"})

        weights = payload.get("weights")
        round_seconds = payload.get("round_seconds")
        hist_draws = payload.get("hist_draws")

        if (weights is None and round_seconds is None and hist_draws is None
                and payload.get("max_pick") is None and payload.get("default_count") is None
                and payload.get("weight_max") is None and payload.get("class") is None):
            return self._json(400, {"ok": False, "error": "nothing to save"})

        with _lock:
            st = state_read(cid)
            n = len(st.get("names") or [])

            if weights is not None:
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

            if round_seconds is not None:
                try:
                    st["round_seconds"] = max(10, min(86400, int(round_seconds)))
                except (TypeError, ValueError):
                    return self._json(400, {"ok": False, "error": "bad round_seconds"})

            if hist_draws is not None:
                try:
                    st["hist_draws"] = max(0, min(20, int(hist_draws)))
                except (TypeError, ValueError):
                    return self._json(400, {"ok": False, "error": "bad hist_draws"})

            if "max_pick" in payload and payload["max_pick"] is not None:
                try:
                    st["max_pick"] = max(1, min(20, int(payload["max_pick"])))
                except (TypeError, ValueError):
                    return self._json(400, {"ok": False, "error": "bad max_pick"})

            if "default_count" in payload and payload["default_count"] is not None:
                try:
                    st["default_count"] = max(1, min(20, int(payload["default_count"])))
                except (TypeError, ValueError):
                    return self._json(400, {"ok": False, "error": "bad default_count"})

            if "weight_max" in payload and payload["weight_max"] is not None:
                try:
                    st["weight_max"] = max(1, min(1000, float(payload["weight_max"])))
                except (TypeError, ValueError):
                    return self._json(400, {"ok": False, "error": "bad weight_max"})

            if "class" in payload and payload["class"] is not None:
                cls = str(payload["class"]).strip()
                if not cls or len(cls) > 40:
                    return self._json(400, {"ok": False, "error": "bad class name"})
                st["class"] = cls

            st["updated"] = int(time.time())
            st["rev"] = int(st.get("rev") or 0) + 1
            state_write(cid, st)

        log_line("saved: weights=%s rs=%s hd=%s" % (
            "yes" if weights is not None else "-",
            st.get("round_seconds"), st.get("hist_draws")))
        return self._json(200, {"ok": True, "updated": st["updated"],
                                "round_seconds": st.get("round_seconds"),
                                "hist_draws": st.get("hist_draws")})

    def rev(self, q):
        """只返回状态版本号（几十字节），给轮询兜底用；不写任何状态。"""
        rec = class_by_key((q.get("k") or [""])[0])
        if not rec:
            return self._send(403, "forbidden")
        cid = rec["id"]
        # 客户端每次都会带上自己的班级代号；对不上就是别的班的客户端
        want = (q.get("cid") or [""])[0]
        if want and want != cid:
            log_line("class mismatch: key->%s cid=%s ip=%s" % (cid, want, self._client_ip()))
            return self._send(403, "forbidden")
        with _lock:
            r = int(state_read(cid).get("rev") or 0)
        return self._send(200, "rev=%d\n" % r)
    def stream(self, q):
        """SSE 长连接：状态里的 rev 一变就推一条，客户端收到后立刻重新同步。

        客户端不是浏览器，拿不到 Cookie，所以用同步密钥鉴权；此接口只读。
        """
        rec = class_by_key((q.get("k") or [""])[0])
        if not rec:
            return self._send(403, "forbidden")
        cid = rec["id"]
        # 客户端每次都会带上自己的班级代号；对不上就是别的班的客户端
        want = (q.get("cid") or [""])[0]
        if want and want != cid:
            log_line("class mismatch: key->%s cid=%s ip=%s" % (cid, want, self._client_ip()))
            return self._send(403, "forbidden")

        with _stream_lock:
            if _stream_count[0] >= MAX_STREAMS:
                return self._send(503, "too many streams")
            _stream_count[0] += 1

        try:
            # 必须用 chunked：没有 Content-Length 又不分块时，WinHTTP 会一直缓冲
            # 到连接结束才吐数据，客户端就永远收不到"实时"推送。
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.close_connection = True

            def send_chunk(payload):
                self.wfile.write(("%X\r\n" % len(payload)).encode("ascii") + payload + b"\r\n")
                self.wfile.flush()

            try:
                send_chunk(b"retry: 3000\n\n")
            except OSError:
                return

            with _lock:
                last = int(state_read(cid).get("rev") or 0)
            idle = 0
            deadline = time.time() + STREAM_MAX_SECONDS

            while time.time() < deadline:
                time.sleep(1)
                with _lock:
                    rev = int(state_read(cid).get("rev") or 0)
                if rev != last:
                    last = rev
                    try:
                        send_chunk(("event: changed\ndata: %d\n\n" % rev).encode("utf-8"))
                    except OSError:
                        return
                    idle = 0
                else:
                    idle += 1
                    if idle >= 20:
                        idle = 0
                        try:
                            send_chunk(b": ping\n\n")
                        except OSError:
                            return
        finally:
            with _stream_lock:
                _stream_count[0] -= 1
    def version(self, q=None):
        """热更新清单：data/version.txt 原样返回（无缓存头）。"""
        p = os.path.join(DATA_DIR, "version.txt")
        # 每个班可以有自己那份清单：data/rollcall/<cid>.txt
        cid = ((q or {}).get("cid") or [""])[0]
        if cid and _cid_ok(cid):
            own = os.path.join(DATA_DIR, "rollcall", cid + ".txt")
            if os.path.exists(own):
                p = own
        try:
            with open(p, "r", encoding="utf-8") as f:
                body = f.read()
        except OSError:
            return self._send(404, "no version")
        return self._send(200, body, "text/plain; charset=utf-8")

    def sync(self, q):
        rec = class_by_key((q.get("k") or [""])[0])
        if not rec:
            return self._send(403, "forbidden")
        cid = rec["id"]
        # 客户端每次都会带上自己的班级代号；对不上就是别的班的客户端
        want = (q.get("cid") or [""])[0]
        if want and want != cid:
            log_line("class mismatch: key->%s cid=%s ip=%s" % (cid, want, self._client_ip()))
            return self._send(403, "forbidden")
        try:
            n = int((q.get("n") or ["0"])[0])
        except ValueError:
            n = 0

        with _lock:
            st = state_read(cid)
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
            state_write(cid, st)

        body = "RC1\nv=%d\nrs=%d\nhd=%d\nmp=%d\ndc=%d\nwm=%d\nnxi=%d\n" % (
            int(time.time()),
            int(st.get("round_seconds") or 300),
            int(st.get("hist_draws") or 0),
            int(st.get("max_pick") or 5),
            int(st.get("default_count") or 1),
            int(st.get("weight_max") or 100),
            int(st.get("next_id") or 0))
        body += "cn=%s\n" % (st.get("class") or "")
        if st.get("next_name"):
            body += "nx=%s\n" % st["next_name"]
        body += "w=%s\n" % ",".join("%.4g" % float(w) for w in (st.get("weights") or []))
        if st.get("names"):
            body += "n=%s\n" % "|".join(st["names"])
        return self._send(200, body, "text/plain; charset=utf-8")

    def next_person(self):
        """指定下一位抽到的人（name 为空则取消指定）。"""
        cid = self._authed()
        if not cid or cid == MASTER_ID:
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if self.headers.get("X-RC") != "1":
            return self._json(403, {"ok": False, "error": "csrf"})
        try:
            payload = json.loads(self._body().decode("utf-8"))
            name = str(payload.get("name") or "").strip()
        except (ValueError, KeyError):
            return self._json(400, {"ok": False, "error": "bad json"})

        with _lock:
            st = state_read(cid)
            names = st.get("names") or []
            if name and name not in names:
                return self._json(400, {"ok": False, "error": "no such student"})
            st["next_name"] = name
            st["next_id"] = int(st.get("next_id") or 0) + 1
            st["updated"] = int(time.time())
            st["rev"] = int(st.get("rev") or 0) + 1
            state_write(cid, st)

        log_line("next person: %s" % (name or "(cleared)"))
        return self._json(200, {"ok": True, "next_name": name, "next_id": st["next_id"]})

    def roster(self):
        """整份替换名单：新增 / 删除都走这里；同名的人保留原权重。"""
        cid = self._authed()
        if not cid or cid == MASTER_ID:
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
            st = state_read(cid)
            old_names = st.get("names") or []
            old_w = st.get("weights") or []
            by_name = {n: w for n, w in zip(old_names, old_w)}
            st["names"] = clean
            st["weights"] = [float(by_name.get(n, 1.0)) for n in clean]
            st["updated"] = int(time.time())
            st["rev"] = int(st.get("rev") or 0) + 1
            state_write(cid, st)

        log_line("roster updated: %d students" % len(clean))
        return self._json(200, {"ok": True, "count": len(clean)})

    # -- 班级管理（只有管理员能用）----------------------------------------
    def _master(self):
        return self._authed() == MASTER_ID

    def classes(self):
        if not self._master():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if self.headers.get("X-RC") != "1":
            return self._json(403, {"ok": False, "error": "csrf"})
        try:
            payload = json.loads(self._body().decode("utf-8"))
            name = str(payload.get("name") or "").strip()
            names = payload.get("names") or []
        except (ValueError, KeyError, AttributeError):
            return self._json(400, {"ok": False, "error": "bad json"})
        if not name or len(name) > 40:
            return self._json(400, {"ok": False, "error": "bad name"})
        clean = []
        for nm in names:
            if not isinstance(nm, str):
                continue
            nm = nm.strip()
            if nm and len(nm) <= 12 and "|" not in nm:
                clean.append(nm)
        if len(clean) > MAX_STUDENTS:
            return self._json(400, {"ok": False, "error": "too many students"})

        with _lock:
            rec, token = class_create(name, clean)
        if not rec:
            return self._json(400, {"ok": False, "error": "too many classes"})
        log_line("class created: %s (%s, %d students)" % (rec["id"], name, len(clean)))
        return self._json(200, {"ok": True, "id": rec["id"], "token": token,
                                "sync_key": rec["sync_key"], "name": name})

    def enter(self):
        """管理员以某个班级的身份进入它的后台（拿到该班级的会话）。"""
        if not self._master():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if self.headers.get("X-RC") != "1":
            return self._json(403, {"ok": False, "error": "csrf"})
        try:
            payload = json.loads(self._body().decode("utf-8"))
            tid = str(payload.get("id") or "").strip()
        except (ValueError, KeyError, AttributeError):
            return self._json(400, {"ok": False, "error": "bad json"})
        if not class_read(tid):
            return self._json(404, {"ok": False, "error": "no such class"})
        cookie = "rc_sess=%s; Path=/api; Max-Age=%d; HttpOnly; Secure; SameSite=Lax" % (
            make_session(tid), SESSION_TTL)
        log_line("master entered %s" % tid)
        return self._json(200, {"ok": True, "id": tid}, [("Set-Cookie", cookie)])

    def newtoken(self):
        """给某个班级换一把新 token（旧的立刻失效），明文只返回一次。"""
        if not self._master():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if self.headers.get("X-RC") != "1":
            return self._json(403, {"ok": False, "error": "csrf"})
        try:
            payload = json.loads(self._body().decode("utf-8"))
            tid = str(payload.get("id") or "").strip()
        except (ValueError, KeyError, AttributeError):
            return self._json(400, {"ok": False, "error": "bad json"})
        with _lock:
            token = class_rotate_token(tid)
        if not token:
            return self._json(404, {"ok": False, "error": "no such class"})
        log_line("token rotated: %s" % tid)
        return self._json(200, {"ok": True, "id": tid, "token": token})



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

    cid = "c1"     # 种子名单只往第一个班级灌
    st = state_read(cid)
    if st.get("names") and not force:
        if cls:
            st["class"] = cls
            state_write(cid, st)
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
    state_write(cid, st)


def main():
    ensure_dirs()
    migrate_legacy()
    if os.path.exists(NAMES_FILE):
        apply_roster()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    httpd.daemon_threads = True
    log_line("listening on 127.0.0.1:%d" % PORT)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
