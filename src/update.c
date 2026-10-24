#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <winhttp.h>
#include <shellapi.h>
#include <shlobj.h>
#include <wincrypt.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "update.h"

/* 由 main.c 提供 */
extern const wchar_t *g_appVersion;
extern const wchar_t *g_syncIp;

#define MANIFEST_HOST L"example.com"
#define MANIFEST_PATH L"/api/rollcall/version"

/* ---------------- 路径 ---------------- */

static void StateDir(wchar_t *buf, int cap)
{
    wchar_t dir[MAX_PATH] = L"";
    (void)cap;
    if (FAILED(SHGetFolderPathW(NULL, CSIDL_APPDATA, NULL, 0, dir)) || !dir[0]) {
        GetTempPathW(MAX_PATH, dir);
    }
    wsprintfW(buf, L"%s\\XananRollCall", dir);
    CreateDirectoryW(buf, NULL);
}

static void PendingPath(wchar_t *buf, int cap)
{
    wchar_t dir[MAX_PATH];
    (void)cap;
    StateDir(dir, MAX_PATH);
    wsprintfW(buf, L"%s\\pending.exe", dir);
}

/* ---------------- 版本号 ---------------- */

static void ParseVer(const wchar_t *s, int v[3])
{
    int i = 0;
    v[0] = v[1] = v[2] = 0;
    while (*s && i < 3) {
        int n = 0, any = 0;
        while (*s >= L'0' && *s <= L'9') { n = n * 10 + (*s - L'0'); s++; any = 1; }
        if (any) v[i++] = n;
        while (*s && *s != L'.') s++;
        if (*s == L'.') s++;
        else break;
    }
}

static int CmpVer(const wchar_t *a, const wchar_t *b)
{
    int va[3], vb[3], i;
    ParseVer(a, va);
    ParseVer(b, vb);
    for (i = 0; i < 3; i++) {
        if (va[i] != vb[i]) return va[i] < vb[i] ? -1 : 1;
    }
    return 0;
}

/* ---------------- HTTP ---------------- */

/* 把 http(s)://host[:port]/path 拆开 */
static BOOL SplitUrl(const wchar_t *url, int *secure, wchar_t *host, int hostCap,
                     INTERNET_PORT *port, wchar_t *path, int pathCap)
{
    const wchar_t *p = url;
    const wchar_t *h;
    int i;

    *secure = 0;
    if (wcsncmp(p, L"http://", 7) == 0) { p += 7; *port = 80; }
    else if (wcsncmp(p, L"https://", 8) == 0) { p += 8; *port = 443; *secure = 1; }
    else return FALSE;

    h = p;
    while (*p && *p != L'/' && *p != L':') p++;

    i = 0;
    while (h < p && i < hostCap - 1) host[i++] = *h++;
    host[i] = 0;
    if (!host[0]) return FALSE;

    if (*p == L':') {
        int n = 0;
        p++;
        while (*p >= L'0' && *p <= L'9') { n = n * 10 + (*p - L'0'); p++; }
        if (n > 0) *port = (INTERNET_PORT)n;
    }

    if (*p == L'/') {
        i = 0;
        while (*p && i < pathCap - 1) path[i++] = *p++;
        path[i] = 0;
    } else {
        lstrcpynW(path, L"/", pathCap);
    }
    return TRUE;
}

/* 连服务器；MANIFEST_HOST 走写死的 IP（不依赖 DNS），其它域名走正常解析 */
static HINTERNET ConnectHost(HINTERNET hs, const wchar_t *host, INTERNET_PORT port)
{
    if (lstrcmpiW(host, MANIFEST_HOST) == 0 && g_syncIp)
        return WinHttpConnect(hs, g_syncIp, port, 0);
    return WinHttpConnect(hs, host, port, 0);
}

static void AddHostHeader(HINTERNET hr, const wchar_t *host)
{
    wchar_t buf[160];
    wsprintfW(buf, L"Host: %s", host);
    WinHttpAddRequestHeaders(hr, buf, (DWORD)-1,
                             WINHTTP_ADDREQ_FLAG_ADD | WINHTTP_ADDREQ_FLAG_REPLACE);
}

/* GET 到内存（清单用） */
static BOOL HttpGetText(const wchar_t *url, char *buf, int cap)
{
    int secure = 0;
    INTERNET_PORT port = 80;
    wchar_t host[128], path[512];
    HINTERNET hs = NULL, hc = NULL, hr = NULL;
    BOOL ok = FALSE;
    DWORD code = 0, len = sizeof(code), total = 0;

    if (!SplitUrl(url, &secure, host, 128, &port, path, 512)) return FALSE;

    hs = WinHttpOpen(L"XananRollCall/1.0", WINHTTP_ACCESS_TYPE_NO_PROXY,
                     WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!hs) return FALSE;
    WinHttpSetTimeouts(hs, 1500, 1500, 2000, 3000);

    hc = ConnectHost(hs, host, port);
    if (!hc) goto done;

    hr = WinHttpOpenRequest(hc, L"GET", path, NULL, WINHTTP_NO_REFERER,
                            WINHTTP_DEFAULT_ACCEPT_TYPES, secure ? WINHTTP_FLAG_SECURE : 0);
    if (!hr) goto done;
    AddHostHeader(hr, host);

    if (!WinHttpSendRequest(hr, WINHTTP_NO_ADDITIONAL_HEADERS, 0, NULL, 0, 0, 0)) goto done;
    if (!WinHttpReceiveResponse(hr, NULL)) goto done;
    if (!WinHttpQueryHeaders(hr, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                             WINHTTP_HEADER_NAME_BY_INDEX, &code, &len, WINHTTP_NO_HEADER_INDEX))
        goto done;
    if (code != 200) goto done;

    for (;;) {
        DWORD rd = 0;
        if (!WinHttpReadData(hr, buf + total, (DWORD)(cap - 1 - total), &rd)) goto done;
        if (rd == 0) break;
        total += rd;
        if (total >= (DWORD)(cap - 1)) break;
    }
    buf[total] = 0;
    ok = TRUE;

done:
    if (hr) WinHttpCloseHandle(hr);
    if (hc) WinHttpCloseHandle(hc);
    if (hs) WinHttpCloseHandle(hs);
    return ok;
}

/* GET 到文件（新 exe 用） */
static BOOL HttpDownload(const wchar_t *url, const wchar_t *dest)
{
    int secure = 0;
    INTERNET_PORT port = 80;
    wchar_t host[128], path[512];
    HINTERNET hs = NULL, hc = NULL, hr = NULL;
    FILE *f = NULL;
    BOOL ok = FALSE;
    DWORD code = 0, len = sizeof(code);
    char buf[8192];

    if (!SplitUrl(url, &secure, host, 128, &port, path, 512)) return FALSE;

    hs = WinHttpOpen(L"XananRollCall/1.0", WINHTTP_ACCESS_TYPE_NO_PROXY,
                     WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!hs) return FALSE;
    WinHttpSetTimeouts(hs, 2000, 2500, 3000, 6000);

    hc = ConnectHost(hs, host, port);
    if (!hc) goto done;

    hr = WinHttpOpenRequest(hc, L"GET", path, NULL, WINHTTP_NO_REFERER,
                            WINHTTP_DEFAULT_ACCEPT_TYPES, secure ? WINHTTP_FLAG_SECURE : 0);
    if (!hr) goto done;
    AddHostHeader(hr, host);

    if (!WinHttpSendRequest(hr, WINHTTP_NO_ADDITIONAL_HEADERS, 0, NULL, 0, 0, 0)) goto done;
    if (!WinHttpReceiveResponse(hr, NULL)) goto done;
    if (!WinHttpQueryHeaders(hr, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                             WINHTTP_HEADER_NAME_BY_INDEX, &code, &len, WINHTTP_NO_HEADER_INDEX))
        goto done;
    if (code != 200) goto done;

    f = _wfopen(dest, L"wb");
    if (!f) goto done;

    for (;;) {
        DWORD rd = 0;
        if (!WinHttpReadData(hr, buf, sizeof(buf), &rd)) goto done;
        if (rd == 0) break;
        if (fwrite(buf, 1, rd, f) != rd) goto done;
    }
    ok = TRUE;

done:
    if (f) fclose(f);
    if (hr) WinHttpCloseHandle(hr);
    if (hc) WinHttpCloseHandle(hc);
    if (hs) WinHttpCloseHandle(hs);
    return ok;
}

/* ---------------- SHA-256 ---------------- */

static BOOL Sha256File(const wchar_t *path, wchar_t *hex, int cap)
{
    HCRYPTPROV prov = 0;
    HCRYPTHASH hash = 0;
    FILE *f = NULL;
    BYTE buf[8192];
    BYTE digest[32];
    DWORD dlen = sizeof(digest), i, n;
    BOOL ok = FALSE;
    int pos = 0;

    if (!CryptAcquireContextW(&prov, NULL, NULL, PROV_RSA_AES, CRYPT_VERIFYCONTEXT)) return FALSE;
    if (!CryptCreateHash(prov, CALG_SHA_256, 0, 0, &hash)) goto done;

    f = _wfopen(path, L"rb");
    if (!f) goto done;

    while ((n = (DWORD)fread(buf, 1, sizeof(buf), f)) > 0) {
        if (!CryptHashData(hash, buf, n, 0)) goto done;
    }
    if (!CryptGetHashParam(hash, HP_HASHVAL, digest, &dlen, 0)) goto done;

    if (pos + 64 >= cap) goto done;
    for (i = 0; i < dlen; i++)
        wsprintfW(hex + i * 2, L"%02x", digest[i]);
    hex[dlen * 2] = 0;
    ok = TRUE;

done:
    if (f) fclose(f);
    if (hash) CryptDestroyHash(hash);
    if (prov) CryptReleaseContext(prov, 0);
    return ok;
}

/* ---------------- 清单与下载 ---------------- */

/* 从 "key=value" 里取一行 */
static BOOL GetField(const char *text, const char *key, char *out, int cap)
{
    const char *p = text;
    size_t klen = strlen(key);
    while (*p) {
        if (strncmp(p, key, klen) == 0 && p[klen] == '=') {
            const char *v = p + klen + 1;
            int i = 0;
            while (*v && *v != '\r' && *v != '\n' && i < cap - 1) out[i++] = *v++;
            out[i] = 0;
            return TRUE;
        }
        while (*p && *p != '\n') p++;
        if (*p == '\n') p++;
    }
    return FALSE;
}

static BOOL DownloadAndVerify(const wchar_t *url, const wchar_t *tmp, const wchar_t *sha);

static void DoCheck(void)
{
    static char manifest[2048];
    char ver[64], url[512], alt[512], sha[128];
    wchar_t wver[64], wurl[512], walt[512], wsha[128], tmp[MAX_PATH], pending[MAX_PATH];

    if (!HttpGetText(L"http://" MANIFEST_HOST MANIFEST_PATH, manifest, sizeof(manifest))) return;
    if (!GetField(manifest, "ver", ver, sizeof(ver))) return;
    if (!GetField(manifest, "url", url, sizeof(url))) return;

    MultiByteToWideChar(CP_UTF8, 0, ver, -1, wver, 64);
    if (CmpVer(wver, g_appVersion) <= 0) return;   /* 已是最新 */

    MultiByteToWideChar(CP_UTF8, 0, url, -1, wurl, 512);
    alt[0] = 0;
    if (GetField(manifest, "alt", alt, sizeof(alt))) MultiByteToWideChar(CP_UTF8, 0, alt, -1, walt, 512);
    else walt[0] = 0;

    sha[0] = 0;
    if (GetField(manifest, "sha256", sha, sizeof(sha))) MultiByteToWideChar(CP_UTF8, 0, sha, -1, wsha, 128);
    else wsha[0] = 0;

    PendingPath(pending, MAX_PATH);
    wsprintfW(tmp, L"%s.dl", pending);

    /* 先试 CDN 镜像，失败或校验不过再回源站；两条都按 sha256 校验 */
    if (!DownloadAndVerify(walt, tmp, wsha) && !DownloadAndVerify(wurl, tmp, wsha)) {
        DeleteFileW(tmp);
        return;
    }

    MoveFileExW(tmp, pending, MOVEFILE_REPLACE_EXISTING);
}

static BOOL DownloadAndVerify(const wchar_t *url, const wchar_t *tmp, const wchar_t *sha)
{
    wchar_t hex[128];

    if (!url || !url[0]) return FALSE;

    DeleteFileW(tmp);
    if (!HttpDownload(url, tmp)) return FALSE;

    if (sha && sha[0]) {
        if (!Sha256File(tmp, hex, 128) || lstrcmpiW(hex, sha) != 0) return FALSE;
    }
    return TRUE;
}

static DWORD WINAPI CheckThread(LPVOID param)
{
    (void)param;
    DoCheck();
    return 0;
}

void Update_StartCheck(void)
{
    HANDLE th = CreateThread(NULL, 0, CheckThread, NULL, 0, NULL);
    if (th) CloseHandle(th);
}

/* ---------------- 退出时替换 ---------------- */

BOOL Update_ApplyPending(void)
{
    wchar_t pending[MAX_PATH], exe[MAX_PATH], tmpdir[MAX_PATH], helper[MAX_PATH], cmd[2048];
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    DWORD pid = GetCurrentProcessId();

    PendingPath(pending, MAX_PATH);
    if (GetFileAttributesW(pending) == INVALID_FILE_ATTRIBUTES) return FALSE;

    if (!GetModuleFileNameW(NULL, exe, MAX_PATH)) return FALSE;

    /* 关键：助手不能是"当前这个 exe"——Windows 会锁住正在运行的镜像文件，
     * 那样就永远替换不了自己。先把自身复制到临时目录，再从那里以助手身份运行。 */
    GetTempPathW(MAX_PATH, tmpdir);
    wsprintfW(helper, L"%sXananRollCallHelper.exe", tmpdir);
    if (!CopyFileW(exe, helper, FALSE)) {
        wsprintfW(helper, L"%sXananRollCallHelper_%lu.exe", tmpdir, pid);
        if (!CopyFileW(exe, helper, FALSE)) return FALSE;
    }

    wsprintfW(cmd, L"\"%s\" --apply-update %lu \"%s\" \"%s\"", helper, pid, pending, exe);

    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    ZeroMemory(&pi, sizeof(pi));

    if (!CreateProcessW(helper, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi))
        return FALSE;

    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return TRUE;
}

int Update_HelperMain(LPWSTR cmdline)
{
    int argc = 0;
    LPWSTR *argv = CommandLineToArgvW(cmdline, &argc);
    wchar_t pending[MAX_PATH], target[MAX_PATH];
    HANDLE h;
    DWORD pid = 0;
    int i, tries;

    if (!argv || argc < 4) {
        if (argv) LocalFree(argv);
        return 1;
    }

    pid = (DWORD)_wtoi(argv[2]);
    lstrcpynW(pending, argv[3], MAX_PATH);
    lstrcpynW(target, argv[4], MAX_PATH);

    if (pid) {
        h = OpenProcess(SYNCHRONIZE, FALSE, pid);
        if (h) {
            WaitForSingleObject(h, 30000);
            CloseHandle(h);
        }
    }

    /* 等原进程彻底退出、文件句柄释放 */
    for (i = 0; i < 20; i++) {
        if (CopyFileW(pending, target, FALSE)) break;
        Sleep(250);
    }

    if (GetFileAttributesW(pending) != INVALID_FILE_ATTRIBUTES) {
        for (tries = 0; tries < 4; tries++) {
            if (DeleteFileW(pending)) break;
            Sleep(200);
        }
    }

    LocalFree(argv);
    return 0;
}
