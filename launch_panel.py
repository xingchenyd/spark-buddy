# -*- coding: utf-8 -*-
"""静默后台启动入口（由 venv 的 pythonw.exe 运行，无控制台窗口）。

- stdout/stderr 重定向到 logs/panel_bg.log：pythonw 下标准流默认无效，
  任何异常都无处可看；重定向后 uvicorn 横幅与 traceback 都留档可查。
- 若面板已在运行（端口 8765 已监听）：直接打开浏览器并退出，
  因此重复双击快捷方式无害。
- 设置 SPARK_AUTOPEN=1，由 app.main 在服务就绪后自动打开浏览器
  （重启后 Python 冷启动可能需要近 1 分钟，浏览器会自动弹出，无需等待）。
"""
import datetime
import os
import socket
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LOG_DIR = os.path.join(ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_bg = open(os.path.join(LOG_DIR, "panel_bg.log"), "a", buffering=1,
           encoding="utf-8", errors="replace")
_bg.write("\n==== launch_panel start %s ====\n"
          % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
sys.stdout = _bg
sys.stderr = _bg


def _port_open():
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 8765))
        return True
    except Exception:
        return False
    finally:
        s.close()


if _port_open():
    import webbrowser

    _bg.write("panel already running -> open browser and exit\n")
    webbrowser.open("http://127.0.0.1:8765")
    sys.exit(0)

os.environ["SPARK_AUTOPEN"] = "1"

import uvicorn

uvicorn.run("app.main:app", host="127.0.0.1", port=8765, log_level="warning")
