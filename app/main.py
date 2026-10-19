"""spark-buddy Web 面板（FastAPI）。绑定 127.0.0.1，不对外暴露。"""
import os
import shutil
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import config, douyin, scheduler

INDEX = Path(__file__).parent / "static" / "index.html"
_stop = threading.Event()
_sched_thread = None
_login_thread = None


def _maybe_autopen():
    """SPARK_AUTOPEN=1（由 launch_panel.py 设置）时，服务就绪后自动打开浏览器。"""
    if os.environ.get("SPARK_AUTOPEN") != "1":
        return
    import webbrowser

    def _open_when_ready():
        try:
            webbrowser.open("http://127.0.0.1:8765")
        except Exception:
            pass

    threading.Timer(1.5, _open_when_ready).start()


def _plog(msg):
    """面板级事件写入 app.log（与 [task]/[douyin] 同格式，前缀 [panel]）。"""
    try:
        with open(config.LOGS / "app.log", "a", encoding="utf-8") as f:
            f.write("[%s] [panel] %s\n"
                    % (datetime.now().strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


_disk_ver_cache = (0.0, None)


def _disk_code_version():
    """重读磁盘上 config.py 的 CODE_VERSION。
    进程内存中的值不会随文件更新而变——这正是 2026-10-19 事故根因：
    10-17 启动的旧面板一直驻留到 10-19，两天的批次全跑的旧代码。"""
    import re
    global _disk_ver_cache
    now = time.time()
    if now - _disk_ver_cache[0] < 30:
        return _disk_ver_cache[1]
    try:
        with open(config.BASE / "app" / "config.py", encoding="utf-8") as f:
            m = re.search(r"CODE_VERSION\s*=\s*(\d+)", f.read())
        ver = int(m.group(1)) if m else None
    except Exception:
        ver = None
    _disk_ver_cache = (now, ver)
    return ver


def _spawn_replacement():
    """拉起新版面板完成热替换。优先 ShellExecuteW（同步返回码 >32 可即时校验，
    避免再犯“派发了就当成功”的错——2026-10-19 下午实测：Popen 派发的
    PowerShell 引号被转义层吞掉、进程秒退，面板毫不知情，热更新静默失效 1 小时）。
    失败再退回 WMI（与桌面快捷方式 start.bat 同路径）。
    启动后 120s 核验一次心跳版本是否翻转，未翻转必留日志。"""
    pyw = config.BASE / ".venv" / "Scripts" / "pythonw.exe"
    script = config.BASE / "launch_panel.py"
    if not (os.path.exists(pyw) and os.path.exists(script)):
        _plog("hot-restart: launcher files missing -> skip")
        return False
    ok = False
    try:
        import ctypes
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "open", str(pyw), '"%s"' % str(script), str(config.BASE), 0)
        ok = ret > 32
        _plog("hot-restart: ShellExecuteW -> %s (%s)"
              % (ret, "ok" if ok else "FAILED"))
    except Exception as e:
        _plog("hot-restart: ShellExecuteW error: %r" % e)
    if not ok:
        try:
            import subprocess
            cmdline = '"%s" "%s"' % (pyw, script)
            ps = ("$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                  "-Arguments @{CommandLine='%s';CurrentDirectory='%s'}; "
                  "if($r.ReturnValue -ne 0){exit 1}" % (cmdline, config.BASE))
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", ps],
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, close_fds=True)
            ok = True
            _plog("hot-restart: WMI spawn dispatched (unverified)")
        except Exception as e:
            _plog("hot-restart: WMI spawn failed: %r" % e)
            return False
    # 120s 后核验：心跳仍是本版本 => 替换没生效（替换成功时本进程已退出，不会走到这）
    def _verify():
        try:
            time.sleep(120)
            hb = config.load_alive()
            if hb.get("version") == config.CODE_VERSION:
                _plog("hot-restart: replacement did NOT take effect within 120s "
                      "(still v%s)" % config.CODE_VERSION)
        except Exception:
            pass
    threading.Thread(target=_verify, daemon=True).start()
    return True


def _heartbeat_loop(stop_event):
    """每 5 秒写一次心跳文件（PID/版本/忙碌状态）：启动器据此识别旧版本面板。
    2026-10-19 起兼任热更新哨兵：发现磁盘代码比本进程新且空闲时，
    自动拉起新版面板完成替换——用户不再需要手动双击快捷方式让新代码生效。"""
    import time as _t
    next_stale_check = 0.0
    last_spawn = 0.0
    while not stop_event.is_set():
        try:
            busy = bool(scheduler.task_running) or (
                _login_thread is not None and _login_thread.is_alive())
            config.save_alive({
                "pid": os.getpid(),
                "version": config.CODE_VERSION,
                "ts": _t.time(),
                "busy": busy,
            })
            now = _t.time()
            if now >= next_stale_check:
                next_stale_check = now + 60
                disk = _disk_code_version()
                if (disk and disk > config.CODE_VERSION and not busy
                        and now - last_spawn >= 180):
                    _plog("hot-restart: disk code v%s > running v%s -> spawn replacement"
                          % (disk, config.CODE_VERSION))
                    if _spawn_replacement():
                        last_spawn = now
        except Exception:
            pass
        stop_event.wait(5)


async def lifespan(app: FastAPI):
    global _sched_thread
    config.ensure_dirs()
    douyin.purge_old_shots()   # 启动即清理：截图只保留近一天（2026-10-19 用户要求）
    _stop.clear()
    _sched_thread = threading.Thread(target=scheduler.scheduler_loop, args=(_stop,), daemon=True)
    _sched_thread.start()
    threading.Thread(target=_heartbeat_loop, args=(_stop,), daemon=True).start()
    _maybe_autopen()
    yield
    _stop.set()


app = FastAPI(title="spark-buddy", lifespan=lifespan)


def _today():
    return datetime.now().strftime("%Y-%m-%d")


@app.get("/")
def index():
    return FileResponse(INDEX, media_type="text/html")


@app.get("/api/status")
def status():
    cfg = config.load_config()
    st = config.load_state()
    today = _today()
    ok = fail = 0
    for e in config.read_history(500):
        if e.get("time", "").startswith(today):
            if e.get("ok"):
                ok += 1
            else:
                fail += 1
    enabled = [t for t in cfg.get("targets", []) if t.get("enabled", True)]
    return {
        "nickname": st.get("nickname", ""),
        "login_ok": bool(st.get("login_ok")),
        "login_browser_open": bool(_login_thread and _login_thread.is_alive()),
        "task_running": scheduler.task_running,
        "today": {"total": len(enabled), "ok": ok, "fail": fail},
        "targets_enabled": len(enabled),
        "targets_total": len(cfg.get("targets", [])),
        "next": scheduler.next_run_info(),
        "last_run_date": st.get("last_run_date", ""),
    }


@app.post("/api/login/start")
def login_start():
    global _login_thread
    if _login_thread and _login_thread.is_alive():
        return {"started": False, "msg": "登录窗口已在运行，请在弹出的浏览器中扫码"}
    _login_thread = threading.Thread(target=douyin.login_flow, daemon=True)
    _login_thread.start()
    return {"started": True}


@app.get("/api/login/poll")
def login_poll():
    st = douyin.login_status()
    if st["done"]:
        config.update_state(login_ok=True, nickname=st["nickname"])
    return st


@app.post("/api/login/check")
def login_check():
    if scheduler.task_running or (_login_thread and _login_thread.is_alive()):
        return JSONResponse({"ok": False, "msg": "浏览器正忙，稍后再试"}, status_code=409)

    def worker():
        ok = douyin.check_logged_in()
        config.update_state(login_ok=ok)

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "msg": "正在检测登录态，几秒后刷新页面查看结果"}


@app.post("/api/logout")
def logout():
    if not douyin._BROWSER_LOCK.acquire(blocking=False):
        return JSONResponse({"ok": False, "msg": "浏览器正在被其他任务使用，稍后再试"}, status_code=409)
    try:
        if config.BROWSER_DATA.exists():
            shutil.rmtree(config.BROWSER_DATA, ignore_errors=True)
        config.BROWSER_DATA.mkdir(parents=True, exist_ok=True)
        config.update_state(login_ok=False, nickname="")
        return {"ok": True, "msg": "登录态已清除"}
    finally:
        douyin._BROWSER_LOCK.release()


@app.get("/api/config")
def get_config():
    return config.load_config()


@app.post("/api/config")
def set_config(cfg: dict):
    old = config.load_config()
    # 只允许覆盖已知字段，防脏数据
    clean = {k: cfg[k] for k in old if k in cfg}
    # targets：uid（抖音号）优先去重，无 uid 按昵称去重；兼容旧数据（无 uid）
    targets = []
    seen_keys = set()
    for t in cfg.get("targets", []):
        name = str(t.get("name", "")).strip()
        if not name:
            continue
        uid = str(t.get("uid", "")).strip()
        key = uid or ("name:" + name)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        targets.append({
            "name": name[:40],
            "uid": uid,
            "avatar": str(t.get("avatar", ""))[:500],
            "enabled": bool(t.get("enabled", True)),
        })
    clean["targets"] = targets
    clean["message"] = str(cfg.get("message", "")).strip() or "✨今日火花+1"
    try:
        clean["min_delay"] = max(3, int(clean.get("min_delay", 30)))
        clean["max_delay"] = max(clean["min_delay"] + 1, int(clean.get("max_delay", 90)))
    except Exception:
        clean["min_delay"], clean["max_delay"] = 30, 90
    if not str(clean.get("send_time", "10:00")).count(":"):
        clean["send_time"] = "10:00"
    config.save_config(clean)
    return {"ok": True, "config": clean}


@app.get("/api/history")
def history(limit: int = 100):
    return config.read_history(max(1, min(limit, 500)))


@app.post("/api/contacts/fetch")
def contacts_fetch():
    """打开消息中心读取会话列表（昵称/头像/预览/火花数）。同步执行，约需数秒。"""
    if scheduler.task_running or (_login_thread and _login_thread.is_alive()):
        return JSONResponse({"ok": False, "msg": "浏览器正忙（登录/发送中），稍后再试"}, status_code=409)
    contacts, shot, err = douyin.fetch_contacts()
    return {"ok": not err, "contacts": contacts, "shot": shot, "msg": err}


@app.post("/api/contacts/import-spark")
def contacts_import_spark():
    """一键导入全部带火花的好友（首次使用零配置起步）。
    只添加带火花且未在目标列表中的会话；已有的目标不动。"""
    if scheduler.task_running or (_login_thread and _login_thread.is_alive()):
        return JSONResponse({"ok": False, "msg": "浏览器正忙（登录/发送中），稍后再试"}, status_code=409)
    sessions, shot, err = douyin.fetch_sessions()
    if err and not sessions:
        return JSONResponse({"ok": False, "msg": err, "shot": shot})
    cfg = config.load_config()
    existing = set()
    for t in cfg.get("targets", []):
        existing.add((t.get("uid") or "").strip() or ("name:" + t.get("name", "")))
        existing.add("name:" + t.get("name", ""))
    added = []
    for c in sessions:
        name = str(c.get("name", "")).strip()
        try:
            spark = int(c.get("spark") or 0)
        except Exception:
            spark = 0
        if not name or spark <= 0:
            continue
        key = (c.get("uid") or "").strip() or ("name:" + name)
        if key in existing:
            continue
        cfg.setdefault("targets", []).append({
            "name": name,
            "uid": str(c.get("uid", "")),
            "avatar": str(c.get("avatar", "")),
            "enabled": True,
        })
        existing.add(key)
        existing.add("name:" + name)
        added.append(name)
    if added:
        config.save_config(cfg)
    return {
        "ok": True,
        "added": len(added),
        "names": added,
        "sessions": len(sessions),
        "msg": ("已导入 %d 位火花好友：%s" % (len(added), "、".join(added))) if added
               else "没有需要导入的（带火花的会话都已在目标列表中）",
    }


@app.post("/api/contacts/search")
def contacts_search(body: dict):
    """按昵称/抖音号搜索用户。body: {kw}"""
    if scheduler.task_running or (_login_thread and _login_thread.is_alive()):
        return JSONResponse({"ok": False, "msg": "浏览器正忙（登录/发送中），稍后再试"}, status_code=409)
    contacts, shot, err = douyin.search_users(str(body.get("kw", "")))
    return {"ok": not err, "contacts": contacts, "shot": shot, "msg": err}


@app.post("/api/contacts/enrich")
def contacts_enrich():
    """给配置里没有抖音号的目标补全：主站搜索逐人精确匹配（每人约 5 秒）。
    补全结果（抖音号+头像）写回配置长期保留。"""
    if scheduler.task_running or (_login_thread and _login_thread.is_alive()):
        return JSONResponse({"ok": False, "msg": "浏览器正忙（登录/发送中），稍后再试"}, status_code=409)
    cfg = config.load_config()
    need = {t["name"]: t.get("avatar", "") for t in cfg.get("targets", [])
            if t.get("enabled") and not t.get("uid")}
    if not need:
        return {"ok": True, "msg": "所有已启用的目标都有抖音号，无需补全", "found": 0, "total": 0}
    found = douyin.enrich_uid(need)
    if found:
        for t in cfg["targets"]:
            info = found.get(t["name"])
            if info and info.get("uid"):
                t["uid"] = info["uid"]
        config.save_config(cfg)
    miss = [n for n in need if n not in found]
    msg = "补全成功 %d/%d 人" % (len(found), len(need))
    if miss:
        msg += "；未匹配到： %s（可手动添加抖音号，或昵称与抖音号不一致导致）" % "、".join(miss[:6])
    return {"ok": True, "msg": msg, "found": len(found), "total": len(need), "miss": miss}


@app.post("/api/run-now")
def run_now():
    if scheduler.task_running:
        return JSONResponse({"ok": False, "msg": "任务正在运行中"}, status_code=409)
    if _login_thread and _login_thread.is_alive():
        return JSONResponse({"ok": False, "msg": "登录窗口开着，请先完成或关闭扫码登录"}, status_code=409)
    threading.Thread(target=scheduler.run_daily_task, kwargs={"mark_done": False}, daemon=True).start()
    return {"ok": True, "msg": "发送任务已启动（跳过今天已成功的目标）"}


@app.post("/api/send-one")
def send_one(body: dict):
    """给单个目标发送：text 留空则用统一内容。同步执行（约 10-40 秒）。"""
    if scheduler.task_running or (_login_thread and _login_thread.is_alive()):
        return JSONResponse({"ok": False, "msg": "浏览器正忙（登录/批量发送中），稍后再试"}, status_code=409)
    cfg = config.load_config()
    name = str(body.get("name", "")).strip()
    text = str(body.get("text", "")).strip() or scheduler.build_message(cfg)
    if not name:
        return JSONResponse({"ok": False, "msg": "缺少目标昵称"}, status_code=400)
    if not any(t["name"] == name for t in cfg.get("targets", [])):
        return JSONResponse({"ok": False, "msg": "「%s」不在目标列表中" % name}, status_code=400)

    scheduler.task_running = True
    try:
        ok, detail = douyin.send_message(name, text)
        config.add_history({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "name": name,
            "ok": ok,
            "detail": detail,
            "text": text,
        })
        scheduler._flush_avatars()   # 单独发送也可能顺手抓到头像
        return {"ok": ok, "msg": ("%s：%s" % (name, detail)) if ok else ("%s 发送失败：%s" % (name, detail))}
    finally:
        scheduler.task_running = False


@app.post("/api/shutdown")
def shutdown():
    """停止面板进程（面板以无窗口方式后台运行时，由此接口负责退出）。"""
    if scheduler.task_running:
        return JSONResponse({"ok": False, "msg": "发送任务进行中，无法停止"}, status_code=409)
    if _login_thread and _login_thread.is_alive():
        return JSONResponse({"ok": False, "msg": "登录窗口开着，请先完成或关闭扫码登录"}, status_code=409)

    def _exit():
        time.sleep(0.6)  # 等响应先发回浏览器
        os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()
    return {"ok": True, "msg": "面板正在停止，定时发送随之暂停。下次双击快捷方式即可重新启动。"}


@app.get("/api/logs")
def logs(n: int = 120):
    f = config.LOGS / "app.log"
    if not f.exists():
        return PlainTextResponse("(暂无日志)")
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    return PlainTextResponse("\n".join(lines[-max(10, min(n, 500)):]))


@app.get("/shots/{name}")
def shot(name: str):
    if "/" in name or "\\" in name or ".." in name:
        return JSONResponse({"error": "bad name"}, status_code=400)
    p = config.SHOTS / name
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, media_type="image/png")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
