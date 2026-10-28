/* 班级随机点名 —— 单文件 Win32/GDI 界面，无第三方依赖。
 * 每次可抽 1..N 人：同一次内不重复，不同次之间可重复。概率由后台控制。 */

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <windowsx.h>
#include <imm.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>

#include "roster.h"
#include "state.h"
#include "update.h"

#ifndef APP_VERSION
#define APP_VERSION L"1.0.12"
#endif
#include "net.h"

/* 名单容量以网络层/状态层的定义为准，避免三处写死不一致 */
/* 单次最多抽几个人 */
#define MAX_PICK 5
#define ROSTER_MAX NET_MAX_STUDENTS
#define NAME_LEN   NET_NAME_WCHARS
typedef char roster_size_check[(NET_MAX_STUDENTS == STATE_MAX && NET_NAME_WCHARS == STATE_NAME_WCHARS) ? 1 : -1];

#define APP_NAME    L"班级随机点名"
#define CLASS_NAME  L"高一(1)班"

/* 直连用的 IP：服务器地址固定，写死可以完全绕开 DNS（校园网 DNS 经常抽风） */
#define SYNC_IP     L"127.0.0.1"        /* 改成你自己的服务器 IP */
#define SYNC_HOST   L"example.com"      /* 改成你自己的域名 */
#define SYNC_KEY    L"00000000000000000000000000000000" /* 和服务端 secret.json 里的 sync_key 一致 */

#define TIMER_ANIM   1
#define HIST_MAX     20      /* 防重复窗口最多记几次（服务端可调，0 = 关闭）*/
#define MAX_PICK_ABS 20      /* 一次最多抽几人的硬上限 */
#define WM_STATE_CHANGED (WM_APP + 3)   /* 服务器推来"状态变了" */
#define ANIM_TICKS   10
#define ANIM_MS      55
#define WM_SYNC_DONE (WM_APP + 1)

/* ---------- 配色 ---------- */
#define C_BG        RGB(250, 250, 248)
#define C_TEXT      RGB(18, 19, 21)
#define C_SUB       RGB(140, 140, 134)
#define C_BTN       RGB(24, 26, 31)
#define C_BTN_HOVER RGB(0, 0, 0)
#define C_LINE      RGB(230, 228, 222)
#define C_AMBER     RGB(196, 132, 8)

typedef struct {
    RECT rcTitle, rcMinus, rcPlus, rcBtn, rcResult;
    int m;
} Layout;

static HINSTANCE g_inst;
static HWND      g_hwnd;
static HFONT     g_fTitle, g_fSub, g_fName, g_fBtn, g_fGlyph;
static HBRUSH    g_brBg;

static double g_scale = 1.0;

/* 名单以编译进 exe 的为默认，同步到服务器名单后覆盖（并落盘缓存） */
static wchar_t   g_names[ROSTER_MAX][NAME_LEN];
static int       g_n = 0;
static double    g_w[ROSTER_MAX];
static int       g_count = 1;        /* 每次抽几个人 */
static long long g_version = 0;

static int g_result[ROSTER_MAX];
static int g_resultN = 0;

/* 防重复窗口：最近 g_histMax 次抽到的人；g_histN 是已填条数。
 * 两个参数都由服务端下发（后台可调），离线时用本地缓存的值。 */
static unsigned char g_hist[HIST_MAX][ROSTER_MAX];
static int           g_histN = 0;
static int           g_histMax = 3;        /* 0 = 关闭防重复 */
static long long     g_roundStart = 0;     /* 窗口起点（unix 秒） */
static int           g_roundSeconds = 300; /* 窗口多久清空一次 */

/* 其余后台可调项 */
static int           g_maxPick = 5;        /* 一次最多抽几人 */
static int           g_defaultCount = 1;   /* 新机器默认抽几人 */
static int           g_fresh = 0;          /* 1 = 还没读过本地状态（首次运行） */
static wchar_t       g_className[64] = CLASS_NAME;

/* 指定下一位：nextId 变了才算"新的一次指定" */
static wchar_t       g_nextName[NAME_LEN] = L"";
static int           g_nextId = 0;
static int           g_nextUsed = 0;

static int g_animTick = 0;
static int g_animSet[ROSTER_MAX];
static int g_animN = 0;

static int g_hoverBtn = 0, g_hoverMinus = 0, g_hoverPlus = 0, g_pressed = 0;
static int g_tracking = 0;

static wchar_t g_toast[96];
static DWORD   g_toastUntil = 0;

/* 给 update.c 用 */
const wchar_t *g_appVersion = APP_VERSION;
const wchar_t *g_syncIp = SYNC_IP;

static HANDLE        g_syncEvent = NULL;
static volatile LONG g_watchStop = 0;
static volatile LONG g_syncBusy = 0;

static int S(int v) { return (int)(v * g_scale + 0.5); }

/* 单次可选人数上限：最多 5 人，也不会超过总人数 */
static int PickLimit(void)
{
    int lim = g_maxPick;

    if (lim < 1) lim = 1;
    if (lim > MAX_PICK_ABS) lim = MAX_PICK_ABS;
    return g_n < lim ? g_n : lim;
}

/* ---------------- 基础绘制 ---------------- */

static HFONT MakeFont(int px, int weight, const wchar_t *face)
{
    return CreateFontW(-px, 0, 0, 0, weight, FALSE, FALSE, FALSE, DEFAULT_CHARSET,
                       OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
                       DEFAULT_PITCH | FF_DONTCARE, face);
}

static int CALLBACK FontEnumProc(const LOGFONTW *lf, const TEXTMETRICW *tm, DWORD type, LPARAM p)
{
    (void)lf; (void)tm; (void)type;
    *(int *)p = 1;
    return 0;
}

static int HasFont(const wchar_t *face)
{
    HDC dc = GetDC(NULL);
    LOGFONTW lf;
    int found = 0;
    ZeroMemory(&lf, sizeof(lf));
    lf.lfCharSet = DEFAULT_CHARSET;
    lstrcpynW(lf.lfFaceName, face, LF_FACESIZE);
    EnumFontFamiliesExW(dc, &lf, FontEnumProc, (LPARAM)&found, 0);
    ReleaseDC(NULL, dc);
    return found;
}

static void FillRound(HDC dc, const RECT *r, int radius, COLORREF color)
{
    HBRUSH br = CreateSolidBrush(color);
    HPEN   pen = CreatePen(PS_SOLID, 1, color);
    HGDIOBJ ob = SelectObject(dc, br);
    HGDIOBJ op = SelectObject(dc, pen);
    RoundRect(dc, r->left, r->top, r->right, r->bottom, radius * 2, radius * 2);
    SelectObject(dc, ob);
    SelectObject(dc, op);
    DeleteObject(br);
    DeleteObject(pen);
}

static void StrokeCircle(HDC dc, const RECT *r, COLORREF fill, COLORREF line)
{
    HBRUSH br = CreateSolidBrush(fill);
    HPEN   pen = CreatePen(PS_SOLID, 1, line);
    HGDIOBJ ob = SelectObject(dc, br);
    HGDIOBJ op = SelectObject(dc, pen);
    Ellipse(dc, r->left, r->top, r->right, r->bottom);
    SelectObject(dc, ob);
    SelectObject(dc, op);
    DeleteObject(br);
    DeleteObject(pen);
}

/* 步进钮里的 +/− 用矢量线条画：文字字形在 20px 下又细又灰，像禁用状态 */
static void DrawStepGlyph(HDC dc, const RECT *r, int plus, COLORREF color)
{
    int cx = (r->left + r->right) / 2;
    int cy = (r->top + r->bottom) / 2;
    int len = (r->right - r->left) * 4 / 10;
    int th = S(2);
    HBRUSH br;
    RECT h, v;

    if (len < 6) len = 6;
    if (th < 2) th = 2;

    br = CreateSolidBrush(color);
    h.left = cx - len / 2; h.right = h.left + len;
    h.top = cy - th / 2;   h.bottom = h.top + th;
    FillRect(dc, &h, br);

    if (plus) {
        v.left = cx - th / 2; v.right = v.left + th;
        v.top = cy - len / 2; v.bottom = v.top + len;
        FillRect(dc, &v, br);
    }
    DeleteObject(br);
}

static void TextRect(HDC dc, HFONT f, COLORREF color, const wchar_t *t, RECT r, UINT flags)
{
    HGDIOBJ old = SelectObject(dc, f);
    SetTextColor(dc, color);
    DrawTextW(dc, t, -1, &r, flags | DT_NOPREFIX);    SelectObject(dc, old);
}

/* ---------------- 布局 ---------------- */

static void ComputeLayout(const RECT *rc, Layout *L)
{
    int m = S(26);
    int step = S(40);
    int gap = S(16);
    int btnW = S(190), btnH = S(52);
    int total = step + gap + btnW + gap + step;
    int left = (rc->right - total) / 2;
    int rowTop = rc->bottom - S(38) - btnH;
    int cy = rowTop + btnH / 2;

    L->m = m;

    L->rcTitle.left = m;
    L->rcTitle.top = m;
    L->rcTitle.right = rc->right - m;
    L->rcTitle.bottom = m + S(20);

    L->rcMinus.left = left;
    L->rcMinus.right = left + step;
    L->rcMinus.top = cy - step / 2;
    L->rcMinus.bottom = cy + step / 2;

    L->rcBtn.left = L->rcMinus.right + gap;
    L->rcBtn.right = L->rcBtn.left + btnW;
    L->rcBtn.top = rowTop;
    L->rcBtn.bottom = rowTop + btnH;

    L->rcPlus.left = L->rcBtn.right + gap;
    L->rcPlus.right = L->rcPlus.left + step;
    L->rcPlus.top = L->rcMinus.top;
    L->rcPlus.bottom = L->rcMinus.bottom;

    L->rcResult.left = m;
    L->rcResult.right = rc->right - m;
    L->rcResult.top = m + S(42);
    L->rcResult.bottom = rowTop - S(30);
}

/* ---------------- 抽签 ---------------- */

static int DrawableTotal(void)
{
    int i, n = 0;
    for (i = 0; i < g_n; i++)
        if (g_w[i] > 0) n++;
    return n;
}

/* ---------------- 防重复：最近 N 次抽到的人不再出现，每 M 秒清空 ---------------- */

static long long NowSec(void) { return (long long)time(NULL); }

/* 环里第 s 条（0 = 最旧） */
static int HistHit(int i)
{
    int s, n = g_histN < g_histMax ? g_histN : g_histMax;

    if (g_histMax <= 0) return 0;          /* 后台把防重复关了 */
    for (s = 0; s < n; s++)
        if (g_hist[s][i]) return 1;
    return 0;
}

static void RoundClear(void)
{
    memset(g_hist, 0, sizeof(g_hist));
    g_histN = 0;
    g_roundStart = NowSec();
}

/* 到 5 分钟就把窗口清空 */
static void RoundResetIfNeeded(void)
{
    if (g_roundStart == 0) { RoundClear(); return; }
    if (NowSec() - g_roundStart >= g_roundSeconds) RoundClear();
}

/* 把这次抽到的 k 个人推进窗口 */
static void HistPush(const int *idx, int n)
{
    int s, i;

    /* g_hist[0] 是最新一次，越往后越旧 */
    if (g_histMax <= 0) { g_histN = 0; return; }   /* 关闭时不记账 */
    if (g_histN < g_histMax) g_histN++;
    if (g_histN > g_histMax) g_histN = g_histMax;
    for (s = g_histN - 1; s > 0; s--)
        memcpy(g_hist[s], g_hist[s - 1], ROSTER_MAX);
    memset(g_hist[0], 0, ROSTER_MAX);
    for (i = 0; i < n; i++) g_hist[0][idx[i]] = 1;
}

/* 按权重不放回抽 k 人：
 *   0) 先把"最近 3 次抽到过"的人排除掉
 *   1) 在剩下的人里按权重抽（权重 > 0 的优先）
 *   2) 权重 > 0 的都抽过了，才轮到权重 0 的（均匀抽）
 *   3) 全被排除（人太少或抽太频繁）→ 清空窗口重来
 * 抽到的这 k 个人会被推进滚动窗口。 */
static int PickSet(int *out, int k)
{
    unsigned char taken[ROSTER_MAX];
    int i, j, got = 0;

    for (i = 0; i < g_n; i++) taken[i] = (unsigned char)HistHit(i);

    /* 后台指定了"下一位"：不管防重复窗口，先把这个人放进结果 */
    if (g_nextName[0] && !g_nextUsed && got < k) {
        int fi = -1;

        for (i = 0; i < g_n; i++)
            if (lstrcmpiW(g_names[i], g_nextName) == 0) { fi = i; break; }
        g_nextUsed = 1;                 /* 用一次就作废 */
        if (fi >= 0) {
            taken[fi] = 1;
            out[got++] = fi;
        }
    }

    for (j = got; j < k; j++) {
        int cand[ROSTER_MAX], m = 0;
        double total = 0.0, r;
        int pick;

        for (i = 0; i < g_n; i++)
            if (!taken[i] && g_w[i] > 0) { cand[m++] = i; total += g_w[i]; }

        if (m == 0) {
            for (i = 0; i < g_n; i++)
                if (!taken[i] && g_w[i] <= 0) cand[m++] = i;
            total = 0.0;   /* 0 权重的人按均匀抽 */
        }

        if (m == 0) {
            /* 最近 3 次的都排除了还是没人：清空窗口，但别把本次已抽到的又抽一遍 */
            memset(g_hist, 0, sizeof(g_hist));
            g_histN = 0;
            for (i = 0; i < got; i++) taken[out[i]] = 1;
            for (i = 0; i < g_n; i++)
                if (!taken[i] && g_w[i] > 0) { cand[m++] = i; total += g_w[i]; }
            if (m == 0) {
                for (i = 0; i < g_n; i++)
                    if (!taken[i]) cand[m++] = i;
                total = 0.0;
            }
            if (m == 0) break;
        }

        if (total > 0.0) {
            r = ((double)rand() / ((double)RAND_MAX + 1.0)) * total;
            pick = cand[m - 1];
            for (i = 0; i < m; i++) {
                r -= g_w[cand[i]];
                if (r <= 0.0) { pick = cand[i]; break; }
            }
        } else {
            pick = cand[rand() % m];
        }

        taken[pick] = 1;
        out[got++] = pick;
    }

    if (got > 0) HistPush(out, got);
    return got;
}
/* 动画用：从全体里均匀挑 k 个不重复的（不看权重） */
static void PickUniformSet(int *out, int k)
{
    int pool[ROSTER_MAX], i;
    for (i = 0; i < g_n; i++) pool[i] = i;
    if (k > g_n) k = g_n;
    for (i = 0; i < k; i++) {
        int j = i + rand() % (g_n - i);
        int t = pool[i]; pool[i] = pool[j]; pool[j] = t;
        out[i] = pool[i];
    }
}

/* 抽完之后把结果顺序打乱：否则"权重 0 的人"总落在数组末尾，看起来像固定位置 */
static void ShuffleInts(int *a, int n)
{
    int i;
    for (i = n - 1; i > 0; i--) {
        int j = rand() % (i + 1);
        int t = a[i]; a[i] = a[j]; a[j] = t;
    }
}

static void Toast(const wchar_t *msg)
{
    lstrcpynW(g_toast, msg, 96);
    g_toastUntil = GetTickCount() + 2600;
}

static void SaveNow(void)
{
    static StateData sd;

    memset(&sd, 0, sizeof(sd));
    sd.n = g_n;
    sd.count = g_count;
    sd.ver = g_version;
    sd.haveWeights = 1;
    sd.roundStart = g_roundStart;
    sd.roundSeconds = g_roundSeconds;
    sd.histMax = g_histMax;
    sd.maxPick = g_maxPick;
    sd.defaultCount = g_defaultCount;
    sd.nextId = g_nextId;
    sd.nextUsed = g_nextUsed;
    sd.histN = g_histN;
    memcpy(sd.w, g_w, sizeof(double) * (g_n > 0 ? g_n : 0));
    memcpy(sd.hist, g_hist, sizeof(g_hist));
    lstrcpynW(sd.className, g_className, STATE_CLASS_WCHARS);
    lstrcpynW(sd.nextName, g_nextName, STATE_NAME_WCHARS);

    State_Save(&sd, g_names);
}

static void BeginRoll(void)
{
    int k, avail, i;

    if (g_animTick > 0) return;

    RoundResetIfNeeded();   /* 跨分钟先开新一轮 */

    avail = DrawableTotal();
    if (avail <= 0) {
        Toast(L"没有可选的人：请到后台把概率调大于 0");
        InvalidateRect(g_hwnd, NULL, FALSE);
        return;
    }

    k = g_count > g_n ? g_n : g_count;
    g_resultN = PickSet(g_result, k);
    ShuffleInts(g_result, g_resultN);   /* 显示顺序也随机，0 权重的人不会总在末尾 */
    SaveNow();              /* 轮次状态立刻落盘，重启也不会重复 */
    g_animN = g_resultN;
    for (i = 0; i < g_animN; i++) g_animSet[i] = g_result[i];

    if (g_animN <= 0) {
        Toast(L"本轮已经抽完了");
        InvalidateRect(g_hwnd, NULL, FALSE);
        return;
    }

    g_animTick = 1;
    SetTimer(g_hwnd, TIMER_ANIM, ANIM_MS, NULL);
}

static void SetCount(int n)
{
    if (n < 1) n = 1;
    if (n > PickLimit()) n = PickLimit();
    if (n == g_count) return;
    g_count = n;
    SaveNow();
    InvalidateRect(g_hwnd, NULL, FALSE);
}

/* ---------------- 同步线程 ---------------- */

static DWORD WINAPI SyncThread(LPVOID param)
{
    HWND hwnd = (HWND)param;
    static const DWORD backoff[] = { 8000, 25000, 60000 };
    int attempt = 0;

    for (;;) {
        NetSyncResult *sp = (NetSyncResult *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(NetSyncResult));
        if (!sp) break;

        if (Net_Sync(SYNC_HOST, SYNC_IP, SYNC_KEY, g_n, sp) == NET_OK) {
            PostMessageW(hwnd, WM_SYNC_DONE, 1, (LPARAM)sp);
            break;
        }
        HeapFree(GetProcessHeap(), 0, sp);

        attempt++;
        {
            /* 前 3 次按 8/25/60 秒；之后每 2 分钟一直试下去，直到连上为止。
             * 机房"先开机、后联网"很常见，不能试几次就放弃。 */
            DWORD waitMs = (attempt <= 3) ? backoff[attempt - 1] : 120000;
            if (WaitForSingleObject(g_syncEvent, waitMs) == WAIT_OBJECT_0)
                attempt = 0;   /* 手动 F5 / 服务器推送：立刻重试，重新计次 */
        }
    }

    InterlockedExchange(&g_syncBusy, 0);
    return 0;
}

static void KickSync(HWND hwnd)
{
    if (InterlockedCompareExchange(&g_syncBusy, 1, 0) == 0) {
        HANDLE th = CreateThread(NULL, 0, SyncThread, (LPVOID)hwnd, 0, NULL);
        if (th) CloseHandle(th);
        else InterlockedExchange(&g_syncBusy, 0);   /* 起线程失败也别把同步锁死 */
    } else if (g_syncEvent) {
        SetEvent(g_syncEvent);
    }
}

/* ---------------- 长连接监听（SSE 推送） ---------------- */

static void OnRemoteChange(void *ctx)
{
    PostMessageW((HWND)ctx, WM_STATE_CHANGED, 0, 0);
}

static DWORD WINAPI WatchThread(LPVOID param)
{
    HWND hwnd = (HWND)param;
    static const DWORD backoff[] = { 1000, 2000, 5000, 10000, 30000, 60000 };
    int idx = 0, fails = 0, i;
    long long lastRev = -1;

    for (;;) {
        int r;

        if (InterlockedExchangeAdd(&g_watchStop, 0)) break;
        r = Net_Watch(SYNC_HOST, SYNC_IP, SYNC_KEY, &g_watchStop, OnRemoteChange, hwnd);
        if (InterlockedExchangeAdd(&g_watchStop, 0)) break;

        if (r == 2) {                 /* 连上过又断：立刻重连，退避归零 */
            fails = 0;
            idx = 0;
            lastRev = -1;
            continue;
        }

        fails++;
        if (fails >= 3) {
            /* 连续 3 次连不上长连接：有些校园网只放行代理流量，裸 socket 连不出去。
             * 退化成轮询（每 3 秒问一次 rev，几十字节），约 5 分钟后再试长连接。 */
            for (i = 0; i < 100; i++) {
                long long rev = 0;

                if (InterlockedExchangeAdd(&g_watchStop, 0)) return 0;
                if (Net_FetchRev(SYNC_HOST, SYNC_IP, SYNC_KEY, &rev)) {
                    if (lastRev >= 0 && rev != lastRev)
                        PostMessageW(hwnd, WM_STATE_CHANGED, 0, 0);
                    lastRev = rev;
                }
                Sleep(3000);
            }
            fails = 0;
            continue;
        }

        Sleep(backoff[idx]);
        if (idx < 5) idx++;
    }
    return 0;
}

/* ---------------- 绘制 ---------------- */

static void DrawSet(HDC dc, const RECT *area, const int *set, int n)
{
    int W = area->right - area->left;
    int H = area->bottom - area->top;
    int maxChars = 1;
    int cols, rows, bestCols = 1, bestSize = S(14);
    int i, row;
    HFONT f, old;
    RECT r;
    int resW, resH;

    if (n <= 0) return;

    for (i = 0; i < n; i++) {
        int len = lstrlenW(g_names[set[i]]);
        if (len > maxChars) maxChars = len;
    }

    for (cols = 1; cols <= n && cols <= 8; cols++) {
        int cw, ch, byH, byW, sz;
        rows = (n + cols - 1) / cols;
        cw = W / cols;
        ch = H / rows;
        byH = (int)(ch * 0.68);
        byW = (int)(cw / (maxChars + 0.6));
        sz = byH < byW ? byH : byW;
        if (sz > bestSize) { bestSize = sz; bestCols = cols; }
    }
    if (bestSize > S(58)) bestSize = S(58);
    if (bestSize < S(13)) bestSize = S(13);

    rows = (n + bestCols - 1) / bestCols;
    resW = W / bestCols;
    resH = (int)(bestSize * 1.42);          /* 行高跟着字号走，少人时不会把结果撑满整屏 */
    {
        int gridH = resH * rows;
        int y0 = area->top + (H - gridH) / 2;
        if (y0 < area->top) y0 = area->top;

        f = MakeFont(bestSize, FW_BOLD, L"Microsoft YaHei");
        old = SelectObject(dc, f);

        for (row = 0; row < rows; row++) {
            int cnt = n - row * bestCols;
            int x0;
            if (cnt > bestCols) cnt = bestCols;
            x0 = area->left + (W - cnt * resW) / 2;
            for (i = 0; i < cnt; i++) {
                r.left = x0 + i * resW;
                r.right = r.left + resW;
                r.top = y0 + row * resH;
                r.bottom = r.top + resH;
                TextRect(dc, f, C_TEXT, g_names[set[row * bestCols + i]], r,
                         DT_SINGLELINE | DT_VCENTER | DT_CENTER | DT_END_ELLIPSIS);
            }
        }

        SelectObject(dc, old);
        DeleteObject(f);
    }
}

static void DrawScene(HDC dc, const RECT *rc)
{
    Layout L;
    const int *set;
    int n;
    wchar_t label[64];
    RECT r;

    ComputeLayout(rc, &L);

    FillRect(dc, rc, g_brBg);
    SetBkMode(dc, TRANSPARENT);

    /* 左上：班级名（轮次完全隐性，不显示剩余人数） */
    {
        wchar_t title[96];
        wsprintfW(title, L"%s · 随机点名", g_className);
        TextRect(dc, g_fTitle, C_SUB, title, L.rcTitle, DT_SINGLELINE | DT_VCENTER | DT_LEFT);
    }

    /* 结果区 */
    if (g_animTick > 0) {
        set = g_animSet;
        n = g_animN;
    } else {
        set = g_result;
        n = g_resultN;
    }

    if (n > 0) {
        DrawSet(dc, &L.rcResult, set, n);
    } else {
        RECT one = L.rcResult;
        RECT meta;
        TextRect(dc, g_fName, C_TEXT, L"准备就绪", one, DT_SINGLELINE | DT_VCENTER | DT_CENTER);
        meta = one;
        meta.top = (one.top + one.bottom) / 2 + S(40);
        meta.bottom = meta.top + S(20);
        {
            wchar_t m2[64];
            wsprintfW(m2, L"共 %d 人 · 每次抽 %d 人", g_n, g_count);
            TextRect(dc, g_fSub, C_SUB, m2, meta, DT_SINGLELINE | DT_VCENTER | DT_CENTER);
        }
    }

    /* 步进按钮 */
    {
        int canMinus = g_count > 1;
        int canPlus = g_count < PickLimit();

        StrokeCircle(dc, &L.rcMinus, g_hoverMinus && canMinus ? RGB(243, 242, 237) : RGB(255, 255, 255),
                     g_hoverMinus && canMinus ? RGB(200, 198, 190) : C_LINE);
        DrawStepGlyph(dc, &L.rcMinus, 0, canMinus ? C_TEXT : RGB(196, 194, 188));

        StrokeCircle(dc, &L.rcPlus, g_hoverPlus && canPlus ? RGB(243, 242, 237) : RGB(255, 255, 255),
                     g_hoverPlus && canPlus ? RGB(200, 198, 190) : C_LINE);
        DrawStepGlyph(dc, &L.rcPlus, 1, canPlus ? C_TEXT : RGB(196, 194, 188));
    }

    /* 主按钮 */
    {
        COLORREF bg = g_hoverBtn ? C_BTN_HOVER : C_BTN;
        RECT rb = L.rcBtn;
        if (g_pressed) { rb.top += S(1); rb.bottom += S(1); }
        wsprintfW(label, L"抽 %d 位", g_count);
        FillRound(dc, &rb, S(12), bg);
        TextRect(dc, g_fBtn, RGB(255, 255, 255), label, rb,
                 DT_SINGLELINE | DT_VCENTER | DT_CENTER);
    }

    /* 提示 */
    if (GetTickCount() < g_toastUntil) {
        r.left = L.rcResult.left;
        r.right = L.rcResult.right;
        r.top = L.rcBtn.top - S(30);
        r.bottom = r.top + S(20);
        TextRect(dc, g_fSub, C_AMBER, g_toast, r, DT_SINGLELINE | DT_VCENTER | DT_CENTER);
    }
}

static void OnPaint(HWND hwnd)
{
    PAINTSTRUCT ps;
    HDC dc = BeginPaint(hwnd, &ps);
    RECT rc;
    HDC mem;
    HBITMAP bmp;
    HGDIOBJ old;

    GetClientRect(hwnd, &rc);
    mem = CreateCompatibleDC(dc);
    bmp = CreateCompatibleBitmap(dc, rc.right, rc.bottom);
    old = SelectObject(mem, bmp);

    DrawScene(mem, &rc);

    BitBlt(dc, 0, 0, rc.right, rc.bottom, mem, 0, 0, SRCCOPY);

    SelectObject(mem, old);
    DeleteObject(bmp);
    DeleteDC(mem);
    EndPaint(hwnd, &ps);
}

/* ---------------- 窗口过程 ---------------- */

static void ApplySync(NetSyncResult *sp)
{
    int i, changed = 0;

    /* 只有 Net_Sync 成功时才会走到这里，所以直接套用。
     * 服务器下发了名单就以它为准（后台加人后客户端重启/F5 就能看到）；
     * 没下发（nCount==0）就只更新权重，名单仍用本机缓存的。 */
    if (sp->nCount > 0) {
        int n = sp->nCount > ROSTER_MAX ? ROSTER_MAX : sp->nCount;
        for (i = 0; i < n; i++) {
            lstrcpynW(g_names[i], sp->names[i], NAME_LEN);
            if (i >= sp->wCount) g_w[i] = 1.0;
        }
        g_n = n;
        if (g_count > PickLimit()) g_count = PickLimit();
        changed = 1;
    }
    for (i = 0; i < sp->wCount && i < g_n; i++) {
        if (sp->w[i] >= 0 && fabs(sp->w[i] - g_w[i]) > 1e-6) {
            g_w[i] = sp->w[i];
            changed = 1;
        }
    }
    /* 后台可调的两项：防重复窗口大小 / 清空周期 */
    if (sp->roundSeconds >= 10 && sp->roundSeconds <= 86400 &&
        sp->roundSeconds != g_roundSeconds) {
        g_roundSeconds = sp->roundSeconds;
        changed = 1;
    }
    if (sp->histDraws >= 0 && sp->histDraws <= HIST_MAX && sp->histDraws != g_histMax) {
        g_histMax = sp->histDraws;
        g_histN = 0;
        memset(g_hist, 0, sizeof(g_hist));
        g_roundStart = NowSec();
        changed = 1;
    }

    if (sp->maxPick >= 1 && sp->maxPick <= MAX_PICK_ABS && sp->maxPick != g_maxPick) {
        g_maxPick = sp->maxPick;
        if (g_count > PickLimit()) g_count = PickLimit();
        changed = 1;
    }
    if (sp->defaultCount >= 1 && sp->defaultCount <= MAX_PICK_ABS &&
        sp->defaultCount != g_defaultCount) {
        g_defaultCount = sp->defaultCount;
        changed = 1;
    }
    if (g_fresh) {                    /* 首次运行：用后台配的默认人数 */
        g_count = g_defaultCount > PickLimit() ? PickLimit() : g_defaultCount;
        g_fresh = 0;
        changed = 1;
    }
    if (sp->className[0] && lstrcmpW(sp->className, g_className) != 0) {
        lstrcpynW(g_className, sp->className, 64);
        changed = 1;
    }
    if (sp->nextId != g_nextId) {      /* 后台重新指定了"下一位" */
        g_nextId = sp->nextId;
        lstrcpynW(g_nextName, sp->nextName, NAME_LEN);
        g_nextUsed = 0;
        changed = 1;
    }

    g_version = sp->ver;
    if (changed) SaveNow();
    InvalidateRect(g_hwnd, NULL, FALSE);
}

static void UpdateHover(HWND hwnd, int x, int y)
{
    Layout L;
    RECT rc;
    POINT p;
    int b, mi, pl;

    GetClientRect(hwnd, &rc);
    ComputeLayout(&rc, &L);
    p.x = x; p.y = y;

    b = PtInRect(&L.rcBtn, p) != 0;
    mi = PtInRect(&L.rcMinus, p) != 0;
    pl = PtInRect(&L.rcPlus, p) != 0;

    if (b != g_hoverBtn || mi != g_hoverMinus || pl != g_hoverPlus) {
        g_hoverBtn = b;
        g_hoverMinus = mi;
        g_hoverPlus = pl;
        InvalidateRect(hwnd, NULL, FALSE);
    }
}

static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_CREATE: {
        int dpi, i;
        HDC dc = GetDC(hwnd);
        dpi = GetDeviceCaps(dc, LOGPIXELSX);
        ReleaseDC(hwnd, dc);
        g_scale = (double)dpi / 96.0;

        {
            const wchar_t *face = HasFont(L"Microsoft YaHei") ? L"Microsoft YaHei" : L"SimSun";
            g_fTitle = MakeFont(S(13), FW_NORMAL, face);
            g_fSub   = MakeFont(S(12), FW_NORMAL, face);
            g_fName  = MakeFont(S(58), FW_BOLD, face);
            g_fBtn   = MakeFont(S(15), 600, face);
            g_fGlyph = MakeFont(S(20), FW_NORMAL, face);
        }

        srand((unsigned)(GetTickCount() ^ (DWORD)GetCurrentProcessId() ^ (unsigned)time(NULL)));

        /* 先用编译进 exe 的名单兜底，再用本机缓存覆盖（缓存里有上次同步到的服务器名单） */
        for (i = 0; i < ROSTER_COUNT; i++) {
            lstrcpynW(g_names[i], kRosterNames[i], NAME_LEN);
            g_w[i] = (i < ROSTER_COUNT) ? kRosterWeights[i] : 1.0;
        }
        g_n = ROSTER_COUNT;
        g_fresh = State_Exists() ? 0 : 1;
        {
            static StateData sd;
            int cachedN = 0;

            memset(&sd, 0, sizeof(sd));
            State_Load(&sd, g_names, &cachedN);

            if (cachedN > 0 && cachedN <= ROSTER_MAX) {
                g_n = cachedN;
                if (sd.haveWeights) {
                    for (i = 0; i < g_n && i < STATE_MAX; i++)
                        if (sd.w[i] >= 0) g_w[i] = sd.w[i];
                }
            }
            if (sd.count >= 1) g_count = sd.count;
            g_version = sd.ver;
            g_roundStart = sd.roundStart;
            if (sd.roundSeconds > 0) g_roundSeconds = sd.roundSeconds;
            if (sd.histMax >= 0 && sd.histMax <= HIST_MAX) g_histMax = sd.histMax;
            if (sd.maxPick >= 1 && sd.maxPick <= MAX_PICK_ABS) g_maxPick = sd.maxPick;
            if (sd.defaultCount >= 1 && sd.defaultCount <= MAX_PICK_ABS) g_defaultCount = sd.defaultCount;
            g_nextId = sd.nextId;
            g_nextUsed = sd.nextUsed;
            if (sd.className[0]) lstrcpynW(g_className, sd.className, STATE_CLASS_WCHARS);
            lstrcpynW(g_nextName, sd.nextName, STATE_NAME_WCHARS);
            memcpy(g_hist, sd.hist, sizeof(g_hist));
            g_histN = sd.histN;
        }
        if (g_count < 1) g_count = 1;
        if (g_count > PickLimit()) g_count = PickLimit();

        RoundResetIfNeeded();   /* 窗口过期就清掉 */

        ImmAssociateContext(hwnd, NULL);

        g_syncEvent = CreateEventW(NULL, FALSE, FALSE, NULL);
        KickSync(hwnd);
        {
            HANDLE th = CreateThread(NULL, 0, WatchThread, (LPVOID)hwnd, 0, NULL);
            if (th) CloseHandle(th);
        }
        return 0;
    }

    case WM_ERASEBKGND:
        return 1;

    case WM_PAINT:
        OnPaint(hwnd);
        return 0;

    case WM_STATE_CHANGED:
        KickSync(hwnd);      /* 服务器状态变了：立刻重新拉一次，秒级生效 */
        return 0;

    case WM_TIMER:
        if (wp == TIMER_ANIM) {
            g_animTick++;
            if (g_animTick <= ANIM_TICKS) {
                PickUniformSet(g_animSet, g_animN);
                InvalidateRect(hwnd, NULL, FALSE);
            } else {
                KillTimer(hwnd, TIMER_ANIM);
                g_animTick = 0;
                InvalidateRect(hwnd, NULL, FALSE);
            }
        }
        return 0;

    case WM_MOUSEMOVE:
        if (!g_tracking) {
            TRACKMOUSEEVENT tme;
            tme.cbSize = sizeof(tme);
            tme.dwFlags = TME_LEAVE;
            tme.hwndTrack = hwnd;
            tme.dwHoverTime = 0;
            TrackMouseEvent(&tme);
            g_tracking = 1;
        }
        UpdateHover(hwnd, GET_X_LPARAM(lp), GET_Y_LPARAM(lp));
        return 0;

    case WM_MOUSELEAVE:
        g_tracking = 0;
        g_hoverBtn = g_hoverMinus = g_hoverPlus = 0;
        InvalidateRect(hwnd, NULL, FALSE);
        return 0;

    case WM_SETCURSOR:
        if (LOWORD(lp) == HTCLIENT) {
            if (g_hoverBtn || g_hoverMinus || g_hoverPlus) {
                SetCursor(LoadCursorW(NULL, IDC_HAND));
                return TRUE;
            }
        }
        break;

    case WM_LBUTTONDOWN: {
        Layout L;
        RECT rc, rm, rp;
        POINT p;
        GetClientRect(hwnd, &rc);
        ComputeLayout(&rc, &L);
        p.x = GET_X_LPARAM(lp);
        p.y = GET_Y_LPARAM(lp);

        /* 步进钮的热区往外放宽 5px：小圆点太容易点空，尤其是 125% 缩放或机房鼠标 */
        rm = L.rcMinus;
        rp = L.rcPlus;
        InflateRect(&rm, S(5), S(5));
        InflateRect(&rp, S(5), S(5));

        if (PtInRect(&rm, p)) {
            SetCount(g_count - 1);
        } else if (PtInRect(&rp, p)) {
            SetCount(g_count + 1);
        } else if (PtInRect(&L.rcBtn, p)) {
            g_pressed = 1;
            SetCapture(hwnd);
            InvalidateRect(hwnd, NULL, FALSE);
        }
        return 0;
    }

    case WM_LBUTTONUP: {
        if (g_pressed) {
            Layout L;
            RECT rc;
            POINT p;
            GetClientRect(hwnd, &rc);
            ComputeLayout(&rc, &L);
            p.x = GET_X_LPARAM(lp);
            p.y = GET_Y_LPARAM(lp);
            g_pressed = 0;
            ReleaseCapture();
            if (PtInRect(&L.rcBtn, p)) BeginRoll();
            else InvalidateRect(hwnd, NULL, FALSE);
        }
        return 0;
    }

    case WM_MOUSEWHEEL: {
        int delta = GET_WHEEL_DELTA_WPARAM(wp);
        if (delta > 0) SetCount(g_count + 1);
        else if (delta < 0) SetCount(g_count - 1);
        return 0;
    }

    case WM_KEYDOWN:
        if (wp == VK_SPACE || wp == VK_RETURN) { BeginRoll(); return 0; }
        if (wp == VK_UP || wp == VK_ADD || wp == VK_RIGHT) { SetCount(g_count + 1); return 0; }
        if (wp == VK_DOWN || wp == VK_SUBTRACT || wp == VK_LEFT) { SetCount(g_count - 1); return 0; }
        if (wp == VK_F5) { KickSync(hwnd); return 0; }
        if (wp == VK_ESCAPE) { PostMessageW(hwnd, WM_CLOSE, 0, 0); return 0; }
        break;

    case WM_SYNC_DONE: {
        NetSyncResult *sp = (NetSyncResult *)lp;
        if (sp) {
            ApplySync(sp);
            HeapFree(GetProcessHeap(), 0, sp);
        }
        return 0;
    }

    case WM_GETMINMAXINFO: {
        /* 固定尺寸，禁止最大化把自己拉变形 */
        MINMAXINFO *mmi = (MINMAXINFO *)lp;
        mmi->ptMaxTrackSize.x = S(680) + 32;
        mmi->ptMaxTrackSize.y = S(420) + 32;
        return 0;
    }

    case WM_CLOSE:
        /* 退出前如果已经下好新版本，就安排替换（用户完全无感） */
        if (Update_ApplyPending()) {
            DestroyWindow(hwnd);
            return 0;
        }
        break;

    case WM_DESTROY:
        /* 同步线程可能还阻塞在 g_syncEvent 上，这里不关句柄：进程退出会统一回收，
         * 关掉反而会让线程等在已释放的句柄上。 */
        if (g_fTitle) DeleteObject(g_fTitle);
        if (g_fSub) DeleteObject(g_fSub);
        if (g_fName) DeleteObject(g_fName);
        if (g_fBtn) DeleteObject(g_fBtn);
        if (g_fGlyph) DeleteObject(g_fGlyph);
        if (g_brBg) DeleteObject(g_brBg);
        InterlockedExchange(&g_watchStop, 1);
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, LPWSTR cmd, int show)
{
    WNDCLASSEXW wc;
    MSG msg;
    RECT r;
    DWORD style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX;

    (void)prev; (void)cmd;
    g_inst = inst;

    /* 热更新助手模式：等待旧进程退出后替换 exe，不建窗口、不提示 */
    {
        LPWSTR cl = GetCommandLineW();
        if (cl && wcsstr(cl, L"--apply-update")) return Update_HelperMain(cl);
    }

    /* 上次已经下好的更新：直接换上（改名法，不重启、不关窗） */
    Update_TryApplyAtStartup();

    ZeroMemory(&wc, sizeof(wc));
    wc.cbSize = sizeof(wc);
    wc.lpfnWndProc = WndProc;
    wc.hInstance = inst;
    wc.hCursor = LoadCursorW(NULL, IDC_ARROW);
    /* 按目标尺寸取图标帧：直接 LoadIcon 拿的是 32px，标题栏 16px 会糊 */
    wc.hIcon = (HICON)LoadImageW(inst, MAKEINTRESOURCEW(1), IMAGE_ICON,
                                 GetSystemMetrics(SM_CXICON), GetSystemMetrics(SM_CYICON),
                                 LR_DEFAULTCOLOR);
    wc.hIconSm = (HICON)LoadImageW(inst, MAKEINTRESOURCEW(1), IMAGE_ICON,
                                   GetSystemMetrics(SM_CXSMICON), GetSystemMetrics(SM_CYSMICON),
                                   LR_DEFAULTCOLOR);
    wc.lpszClassName = L"XananRollCallWnd";
    wc.style = CS_HREDRAW | CS_VREDRAW;
    RegisterClassExW(&wc);

    g_brBg = CreateSolidBrush(C_BG);

    /* 先按 96dpi 估算，窗口创建后再按真实 DPI 校正 */
    r.left = 0; r.top = 0; r.right = 680; r.bottom = 420;
    AdjustWindowRectEx(&r, style, FALSE, 0);

    g_hwnd = CreateWindowExW(0, wc.lpszClassName, APP_NAME, style,
                             CW_USEDEFAULT, CW_USEDEFAULT,
                             r.right - r.left, r.bottom - r.top,
                             NULL, NULL, inst, NULL);
    if (!g_hwnd) return 1;

    {
        int dpi;
        HDC dc = GetDC(g_hwnd);
        dpi = GetDeviceCaps(dc, LOGPIXELSX);
        ReleaseDC(g_hwnd, dc);
        if (dpi != 96) {
            int w = MulDiv(680, dpi, 96), h = MulDiv(420, dpi, 96);
            r.left = 0; r.top = 0; r.right = w; r.bottom = h;
            AdjustWindowRectEx(&r, style, FALSE, 0);
            SetWindowPos(g_hwnd, NULL, 0, 0, r.right - r.left, r.bottom - r.top,
                         SWP_NOMOVE | SWP_NOZORDER);
        }
    }

    ShowWindow(g_hwnd, show);
    UpdateWindow(g_hwnd);

    Update_StartCheck();   /* 后台静默检查更新，不提示 */

    while (GetMessageW(&msg, NULL, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    return (int)msg.wParam;
}
