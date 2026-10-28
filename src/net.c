#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <winhttp.h>
#include <iphlpapi.h>
#include <icmpapi.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "net.h"

extern const wchar_t *g_appVersion;

#define SYNC_PORT   80
#define PING_MS     100   /* 需求：100ms 内拿到 ping 结果 */
#define TCP_MS      350   /* ICMP 被墙时的兜底探测 */
#define CONNECT_MS  800
#define WATCH_IDLE_MS 45000  /* SSE 收不到任何字节就算断线（服务器 20s 一次心跳）*/
#define DNS_MS      1000
#define RESP_MAX    16384 /* 128 个 UTF-8 名字也放得下 */

static int EnsureWinsock(void)
{
    static int inited = 0;
    WSADATA wd;
    if (!inited) {
        if (WSAStartup(MAKEWORD(2, 2), &wd) != 0) return 0;
        inited = 1;
    }
    return 1;
}

static int ParseIPv4(const wchar_t *s, IN_ADDR *out)
{
    return InetPtonW(AF_INET, s, out) == 1;
}

static int PingIPv4(DWORD ip, int timeoutMs)
{
    HANDLE h;
    unsigned char reply[sizeof(ICMP_ECHO_REPLY) + 64];
    DWORD n;

    h = IcmpCreateFile();
    if (h == INVALID_HANDLE_VALUE) return 0;

    n = IcmpSendEcho(h, ip, "rc", 2, NULL, reply, sizeof(reply), (DWORD)timeoutMs);
    IcmpCloseHandle(h);
    return n > 0;
}

static int TcpProbe(DWORD ip, int port, int timeoutMs)
{
    SOCKET s;
    struct sockaddr_in sa;
    fd_set wr, ex;
    struct timeval tv;
    u_long nb = 1;
    int ok = 0;

    s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) return 0;

    ZeroMemory(&sa, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons((u_short)port);
    sa.sin_addr.s_addr = ip;

    ioctlsocket(s, FIONBIO, &nb);
    connect(s, (struct sockaddr *)&sa, sizeof(sa));

    FD_ZERO(&wr);
    FD_SET(s, &wr);
    FD_ZERO(&ex);
    FD_SET(s, &ex);
    tv.tv_sec = timeoutMs / 1000;
    tv.tv_usec = (timeoutMs % 1000) * 1000;

    if (select(0, NULL, &wr, &ex, &tv) > 0 && FD_ISSET(s, &wr)) ok = 1;

    closesocket(s);
    return ok;
}

static int ReadAll(HINTERNET hr, char *resp, int cap)
{
    DWORD total = 0;
    for (;;) {
        DWORD rd = 0;
        if (!WinHttpReadData(hr, resp + total, (DWORD)(cap - 1 - total), &rd)) return 0;
        if (rd == 0) break;
        total += rd;
        if (total >= (DWORD)(cap - 1)) break;
    }
    resp[total] = 0;
    return 1;
}

static void ParseWeights(const char *p, NetSyncResult *out)
{
    int i = 0;
    while (i < NET_MAX_STUDENTS && *p) {
        char *end = NULL;
        double v = strtod(p, &end);
        if (end == p) break;
        if (v < 0) v = 0;
        if (v > 1000) v = 1000;
        out->w[i++] = v;
        p = end;
        while (*p == ',' || *p == ' ' || *p == '\r' || *p == '\n') p++;
    }
    out->wCount = i;
}

/* 名单是 UTF-8、用 '|' 分隔 */
static void ParseNames(const char *p, NetSyncResult *out)
{
    int n = 0;
    while (*p && n < NET_MAX_STUDENTS) {
        const char *q = strchr(p, '|');
        int len = q ? (int)(q - p) : (int)strlen(p);
        if (len > 0) {
            int wlen = MultiByteToWideChar(CP_UTF8, 0, p, len, out->names[n], NET_NAME_WCHARS - 1);
            if (wlen > 0) {
                out->names[n][wlen] = 0;
                n++;
            }
        }
        if (!q) break;
        p = q + 1;
    }
    out->nCount = n;
}

/* 取一条 "key=值" 的 UTF-8 文本行并转成宽字符；返回值 1 表示这行存在 */
static int ParseWideLine(const char *resp, const char *key, wchar_t *out, int cap)
{
    const char *p, *e;
    int n;

    if (cap > 0) out[0] = 0;
    p = strstr(resp, key);
    if (!p) return 0;
    p += strlen(key);
    e = p;
    while (*e && *e != '\r' && *e != '\n') e++;
    n = (int)(e - p);
    if (n > 0 && cap > 1) {
        if (n > cap - 1) n = cap - 1;
        MultiByteToWideChar(CP_UTF8, 0, p, n, out, cap - 1);
        out[n] = 0;
    }
    return 1;
}
static int ParseResponse(const char *resp, NetSyncResult *out)
{
    const char *pv = strstr(resp, "\nv=");
    const char *pw = strstr(resp, "\nw=");
    const char *pn = strstr(resp, "\nn=");

    if (strncmp(resp, "RC1", 3) != 0) return 0;
    if (!pw) return 0;

    if (pv && pv < pw) out->ver = _strtoi64(pv + 3, NULL, 10);

    {
        const char *prs = strstr(resp, "\nrs=");
        const char *phd = strstr(resp, "\nhd=");
        const char *pmp = strstr(resp, "\nmp=");
        const char *pdc = strstr(resp, "\ndc=");
        const char *pwm = strstr(resp, "\nwm=");
        const char *pnx = strstr(resp, "\nnxi=");
        if (prs) out->roundSeconds = (int)_strtoi64(prs + 4, NULL, 10);
        if (phd) out->histDraws = (int)_strtoi64(phd + 4, NULL, 10);
        if (pmp) out->maxPick = (int)_strtoi64(pmp + 4, NULL, 10);
        if (pdc) out->defaultCount = (int)_strtoi64(pdc + 4, NULL, 10);
        if (pwm) out->weightMax = (int)_strtoi64(pwm + 4, NULL, 10);
        if (pnx) out->nextId = (int)_strtoi64(pnx + 5, NULL, 10);
        ParseWideLine(resp, "\ncn=", out->className, 64);
        ParseWideLine(resp, "\nnx=", out->nextName, NET_NAME_WCHARS);
    }

    ParseWeights(pw + 3, out);
    if (pn) ParseNames(pn + 3, out);

    return out->wCount > 0 || out->nCount > 0;
}

static int HttpGetSmall(HINTERNET hc, LPCWSTR extraHdr, LPCWSTR path,
                        char *resp, int cap)
{
    HINTERNET hr;
    DWORD code = 0, len = sizeof(code);
    int ok = 0, total = 0;

    hr = WinHttpOpenRequest(hc, L"GET", path, NULL, WINHTTP_NO_REFERER,
                            WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
    if (!hr) return 0;

    if (extraHdr)
        WinHttpAddRequestHeaders(hr, extraHdr, (DWORD)-1,
                                 WINHTTP_ADDREQ_FLAG_ADD | WINHTTP_ADDREQ_FLAG_REPLACE);

    if (WinHttpSendRequest(hr, WINHTTP_NO_ADDITIONAL_HEADERS, 0, NULL, 0, 0, 0) &&
        WinHttpReceiveResponse(hr, NULL) &&
        WinHttpQueryHeaders(hr, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                            WINHTTP_HEADER_NAME_BY_INDEX, &code, &len, WINHTTP_NO_HEADER_INDEX) &&
        code == 200) {
        for (;;) {
            DWORD rd = 0;
            if (!WinHttpReadData(hr, resp + total, (DWORD)(cap - 1 - total), &rd)) break;
            if (rd == 0) { ok = 1; break; }
            total += (int)rd;
            if (total >= cap - 1) { ok = 1; break; }
        }
        resp[total] = 0;
    }

    WinHttpCloseHandle(hr);
    return ok;
}
static int HttpPost(HINTERNET hc, LPCWSTR extraHdr, LPCWSTR path,
                    const char *body, char *resp, int cap)
{
    HINTERNET hr;
    DWORD code = 0, len = sizeof(code);
    int ok = 0;

    hr = WinHttpOpenRequest(hc, L"POST", path, NULL, WINHTTP_NO_REFERER,
                            WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
    if (!hr) return 0;

    if (extraHdr)
        WinHttpAddRequestHeaders(hr, extraHdr, (DWORD)-1,
                                 WINHTTP_ADDREQ_FLAG_ADD | WINHTTP_ADDREQ_FLAG_REPLACE);

    if (WinHttpSendRequest(hr, L"Content-Type: text/plain\r\n", (DWORD)-1,
                           (LPVOID)body, (DWORD)strlen(body), (DWORD)strlen(body), 0) &&
        WinHttpReceiveResponse(hr, NULL)) {

        if (WinHttpQueryHeaders(hr, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                                WINHTTP_HEADER_NAME_BY_INDEX, &code, &len,
                                WINHTTP_NO_HEADER_INDEX) && code == 200) {
            ok = ReadAll(hr, resp, cap);
        }
    }

    WinHttpCloseHandle(hr);
    return ok;
}

int Net_Sync(const wchar_t *host, const wchar_t *ip, const wchar_t *key, int count,
             NetSyncResult *out)
{
    wchar_t path[256];
    wchar_t hostHdr[160];
    char body[16];
    static char resp[RESP_MAX];
    IN_ADDR addr;
    HINTERNET hs = NULL;
    int ok = 0;

    if (!out) return NET_FAILED;
    ZeroMemory(out, sizeof(*out));

    if (!EnsureWinsock()) return NET_UNREACH;

    wsprintfW(path, L"/api/rollcall/sync?k=%s&n=%d&c=%s", key, count, g_appVersion ? g_appVersion : L"0");
    wsprintfW(hostHdr, L"Host: %s", host);
    strcpy(body, "RC1\n");

    hs = WinHttpOpen(L"XananRollCall/1.0", WINHTTP_ACCESS_TYPE_NO_PROXY,
                     WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!hs) return NET_FAILED;

    WinHttpSetTimeouts(hs, DNS_MS, CONNECT_MS, 1500, 2500);

    if (ParseIPv4(ip, &addr)) {
        /* 1) ping 100ms 作为快筛；ICMP 被墙就补一次 350ms 的 TCP 探测 */
        if (!PingIPv4(addr.S_un.S_addr, PING_MS) &&
            !TcpProbe(addr.S_un.S_addr, SYNC_PORT, TCP_MS)) {
            WinHttpCloseHandle(hs);
            return NET_UNREACH;
        }

        /* 2) 直连 IP + Host 头伪装域名：完全不查 DNS */
        {
            HINTERNET hc = WinHttpConnect(hs, ip, SYNC_PORT, 0);
            if (hc) {
                if (HttpPost(hc, hostHdr, path, body, resp, sizeof(resp)))
                    ok = ParseResponse(resp, out);
                WinHttpCloseHandle(hc);
            }
        }
        if (ok) { WinHttpCloseHandle(hs); return NET_OK; }
    }

    /* 3) DNS 兜底（顺带支持系统代理）*/
    {
        HINTERNET hs2 = WinHttpOpen(L"XananRollCall/1.0", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                                    WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
        if (hs2) {
            HINTERNET hc;
            WinHttpSetTimeouts(hs2, DNS_MS, CONNECT_MS, 1500, 2500);
            hc = WinHttpConnect(hs2, host, SYNC_PORT, 0);
            if (hc) {
                if (HttpPost(hc, NULL, path, body, resp, sizeof(resp)))
                    ok = ParseResponse(resp, out);
                WinHttpCloseHandle(hc);
            }
            WinHttpCloseHandle(hs2);
        }
    }

    WinHttpCloseHandle(hs);
    return ok ? NET_OK : NET_FAILED;
}

/* ---------------- 长连接监听（SSE，裸 socket） ----------------
 * 不用 WinHTTP：它对没有 Content-Length / 分块的响应会缓冲到连接结束才吐数据，
 * 实测 WinHttpReadData 十几秒都拿不到字节。直接用 recv()，天生边到边给。 */

static SOCKET ConnectTcp(DWORD ip, int port, int timeoutMs)
{
    SOCKET s;
    struct sockaddr_in sa;
    u_long nb = 1;

    s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) return INVALID_SOCKET;

    ZeroMemory(&sa, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons((u_short)port);
    sa.sin_addr.s_addr = ip;

    ioctlsocket(s, FIONBIO, &nb);

    if (connect(s, (struct sockaddr *)&sa, sizeof(sa)) == SOCKET_ERROR) {
        if (WSAGetLastError() != WSAEWOULDBLOCK) { closesocket(s); return INVALID_SOCKET; }
        {
            fd_set wf, ef;
            struct timeval tv;
            FD_ZERO(&wf); FD_SET(s, &wf);
            FD_ZERO(&ef); FD_SET(s, &ef);
            tv.tv_sec = timeoutMs / 1000;
            tv.tv_usec = (timeoutMs % 1000) * 1000;
            if (select((int)s + 1, NULL, &wf, &ef, &tv) <= 0) { closesocket(s); return INVALID_SOCKET; }
            {
                int err = 0, len = sizeof(err);
                getsockopt(s, SOL_SOCKET, SO_ERROR, (char *)&err, &len);
                if (err != 0) { closesocket(s); return INVALID_SOCKET; }
            }
        }
    }

    nb = 0;
    ioctlsocket(s, FIONBIO, &nb);
    return s;
}

int Net_Watch(const wchar_t *host, const wchar_t *ip, const wchar_t *key,
              volatile LONG *stop, void (*onChange)(void *), void *ctx)
{
    SOCKET s = INVALID_SOCKET;
    struct in_addr addr;
    char keyA[80], hostA[160], req[512];
    char buf[512], carry[32], head[64];
    int carryLen = 0, headLen = 0, gotStatus = 0, connected = 0, rc = 1;
    DWORD last = GetTickCount();

    if (!EnsureWinsock()) return 1;
    if (!ParseIPv4(ip, &addr)) return 1;

    WideCharToMultiByte(CP_ACP, 0, key, -1, keyA, sizeof(keyA), NULL, NULL);
    WideCharToMultiByte(CP_ACP, 0, host, -1, hostA, sizeof(hostA), NULL, NULL);

    s = ConnectTcp(addr.S_un.S_addr, SYNC_PORT, TCP_MS * 3);
    if (s == INVALID_SOCKET) return 1;

    _snprintf(req, sizeof(req) - 1,
              "GET /api/rollcall/stream?k=%s HTTP/1.1\r\n"
              "Host: %s\r\n"
              "Accept: text/event-stream\r\n"
              "User-Agent: XananRollCall/1.0\r\n"
              "Connection: close\r\n\r\n",
              keyA, hostA);
    req[sizeof(req) - 1] = 0;
    if (send(s, req, (int)strlen(req), 0) <= 0) { closesocket(s); return 1; }

    for (;;) {
        fd_set rf;
        struct timeval tv;

        if (stop && InterlockedExchangeAdd(stop, 0)) { rc = 0; break; }

        FD_ZERO(&rf);
        FD_SET(s, &rf);
        tv.tv_sec = 1;
        tv.tv_usec = 0;

        {
            int sel = select((int)s + 1, &rf, NULL, NULL, &tv);
            if (sel == 0) {
                if (GetTickCount() - last > WATCH_IDLE_MS) { rc = connected ? 2 : 1; break; }
                continue;
            }
            if (sel < 0) { rc = connected ? 2 : 1; break; }
        }

        {
            int rd = recv(s, buf, (int)sizeof(buf) - 1, 0);
            if (rd <= 0) { rc = connected ? 2 : 1; break; }
            last = GetTickCount();

            /* 先把状态行取出来：不是 200 就别当长连接用 */
            if (!gotStatus) {
                int copy = rd < (int)sizeof(head) - 1 - headLen ? rd : (int)sizeof(head) - 1 - headLen;
                if (copy > 0) {
                    memcpy(head + headLen, buf, copy);
                    headLen += copy;
                    head[headLen] = 0;
                }
                if (headLen >= 12 || strstr(head, "\r\n")) {
                    gotStatus = 1;
                    if (!strstr(head, " 200")) { rc = 1; break; }
                    connected = 1;
                }
            }

            buf[rd] = 0;
            if (carryLen) {
                char tmp[600];
                int total = carryLen + rd;
                if (total > (int)sizeof(tmp) - 1) total = (int)sizeof(tmp) - 1;
                memcpy(tmp, carry, carryLen);
                memcpy(tmp + carryLen, buf, rd);
                tmp[total] = 0;
                if (strstr(tmp, "changed")) {
                    if (onChange) onChange(ctx);
                }
                carryLen = total >= 16 ? 16 : total;
                memcpy(carry, tmp + total - carryLen, carryLen);
            } else {
                if (strstr(buf, "changed")) {
                    if (onChange) onChange(ctx);
                }
                carryLen = rd >= 16 ? 16 : rd;
                memcpy(carry, buf + rd - carryLen, carryLen);
            }
        }
    }

    closesocket(s);
    return rc;
}

/* ---------------- 轮询兜底：只取 rev ---------------- */

int Net_FetchRev(const wchar_t *host, const wchar_t *ip, const wchar_t *key, long long *rev)
{
    wchar_t path[256] = L"", hostHdr[160] = L"";
    char resp[256];
    int ok = 0;

    if (!rev) return 0;
    if (!EnsureWinsock()) return 0;

    wsprintfW(path, L"/api/rollcall/rev?k=%s", key);
    wsprintfW(hostHdr, L"Host: %s", host);

    resp[0] = 0;

    {
        HINTERNET hs = WinHttpOpen(L"XananRollCall/1.0", WINHTTP_ACCESS_TYPE_NO_PROXY,
                                   WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
        if (hs) {
            HINTERNET hc;
            WinHttpSetTimeouts(hs, DNS_MS, CONNECT_MS, 1500, 2500);
            {
                IN_ADDR tmp;
                const wchar_t *use = ParseIPv4(ip, &tmp) ? ip : host;
                hc = WinHttpConnect(hs, use, SYNC_PORT, 0);
            }
            if (hc) {
                if (HttpGetSmall(hc, hostHdr, path, resp, sizeof(resp))) ok = 1;
                WinHttpCloseHandle(hc);
            }
            WinHttpCloseHandle(hs);
        }
    }

    /* 直连失败就走域名 + 系统代理（校园网常见） */
    if (!ok) {
        HINTERNET hs2 = WinHttpOpen(L"XananRollCall/1.0", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                                    WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
        if (hs2) {
            HINTERNET hc2;
            WinHttpSetTimeouts(hs2, DNS_MS, CONNECT_MS, 1500, 2500);
            hc2 = WinHttpConnect(hs2, host, SYNC_PORT, 0);
            if (hc2) {
                if (HttpGetSmall(hc2, NULL, path, resp, sizeof(resp))) ok = 1;
                WinHttpCloseHandle(hc2);
            }
            WinHttpCloseHandle(hs2);
        }
    }

    if (!ok) return 0;
    {
        char *p = strstr(resp, "rev=");
        if (!p) return 0;
        *rev = _strtoi64(p + 4, NULL, 10);
    }
    return 1;
}