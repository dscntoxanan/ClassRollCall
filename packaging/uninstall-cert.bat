@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

rem 本机安装过的测试证书指纹（与 ClassRollCall.cer 一致，SHA-1）
set "THUMB=PUT-YOUR-CERT-THUMBPRINT-HERE"

if /i "%~1"=="--elevated" goto :run

rem 与 install-cert.bat 同一套判据：fltmc 不依赖任何服务，避免误判后无限提权
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
echo    正在移除证书信任 ...

rem certutil 的失败码是负的有符号数，cmd 的 "if errorlevel 1" 判不出来，
rem 这里不靠它判断，而是最后用 -store 复核。
certutil -delstore Root "%THUMB%" >nul 2>&1
certutil -delstore TrustedPublisher "%THUMB%" >nul 2>&1
certutil -user -delstore Root "%THUMB%" >nul 2>&1
certutil -user -delstore TrustedPublisher "%THUMB%" >nul 2>&1

certutil -store Root "%THUMB%" >nul 2>&1
if "%errorlevel%"=="0" goto :still

echo    完成：证书已从本机移除，exe 会恢复显示「未知发布者」。
echo.
pause
exit /b 0

:still
echo.
echo    [失败] 证书仍在「受信任的根证书颁发机构」里（可能被组策略锁定）。
echo    手动删除：certmgr.msc -^> 受信任的根证书颁发机构 -^> 删除 WhiteShadow Studio
echo.
pause
exit /b 1
