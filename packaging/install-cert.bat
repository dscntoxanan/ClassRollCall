@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

rem 已经提权过一次就直接干活，绝不再提权一次（避免 UAC 死循环）
if /i "%~1"=="--elevated" goto :run

rem fltmc 只有管理员能执行，且不依赖 Server 服务
rem （net session 依赖 LanmanServer，机房镜像里常被停掉，会误判成“没提权”而无限弹 UAC）
fltmc >nul 2>&1
if "%errorlevel%"=="0" goto :run

echo.
echo    正在请求管理员权限，请在弹窗里点「是」...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs"
if not "%errorlevel%"=="0" (
    echo.
    echo    提权被取消，或系统禁止提权。
    echo    请右键本文件 -^> 以管理员身份运行。
    echo.
    pause
    exit /b 1
)
exit /b 0

:run
echo.
echo    正在导入证书 ClassRollCall.cer ...

rem 注意：certutil 失败时返回 0x80070005（有符号是负数），
rem cmd 的 "if errorlevel 1" 对这种负数不成立，必须用字符串比较，否则会把失败当成功。
certutil -addstore -f Root "ClassRollCall.cer" >nul
if not "%errorlevel%"=="0" goto :fail_root

certutil -addstore -f TrustedPublisher "ClassRollCall.cer" >nul
if not "%errorlevel%"=="0" goto :fail_pub

echo    完成。以后这个 exe 的发布者会显示为 “WhiteShadow Studio”。
echo    不装证书也能正常用，只是属性里显示「未知发布者」。
echo.
echo    卸载方法：certmgr.msc -^> 受信任的根证书颁发机构 -^> 删除 WhiteShadow Studio
echo.
pause
exit /b 0

:fail_root
echo.
echo    [失败] 导入「受信任的根证书颁发机构」失败。
echo    常见原因：
echo      1) 没有真正拿到管理员权限
echo      2) ClassRollCall.cer 没有和本文件放在同一个目录
echo      3) 系统策略禁止修改证书存储
echo.
pause
exit /b 1

:fail_pub
echo.
echo    [失败] 导入「受信任的发布者」失败（根证书已导入成功）。
echo    这通常不影响使用，exe 仍可运行。
echo.
pause
exit /b 1
