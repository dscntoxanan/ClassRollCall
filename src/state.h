#ifndef STATE_H
#define STATE_H

#define STATE_MAX         128
#define STATE_NAME_WCHARS 24

/* 本地状态（名单 / 概率 / 每次抽取人数 / 本轮已抽 / 轮次时间 / 版本），
 * 存 %APPDATA%\XananRollCall\state.dat。
 * 名单也会落盘，这样某次启动即使没网，用的也是上次同步下来的名单。 */
void State_Load(double *w, int *n, wchar_t names[][STATE_NAME_WCHARS], int *count,
                long long *ver, long long *roundStart, unsigned char *hist, int *histN);
void State_Save(const double *w, int n, wchar_t names[][STATE_NAME_WCHARS], int count,
                long long ver, long long roundStart, const unsigned char *hist, int histN);

#endif
