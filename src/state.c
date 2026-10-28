#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shlobj.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "state.h"

#define FILE_MAGIC "RC1"

static void BuildPath(wchar_t *buf)
{
    wchar_t dir[MAX_PATH] = L"";

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

int State_Exists(void)
{
    wchar_t path[MAX_PATH];
    BuildPath(path);
    return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}

void State_Load(StateData *sd, wchar_t names[][STATE_NAME_WCHARS], int *nameCount)
{
    wchar_t path[MAX_PATH];
    char line[8192];
    FILE *f;
    int loadedNames = 0;

    if (!sd) return;
    memset(sd, 0, sizeof(*sd));
    sd->count = 1;
    sd->roundSeconds = 300;
    sd->histMax = 3;
    sd->maxPick = 5;
    sd->defaultCount = 1;

    BuildPath(path);
    f = _wfopen(path, L"rb");
    if (!f) { if (nameCount) *nameCount = 0; return; }

    if (!fgets(line, sizeof(line), f) || strncmp(line, FILE_MAGIC, 3) != 0) {
        fclose(f);
        if (nameCount) *nameCount = 0;
        return;
    }

    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "n=", 2) == 0) {
            int v = atoi(line + 2);
            if (v < 0) v = 0;
            if (v > STATE_MAX) v = STATE_MAX;
            sd->n = v;
        } else if (strncmp(line, "r=", 2) == 0) {
            sd->roundStart = _strtoi64(line + 2, NULL, 10);
        } else if (strncmp(line, "rs=", 3) == 0) {
            sd->roundSeconds = atoi(line + 3);
        } else if (strncmp(line, "hd=", 3) == 0) {
            sd->histMax = atoi(line + 3);
        } else if (strncmp(line, "mp=", 3) == 0) {
            sd->maxPick = atoi(line + 3);
        } else if (strncmp(line, "dc=", 3) == 0) {
            sd->defaultCount = atoi(line + 3);
        } else if (strncmp(line, "nxi=", 4) == 0) {
            sd->nextId = atoi(line + 4);
        } else if (strncmp(line, "nxu=", 4) == 0) {
            sd->nextUsed = atoi(line + 4);
        } else if (strncmp(line, "nx=", 3) == 0) {
            char *p = line + 3;
            int len = (int)strlen(p);
            while (len > 0 && (p[len - 1] == '\r' || p[len - 1] == '\n')) p[--len] = 0;
            if (len > 0) MultiByteToWideChar(CP_UTF8, 0, p, len, sd->nextName, STATE_NAME_WCHARS - 1);
        } else if (strncmp(line, "cl=", 3) == 0) {
            char *p = line + 3;
            int len = (int)strlen(p);
            while (len > 0 && (p[len - 1] == '\r' || p[len - 1] == '\n')) p[--len] = 0;
            if (len > 0) MultiByteToWideChar(CP_UTF8, 0, p, len, sd->className, STATE_CLASS_WCHARS - 1);
        } else if (strncmp(line, "d", 1) == 0 && line[1] >= '0' && line[2] >= '0' && line[3] == '=') {
            int slot = (line[1] - '0') * 10 + (line[2] - '0'), i;
            if (slot >= 0 && slot < STATE_HIST) {
                for (i = 0; i < STATE_MAX && line[4 + i]; i++)
                    sd->hist[slot][i] = (line[4 + i] == '1') ? 1 : 0;
                if (slot + 1 > sd->histN) sd->histN = slot + 1;
            }
        } else if (strncmp(line, "count=", 6) == 0) {
            sd->count = atoi(line + 6);
            if (sd->count < 1) sd->count = 1;
        } else if (strncmp(line, "ver=", 4) == 0) {
            sd->ver = _strtoi64(line + 4, NULL, 10);
        } else if (strncmp(line, "w=", 2) == 0) {
            char *p = line + 2;
            int i;
            for (i = 0; i < STATE_MAX; i++) {
                char *end = NULL;
                double v = strtod(p, &end);
                if (end == p) break;
                if (v < 0) v = 0;
                if (v > 1000) v = 1000;
                sd->w[i] = v;
                p = end;
                if (*p == ',') p++;
            }
            sd->haveWeights = 1;
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

    if (nameCount) *nameCount = loadedNames;
}

void State_Save(const StateData *sd, wchar_t names[][STATE_NAME_WCHARS])
{
    wchar_t path[MAX_PATH];
    wchar_t tmp[MAX_PATH];
    FILE *f;
    int i, n, histN;

    if (!sd) return;
    n = sd->n;
    if (n < 0) n = 0;
    if (n > STATE_MAX) n = STATE_MAX;
    histN = sd->histN;
    if (histN > STATE_HIST) histN = STATE_HIST;

    BuildPath(path);
    wsprintfW(tmp, L"%s.tmp", path);

    f = _wfopen(tmp, L"wb");
    if (!f) return;

    fprintf(f, FILE_MAGIC "\n");
    fprintf(f, "count=%d\n", sd->count);
    fprintf(f, "ver=%lld\n", sd->ver);
    fprintf(f, "r=%lld\n", sd->roundStart);
    fprintf(f, "rs=%d\n", sd->roundSeconds);
    fprintf(f, "hd=%d\n", sd->histMax);
    fprintf(f, "mp=%d\n", sd->maxPick);
    fprintf(f, "dc=%d\n", sd->defaultCount);
    fprintf(f, "nxi=%d\n", sd->nextId);
    fprintf(f, "nxu=%d\n", sd->nextUsed);
    fprintf(f, "n=%d\n", n);

    {
        char utf8[STATE_CLASS_WCHARS * 4];
        if (WideCharToMultiByte(CP_UTF8, 0, sd->className, -1, utf8, sizeof(utf8) - 1, NULL, NULL) > 1)
            fprintf(f, "cl=%s\n", utf8);
    }
    {
        char utf8[STATE_NAME_WCHARS * 4];
        if (WideCharToMultiByte(CP_UTF8, 0, sd->nextName, -1, utf8, sizeof(utf8) - 1, NULL, NULL) > 1)
            fprintf(f, "nx=%s\n", utf8);
    }

    {
        int s;
        for (s = 0; s < STATE_HIST; s++) {
            fprintf(f, "d%02d=", s);
            for (i = 0; i < n; i++)
                fputc(s < histN ? (sd->hist[s][i] ? '1' : '0') : '0', f);
            fputc('\n', f);
        }
    }

    fputs("w=", f);
    for (i = 0; i < n; i++) {
        if (i) fputc(',', f);
        fprintf(f, "%.4g", sd->w[i]);
    }
    fputc('\n', f);

    for (i = 0; i < n && names; i++) {
        char utf8[STATE_NAME_WCHARS * 4];
        int len = WideCharToMultiByte(CP_UTF8, 0, names[i], -1, utf8, sizeof(utf8) - 1, NULL, NULL);
        if (len > 1) fprintf(f, "s=%s\n", utf8);
    }
    fclose(f);

    /* 先写临时文件再替换，断电也不会写坏原状态 */
    MoveFileExW(tmp, path, MOVEFILE_REPLACE_EXISTING);
}
