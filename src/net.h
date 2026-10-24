#ifndef NET_H
#define NET_H

/* 服务器可以下发的最大人数 / 单个姓名长度（宽字符） */
#define NET_MAX_STUDENTS 128
#define NET_NAME_WCHARS 24

/* 同步结果 */
#define NET_OK      0   /* 成功 */
#define NET_UNREACH 1   /* 根本连不上（ping/TCP 都失败）*/
#define NET_FAILED  2   /* 连上了但请求/解析失败 */

typedef struct {
    long long ver;
    int wCount;
    double w[NET_MAX_STUDENTS];
    int nCount;                                  /* 0 表示服务器没下发名单 */
    wchar_t names[NET_MAX_STUDENTS][NET_NAME_WCHARS];
} NetSyncResult;

/* 拉一次概率（如果服务器有名单，也一并拉下来）。
 *   host  域名（只用于 Host 头和 DNS 兜底）
 *   ip    服务器 IP 字面量：优先直连它，**不查 DNS**
 *   key   同步密钥
 *   count 本机当前人数（服务器没有名单时作参考）
 * 成功返回 NET_OK，结果写进 out。 */
int Net_Sync(const wchar_t *host, const wchar_t *ip, const wchar_t *key, int count,
             NetSyncResult *out);
/* 长连接监听（SSE）：服务器状态一变就调用 onChange，实现秒级同步。
 *   stop  指向 volatile LONG，置 1 时尽快返回
 *   返回  0 = 收到 stop 退出；1 = 连不上；2 = 连上后断开（建议重置退避）
 * 回调在后台线程里执行，不要在里面做重活，PostMessage 回主线程即可。 */
int Net_Watch(const wchar_t *host, const wchar_t *ip, const wchar_t *key,
              volatile LONG *stop, void (*onChange)(void *), void *ctx);

#endif
