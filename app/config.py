"""spark-buddy 配置与数据存储（JSON / JSONL，线程安全）。"""
import json
import os
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
SHOTS = DATA / "screenshots"
LOGS = BASE / "logs"
BROWSER_DATA = BASE / "browser_data"

CONFIG_PATH = DATA / "config.json"
STATE_PATH = DATA / "state.json"
HISTORY_PATH = DATA / "history.jsonl"

_lock = threading.Lock()

# 面板代码版本（构建序号 YYYYMMDD+当日两位序号）：每次改动代码必须 bump。
# 双击启动器据此自动替换旧面板（2026-10-18）；面板心跳每分钟自检磁盘版本，
# 发现自己过时且空闲时自动拉起新版完成热替换，无需用户手动重启（2026-10-19）。
CODE_VERSION = 2026101902

ALIVE_PATH = DATA / "panel_alive.json"


def load_alive():
    try:
        with open(ALIVE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_alive(obj):
    try:
        tmp = str(ALIVE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, str(ALIVE_PATH))
    except Exception:
        pass

DEFAULT_CONFIG = {
    "send_time": "10:00",          # 每日发送时间 HH:MM
    "catch_up": True,              # 错过时间后启动面板是否补发
    "min_delay": 30,               # 每个目标之间最小间隔（秒）
    "max_delay": 90,               # 每个目标之间最大间隔（秒）
    "use_hitokoto": False,         # 附加一言 API 随机句子
    "second_round": False,         # 全部跑完一遍后对失败者追加第二轮（默认关闭）
    "message": "今日火花+1，互帮互助 🔥",  # 统一发送内容（所有人一样）
    "targets": [],                 # [{"uid": "抖音号", "name": "昵称", "avatar": "URL", "enabled": true}]
}


def _migrate(cfg):
    """兼容旧版配置：messages 数组 → message 单字段。"""
    if "message" not in cfg and isinstance(cfg.get("messages"), list) and cfg["messages"]:
        cfg["message"] = str(cfg["messages"][0])
    cfg.pop("messages", None)
    return cfg


def ensure_dirs():
    for p in (DATA, SHOTS, LOGS, BROWSER_DATA):
        p.mkdir(parents=True, exist_ok=True)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    with _lock:
        cfg = _read_json(CONFIG_PATH, {})
        merged = dict(DEFAULT_CONFIG)
        merged.update({k: v for k, v in cfg.items() if v is not None})
        return _migrate(merged)


def save_config(cfg):
    with _lock:
        _write_json(CONFIG_PATH, cfg)


def load_state():
    with _lock:
        return _read_json(STATE_PATH, {})


def update_state(**kv):
    with _lock:
        st = _read_json(STATE_PATH, {})
        st.update(kv)
        _write_json(STATE_PATH, st)
        return st


def add_history(entry):
    """entry: {time, name, ok, detail, screenshot}"""
    with _lock:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_history(limit=200):
    if not HISTORY_PATH.exists():
        return []
    with _lock:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def today_ok_names(today_str):
    """今天已成功发送的目标昵称集合。"""
    names = set()
    for e in read_history(500):
        if e.get("time", "").startswith(today_str) and e.get("ok"):
            names.add(e.get("name"))
    return names
