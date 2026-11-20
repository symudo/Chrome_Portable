@echo off
chcp 65001 >nul
echo 正在注入 Chrome++ 到 chrome.exe ...

set "TARGET_EXE=chrome.exe"
if not "%~1"=="" set "TARGET_EXE=%~1"

if not exist "%TARGET_EXE%" (
    if exist "Chrome\%TARGET_EXE%" set "TARGET_EXE=Chrome\%TARGET_EXE%"
)

if not exist "%TARGET_EXE%" (
    echo [错误] 未找到目标程序: %TARGET_EXE%
    pause
    exit /b 1
)

if "%PROCESSOR_ARCHITECTURE%"=="AMD64" (
    setdll-x64.exe /d:version.dll "%TARGET_EXE%"
) else if "%PROCESSOR_ARCHITECTURE%"=="ARM64" (
    setdll-arm64.exe /d:version.dll "%TARGET_EXE%"
) else (
    setdll-x86.exe /d:version.dll "%TARGET_EXE%"
)

if %ERRORLEVEL% equ 0 (
    echo [成功] 已成功向 %TARGET_EXE% 注入 version.dll!
) else (
    echo [失败] 注入失败，错误码: %ERRORLEVEL%
)

pause
