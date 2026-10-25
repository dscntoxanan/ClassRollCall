# 服务端（可选）

给 `ClassRollCall.exe` 配套的极简后端：**Python 3 标准库，无第三方依赖**，
一个 `ThreadingHTTPServer` 干完所有活。

它的作用只有三个：

1. 让客户端**同步名单和权重**（不改名单就不用重新编译 exe）
2. 提供一个**网页后台**，调每个人的概率、加删学生
3. 提供**热更新清单**，客户端据此静默升级

> 不部署服务端也能用：客户端会退回本地名单（`src/roster.txt` 编进 exe 的那份）。

## 文件

| 文件 | 说明 |
|---|---|
| `app.py` | 全部逻辑（含内嵌的登录页和后台页面） |
| `gen-secret.py` | 生成 `secret.json`（Token 哈希、同步密钥、会话密钥） |
| `rollcall-api.service` | systemd 单元 |
| `nginx-rollcall.conf` | nginx 反向代理片段 |
| `secret.json.example` | 配置示例 |
| `state.example.json` | 数据文件示例 |

运行后会生成：`data/state.json`（名单+权重）、`data/names.json`、`data/version.txt`（热更新清单）、
`logs/api.log`。

## 快速开始

```bash
cd server
python3 gen-secret.py "你的管理Token"     # 生成 secret.json（权限 600）
python3 app.py                            # 默认监听 127.0.0.1:8120
```

浏览器打开 <http://127.0.0.1:8120/api/admin>，用刚才的 Token 登录。

把 `gen-secret.py` 打印出来的 `sync_key` 填到客户端 `src/main.c` 的 `SYNC_KEY`，
把服务器地址填到 `SYNC_IP` / `SYNC_HOST`，重新编译即可。

## 部署（systemd）

```bash
sudo mkdir -p /opt/rollcall-api
sudo cp -r . /opt/rollcall-api/
sudo chown -R www:www /opt/rollcall-api
sudo chmod 600 /opt/rollcall-api/secret.json

sudo cp rollcall-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rollcall-api
systemctl status rollcall-api
```

`rollcall-api.service` 里假设了用户 `www`、目录 `/opt/rollcall-api`，按实际改。

## 反向代理（nginx）

把 `nginx-rollcall.conf` 放进站点的 `include` 目录（或直接贴进 `server {}` 块），
它只代理 `/api/` 前缀，其余路径行为不变；删掉这段配置就能完全回滚。

```nginx
location ^~ /api/ {
    proxy_pass http://127.0.0.1:8120;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;   # 后台靠它判断是否强制 HTTPS
    ...
}
```

> 注意：`/api/admin` 在 `X-Forwarded-Proto: http` 时会 302 到 HTTPS。
> 纯 HTTP 内网测试时直接用 `http://127.0.0.1:8120/api/admin`（不带该头）即可。

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| `GET` | `/api/health` | 无 | 返回 `ok` |
| `POST` | `/api/rollcall/sync?k=<sync_key>&n=<客户端人数>` | 同步密钥 | 返回名单+权重，纯文本 |
| `GET` | `/api/rollcall/version` | 无 | 热更新清单（`data/version.txt` 原样返回） |
| `GET` | `/api/rollcall/stream?k=<sync_key>` | 同步密钥 | **SSE 长连接**：`rev` 一变就推 `event: changed` |
| `GET` | `/api/rollcall/rev?k=<sync_key>` | 同步密钥 | 只返回 `rev=N`（几十字节），轮询兜底用 |
| `POST` | `/api/admin/login` | 表单 `token` | 成功后种 Cookie |
| `GET` | `/api/admin` | Cookie | 网页后台 |
| `GET` | `/api/admin/state` | Cookie | 当前名单/权重 JSON |
| `POST` | `/api/admin/save` | Cookie + `X-RC: 1` | `{"weights":[...]}` |
| `POST` | `/api/admin/roster` | Cookie + `X-RC: 1` | `{"names":[...]}`，整份替换，同名继承权重 |
| `POST` | `/api/admin/logout` | Cookie + `X-RC: 1` | 退出 |

`/api/rollcall/sync` 的响应格式（纯文本，UTF-8）：

```
RC1
v=<状态版本号>
w=1,1,5,0.3,...
n=张三|李四|王五|...
```

## 秒级推送（SSE）

`/api/rollcall/stream` 是一条 **Server-Sent Events** 长连接：

- 鉴权用同步密钥（客户端不是浏览器，没有 Cookie）
- 状态里的 `rev` 每次保存/改名单都会 +1，一变就推 `event: changed`
- 每 20 秒发一次 `: ping` 心跳，防止中间设备掐连接
- 单连接最长 1800 秒，到点客户端会重连；同时最多 16 条

客户端收到 `changed` 后立刻重新走一次 `/api/rollcall/sync` 拉数据（推送只做"通知"，不传业务数据）。

> nginx 必须 `proxy_buffering off;` 且 `proxy_read_timeout` 够长，否则推送会被缓存住。
>
> 客户端还有一层兜底：长连接连续 3 次连不上（校园网常见——只放行系统代理的流量，
> 裸 socket 出不去），就退化成每 3 秒轮询一次 `/api/rollcall/rev`，约 5 分钟后再试长连接。

## 安全'

- `secret.json` 存的是 **Token 的 SHA-256**，不是 Token 原文；泄露文件不等于泄露 Token
- 文件权限必须 `600`，且服务以非 root 用户运行（单元里是 `User=www`）
- `X-RC: 1` 是写操作的 CSRF 防护头，浏览器跨站表单带不上这个头
- 登录有按 IP 的失败计数与锁定（`_login_fail`）
- 建议全站 HTTPS；同步接口的密钥走的是请求参数，明文 HTTP 下会被中间人看到

## 热更新清单

`data/version.txt` 直接原样返回给客户端，格式：

```
ver=1.0.6
url=http://example.com/download/rollcall/ClassRollCall_v1.0.6.exe
alt=https://cdn.example.com/download/rollcall/ClassRollCall_v1.0.6.exe
sha256=<64 位十六进制>
```

客户端下载后按 `sha256` 校验，存成 `pending.exe`，**退出时**替换自身。
升级时记得**换文件名**（带版本号），否则 CDN 可能还在发旧字节。
