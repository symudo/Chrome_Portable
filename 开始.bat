@echo off
chcp 65001 >nul
setlocal DisableDelayedExpansion

set "WORK_DIR=%~dp0"
set "APP_DIR=%WORK_DIR%Chrome"
set "CHROME_EXE="

if exist "%APP_DIR%\chrome.exe" (
    set "CHROME_EXE=%APP_DIR%\chrome.exe"
    goto :found
)

:: 兼容旧版：chrome.exe 在版本目录中
for /d %%d in ("%APP_DIR%\*") do (
    if exist "%%d\chrome.exe" (
        set "CHROME_EXE=%%d\chrome.exe"
        goto :found
    )
)

echo 未找到 chrome.exe
pause
exit /b 1

:found
set "LNK_PATH=%WORK_DIR%Chrome.lnk"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = New-Object -ComObject WScript.Shell; $l = $s.CreateShortcut($env:LNK_PATH); $l.TargetPath = $env:CHROME_EXE; $l.Arguments = ''; $l.WorkingDirectory = $env:WORK_DIR; $l.IconLocation = $env:CHROME_EXE + ',0'; $l.Save()"

if exist "%LNK_PATH%" (
    echo 快捷方式创建成功
    setlocal EnableDelayedExpansion
    echo 指向：!CHROME_EXE!
    endlocal
    exit /b 0
) else (
    echo 快捷方式创建失败
    pause
    exit /b 1
)
