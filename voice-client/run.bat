@echo off
rem ============================================================
rem  Home Music Agent - Windows Voice Client launcher
rem
rem  Usage:
rem    run.bat                    GUI (hold button / F9 to talk)
rem    run.bat --voice            v0.3 always-on: say wake word, then talk
rem    run.bat --say "text"       skip audio, hit /agent directly (debug)
rem    run.bat --list-devices     list recording devices
rem    run.bat --record 6         record N sec + transcribe
rem    run.bat --mic-level 6      mic level only (no STT, no command)
rem    run.bat any_script.py      run that script directly (self-checks)
rem
rem  NOTE 1: pinned to the system interpreter. Bare "python" in Git Bash hits
rem          the managed runtime; site-packages are NOT shared, which causes
rem          "No module named 'openwakeword'".
rem  NOTE 2: do NOT add "chcp 65001" here. PowerShell 5.1 caches
rem          [Console]::OutputEncoding at startup, so switching the code page
rem          makes PowerShell's OWN Chinese output garbled, and Ctrl+C (how you
rem          stop --voice) skips any restore at the end. Instead, all scripts
rem          print GBK-safe text only; see _console.py.
rem  NOTE 3: keep this file pure ASCII. Non-ASCII bytes get mangled by cmd.exe
rem          under the default CP936 codepage and break parsing.
rem ============================================================
setlocal
set PY=AppData\Local\Programs\Python\Python313\python.exe
if not exist "%PY%" set PY=python
cd /d "%~dp0"

rem First arg is a .py script -> run the script itself, not voice_client.py
if /i "%~x1"==".py" goto :runscript

"%PY%" voice_client.py %*
goto :end

:runscript
"%PY%" %*

:end
endlocal
