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
    wchar_t       className[STATE_CLASS_WCHARS];
    wchar_t       nextName[STATE_NAME_WCHARS];
    unsigned char hist[STATE_HIST][STATE_MAX];
    int           histN;
} StateData;

int  State_Exists(void);
void State_Load(StateData *sd, wchar_t names[][STATE_NAME_WCHARS], int *nameCount);
void State_Save(const StateData *sd, wchar_t names[][STATE_NAME_WCHARS]);

#endif
