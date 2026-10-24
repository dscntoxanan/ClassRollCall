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

#define SYNC_PORT   80
#define PING_MS     100   /* 需求：100ms 内拿到 ping 结果 */
#define TCP_MS      350   /* ICMP 被墙时的兜底探测 */
#define CONNECT_MS  800
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

static int ParseResponse(const char *resp, NetSyncResult *out)
{
    const char *pv = strstr(resp, "\nv=");
    const char *pw = strstr(resp, "\nw=");
    const char *pn = strstr(resp, "\nn=");

    if (strncmp(resp, "RC1", 3) != 0) return 0;
    if (!pw) return 0;

    if (pv && pv < pw) out->ver = _strtoi64(pv + 3, NULL, 10);

    ParseWeights(pw + 3, out);
    if (pn) ParseNames(pn + 3, out);

    return out->wCount > 0 || out->nCount > 0;
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

    wsprintfW(path, L"/api/rollcall/sync?k=%s&n=%d", key, count);
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
