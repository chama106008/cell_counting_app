@echo off
setlocal ENABLEEXTENSIONS ENABLEDELAYEDEXPANSION
cd /d "%~dp0"
chcp 65001 >nul

:: ====== 0) Python 検出（py → python）======
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"

if not defined PY (
  :: 見やすいポップアップ（なければ普通の echo に置換してOK）
  powershell -NoProfile -Command ^
    "Add-Type -AssemblyName PresentationFramework;[System.Windows.MessageBox]::Show('Python 3.12+ が見つかりません。インストールしてから再実行してください。','CellCountApp',[System.Windows.MessageBoxButton]::OK,[System.Windows.MessageBoxImage]::Warning)" 
  echo [ERROR] Python not found
  goto :EOF
)

for /f "delims=" %%V in ('%PY% -V 2^>^&1') do set "PYVER=%%V"
echo Using: %PY%  (%PYVER%)

:: ====== 1) 初回セットアップの所要時間の確認（Yesなら続行 / Noなら終了）======
powershell -NoProfile -Command ^
  "Add-Type -AssemblyName PresentationFramework; if([System.Windows.MessageBox]::Show('初回セットアップには数分かかる場合があります。続行しますか？','CellCountApp',[System.Windows.MessageBoxButton]::YesNo,[System.Windows.MessageBoxImage]::Information) -ne 'Yes'){exit 1}"
if errorlevel 1 goto :EOF

:: ====== 2) ここから先は “元の run_app.bat の本文” をそのまま呼び出す ======
call :ORIGINAL
set "RC=%ERRORLEVEL%"

:: ====== 3) 失敗時は閉じずに残す（ログを読む時間の猶予用）======
if not "%RC%"=="0" (
  echo.
  echo [ERROR] run_app 内部でエラーが発生しました。ERRORLEVEL=%RC%
  echo ウィンドウを閉じる前にメッセージを確認してください。
  pause
)

endlocal
goto :EOF



@echo off
setlocal ENABLEDELAYEDEXPANSION

REM --- move to script dir ---
cd /d "%~dp0"

REM --- Python が無ければ埋め込み版を試す or システム python を使う ---
where python >nul 2>nul
if %errorlevel% neq 0 (
  echo Python が見つかりません。Windows ストア版や公式 Python をインストールしてください。
  pause
  exit /b 1
)

REM --- venv 準備 ---
if not exist ".venv" (
  echo [SETUP] creating venv...
  python -m venv .venv
)

REM --- venv 有効化 ---
call .venv\Scripts\activate

REM --- pip 更新 ---
python -m pip install -U pip wheel

REM --- 依存インストール（torchはGPU向けを個別に） ---
if not exist ".venv\installed.flag" (
  echo [SETUP] installing python packages...
  REM ※ まず CPU 版で動かしたい場合は次の行だけでOK：
  REM python -m pip install -r requirements.txt

  REM GPU を使う場合（CUDA12.1のPyTorch）：
  python -m pip uninstall -y torch torchvision torchaudio 2>nul
  python -m pip install --index-url https://download.pytorch.org/whl/cu121 torch torchvision torchaudio

  python -m pip install -r requirements.txt

  echo ok> .venv\installed.flag
)

REM ---- Force Qt plugin paths to venv ----
set "QT_BASE=%VIRTUAL_ENV%\Lib\site-packages\PyQt5\Qt"
set "QT_PLUGIN_PATH=%QT_BASE%\plugins"
set "QT_QPA_PLATFORM_PLUGIN_PATH=%QT_PLUGIN_PATH%\platforms"
set "QT_QPA_PLATFORM=windows"

REM --- Qtレポート ---
set QT_DEBUG_PLUGINS=1

REM --- アプリ起動 ---
python app.py


REM === 最後に必ず戻る ===
goto :EOF
