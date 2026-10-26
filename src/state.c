#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shlobj.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#include "state.h"

#define FILE_MAGIC "RC1"

static void BuildPath(wchar_t *buf, int cap)
{
    wchar_t dir[MAX_PATH] = L"";
    (void)cap;

    if (FAILED(SHGetFolderPathW(NULL, CSIDL_APPDATA, NULL, 0, dir))) {
        dir[0] = 0;
    }
    if (!dir[0]) {
        DWORD n = GetTempPathW(MAX_PATH, dir);
        if (n == 0 || n >= MAX_PATH) { lstrcpyW(dir, L"."); }
    }
    {
        wchar_t sub[MAX_PATH];
        wsprintfW(sub, L"%s\\XananRollCall", dir);
        CreateDirectoryW(sub, NULL);
        wsprintfW(buf, L"%s\\state.dat", sub);
    }
}

void State_Load(double *w, int *n, wchar_t names[][STATE_NAME_WCHARS], int *count,
                long long *ver, long long *roundStart, unsigned char *hist, int *histN)
{
    wchar_t path[MAX_PATH];
    char line[8192];
    FILE *f;
    int loadedNames = 0;

    if (n) *n = 0;
    if (count) *count = 1;
    if (ver) *ver = 0;
    if (roundStart) *roundStart = 0;
    if (histN) *histN = 0;
    if (hist) memset(hist, 0, STATE_MAX * 3);

    BuildPath(path, MAX_PATH);
    f = _wfopen(path, L"rb");
    if (!f) return;

    if (!fgets(line, sizeof(line), f) || strncmp(line, FILE_MAGIC, 3) != 0) { fclose(f); return; }

    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "n=", 2) == 0) {
            int v = atoi(line + 2);
            if (v < 0) v = 0;
            if (v > STATE_MAX) v = STATE_MAX;
            if (n) *n = v;
        } else if (strncmp(line, "r=", 2) == 0) {
            if (roundStart) *roundStart = _strtoi64(line + 2, NULL, 10);
        } else if (strncmp(line, "d", 1) == 0 && line[1] >= '0' && line[1] <= '2' && line[2] == '=' && hist) {
            int slot = line[1] - '0', i;
            unsigned char *dst = hist + (size_t)slot * STATE_MAX;
            for (i = 0; i < STATE_MAX && line[3 + i]; i++)
                dst[i] = (line[3 + i] == '1') ? 1 : 0;
            if (histN && slot + 1 > *histN) *histN = slot + 1;
        } else if (strncmp(line, "count=", 6) == 0) {
            if (count) *count = atoi(line + 6);
            if (*count < 1) *count = 1;
        } else if (strncmp(line, "ver=", 4) == 0) {
            if (ver) *ver = _strtoi64(line + 4, NULL, 10);
        } else if (strncmp(line, "w=", 2) == 0) {
            char *p = line + 2;
            int i;
            for (i = 0; i < STATE_MAX; i++) {
                char *end = NULL;
                double v = strtod(p, &end);
                if (end == p) break;
                if (v < 0) v = 0;
                if (v > 1000) v = 1000;
                w[i] = v;
                p = end;
                if (*p == ',') p++;
            }
        } else if (strncmp(line, "s=", 2) == 0 && names && loadedNames < STATE_MAX) {
            char *p = line + 2;
            int len = (int)strlen(p);
            while (len > 0 && (p[len - 1] == '\r' || p[len - 1] == '\n')) p[--len] = 0;
            if (len > 0) {
                int wlen = MultiByteToWideChar(CP_UTF8, 0, p, len, names[loadedNames], STATE_NAME_WCHARS - 1);
                if (wlen > 0) {
                    names[loadedNames][wlen] = 0;
                    loadedNames++;
                }
            }
        }
    }
    fclose(f);
}

void State_Save(const double *w, int n, wchar_t names[][STATE_NAME_WCHARS], int count,
                long long ver, long long roundStart, const unsigned char *hist, int histN)
{
    wchar_t path[MAX_PATH];
    wchar_t tmp[MAX_PATH];
    FILE *f;
    int i;

    if (n > STATE_MAX) n = STATE_MAX;
    BuildPath(path, MAX_PATH);
    wsprintfW(tmp, L"%s.tmp", path);

    f = _wfopen(tmp, L"wb");
    if (!f) return;

    fprintf(f, FILE_MAGIC "\n");
    fprintf(f, "count=%d\n", count);
    fprintf(f, "ver=%lld\n", ver);
    fprintf(f, "r=%lld\n", roundStart);
    fprintf(f, "n=%d\n", n);
    {
        int s;
        for (s = 0; s < 3; s++) {
            fprintf(f, "d%d=", s);
            for (i = 0; i < n; i++)
                fputc((s < histN && hist && hist[(size_t)s * STATE_MAX + i]) ? '1' : '0', f);
            fputc('\n', f);
        }
    }
    fputs("w=", f);
    for (i = 0; i < n; i++) {
        if (i) fputc(',', f);
        fprintf(f, "%.4g", w[i]);
    }
    fputc('\n', f);
    for (i = 0; i < n; i++) {
        char utf8[STATE_NAME_WCHARS * 4];
        int len = WideCharToMultiByte(CP_UTF8, 0, names[i], -1, utf8, sizeof(utf8) - 1, NULL, NULL);
        if (len > 1) fprintf(f, "s=%s\n", utf8);
    }
    fclose(f);

    /* 先写临时文件再替换，断电也不会写坏原状态 */
    MoveFileExW(tmp, path, MOVEFILE_REPLACE_EXISTING);
}
