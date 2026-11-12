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
    GET  /api/admin/grades               读取成绩科目与每科权重（需登录）
    POST /api/admin/grades/parse         上传 XML（请求体是原始字节）-> 解析预览
    POST /api/admin/grades/apply         确认列 -> 科目映射，写入分数并算权重
    POST /api/admin/grades/subject       单科启用/停用
    POST /api/admin/grades/rule          改 W_MAX / FLOOR 并立即重算
    POST /api/admin/grades/clear         清除全部成绩
    POST /api/rollcall/sync?k=..&n=..    客户端同步（共享 key）

数据文件：
    data/state.json   {"class","names","weights","updated","last_sync","sync_count"}
    secret.json       {"token_sha256","sync_key","cookie_secret"}   600 权限
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
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

    # 成绩 / 科目：规则与分数都要规范化，坏值回默认，别让脏数据把 sync 打崩
    st["weight_rule"] = _rule_norm(st.get("weight_rule"))
    st["subjects"] = _subjects_norm(st.get("subjects"))
    return st


# ---------------------------------------------------------------- 成绩 / 科目
#
# state["weight_rule"] = {"w_max": 5.0, "floor": 0.6}
# state["subjects"]    = {"语文": {"full": 100.0, "enabled": True,
#                                 "scores": {"张三": 90.0, "李四": None}}}
#                        scores 里 None = 缺考（没有分数）
#
# 权重规则（按「离平均分多远」分段线性，锚点：最低分=W_MAX、平均分=1.0、最高分=FLOOR）
#     低于平均分：w = W_MAX + (1.0 - W_MAX) * (score - min) / (avg - min)
#     高于平均分：w = 1.0   + (FLOOR - 1.0) * (score - avg) / (max - avg)
#     全班同分   -> 全员 1.0；缺考（score 为 None）-> FLOOR

W_MAX_DEFAULT = 5.0
FLOOR_DEFAULT = 0.6
MID_WEIGHT = 1.0      # 等于平均分那位的权重（固定，不给后台改）
GRADE_MAX_BYTES = 2 * 1024 * 1024   # 上传的 XML 上限
GRADE_MAX_ROWS = 500                # 一次导入最多保留多少条记录
MAX_SUBJECTS = 24
PARSE_TTL = 3600                    # 解析结果在内存里留多久（秒）
_parse_cache = {}                   # cid -> {"id","ts","data"}
_parse_lock = threading.Lock()

SUBJECT_ORDER = ("语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理")

# (标准科目名, 别名)：仅用于“猜”，猜错可以在后台手动改
_SUBJECT_ALIAS = (
    ("语文", ("语文", "chinese", "yuwen", "chn", "yw")),
    ("数学", ("数学", "mathematics", "math", "maths", "shuxue", "sx")),
    ("英语", ("英语", "english", "yingyu", "eng", "yy")),
    ("物理", ("物理", "physics", "wuli", "wl")),
    ("化学", ("化学", "chemistry", "huaxue", "hx")),
    ("生物", ("生物", "biology", "shengwu", "sw")),
    ("政治", ("政治", "思想政治", "道德与法治", "道法", "politics", "zhengzhi", "zz")),
    ("历史", ("历史", "history", "lishi", "ls")),
    ("地理", ("地理", "geography", "dili", "dl")),
)

# 像“姓名”的字段名
_NAME_KEYS = ("姓名", "名字", "学生姓名", "学生名", "学生", "考生姓名", "考生",
              "name", "studentname", "stuname", "student", "xm", "xingming")

# 字符串里出现这些，就当“没有分数”
_ABSENT_WORDS = ("", "-", "--", "—", "－", "/", "缺考", "缺", "免考", "未考",
                 "未参加", "无", "n/a", "na", "absent", "none", "null")


def _subj_key(s):
    """列名归一化：全角转半角、去空白与标点、转小写。"""
    out = []
    for ch in str(s or ""):
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            ch = chr(o - 0xFEE0)
        out.append(ch)
    t = "".join(out).strip().lower()
    return re.sub(r"[\s_\-./\\:：、,，;；()（）\[\]【】<>《》\"'`]+", "", t)


_NAME_EXACT = frozenset(_subj_key(x) for x in _NAME_KEYS)


def clean_subject_name(s):
    """科目名清洗：去掉 | 和不可见字符（| 是 sync 协议的分隔符），限长 20。"""
    t = re.sub(r"[\x00-\x1f\x7f]", "", str(s or ""))
    t = t.replace("|", "").replace("｜", "").strip()
    t = re.sub(r"\s+", " ", t)
    return t[:20]


def guess_subject(field):
    """字段名/属性名 -> 标准科目名；猜不到返回空串。"""
    k = _subj_key(field)
    if not k:
        return ""
    for subj, aliases in _SUBJECT_ALIAS:
        for a in aliases:
            if a.isascii():
                if k == a:
                    return subj
                # 长别名允许前后缀（englishtotal / total_english）
                if len(a) >= 3 and (k.startswith(a) or k.endswith(a)):
                    return subj
            elif a in k:
                return subj
    return ""


def _is_name_field(field):
    k = _subj_key(field)
    if not k:
        return False
    for a in _NAME_EXACT:
        if a and (k == a or a in k):
            return True
    return False


def _to_score(v):
    """任意值 -> float；认不出来就返回 None（None 表示缺考/没有分数）。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if s.lower() in _ABSENT_WORDS:
        return None
    s = s.replace(",", "").replace("，", "").replace(" ", "").replace("\u3000", "")
    if s.endswith("分"):
        s = s[:-1]
    try:
        f = float(s)
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _rule_norm(r):
    r = r if isinstance(r, dict) else {}
    w_max = _to_score(r.get("w_max"))
    if w_max is None or w_max <= 0:
        w_max = W_MAX_DEFAULT
    w_max = max(0.1, min(100.0, w_max))
    floor = _to_score(r.get("floor"))
    if floor is None or floor < 0:
        floor = FLOOR_DEFAULT
    floor = max(0.0, min(w_max, floor))
    return {"w_max": round(w_max, 4), "floor": round(floor, 4)}


def _rule_values(st):
    """取生效的 W_MAX / FLOOR。0 是合法值（全下不设下限），不能用 or 兜底。"""
    rule = st.get("weight_rule") or {}
    w_max = _to_score(rule.get("w_max"))
    floor = _to_score(rule.get("floor"))
    if w_max is None or w_max <= 0:
        w_max = W_MAX_DEFAULT
    if floor is None or floor < 0:
        floor = FLOOR_DEFAULT
    return w_max, min(floor, w_max)


def _subjects_norm(src):
    """把 state["subjects"] 收拾干净：科目名去 |、满分夹范围、分数统一成 float/None。"""
    src = src if isinstance(src, dict) else {}
    out = {}
    for name, info in src.items():
        nm = clean_subject_name(name)
        if not nm or nm in out or len(out) >= MAX_SUBJECTS:
            continue
        info = info if isinstance(info, dict) else {}
        full = _to_score(info.get("full"))
        if full is None or full <= 0:
            full = 100.0
        full = max(1.0, min(1000.0, full))
        raw = info.get("scores") if isinstance(info.get("scores"), dict) else {}
        sc = {}
        for k, v in raw.items():
            kk = str(k or "").strip()
            if not kk:
                continue
            sv = _to_score(v)
            sc[kk] = None if sv is None else round(sv, 4)
        out[nm] = {"full": round(full, 4),
                   "enabled": bool(info.get("enabled", True)),
                   "scores": sc}
    return out


def subject_names(st):
    """科目显示顺序：先按常见科目顺序，其余按名字排。"""
    subs = st.get("subjects") or {}
    known = [x for x in SUBJECT_ORDER if x in subs]
    other = sorted(x for x in subs if x not in SUBJECT_ORDER)
    return known + other


def compute_subject_weights(names, scores, full, w_max, floor):
    """按「离平均分多远」算权重（分段线性，三个锚点严格成立）。

    names   : 名单（顺序即 sync 里 n= 的顺序）
    scores  : {姓名: 分数或 None}
    full    : 本科满分（仅存档，公式已不用）
    w_max   : 最低分那位的权重（默认 5.0）
    floor   : 最高分那位的权重（默认 0.6）

    锚点：最低分 -> w_max；等于平均分 -> MID(1.0)；最高分 -> floor。
      低于平均分：w = w_max + (MID - w_max) * (score - min) / (avg - min)
      高于平均分：w = MID   + (floor - MID) * (score - avg) / (max - avg)
      全班同分    ：全员 MID
      缺考        ：floor
    """
    w_max = float(w_max)
    floor = float(floor)
    mid = MID_WEIGHT
    if floor > w_max:
        floor = w_max

    vals = []
    for nm in names:
        s = scores.get(nm)
        vals.append(None if s is None else float(s))
    present = [v for v in vals if v is not None]
    if present:
        avg = sum(present) / len(present)
        vmin = min(present)
        vmax = max(present)
    else:
        avg = vmin = vmax = 0.0

    ws = []
    rows = []
    for nm, s in zip(names, vals):
        if s is None:
            w = floor
        elif vmax <= vmin:
            w = mid                       # 全班同分：谁都一样
        elif s <= avg:
            if avg <= vmin:
                w = mid
            else:
                w = w_max + (mid - w_max) * (s - vmin) / (avg - vmin)
        else:
            if vmax <= avg:
                w = mid
            else:
                w = mid + (floor - mid) * (s - avg) / (vmax - avg)
        if w < floor:
            w = floor
        if w > w_max:
            w = w_max
        w = round(w, 4)
        ws.append(w)
        rows.append({"name": nm,
                     "score": (None if s is None else round(s, 4)),
                     "diff": (None if s is None else round(s - avg, 4)),
                     "weight": w})
    stats = {"count": len(names),
             "missing": sum(1 for v in vals if v is None),
             "avg": round(avg, 4), "min": round(vmin, 4), "max": round(vmax, 4),
             "max_weight": (max(ws) if ws else 0.0)}
    return ws, rows, stats

def grades_json(st, limit=20):
    """后台用的成绩视图：每科一份权重表（默认只给前 limit 行）。"""
    w_max, floor = _rule_values(st)
    names = st.get("names") or []
    subs = []
    for subj in subject_names(st):
        info = (st.get("subjects") or {}).get(subj) or {}
        scores = info.get("scores") or {}
        has_data = any(scores.get(n) is not None for n in names)
        _ws, rows, stats = compute_subject_weights(
            names, scores, info.get("full") or 100.0, w_max, floor)
        subs.append({
            "name": subj,
            "enabled": bool(info.get("enabled", True)),
            "full": float(info.get("full") or 100.0),
            "has_data": has_data,
            "count": stats["count"],
            "missing": stats["missing"],
            "avg": stats["avg"],
            "rows_total": len(rows),
            "rows": rows[:limit],
            "truncated": len(rows) > limit,
        })
    return {"ok": True, "rule": {"w_max": w_max, "floor": floor},
            "names": len(names), "subjects": subs}


def _sync_subject_lines(st):
    """sync 响应末尾追加的 sub= 行：只发「有数据且启用」的科目。"""
    names = st.get("names") or []
    if not names:
        return ""       # 服务器没有名单就没法对齐权重，一行都不发
    w_max, floor = _rule_values(st)
    out = []
    for subj in subject_names(st):
        info = (st.get("subjects") or {}).get(subj) or {}
        if not info.get("enabled", True):
            continue
        scores = info.get("scores") or {}
        if not any(scores.get(n) is not None for n in names):
            continue    # 没有数据 / 全都是缺考 -> 不发
        ws, _rows, _stats = compute_subject_weights(
            names, scores, info.get("full") or 100.0, w_max, floor)
        out.append("sub=%s|1|%s\n" % (subj, ",".join("%.4g" % w for w in ws)))
    return "".join(out)


# ---- XML 解析 ------------------------------------------------------------

def decode_xml_bytes(raw):
    """猜编码并解码，返回 (文本, 编码名)。顺序：BOM -> XML 声明 -> UTF-8 -> GB18030。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8-bom"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    m = re.search(rb"encoding\s*=\s*[\"']([A-Za-z0-9_\-]+)[\"']", raw[:400])
    declared = (m.group(1).decode("ascii", "ignore").lower() if m else "")
    if declared in ("gbk", "gb2312", "gb18030", "gb_2312-80"):
        tries = (("gb18030", "gbk"), ("utf-8", "utf-8"))
    else:
        tries = (("utf-8", "utf-8"), ("gb18030", "gbk"))
    for codec, label in tries:
        try:
            return raw.decode(codec), label
        except UnicodeDecodeError:
            continue
    raise ValueError("认不出文件编码（UTF-8 和 GBK 都解不开），请用记事本另存为 UTF-8 再试")


def _localname(tag):
    t = str(tag or "")
    if "}" in t:
        t = t.split("}", 1)[1]
    return t.strip()


def _flat_fields(elem):
    """把一个元素摊平：属性 + 没有子节点的子节点 -> [(字段名, 原始值)]。"""
    out = []
    for k, v in elem.attrib.items():
        out.append((_localname(k), (v or "").strip()))
    for ch in list(elem):
        if len(list(ch)):
            continue
        name = _localname(ch.tag)
        val = (ch.text or "").strip()
        out.append((name, val))
        for k2, v2 in ch.attrib.items():
            out.append((name + "." + _localname(k2), (v2 or "").strip()))
    return out


def suggest_full(mx):
    """按该列最大值猜满分：<=100 -> 100，<=150 -> 150，<=200 -> 200，再往上取 50 的倍数。"""
    mx = float(mx or 0)
    if mx <= 100:
        return 100.0
    if mx <= 150:
        return 150.0
    if mx <= 200:
        return 200.0
    return float(int((mx + 49) // 50) * 50)


def parse_grade_xml(raw):
    """把任意结构的成绩 XML 解析成「学生行 + 列」。

    识别规则：
      1. 一条记录 = 一个元素：它的子节点全是叶子（或只有属性），
         并且里面既有像姓名的字段，又有至少一个数字字段；
      2. 同名的记录元素按出现次数最多的一组作为“真正的一张表”；
      3. 列 = 这组记录里出现过的所有字段名（按首次出现顺序）。
    解析不出来就抛 ValueError（中文原因），绝不返回空表糊过去。
    """
    text, enc = decode_xml_bytes(raw)
    if not text.strip():
        raise ValueError("文件是空的，没有内容可解析")
    # 编码已经用过了，把声明里的 encoding 去掉，免得 ElementTree 再按字面解一次
    text = re.sub(r"(<\?xml[^>]*?)\s+encoding\s*=\s*[\"'][^\"']*[\"']", r"\1", text, count=1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        pos = getattr(e, "position", None) or (0, 0)
        raise ValueError("XML 格式错误：第 %d 行第 %d 列 —— %s" % (pos[0], max(1, pos[1] + 1), e))

    cands = {}
    for elem in root.iter():
        kids = list(elem)
        if any(len(list(k)) for k in kids):
            continue                      # 子节点还带子节点：这是容器，不是一条记录
        fields = _flat_fields(elem)
        if not fields:
            continue
        tag = _localname(elem.tag) or "?"
        nums = 0
        namecol = ""
        exact = False
        for fname, fval in fields:
            if fval and _is_name_field(fname):
                if not namecol:
                    namecol = fname
                if _subj_key(fname) in _NAME_EXACT:
                    exact = True
            if _to_score(fval) is not None:
                nums += 1
        if not namecol or nums < 1:
            continue
        g = cands.get(tag)
        if g is None:
            g = cands[tag] = {"tag": tag, "rows": [], "cols": [], "seen": set(),
                              "num_n": 0, "exact": False}
        g["rows"].append(fields)
        for fname, _v in fields:
            if fname not in g["seen"]:
                g["seen"].add(fname)
                g["cols"].append(fname)
        if nums > g["num_n"]:
            g["num_n"] = nums
        if exact:
            g["exact"] = True

    if not cands:
        tags = sorted({_localname(e.tag) for e in root.iter()})
        raise ValueError(
            "没找到成绩记录：需要这样的元素 —— 里面既有像姓名的字段，又有至少一个数字字段。"
            "文件里出现的元素有：%s" % ("、".join(tags[:12]) or "（无）"))

    best = max(cands.values(), key=lambda g: (len(g["rows"]), g["num_n"], g["exact"]))
    cols = best["cols"]
    rows = []
    filled = {c: 0 for c in cols}
    numeric = {c: 0 for c in cols}
    for fields in best["rows"]:
        d = {}
        for k, v in fields:
            if k in d and d[k]:
                continue                  # 同名字段重复：前者非空就保留前者
            d[k] = v
        rows.append(d)
        for c in cols:
            v = str(d.get(c, "")).strip()
            if v:
                filled[c] += 1
            if _to_score(v) is not None:
                numeric[c] += 1

    total = len(rows)
    if total > GRADE_MAX_ROWS:
        rows = rows[:GRADE_MAX_ROWS]

    # 姓名列：先精确命中 -> 再包含关键字 -> 最后拿第一个“基本不是数字”的列
    name_col = ""
    for c in cols:
        if _subj_key(c) in _NAME_EXACT and numeric[c] == 0:
            name_col = c
            break
    if not name_col:
        for c in cols:
            if _is_name_field(c) and numeric[c] == 0:
                name_col = c
                break
    if not name_col:
        for c in cols:
            if numeric[c] == 0 and filled[c] >= max(1, total // 2):
                name_col = c
                break

    colmeta = []
    for c in cols:
        mx = max([_to_score(r.get(c)) or 0 for r in rows
                  if _to_score(r.get(c)) is not None] or [0])
        colmeta.append({
            "key": c,
            "numeric": filled[c] > 0 and numeric[c] * 2 >= filled[c],
            "numeric_ratio": round(numeric[c] / filled[c], 3) if filled[c] else 0.0,
            "guess": guess_subject(c),
            "samples": [str(r.get(c, ""))[:24] for r in rows[:3]],
            "max": round(mx, 4),
            "suggest_full": suggest_full(mx),
        })

    warnings = []
    got = [c["guess"] for c in colmeta if c["guess"]]
    if not got:
        warnings.append("没认出任何科目列，请在下面手动把 XML 列指到科目上")
    if not name_col:
        warnings.append("没找到像姓名的列，请在下面手动选一列当姓名，否则导入会失败")
    if total > len(rows):
        warnings.append("文件里有 %d 条记录，只取前 %d 条" % (total, len(rows)))
    if total == 0:
        warnings.append("一条记录都没读到")

    return {
        "encoding": enc,
        "record_tag": best["tag"],
        "total": total,
        "name_column": name_col,
        "columns": colmeta,
        "rows": [{"name": str(r.get(name_col, "")).strip() if name_col else "",
                  "cells": {c: str(r.get(c, "")) for c in cols}} for r in rows],
        "warnings": warnings,
    }


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
  .gsec{margin:30px 0 10px;border-top:1px solid var(--line);padding-top:18px}
  .gsec h2{margin:0 0 12px;font-size:14px;font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .gsec h2 .tag{font-weight:400;color:var(--sub);font-size:12px}
  .gup{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
  .gup input[type=file]{font:inherit;font-size:12.5px;color:var(--sub);max-width:230px}
  .gwarn{background:#fff7e8;border:1px solid #f0dcb4;color:#7a5a13;border-radius:9px;
       padding:9px 12px;font-size:12.5px;margin:8px 0;line-height:1.6}
  .gmap{display:grid;grid-template-columns:1fr 104px 74px 116px;gap:8px 10px;align-items:center;
       padding:7px 0;border-bottom:1px solid #f0eee9;font-size:12.5px}
  .gmap .k{color:var(--sub);word-break:break-all;line-height:1.4}
  .gmap select{height:32px;padding:0 8px;border:1px solid var(--line);border-radius:8px;font:inherit;
       background:#fff;outline:none;width:100%}
  .gmap input{width:100%;height:32px;text-align:center}
  .gcard{border:1px solid var(--line);border-radius:11px;padding:12px 14px;margin:12px 0;background:#fff}
  .gcard h3{margin:0 0 9px;font-size:13.5px;font-weight:600;display:flex;align-items:center;
       gap:10px;flex-wrap:wrap}
  .gcard .sub{color:var(--sub);font-weight:400;font-size:12px}
  .gtbl{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
  .gtbl th{color:var(--sub);font-weight:400;text-align:right;padding:6px 4px;border-bottom:1px solid var(--line)}
  .gtbl th:first-child,.gtbl td:first-child{text-align:left}
  .gtbl td{padding:6px 4px;border-bottom:1px solid #f5f3ee;text-align:right}
  .gtbl tr:last-child td{border-bottom:0}
  .gtbl td.miss{color:#c0392b}
  .gck{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--sub);cursor:pointer}
  .gck input{width:auto;height:auto}
  .gmore{margin-top:8px;font-size:12px;color:var(--sub)}
  .gfoot{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0 6px;
       font-size:12.5px;color:var(--sub)}
  .gfoot button{height:34px;padding:0 16px}
  .gfoot input{width:62px;height:32px;text-align:center}
  .gfoot label{display:flex;align-items:center;gap:6px}
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

  <section class="gsec" id="gsec">
    <h2>成绩 / 科目 <span class="tag" id="gsum">未导入成绩</span></h2>
    <div class="gup">
      <input type="file" id="gfile" accept=".xml,text/xml,application/xml">
      <button id="gupload" style="height:34px;padding:0 14px">上传并解析</button>
      <button class="ghost" id="gclear" style="height:34px;padding:0 14px">清除成绩</button>
    </div>
    <div id="gwarn"></div>
    <div id="gmap"></div>
    <div id="glist"></div>
    <div class="gfoot">
      <label>最低分那位权重 <input type="number" id="gwmax" min="0.1" max="100" step="0.1" value="5"></label>
      <label>最高分那位权重 <input type="number" id="gfloor" min="0" max="100" step="0.1" value="0.6"></label>
      <button class="ghost" id="grule">保存规则</button>
      <span>按平均分分段：最低分=左值、平均分=1、最高分=右值；缺考=右值</span>
    </div>
  </section>
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

// ---- 成绩 / 科目 -------------------------------------------------------
const SUBJECTS = ['语文','数学','英语','物理','化学','生物','政治','历史','地理'];
const gmap = document.getElementById('gmap');
const glist = document.getElementById('glist');
const gwarn = document.getElementById('gwarn');
const gsum = document.getElementById('gsum');
let GPARSE = null;

function el(tag, cls, text){
  const d = document.createElement(tag);
  if(cls) d.className = cls;
  if(text !== undefined && text !== null) d.textContent = String(text);
  return d;
}

function warnBox(msgs){
  gwarn.textContent = '';
  if(!msgs || !msgs.length) return;
  const box = el('div','gwarn');
  msgs.forEach(m => box.appendChild(el('div', null, '· ' + m)));
  gwarn.appendChild(box);
}

async function gpost(url, body){
  let r = null, j = null;
  try {
    r = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-RC':'1'},
                         body:JSON.stringify(body)});
  } catch(e){ showToast('连不上服务器'); return null; }
  try { j = await r.json(); } catch(e){ j = null; }
  if(!r.ok || !j || !j.ok){
    showToast((j && j.error) ? j.error : ('请求失败：HTTP ' + r.status));
    return null;
  }
  return j;
}

function subjectCard(s){
  const card = el('div','gcard');
  const h = el('h3');
  h.appendChild(el('span', null, s.name));
  h.appendChild(el('span','sub','满分 ' + s.full + ' · 缺考 ' + s.missing + ' 人 · 平均 ' + s.avg));
  const ck = el('label','gck');
  const box = document.createElement('input');
  box.type = 'checkbox'; box.checked = !!s.enabled;
  box.onchange = async ()=>{
    const r = await gpost('/api/admin/grades/subject', {subject:s.name, enabled:box.checked});
    if(!r){ box.checked = !box.checked; return; }
    renderSubjects(r);
    showToast(box.checked ? ('已启用 ' + s.name) : ('已停用 ' + s.name + '（同步里不再下发）'));
  };
  ck.appendChild(box);
  ck.appendChild(el('span', null, '启用'));
  h.appendChild(ck);
  card.appendChild(h);

  const t = el('table','gtbl');
  const hr = el('tr');
  ['姓名','分数','离满分差','权重'].forEach(x => hr.appendChild(el('th', null, x)));
  t.appendChild(hr);
  (s.rows || []).forEach(r=>{
    const tr = el('tr');
    const miss = (r.score === null || r.score === undefined);
    tr.appendChild(el('td', null, r.name));
    tr.appendChild(el('td', miss ? 'miss' : null, miss ? '缺考' : r.score));
    tr.appendChild(el('td', miss ? 'miss' : null, miss ? '—' : r.diff));
    tr.appendChild(el('td', null, r.weight));
    t.appendChild(tr);
  });
  card.appendChild(t);
  if(s.truncated){
    card.appendChild(el('div','gmore','只显示前 ' + s.rows.length + ' 行，共 ' + s.rows_total + ' 行'));
  }
  return card;
}

function renderSubjects(data){
  if(!data || !data.rule) return;
  document.getElementById('gwmax').value = data.rule.w_max;
  document.getElementById('gfloor').value = data.rule.floor;
  const shown = (data.subjects || []).filter(s => s.has_data);
  gsum.textContent = shown.length
    ? (shown.length + ' 科有数据 · 名单 ' + data.names + ' 人')
    : '未导入成绩';
  glist.textContent = '';
  if(!shown.length){
    glist.appendChild(el('div','empty','还没有成绩数据。上传一个 XML 就会在这里算出每科权重'));
    return;
  }
  shown.forEach(s => glist.appendChild(subjectCard(s)));
}

function renderPreview(p){
  GPARSE = p;
  gmap.textContent = '';
  warnBox(p.warnings);
  const head = el('div','gmap');
  head.appendChild(el('div','k','姓名列（对应名单上的名字）'));
  const sel = document.createElement('select');
  sel.id = 'gnamecol';
  const o0 = document.createElement('option');
  o0.value = ''; o0.textContent = '（请选择）';
  sel.appendChild(o0);
  (p.columns || []).forEach(c=>{
    const o = document.createElement('option');
    o.value = c.key; o.textContent = c.key;
    sel.appendChild(o);
  });
  if(p.name_column) sel.value = p.name_column;
  head.appendChild(sel);
  head.appendChild(el('div','k', p.total + ' 条记录'));
  head.appendChild(el('div','k','编码 ' + p.encoding));
  gmap.appendChild(head);

  (p.columns || []).forEach(c=>{
    const row = el('div','gmap');
    row.appendChild(el('div','k', c.key + '　最大 ' + c.max + (c.numeric ? '' : '　（非数字列）')));
    const s = document.createElement('select');
    s.className = 'gsub';
    s.dataset.col = c.key;
    const n0 = document.createElement('option');
    n0.value = ''; n0.textContent = '（不用）';
    s.appendChild(n0);
    SUBJECTS.forEach(x=>{
      const o = document.createElement('option');
      o.value = x; o.textContent = x;
      s.appendChild(o);
    });
    if(c.guess) s.value = c.guess;
    row.appendChild(s);
    const f = document.createElement('input');
    f.type = 'number'; f.min = '1'; f.max = '1000'; f.step = '1';
    f.className = 'gfull';
    f.dataset.col = c.key;
    f.value = c.suggest_full || 100;
    row.appendChild(f);
    row.appendChild(el('div','k', (c.guess ? ('猜：' + c.guess) : '未识别科目')));
    gmap.appendChild(row);
  });

  const bar = el('div','gfoot');
  const btn = document.createElement('button');
  btn.textContent = '应用到名单';
  btn.onclick = applyGrades;
  bar.appendChild(btn);
  bar.appendChild(el('span', null, '只写入选中的科目；同名科目会被覆盖，其他科目不动'));
  gmap.appendChild(bar);
}

async function applyGrades(){
  if(!GPARSE){ showToast('先上传并解析 XML'); return; }
  const nameCol = (document.getElementById('gnamecol') || {}).value || '';
  if(!nameCol){ showToast('先选姓名列'); return; }
  const map = {}, full = {}, seen = {};
  let dup = '';
  [...gmap.querySelectorAll('select.gsub')].forEach(s=>{
    if(!s.value) return;
    if(seen[s.value]){ dup = s.value; }
    seen[s.value] = 1;
    map[s.value] = s.dataset.col;
    const f = [...gmap.querySelectorAll('input.gfull')].filter(x => x.dataset.col === s.dataset.col)[0];
    full[s.value] = f ? (parseFloat(f.value) || 100) : 100;
  });
  if(dup){ showToast('「' + dup + '」被指了两列，请只留一个'); return; }
  if(!Object.keys(map).length){ showToast('至少把一列指给一个科目'); return; }
  const r = await gpost('/api/admin/grades/apply',
    {parse_id: GPARSE.parse_id, name_column: nameCol, map: map, full: full});
  if(!r) return;
  GPARSE = null;
  gmap.textContent = '';
  warnBox(r.warnings);
  renderSubjects(r);
  showToast('已导入：' + ((r.applied || []).join('、') || '无'));
}

async function uploadGrades(){
  const inp = document.getElementById('gfile');
  const f = inp.files && inp.files[0];
  if(!f){ showToast('先选一个 XML 文件'); return; }
  if(f.size > 2 * 1024 * 1024){ showToast('文件超过 2 MB'); return; }
  let buf = null;
  try { buf = await f.arrayBuffer(); } catch(e){ showToast('读不到文件内容'); return; }
  let r = null, j = null;
  try {
    r = await fetch('/api/admin/grades/parse',
      {method:'POST',headers:{'Content-Type':'application/xml','X-RC':'1'},body:buf});
  } catch(e){ showToast('连不上服务器'); return; }
  try { j = await r.json(); } catch(e){ j = null; }
  if(!r.ok || !j || !j.ok){
    showToast((j && j.error) ? j.error : ('解析失败：HTTP ' + r.status));
    return;
  }
  renderPreview(j);
  showToast('识别到 ' + (j.columns || []).length + ' 列、' + j.total + ' 条记录');
}

async function saveRule(){
  const w = parseFloat(document.getElementById('gwmax').value);
  const fl = parseFloat(document.getElementById('gfloor').value);
  if(!isFinite(w) || !isFinite(fl)){ showToast('两个框都要填数字'); return; }
  const r = await gpost('/api/admin/grades/rule', {w_max:w, floor:fl});
  if(r){ renderSubjects(r); showToast('规则已保存，客户端下次同步生效'); }
}

document.getElementById('gupload').onclick = uploadGrades;
document.getElementById('grule').onclick = saveRule;
document.getElementById('gclear').onclick = async ()=>{
  if(!confirm('清空所有科目的成绩？（名单和手动权重不受影响）')) return;
  const r = await gpost('/api/admin/grades/clear', {});
  if(!r) return;
  GPARSE = null;
  gmap.textContent = '';
  warnBox([]);
  renderSubjects(r);
  showToast('成绩已清空');
};

(async ()=>{
  try {
    const r = await fetch('/api/admin/grades?limit=20');
    if(r.ok) renderSubjects(await r.json());
  } catch(e){}
})();
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

def roster_md5(names):
    """名单指纹：UTF-8、按顺序用 \n 连接。客户端用同一算法校验本地名单是否被改过。"""
    h = hashlib.md5()
    h.update("\n".join(names or []).encode("utf-8"))
    return h.hexdigest()


def roster_track(st):
    """名单一变就把版本号 +1 并刷新 md5（任何入口改名单都会在这里被发现）。"""
    names = st.get("names") or []
    cur = roster_md5(names)
    if st.get("roster_md5") != cur:
        st["roster_rev"] = int(st.get("roster_rev") or 0) + 1
        st["roster_md5"] = cur
    return st

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

    def _payload(self, limit=65536):
        """读 JSON 请求体；不是对象就返回 None（调用方统一回 400）。"""
        try:
            obj = json.loads(self._body(limit).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return obj if isinstance(obj, dict) else None

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

        if path == "/api/admin/grades":
            return self.grades_get(q)

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

        if path == "/api/admin/grades/parse":
            return self.grades_parse()

        if path == "/api/admin/grades/apply":
            return self.grades_apply()

        if path == "/api/admin/grades/subject":
            return self.grades_subject()

        if path == "/api/admin/grades/rule":
            return self.grades_rule()

        if path == "/api/admin/grades/clear":
            return self.grades_clear()

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

    # -- 成绩 / 科目 ------------------------------------------------------
    def _grade_authed(self):
        """成绩类接口的统一鉴权：必须是某个班级的会话，且带 X-RC 头。"""
        cid = self._authed()
        if not cid or cid == MASTER_ID:
            self._json(401, {"ok": False, "error": "unauthorized"})
            return None
        if self.headers.get("X-RC") != "1":
            self._json(403, {"ok": False, "error": "csrf"})
            return None
        return cid

    def _bump(self, cid, st):
        st["updated"] = int(time.time())
        st["rev"] = int(st.get("rev") or 0) + 1
        state_write(cid, st)

    def grades_get(self, q):
        cid = self._authed()
        if not cid or cid == MASTER_ID:
            return self._json(401, {"ok": False, "error": "unauthorized"})
        try:
            limit = int((q.get("limit") or ["20"])[0])
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(200, limit))
        with _lock:
            st = state_read(cid)
        return self._json(200, grades_json(st, limit))

    def grades_parse(self):
        """请求体 = XML 原始字节（不是 JSON），这样 GBK/UTF-8 都能自己判。"""
        cid = self._grade_authed()
        if not cid:
            return
        raw = self._body(GRADE_MAX_BYTES + 1)
        if not raw:
            return self._json(400, {"ok": False,
                                    "error": "没收到文件内容（空文件，或超过 %d 字节）"
                                             % GRADE_MAX_BYTES})
        if len(raw) > GRADE_MAX_BYTES:
            return self._json(400, {"ok": False,
                                    "error": "文件太大（上限 %d 字节）" % GRADE_MAX_BYTES})
        try:
            parsed = parse_grade_xml(raw)
        except ValueError as e:
            log_line("grades parse failed: %s (%s)" % (cid, e))
            return self._json(400, {"ok": False, "error": str(e)})

        pid = os.urandom(8).hex()
        with _parse_lock:
            _parse_cache[cid] = {"id": pid, "ts": time.time(), "data": parsed}
        out = {"ok": True, "parse_id": pid}
        out.update(parsed)
        log_line("grades parsed: %s tag=%s rows=%d cols=%d enc=%s" % (
            cid, parsed["record_tag"], parsed["total"], len(parsed["columns"]), parsed["encoding"]))
        return self._json(200, out)

    def grades_apply(self):
        """把 XML 列指到科目上，写入分数并按规则算出每科权重。"""
        cid = self._grade_authed()
        if not cid:
            return
        payload = self._payload(GRADE_MAX_BYTES)
        if payload is None:
            return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})

        pid = str(payload.get("parse_id") or "")
        mapping = payload.get("map") if isinstance(payload.get("map"), dict) else {}
        fulls = payload.get("full") if isinstance(payload.get("full"), dict) else {}

        with _parse_lock:
            cache = _parse_cache.get(cid)
            if not cache or cache.get("id") != pid:
                return self._json(409, {"ok": False, "error": "解析结果已失效，请重新上传 XML"})
            if time.time() - cache["ts"] > PARSE_TTL:
                _parse_cache.pop(cid, None)
                return self._json(409, {"ok": False, "error": "解析结果超过 1 小时，请重新上传 XML"})
            parsed = cache["data"]

        cols = [c["key"] for c in parsed["columns"]]
        name_col = str(payload.get("name_column") or parsed["name_column"] or "").strip()
        if not name_col:
            return self._json(400, {"ok": False, "error": "没选姓名列，无法对应到名单"})
        if name_col not in cols:
            return self._json(400, {"ok": False, "error": "姓名列「%s」不在文件里" % name_col})

        clean_map = {}
        used = {}
        for subj, col in mapping.items():
            col = str(col or "").strip()
            if not col:
                continue
            s = clean_subject_name(subj)
            if not s:
                return self._json(400, {"ok": False, "error": "科目名不能为空（或只由 | 组成）"})
            if col not in cols:
                return self._json(400, {"ok": False, "error": "列「%s」不在文件里" % col})
            if s in clean_map:
                return self._json(400, {"ok": False, "error": "科目「%s」被指了两列" % s})
            if col in used:
                return self._json(400, {"ok": False,
                                        "error": "列「%s」同时指给了「%s」和「%s」"
                                                 % (col, used[col], s)})
            clean_map[s] = col
            used[col] = s
        if not clean_map:
            return self._json(400, {"ok": False, "error": "至少要把一列指给一个科目"})
        if len(clean_map) > MAX_SUBJECTS:
            return self._json(400, {"ok": False, "error": "科目太多（最多 %d 个）" % MAX_SUBJECTS})

        with _lock:
            st = state_read(cid)
        roster = list(st.get("names") or [])
        roster_set = set(roster)

        warnings = []
        new_subs = {}
        for subj, col in clean_map.items():
            meta = parsed["columns"][cols.index(col)]
            full = _to_score(fulls.get(subj))
            if full is None or full <= 0:
                full = float(meta.get("suggest_full") or 100.0)
            full = max(1.0, min(1000.0, float(full)))
            scores = {}
            bad = 0
            dups = 0
            blank = 0
            for i, row in enumerate(parsed["rows"]):
                nm = str(row.get("name") or "").strip()
                if not nm:
                    blank += 1
                    continue
                raw_v = str((row.get("cells") or {}).get(col, ""))
                sv = _to_score(raw_v)
                if sv is None and raw_v.strip().lower() not in _ABSENT_WORDS and raw_v.strip():
                    bad += 1
                    if bad <= 3:
                        warnings.append("第 %d 条记录「%s」的「%s」是「%s」，不是数字，按缺考处理"
                                        % (i + 1, nm, subj, raw_v[:20]))
                if nm in scores:
                    dups += 1
                scores[nm] = sv
            if bad > 3:
                warnings.append("「%s」还有 %d 个非数字值，都按缺考处理" % (subj, bad - 3))
            if dups:
                warnings.append("「%s」里有 %d 条重名学生，只保留最后一条" % (subj, dups))
            if blank:
                warnings.append("「%s」有 %d 条记录姓名为空，已跳过" % (subj, blank))
            extra = [n for n in scores if roster_set and n not in roster_set]
            if extra:
                warnings.append("「%s」有 %d 人不在本班名单里（如 %s），权重表里不会有他们；"
                                "要统计请先加名单" % (subj, len(extra), "、".join(extra[:3])))
            new_subs[subj] = {"full": round(full, 4), "enabled": True, "scores": scores}

        with _lock:
            st = state_read(cid)
            subs = dict(st.get("subjects") or {})
            subs.update(new_subs)                       # 只覆盖这次导入的科目，其它科目不动
            st["subjects"] = _subjects_norm(subs)
            st["weight_rule"] = _rule_norm(st.get("weight_rule"))
            self._bump(cid, st)
            out = grades_json(state_read(cid), 20)

        out["warnings"] = warnings
        out["applied"] = sorted(new_subs.keys())
        log_line("grades applied: %s %s" % (cid, ",".join(sorted(new_subs.keys()))))
        return self._json(200, out)

    def grades_subject(self):
        """单科启用/停用。停用的科目不再出现在 sync 里。"""
        cid = self._grade_authed()
        if not cid:
            return
        payload = self._payload()
        if payload is None:
            return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
        subj = clean_subject_name(payload.get("subject"))
        if not subj:
            return self._json(400, {"ok": False, "error": "缺科目名"})
        enabled = bool(payload.get("enabled"))
        with _lock:
            st = state_read(cid)
            subs = st.get("subjects") or {}
            if subj not in subs:
                return self._json(404, {"ok": False, "error": "没有这个科目：%s" % subj})
            subs[subj]["enabled"] = enabled
            st["subjects"] = _subjects_norm(subs)
            self._bump(cid, st)
            out = grades_json(state_read(cid), 20)
        log_line("grades subject: %s %s -> %s" % (cid, subj, "on" if enabled else "off"))
        return self._json(200, out)

    def grades_rule(self):
        """改 W_MAX / FLOOR，改完立刻按新规则重算（权重是读的时候算的，不用另存）。"""
        cid = self._grade_authed()
        if not cid:
            return
        payload = self._payload()
        if payload is None:
            return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
        w_max = _to_score(payload.get("w_max"))
        floor = _to_score(payload.get("floor"))
        if w_max is None or floor is None:
            return self._json(400, {"ok": False, "error": "w_max 和 floor 都必须是数字"})
        if w_max <= 0 or w_max > 100:
            return self._json(400, {"ok": False, "error": "w_max 要在 0.1 ~ 100 之间"})
        if floor < 0 or floor > w_max:
            return self._json(400, {"ok": False, "error": "floor 要在 0 ~ w_max 之间"})
        with _lock:
            st = state_read(cid)
            st["weight_rule"] = {"w_max": round(w_max, 4), "floor": round(floor, 4)}
            self._bump(cid, st)
            out = grades_json(state_read(cid), 20)
        log_line("grades rule: %s w_max=%s floor=%s" % (cid, w_max, floor))
        return self._json(200, out)

    def grades_clear(self):
        """清空全部成绩（名单和手动权重不动）。"""
        cid = self._grade_authed()
        if not cid:
            return
        with _lock:
            st = state_read(cid)
            st["subjects"] = {}
            st["weight_rule"] = _rule_norm(None)
            self._bump(cid, st)
            out = grades_json(state_read(cid), 20)
        with _parse_lock:
            _parse_cache.pop(cid, None)
        log_line("grades cleared: %s" % cid)
        return self._json(200, out)

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
            roster_track(st)          # 名单变了就 +1 版本、刷新 md5
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
            # 名单版本 + 指纹：客户端据此决定是否采用名单、以及本地是否被改过
            body += "rv=%d\n" % int(st.get("roster_rev") or 0)
            body += "rm=%s\n" % (st.get("roster_md5") or roster_md5(st["names"]))
        # 成绩科目：只在末尾追加，老客户端会忽略不认识的 sub= 行
        body += _sync_subject_lines(st)
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
