"""每日定时调度：到点执行发送任务；面板启动后若错过时间且今天未发，自动补跑。"""
import random
import threading
import time
from datetime import datetime

from . import config, douyin

task_running = False
_task_lock = threading.Lock()


def _now():
    return datetime.now()


def _today():
    return _now().strftime("%Y-%m-%d")


def build_message(cfg):
    """统一发送内容（所有人一样），可选附加一言。"""
    text = (cfg.get("message") or "").strip() or "✨今日火花+1"
    if cfg.get("use_hitokoto"):
        try:
            import httpx

            r = httpx.get("https://v1.hitokoto.cn/?encode=text", timeout=5)
            if r.status_code == 200 and r.text.strip():
                text = text + "\n" + r.text.strip()
        except Exception:
            pass  # 一言失败不影响主消息
    return text


def _log(msg):
    line = "[%s] [task] %s" % (_now().strftime("%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    (config.LOGS / "app.log").open("a", encoding="utf-8").write(line + "\n")


def run_daily_task(mark_done=True):
    """执行一次发送任务：跳过今天已成功的目标。返回统计 dict。"""
    global task_running
    with _task_lock:
        if task_running:
            return {"skipped": "任务已在运行中"}
        task_running = True
    try:
        cfg = config.load_config()
        today = _today()
        ok_names = config.today_ok_names(today)
        targets = [t for t in cfg.get("targets", []) if t.get("enabled", True)]
        todo = [t for t in targets if t["name"] not in ok_names]
        _log("start: %d 目标, %d 已发, %d 待发" % (len(targets), len(targets) - len(todo), len(todo)))
        if not todo:
            if mark_done:
                config.update_state(last_run_date=today)
            return {"total": 0, "ok": 0, "fail": 0}

        stat = {"total": len(todo), "ok": 0, "fail": 0}
        lo = max(3, int(cfg.get("min_delay", 30)))
        hi = max(lo + 1, int(cfg.get("max_delay", 90)))
        text = build_message(cfg)
        for i, t in enumerate(todo):
            name = t["name"].strip()
            ok, detail = douyin.send_message(name, text)
            config.add_history({
                "time": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "name": name,
                "ok": ok,
                "detail": detail,
                "text": text,
            })
            stat["ok" if ok else "fail"] += 1
            if not ok and isinstance(detail, str) and "登录态已失效" in detail:
                _log("登录失效，终止本次任务")
                break
            if i < len(todo) - 1:
                d = random.uniform(lo, hi)
                _log("等待 %.0fs 后发送下一个" % d)
                time.sleep(d)
        if mark_done:
            # 当天只跑一轮：失败的目标可用面板「立即发送」补发
            config.update_state(last_run_date=today)
        _log("finished: %s" % stat)
        return stat
    finally:
        task_running = False


def scheduler_loop(stop_event):
    _log("scheduler started (tick 30s)")
    while not stop_event.is_set():
        try:
            cfg = config.load_config()
            hh, mm = (cfg.get("send_time") or "10:00").split(":")[:2]
            target = _now().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            last = config.load_state().get("last_run_date", "")
            today = _today()
            if last != today and _now() >= target:
                if cfg.get("catch_up", True) or _now() <= target.replace(hour=23, minute=59):
                    if not task_running:
                        _log("到点触发（last=%s today=%s）" % (last, today))
                        run_daily_task(mark_done=True)
        except Exception as e:
            _log("scheduler error: %r" % e)
        stop_event.wait(30)


def next_run_info():
    cfg = config.load_config()
    hh, mm = (cfg.get("send_time") or "10:00").split(":")[:2]
    target = _now().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    last = config.load_state().get("last_run_date", "")
    done_today = last == _today()
    return {
        "send_time": cfg.get("send_time"),
        "done_today": done_today,
        "next_run": None if done_today else target.strftime("%Y-%m-%d %H:%M"),
    }
