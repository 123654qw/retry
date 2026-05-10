@echo off
chcp 65001 >nul
echo [Retry] 安装依赖...
py -3 -m pip install pyinstaller pystray pillow --quiet

echo [Retry] 清理旧构建...
if exist build  rmdir /s /q build
if exist dist   rmdir /s /q dist
if exist Retry.spec del /q Retry.spec

echo [Retry] 开始打包...
py -3 -m PyInstaller ^
  --onefile ^
  --noconsole ^
  --name Retry ^
  --icon icon.ico ^
  --add-data "icon.ico;." ^
  --hidden-import pystray ^
  --hidden-import pystray._win32 ^
  --hidden-import PIL ^
  --hidden-import PIL.Image ^
  --hidden-import PIL.ImageDraw ^
  --collect-all pystray ^
  retry.py

if exist dist\Retry.exe (
    echo.
    echo ============================================
    echo  打包成功！
    echo  exe 路径: dist\Retry.exe
    echo ============================================
) else (
    echo.
    echo [错误] 打包失败，请查看上方日志
)
pause
