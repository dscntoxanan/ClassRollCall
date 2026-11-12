#ifndef STATE_H
#define STATE_H

#define STATE_MAX          128
#define STATE_HIST         20
#define STATE_NAME_WCHARS  24
#define STATE_CLASS_WCHARS 64

/* 本地状态（名单以外的全部设置），存 %APPDATA%\XananRollCall\state.dat。
 * 名单单独用 names[][] 传，省得结构体太大。 */
typedef struct {
    double        w[STATE_MAX];
    int           haveWeights;
    int           n;
    int           count;
    long long     ver;
    long long     roundStart;
    int           roundSeconds;
    int           histMax;
    int           maxPick;
    int           defaultCount;
    int           nextId;
    int           nextUsed;
    int           seeded;                      /* 1 = 名单曾由服务器下发过，不再用 exe 里的出厂名单 */
    int           rosterRev;                   /* 本地名单对应的“名单版本号”（服务器发布时 +1） */
    char          rosterMd5[33];               /* 本地名单的 md5，用来发现名单被改过 */
    wchar_t       className[STATE_CLASS_WCHARS];
    wchar_t       nextName[STATE_NAME_WCHARS];
    wchar_t       subName[STATE_NAME_WCHARS];   /* 选中的科目（空 = 正常权重） */
    unsigned char hist[STATE_HIST][STATE_MAX];

    int           histN;
} StateData;

int  State_Exists(void);
void State_Load(StateData *sd, wchar_t names[][STATE_NAME_WCHARS], int *nameCount);
void State_Save(const StateData *sd, wchar_t names[][STATE_NAME_WCHARS]);

#endif
