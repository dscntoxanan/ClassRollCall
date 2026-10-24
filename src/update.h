#ifndef UPDATE_H
#define UPDATE_H

/* 静默热更新：
 *  - 启动时后台拉一次版本清单，有新版本就下载并校验，存到 %APPDATA%\XananRollCall\pending.exe
 *  - 退出时如果存在待安装文件，启动一个隐藏的自身副本去替换 exe（此时原进程已退出，文件不再被占用）
 *  - 全程不弹窗、不提示、不写注册表 */

void Update_StartCheck(void);

/* 退出前调用；有 pending 就安排替换并返回 TRUE（调用方应立即退出） */
BOOL Update_ApplyPending(void);

/* 以 `--apply-update <pid> <pending> <target>` 启动时走这里，返回退出码 */
int Update_HelperMain(LPWSTR cmdline);

#endif
