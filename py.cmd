@echo off
chcp 65001 >nul
rem （中文注释需要 65001 代码页；写在 chcp 之前会被解析错乱）
rem 说明：本脚本存在的唯一原因，是让 pwsh 不必去捕获外部程序的 stdio；
rem 受限沙箱下那样会直接 Access is denied，而 cmd 的文件重定向不受影响。
rem 输出固定按 UTF-8 写进 py_last.log，读它的方式：
rem   Get-Content py_last.log -Encoding UTF8
rem 用法：cmd /c py.cmd your_script.py [参数...]（退出码原样透传）
rem
setlocal
set "PY=python"
set "LOG=%~dp0py_last.log"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

rem 找不到解释器就把原因写进 py_last.log，并用 9009（命令未找到的惯用码）退出
if not exist "%PY%" (
  echo [py.cmd] interpreter not found: %PY%>"%LOG%"
  exit /b 9009
)

rem --- 内存闸门：可用提交内存低于 700 MB 就拒绝启动，避免 0xc0000142 ---
rem 内存闸门前置执行：内存见底时它返回 3，本脚本随即以 3 退出，不抛 0xc0000142
"%PY%" -u "%~dp0ops\_memguard.py" >"%LOG%" 2>&1
if %ERRORLEVEL% EQU 3 exit /b 3

rem 真正执行：-u 关缓冲，stdout 与 stderr 都追加进 py_last.log
"%PY%" -u %* >>"%LOG%" 2>&1
rem 退出码原样透传，调用方用 errorlevel 判断成败才不会被吞掉
exit /b %ERRORLEVEL%
