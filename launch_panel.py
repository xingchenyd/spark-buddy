# -*- coding: utf-8 -*-
"""静默后台启动入口（由 venv 的 pythonw.exe 运行，无控制台窗口）。

- stdout/stderr 重定向到 logs/panel_bg.log：pythonw 下标准流默认无效，
  任何异常都无处可看；重定向后 uvicorn 横幅与 traceback 都留档可查。
- 端口 8765 已被占用时：
  * 运行中的是同版本面板 → 直接打开浏览器并退出（重复双击无害）；
  * 运行中的是**旧版本**面板（代码更新后最常见）→ 先调用旧面板的
    「停止面板」接口优雅退出，再启动本（最新）版本——杜绝"双击了
    却还是旧代码在跑"；旧面板正在发送任务时不强杀，提示稍后再试。
- 设置 SPARK_AUTOPEN=1，由 app.main 在服务就绪后自动打开浏览器
  （重启后 Python 冷启动可能需要近 1 分钟，浏览器会自动弹出，无需等待）。
"""
import datetime
import json
import os
import socket
import sys
import time
import urllib.request

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

from app import config  # noqa: E402  （只依赖标准库，安全）


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


def _running_version():
    """读运行中面板的心跳：返回 (version, fresh)。读不到返回 (None, False)。"""
    try:
        with open(config.ALIVE_PATH, encoding="utf-8") as f:
            hb = json.load(f)
        fresh = (time.time() - float(hb.get("ts", 0))) < 20
        return hb.get("version"), fresh
    except Exception:
        return None, False


def _graceful_shutdown():
    """让运行中的面板自己退出（发送任务进行中会被拒绝）。返回是否成功。"""
    try:
        req = urllib.request.Request("http://127.0.0.1:8765/api/shutdown", method="POST")
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_port_free(timeout_s=15):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _port_open():
            return True
        time.sleep(0.5)
    return not _port_open()


import webbrowser  # noqa: E402

if _port_open():
    ver, fresh = _running_version()
    same = fresh and ver == config.CODE_VERSION
    if same:
        _bg.write("panel already running (v%s) -> open browser and exit\n" % ver)
        webbrowser.open("http://127.0.0.1:8765")
        sys.exit(0)
    # 运行中的是旧版本（或心跳异常的）面板：尝试优雅替换
    _bg.write("stale panel detected (running=%s, latest=%s) -> replacing\n"
              % (ver, config.CODE_VERSION))
    if _graceful_shutdown() and _wait_port_free():
        _bg.write("old panel stopped gracefully\n")
    else:
        _bg.write("old panel busy or refused shutdown -> keep it, open browser\n")
        webbrowser.open("http://127.0.0.1:8765")
        sys.exit(0)

os.environ["SPARK_AUTOPEN"] = "1"

import faulthandler  # noqa: E402
import uvicorn  # noqa: E402

# 硬崩溃（原生段错误/访问违例）也留痕到日志，杜绝"无声死亡查不到原因"
faulthandler.enable(_bg)

try:
    uvicorn.run("app.main:app", host="127.0.0.1", port=8765, log_level="warning")
finally:
    _bg.write("==== panel exited at %s ====\n"
              % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _bg.flush()
