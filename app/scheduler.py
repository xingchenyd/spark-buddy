"""每日定时调度：到点执行发送任务；面板启动后若错过时间且今天未发，自动补跑。

2026-11-16 结构性修复：
- 整轮只启动一次浏览器（begin_batch / end_batch），不再每人冷启动一次；
- 基础设施故障（浏览器缺失、网络不通、登录失效）整轮短路：不消耗目标、
  不写失败历史、不把当天标记为已跑，5 分钟后自动重试，且只补未成功的目标。
"""
import random
import threading
import time
from datetime import datetime

from . import config, douyin

task_running = False
_task_lock = threading.Lock()

# 最近一次基础设施故障（供面板 /api/status 展示）与退避时间戳
last_infra_error = ""
_next_attempt = 0.0
_INFRA_BACKOFF_S = 300


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


def _flush_avatars():
    """把发送时顺手抓到的头像（douyin.AVATAR_HARVEST）写回缺头像的目标。"""
    try:
        harvested = getattr(douyin, "AVATAR_HARVEST", None)
        if not harvested:
            return
        cfg = config.load_config()
        changed = []
        for t in cfg.get("targets", []):
            if (t.get("avatar") or "").strip():
                continue
            av = (harvested.get(t.get("name")) or "").strip()
            if av:
                t["avatar"] = av
                changed.append(t.get("name"))
        if changed:
            config.save_config(cfg)
            _log("头像已随发送补全: %s" % "、".join(changed))
    except Exception as e:
        _log("flush avatars error: %r" % e)


def _send_round(todo, cfg, page):
    """发送一轮：在已打开的浏览器里逐个发送（失败不当场重试）。

    InfraError 不在此捕获，直接冒泡到 run_daily_task 整轮短路。
    """
    stat = {"total": len(todo), "ok": 0, "fail": 0}
    lo = max(3, int(cfg.get("min_delay", 30)))
    hi = max(lo + 1, int(cfg.get("max_delay", 90)))
    text = build_message(cfg)
    for i, t in enumerate(todo):
        name = t["name"].strip()
        ok, detail = douyin.send_message(name, text, page=page)
        config.add_history({
            "time": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "name": name,
            "ok": ok,
            "detail": detail,
            "text": text,
        })
        stat["ok" if ok else "fail"] += 1
        if i < len(todo) - 1:
            d = random.uniform(lo, hi)
            _log("等待 %.0fs 后发送下一个" % d)
            time.sleep(d)
    return stat


def run_daily_task(mark_done=True):
    """执行一次发送任务：跳过今天已成功的目标。返回统计 dict。

    基础设施故障返回 {"infra_error": ...}：不写任何失败历史、不更新
    last_run_date，调度器退避 5 分钟后自动重试，只会补今天尚未成功的目标。
    """
    global task_running, last_infra_error, _next_attempt
    with _task_lock:
        if task_running:
            return {"skipped": "任务已在运行中"}
        task_running = True
    page = None
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
            _flush_avatars()
            return {"total": 0, "ok": 0, "fail": 0}

        # 预检（浏览器 / 网络 / 登录态）+ 整轮复用同一个浏览器
        page = douyin.begin_batch()
        last_infra_error = ""
        stat = _send_round(todo, cfg, page)
        if cfg.get("second_round") and stat["ok"]:
            ok_now = config.today_ok_names(today)
            failed = [t for t in todo if t["name"] not in ok_now]
            if failed:
                _log("second round: 对 %d 个失败目标追加一轮" % len(failed))
                stat2 = _send_round(failed, cfg, page)
                stat["ok"] += stat2["ok"]
                stat["fail"] = stat["total"] - stat["ok"]
        if mark_done:
            config.update_state(last_run_date=today)
        _flush_avatars()
        _log("finished: %s" % stat)
        return stat
    except douyin.InfraError as e:
        last_infra_error = str(e)
        _next_attempt = time.time() + _INFRA_BACKOFF_S
        _log("基础设施故障，整轮中止（不记失败、不标记完成，5 分钟后自动重试）: %s" % e)
        return {"infra_error": str(e)}
    finally:
        if page is not None:
            douyin.end_batch()
        task_running = False


def scheduler_loop(stop_event):
    global _next_attempt
    _log("scheduler started (tick 30s, startup grace 20s)")
    # 开机/重启后代理与网络往往需要十几秒才稳定，先别急着补发
    _next_attempt = time.time() + 20
    while not stop_event.is_set():
        try:
            cfg = config.load_config()
            hh, mm = (cfg.get("send_time") or "10:00").split(":")[:2]
            target = _now().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            last = config.load_state().get("last_run_date", "")
            today = _today()
            if (last != today and _now() >= target and time.time() >= _next_attempt):
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
