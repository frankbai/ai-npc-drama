#!/bin/bash
# macOS：双击启动，浏览器会自动打开。关闭这个终端窗口即停止。
# 第一次双击如果提示"无法打开"，在 Finder 里右键 → 打开；或者在终端里运行 chmod +x start.command
cd "$(dirname "$0")"
PORT=8800
(sleep 1.5; open "http://127.0.0.1:$PORT/") &
python3 run.py --port $PORT --host 127.0.0.1
