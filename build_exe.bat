@echo off
rem m3u8dl.exe をビルドするスクリプト (Windows)。
rem 初回は .venv-build を作成して PyInstaller と pycryptodome をインストールする。
setlocal
cd /d %~dp0

if not exist .venv-build\Scripts\python.exe (
  echo [1/3] ビルド用の仮想環境を作成します...
  py -3.12 -m venv .venv-build || goto :error
  .venv-build\Scripts\python.exe -m pip install --upgrade pip || goto :error
  .venv-build\Scripts\python.exe -m pip install pyinstaller pycryptodome || goto :error
)

echo [2/3] テストを実行します...
.venv-build\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" || goto :error

echo [3/3] exe をビルドします...
.venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean m3u8dl.spec || goto :error

echo.
echo ビルド完了: dist\m3u8dl.exe
exit /b 0

:error
echo.
echo ビルドに失敗しました。
exit /b 1
