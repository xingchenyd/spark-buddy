"""抖音网页版（主站消息中心）浏览器自动化。

设计原则：
- 登录只用"手机扫码"，登录态存在 browser_data 持久化目录，一次登录长期复用；
- 是否已登录用 cookie（sessionid / sessionid_ss）判断，不依赖页面 DOM，最可靠；
- 发消息走真实浏览器 + 强校验（文本严格写入输入框 → 发送后输入框清空或新增
  我方消息行）；基础设施级故障整轮短路并自动延后重试，不产生假失败记录；
- 会话定位用官方 data-e2e 锚点（im-entry / im-dialog / conversation-item），
  搜索兜底只允许侧栏内部搜索框，绝不触碰主站顶部搜索框；
- user_data_dir 同一时刻只能被一个浏览器实例占用，全部会话经 _BROWSER_LOCK 串行化。
"""
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import config

# Playwright 的浏览器默认装在全局 %LOCALAPPDATA%\ms-playwright，任何其它项目
# 升级 playwright 或清理缓存都可能删掉本项目锁定的 chromium-1194。
# 2026-11-16 事故即由此引起：本机只剩 chromium-1248，1.56.0 却硬找 1194，
# 当天 10 个目标全部报 "Executable doesn't exist" 并被写成失败、还把当天标记成已跑。
# 现在统一改为：浏览器装到项目私有目录 + 显式 executable_path 自动发现可用浏览器。
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(config.BROWSERS_DIR)

# 2026-10: im.douyin.com 已被抖音下线（权威 DNS NXDOMAIN）。
# 登录入口改用创作者平台（未登录会自动渲染扫码层）；发消息走主站消息中心。
LOGIN_URL = "https://creator.douyin.com/"
IM_URL = "https://www.douyin.com/message"
COOKIE_URL = "https://www.douyin.com/"
SESSION_COOKIES = ("sessionid", "sessionid_ss")

# 窗口位置：(x, y, w, h)。登录窗口必须落在屏幕内且置顶；发送流程放屏幕外不打扰用户
WIN_ONSCREEN = (80, 40, 1200, 820)
WIN_OFFSCREEN = (2000, 2000, 1280, 900)

# ---- selector 候选链（按顺序尝试；页面改版时优先改这里）----
# 2026-10 实测：消息侧栏有官方 data-e2e 锚点：im-entry / im-dialog / conversation-item
# 消息中心入口（主站右上角）
MSG_ENTRY_SELECTORS = [
    "[data-e2e='im-entry']",
    "[data-e2e='message-entry']",
    "div[aria-label*='消息']",
    "[class*='message'][class*='entry']",
]
# 消息侧栏容器（侧栏打开的标志）
IM_DIALOG = "[data-e2e='im-dialog']"
# 会话列表项（首行文本 = 昵称）
CONVERSATION_ITEM = "[data-e2e='conversation-item']"
# 侧栏内部的搜索框（绝不能用主站顶部搜索框 data-e2e='searchbar-input'！）
SIDEBAR_SEARCH_SELECTORS = [
    "[data-e2e='im-dialog'] input[placeholder*='搜索']",
    "input[class*='im-saas-input']",
    "[class*='searchSearchInput'] input",
]
INPUT_SELECTORS = [
    "div[contenteditable='true']",
    "textarea[placeholder*='输入']",
    "div[data-e2e='chat-input'] textarea",
    "[data-e2e='chat-input'] [contenteditable='true']",
]
SEND_BUTTON_SELECTORS = [
    "button[data-e2e='send']",
    "div[data-e2e='send']",
]

_BROWSER_LOCK = threading.Lock()
_playwright = None
_context = None
_page = None
# 登录线程内部更新、对外只读的扫码状态（避免 playwright 对象跨线程调用）
_login_done = False
_login_nickname = ""
# 发送流程中顺手抓到的头像 {昵称: URL}（发送结束由调度层写入配置，零额外开销）
AVATAR_HARVEST = {}


class InfraError(Exception):
    """基础设施级故障：浏览器不可用 / 网络不通 / 登录态失效。

    这类故障对所有目标都是必然失败。旧版让它逐个跑完 10 个目标、每个都写一条
    FAIL，最后还把 last_run_date 标成今天，导致当天再也不会自动补发——
    2026-10-23 / 11-02 / 11-16 三次全军覆没都是这个放大机制造成的。
    现在统一由调度层短路：不消耗目标、不写失败历史、不标记当天已跑，
    环境恢复后自动补跑未完成的目标。
    """
    pass


class NotLoggedIn(InfraError):
    pass


class SendFailed(Exception):
    pass


# ---- 浏览器可执行文件自动发现（不再绑定单一 revision）----
_GLOBAL_PW_DIRS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright",
    Path.home() / "AppData" / "Local" / "ms-playwright",
]
_SYSTEM_BROWSERS = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]
_browser_exe_cache = None
_browser_lock = threading.Lock()


def _scan_pw_browsers():
    """扫描项目私有 + 全局 Playwright 缓存，返回 [(revision, exe)]，新版本优先。"""
    out = []
    roots = [config.BROWSERS_DIR] + list(_GLOBAL_PW_DIRS)
    seen = set()
    for root in roots:
        key = str(root)
        if key in seen or not root or not root.is_dir():
            continue
        seen.add(key)
        try:
            for d in root.iterdir():
                if not d.is_dir() or not d.name.startswith("chromium-"):
                    continue
                if "headless" in d.name:      # headless shell 不能开可见窗口
                    continue
                m = re.search(r"chromium-(\d+)", d.name)
                rev = int(m.group(1)) if m else 0
                for sub in ("chrome-win64", "chrome-win", "chrome-linux64", "chrome-linux"):
                    cand = d / sub / ("chrome.exe" if os.name == "nt" else "chrome")
                    if cand.is_file():
                        out.append((rev, cand))
                        break
        except Exception:
            continue
    return sorted(out, key=lambda x: -x[0])


def _auto_install_browser():
    """兜底：把匹配当前 playwright 版本的 chromium 装进项目私有目录。"""
    py = config.BASE / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        py = Path(sys.executable)
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(config.BROWSERS_DIR)
    try:
        r = subprocess.run(
            [str(py), "-m", "playwright", "install", "chromium"],
            env=env, cwd=str(config.BASE), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=420, text=True, errors="replace")
        _log("auto-install chromium rc=%s tail=%s"
             % (r.returncode, (r.stdout or "")[-260:].replace("\n", " ")))
        return r.returncode == 0
    except Exception as e:
        _log("auto-install chromium error: %r" % e)
        return False


def ensure_browser_exe(force=False):
    """返回可启动的 Chromium/Edge；彻底找不到时抛 InfraError（由调度层短路）。"""
    global _browser_exe_cache
    with _browser_lock:
        if not force and _browser_exe_cache and Path(_browser_exe_cache).is_file():
            return _browser_exe_cache
        # 上次实际用过的 exe 优先：持久化 Profile 不应在不同 Chromium 版本间来回升级
        last = config.load_browser_exe()
        if last and Path(last).is_file():
            _browser_exe_cache = last
            return last
        cands = _scan_pw_browsers()
        if not cands and _auto_install_browser():
            cands = _scan_pw_browsers()
        if not cands:
            for p in _SYSTEM_BROWSERS:
                if p.is_file():
                    cands.append((0, p))
                    break
        if not cands:
            raise InfraError("找不到可用的 Chromium/Edge 浏览器，请运行 install.bat")
        exe = str(cands[0][1])
        _browser_exe_cache = exe
        config.save_browser_exe(exe)
        _log("ensure_browser: %s (rev %s)" % (exe, cands[0][0]))
        return exe


def browser_diagnostics():
    """供面板 /api/status 展示：浏览器是否就绪、用的是哪一个。"""
    try:
        exe = ensure_browser_exe()
        return {"ok": True, "exe": exe, "private_dir": str(config.BROWSERS_DIR)}
    except InfraError as e:
        return {"ok": False, "error": str(e)}


def login_flow(max_wait_seconds=600):
    """登录线程主函数：打开可见浏览器等待扫码，完成后自动关闭。"""
    global _login_done, _login_nickname
    _login_done = False
    _login_nickname = ""
    _BROWSER_LOCK.acquire()
    started = False
    try:
        page = _open(headless=False, window_pos=WIN_ONSCREEN)
        _page.set_default_timeout(30000)
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        started = True
        # 窗口移回屏幕内并置顶（防止 profile 记忆的旧位置或系统把它放屏幕外/最小化）
        _front_window(*WIN_ONSCREEN)
        _log("login_flow: browser opened, waiting for scan ...")
        if has_session_cookie(_context):
            _login_done = True
            _login_nickname = _try_get_nickname(page)
            _log("login_flow: already logged in")
            return
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline and not _login_done:
            try:
                if page.is_closed() or not _context.pages:
                    _log("login_flow: browser closed by user")
                    break
            except Exception:
                break
            if has_session_cookie(_context):
                _login_done = True
                _login_nickname = _try_get_nickname(page)
                _log("login_flow: scan OK, nickname=%r" % _login_nickname)
                break
            time.sleep(2)
        if not _login_done:
            _log("login_flow: timeout or cancelled")
    except Exception as e:
        _log("login_flow error: %r" % e)
    finally:
        _close()
        _BROWSER_LOCK.release()
        _ = started


def login_status():
    return {"done": _login_done, "nickname": _login_nickname}


def _log(msg):
    line = "[%s] [douyin] %s" % (datetime.now().strftime("%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    (config.LOGS / "app.log").open("a", encoding="utf-8").write(line + "\n")


def _new_context(pw, headless=False, window_pos=WIN_OFFSCREEN):
    x, y, w, h = window_pos
    kwargs = dict(
        user_data_dir=str(config.BROWSER_DATA),
        headless=headless,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport=None,
        timeout=60000,
        # 关键：显式指定实际存在的浏览器，playwright 不再按内置 revision
        # 去硬找 chromium-1194（那个目录一旦被外部清理就全盘失败）。
        executable_path=ensure_browser_exe(),
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-position=%d,%d" % (x, y),
            "--window-size=%d,%d" % (w, h),
        ],
    )
    return pw.chromium.launch_persistent_context(**kwargs)


def _open(headless=False, window_pos=WIN_OFFSCREEN):
    """在持有 _BROWSER_LOCK 的前提下打开持久化浏览器。"""
    global _playwright, _context, _page
    # playwright 导入较重（冷启动可占数秒），延迟到真正需要浏览器时再加载
    from playwright.sync_api import sync_playwright

    try:
        _playwright = sync_playwright().start()
        _context = _new_context(_playwright, headless=headless, window_pos=window_pos)
    except InfraError:
        raise
    except Exception as e:
        _close()
        # 浏览器起不来 = 所有目标必然失败，必须整轮短路
        raise InfraError("浏览器启动失败: %s" % str(e).split("\n")[0]) from e
    _page = _context.pages[0] if _context.pages else _context.new_page()
    _page.set_default_timeout(15000)
    return _page


def _close():
    global _playwright, _context, _page
    try:
        if _context:
            _context.close()
    except Exception:
        pass
    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _playwright = _context = _page = None


def _front_window(x, y, w, h):
    """把当前浏览器主进程的可见窗口移回屏幕内并置顶（尽力而为，失败不影响主流程）。"""
    try:
        import ctypes
        from ctypes import wintypes

        import psutil

        udir = str(config.BROWSER_DATA).lower()
        pids = set()
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                cl = " ".join(p.info["cmdline"] or []).lower()
                if udir in cl and "--type=" not in cl:
                    pids.add(p.info["pid"])
            except Exception:
                continue
        if not pids:
            _log("front_window: no browser process found")
            return False

        user32 = ctypes.windll.user32
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        hwnds = []

        def cb(hwnd, lp):
            if user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pids and user32.GetWindowTextLengthW(hwnd) > 0:
                    hwnds.append(hwnd)
            return True

        enum_cb = EnumProc(cb)  # 防止回调被 GC
        user32.EnumWindows(enum_cb, 0)
        if not hwnds:
            _log("front_window: no visible window for pids %s" % pids)
            return False
        SW_RESTORE = 9
        SWP_SHOWWINDOW = 0x0040
        SWP_NOMOVE_NOSIZE = 0x0003
        for hwnd in hwnds:
            user32.ShowWindow(hwnd, SW_RESTORE)
            # TOPMOST(-1) 落到最前，再 NOTOPMOST(-2) 取消置顶，规避前台锁定限制
            user32.SetWindowPos(hwnd, -1, x, y, w, h, SWP_SHOWWINDOW)
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, SWP_NOMOVE_NOSIZE | SWP_SHOWWINDOW)
        try:
            user32.SetForegroundWindow(hwnds[0])
        except Exception:
            pass
        _log("front_window: raised %d window(s)" % len(hwnds))
        return True
    except Exception as e:
        _log("front_window error: %r" % e)
        return False


def has_session_cookie(context):
    try:
        for c in context.cookies(COOKIE_URL):
            if c["name"] in SESSION_COOKIES and c.get("value"):
                return True
    except Exception:
        pass
    return False


def check_logged_in():
    """打开浏览器检查登录态（cookie），返回 True/False。串行锁。"""
    with _BROWSER_LOCK:
        try:
            page = _open(headless=True)
            _goto_retry(page, IM_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            ok = has_session_cookie(_context)
            _log("check_logged_in -> %s" % ok)
            return ok
        except Exception as e:
            _log("check_logged_in error: %r" % e)
            return False
        finally:
            _close()


def _try_get_nickname(page):
    for sel in ["div[data-e2e='mine-info-name']", ".user-name", "[class*='nickname']"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500):
                return loc.inner_text().strip()
        except Exception:
            continue
    return ""


def _first_visible(page, selectors, timeout=3000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                return loc
        except Exception:
            continue
    return None


def _match_conversation_item(page, name):
    """在当前 DOM 的会话列表里找首行昵称等于 name 的会话项（单轮扫描，不等待）。"""
    try:
        items = page.locator(CONVERSATION_ITEM)
        n = items.count()
        candidates = []
        for i in range(n):
            it = items.nth(i)
            try:
                if not it.is_visible(timeout=500):
                    continue
                txt = (it.inner_text(timeout=800) or "").strip()
            except Exception:
                continue
            if not txt:
                continue
            first_line = txt.split("\n")[0].strip()
            if first_line == name:
                return it          # 精确匹配，最优
            if txt.startswith(name):
                candidates.append(it)  # 容忍昵称后无换行的变体
        return candidates[0] if candidates else None
    except Exception:
        return None


def _wait_conversation_item(page, name, timeout_s=10):
    """等待目标会话项出现；期间在会话列表容器内部滚动（目标可能排在列表底部，
    且列表是虚拟渲染——不滚进视口就不会出现在 DOM 里）。"""
    deadline = time.time() + timeout_s
    scrolled_to_bottom = False
    _scroll_conv_list(page, -100000)   # 先滚回顶部（虚拟列表可能保留了上次滚动位置）
    while time.time() < deadline:
        it = _match_conversation_item(page, name)
        if it is not None:
            return it
        if scrolled_to_bottom:
            page.wait_for_timeout(600)
            continue
        if not _scroll_conv_list(page, 320):
            # 滚到底了还找不到，剩下的时间只做轮询（列表可能仍在加载）
            scrolled_to_bottom = True
        page.wait_for_timeout(600)
    return None


def _harvest_item_avatar(item, name):
    """从会话项/搜索结果项里顺手抓头像 URL（与联系人提取同源，最可靠）。
    抓到存入 AVATAR_HARVEST，发送结束由调度层写回配置（只补缺头像的目标）。"""
    try:
        img = item.locator("img").first
        src = (img.get_attribute("src", timeout=800) or "").strip()
        if src.startswith("//"):
            src = "https:" + src
        if src.startswith("http") and len(src) > 20:
            AVATAR_HARVEST[name] = src
    except Exception:
        pass


def _scroll_conv_list(page, delta):
    """在会话列表容器内部滚动（容器从 conversation-item 向上找最近的可滚动祖先）。
    返回 True 表示仍在列表中且执行了滚动；False 表示到底或列表未渲染。"""
    try:
        return page.evaluate(
            r"""
(delta) => {
  const items = document.querySelectorAll("[data-e2e='conversation-item']");
  if (!items.length) return false;
  let el = items[0], hops = 0;
  while (el && hops < 12) {
    if (el.scrollHeight > el.clientHeight + 10) {
      const before = el.scrollTop;
      el.scrollTop = before + delta;
      return el.scrollTop > before || before + delta < el.scrollHeight;
    }
    el = el.parentElement; hops++;
  }
  return false;
}
""",
            delta,
        )
    except Exception:
        return False


def _wait_conv_list_rendered(page, timeout_s=8):
    """等会话列表渲染出至少一项（im-dialog 可见不代表列表加载完成）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            loc = page.locator(CONVERSATION_ITEM)
            if loc.count() > 0 and loc.first.is_visible(timeout=400):
                return True
        except Exception:
            pass
        page.wait_for_timeout(500)
    return False


def _wait_chat_input(page, timeout_ms=8000):
    """等待聊天窗输入框出现（会话真正打开的标志）。"""
    try:
        page.wait_for_selector("div[contenteditable='true']", timeout=timeout_ms,
                               state="visible")
        loc = page.locator("div[contenteditable='true']").first
        if loc.is_visible(timeout=800):
            return loc
    except Exception:
        pass
    return _first_visible(page, INPUT_SELECTORS, timeout=1500)


def _chat_view_hint(page):
    """抓聊天视图文本片段（如对方隐私限制提示），用于失败原因可读化。"""
    try:
        dlg = page.locator(IM_DIALOG).first
        if dlg.is_visible(timeout=500):
            t = (dlg.inner_text(timeout=800) or "").strip()
            if t:
                return t[:100].replace("\n", " ")
    except Exception:
        pass
    return ""


# ---- 2026-11-16 重写：输入框状态机 + 消息气泡增量校验 ----
# 旧版 _input_box_text 只在 im-dialog 内找「宽>200 且 18<=高<300 的
# contenteditable」，但抖音聊天层是独立的 data-stack-layer="chat"，
# 结果 filled=False 出现在**所有**目标上：文本到底进没进输入框从未被确认，
# 直接按了 Enter，随后又被上面的文本计数随机判成 OK / FAIL。
# 抖音聊天输入框是 Slate 编辑器：清空后 innerText 是零宽空格 \u200b，
# 不是空串。2026-11-17 事故即源于把它当成「仍有文本」——导致全部误判 FAIL，
# 并触发「Enter 后再点发送按钮」的兜底，同一人一天收到 4 条。
_ZERO_WIDTH = "\u200b\ufeff\u200c\u200d\u2060"


def _norm_input(s):
    """把 Slate 空编辑器状态归一为空串。None 保持 None（DOM 标记丢失=未知）。"""
    if s is None:
        return None
    for ch in _ZERO_WIDTH:
        s = s.replace(ch, "")
    return s.strip()


def _mark_input_box(page):
    """只认聊天窗的 Slate 主输入框，绝不退化到「全页面积最大的可编辑元素」。

    2026-11-17 实测：侧栏搜索框 227x32（面积 7264）比真输入框 146x44
    （面积 6424）更大，旧版按面积选会在焦点丢失时把消息打进搜索框并按
    Enter 触发搜索——这就是「很多人没有发送」。
    """
    try:
        return page.evaluate(r"""
() => {
  document.querySelectorAll('[data-sb-input]').forEach(e => e.removeAttribute('data-sb-input'));
  const editorArea = document.querySelector("[class*='messageMsgInput']");
  if (!editorArea) return false;                 // 聊天输入区不存在 = 会话没打开
  const scope = document.querySelector("[data-e2e='im-dialog']") || document;
  const sels = [
    "div[data-slate-editor='true'][contenteditable='true']",
    "div[contenteditable='true'][data-placeholder='发送消息']",
    "[class*='messageEditorinputArea'][contenteditable='true']",
    "[class*='messageMsgInput'] div[contenteditable='true']",
    "[class*='messageMsgInput'] textarea"
  ];
  let best = null;
  for (const s of sels) {
    for (const e of scope.querySelectorAll(s)) {
      const r = e.getBoundingClientRect();
      if (r.width > 40 && r.height >= 14) { best = e; break; }
    }
    if (best) break;
  }
  if (!best) return false;
  best.setAttribute('data-sb-input', '1');
  return true;
}
""")
    except Exception:
        return False


def _read_input(page):
    """返回归一化后的输入框文本；None = 标记随 DOM 重建丢失（未知，不可当作已清空）。"""
    try:
        return _norm_input(page.evaluate(r"""
() => {
  const e = document.querySelector('[data-sb-input="1"]');
  if (!e) return null;
  if (e.tagName === 'TEXTAREA' || e.tagName === 'INPUT') return e.value || '';
  return e.innerText || '';
}
"""))
    except Exception:
        return None


def _input_is_empty(cur):
    return cur is not None and cur == ""


def _chat_bubbles(page):
    """当前聊天窗消息行文本（尾部 80 条）。范围限定聊天层，避免把会话列表
    预览、主站评论等同样带固定文案的节点算进来。"""
    try:
        return page.evaluate(r"""
() => {
  const scope = document.querySelector("[data-stack-layer='chat']") ||
                document.querySelector("[data-e2e='im-dialog']") || document;
  const sels = ["[class*='MessageBoxContentrowBox']", "[data-e2e='message-item']",
                "[class*='messageItem']", "[class*='MessageItem']",
                "[data-e2e='chat-message']", "[class*='message-row']"];
  let best = [];
  for (const s of sels) {
    const ns = [...scope.querySelectorAll(s)]
      .filter(e => e.getBoundingClientRect().height > 8)
      .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200));
    if (ns.length > best.length) best = ns;
  }
  return best.slice(-80);
}
""")
    except Exception:
        return []


def _bubble_added(before, after, text):
    """「含目标文本的消息行数」是否增加。

    2026-11-17 修正：旧版用 len(after) > len(before) + 取尾部切片，但
    ① 尾部 40 条截断会让满窗时长度恒为 40；② MessageBoxContentrowBox 还
    匹配时间、昵称、空行；③ 虚拟列表会卸载旧行使总行数减少（日志里的
    msg=34->31）。只数「含目标文本的行」才能稳定反映新发出了一条。
    """
    if not after:
        return False
    return (sum(1 for t in after if text in t)
            > sum(1 for t in before if text in t))


def _type_into_input(page, text):
    """严格确认 text 真的写进了输入框；写不进去就不允许按 Enter。"""
    observed = None
    for attempt in range(3):
        if not _mark_input_box(page):
            page.wait_for_timeout(400)
            continue
        box = page.locator("[data-sb-input='1']").first
        try:
            box.click(timeout=2500)
        except Exception:
            try:
                box.click(force=True, timeout=1500)
            except Exception:
                pass
        page.wait_for_timeout(150)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(text)
        page.wait_for_timeout(350)
        observed = _read_input(page)
        # 严格相等：Slate 里 Ctrl+A 未生效时会把文本追加到旧内容后面，
        # 一次发送夹带两遍文案。宁可判失败不重发，也不能发出重复内容。
        if observed is not None and observed == text.strip():
            return True, observed
        _log("input attempt %d/3: observed=%r" % (attempt + 1, observed))
    return False, observed


def _dialog_tail_hint(page):
    """失败诊断：抓 im-dialog 的 innerText 尾部（空格归一），便于事后还原页面状态。"""
    try:
        return page.evaluate(
            r"""
() => {
  const dlg = document.querySelector("[data-e2e='im-dialog']");
  if (!dlg) return '';
  const t = (dlg.innerText || '').replace(/\s+/g, ' ').trim();
  return t.slice(-90);
}
"""
        )
    except Exception:
        return ""


def _open_via_sidebar_search(page, name):
    """在消息侧栏内部搜索并点开会话（绝不碰主站顶部搜索框）。

    2026-11-16 修复：旧版 search.click() 在聊天层（MessageBoxContentrowBox /
    data-stack-layer="chat"）盖住搜索框时固定吃满 15s actionability 超时，
    信号3 兜底因此几乎总是失败。改用 focus()+fill() 绕过指针遮挡，
    所有点击改为 force + 短超时，失败快速返回而不是卡住整轮。
    """
    search = _first_visible(page, SIDEBAR_SEARCH_SELECTORS, timeout=2000)
    if search is None:
        _log("sidebar search: no search box found")
        return False
    try:
        try:
            search.focus(timeout=2000)
        except Exception:
            search.click(force=True, timeout=2000)
        search.fill(name, timeout=3000)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1800)
        dlg = page.locator(IM_DIALOG).first
        try:
            btns = dlg.get_by_text("发消息", exact=True)
            for i in range(min(btns.count(), 5)):
                btn = btns.nth(i)
                try:
                    if not btn.is_visible(timeout=400):
                        continue
                    holder = btn
                    matched = False
                    for _ in range(4):
                        holder = holder.locator("xpath=..")
                        t = (holder.inner_text(timeout=600) or "").strip()
                        if t.startswith(name):
                            matched = True
                            break
                        if len(t) > 60:
                            break
                    if matched:
                        _harvest_item_avatar(holder, name)
                        btn.click(force=True, timeout=2500)
                        page.wait_for_timeout(1200)
                        if _wait_chat_input(page, 4000) is not None:
                            return True
                except Exception:
                    continue
        except Exception as e:
            _log("sidebar search send-btn error: %r" % e)
        item = _match_conversation_item(page, name)
        if item is not None:
            _harvest_item_avatar(item, name)
            item.click(force=True, timeout=2500)
            page.wait_for_timeout(1000)
            if _wait_chat_input(page, 4000) is not None:
                return True
        try:
            cand = dlg.get_by_text(name, exact=True)
            for i in range(min(cand.count(), 5)):
                el = cand.nth(i)
                try:
                    if el.is_visible():
                        el.click(force=True, timeout=2000)
                        page.wait_for_timeout(1000)
                        if _wait_chat_input(page, 4000) is not None:
                            return True
                except Exception:
                    continue
        except Exception:
            pass
    except Exception as e:
        _log("sidebar search error: %r" % e)
    return False


def _find_and_open_conversation(page, name):
    """打开与 name 的会话并确认聊天窗已展开（输入框出现）。
    顺序：会话列表（显式等待+滚动）→ 侧栏内搜索。失败原因带页面提示。"""
    hint = ""
    item = _wait_conversation_item(page, name, timeout_s=10)
    if item is not None:
        _harvest_item_avatar(item, name)
        try:
            item.click()
            page.wait_for_timeout(1000)
            if _wait_chat_input(page, 6000) is not None:
                return "list"
            hint = _chat_view_hint(page)
            _log("open conv: clicked item but no chat input; hint=%r" % hint)
        except Exception:
            pass
    if _open_via_sidebar_search(page, name):
        return "search"
    msg = "无法打开与「%s」的会话（会话列表与侧栏搜索均未成功）" % name
    if hint:
        msg += "；页面提示: %s" % hint
    raise SendFailed(msg)


def _reopen_conversation(page, name):
    """回会话列表再重开会话（备用路径）。2026-11-16 加 Escape 关掉遮挡层。"""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        _click_sidebar_back(page)
        page.wait_for_timeout(1000)
        if not _sidebar_panel_visible(page):
            _open_message_center(page, budget_s=90)
        if not _sidebar_panel_visible(page):
            _log("reopen_conversation: sidebar panel still not visible")
            return False
        _find_and_open_conversation(page, name)
        ok = _wait_chat_input(page, 4000) is not None
        _log("reopen_conversation [%s]: chat view ready=%s" % (name, ok))
        return ok
    except Exception as e:
        _log("reopen_conversation error: %r" % e)
        return False


def _ensure_conversation_list(page):
    """批次内切换目标：从聊天视图回到会话列表，必要时重开消息中心。

    旧版每发一个人都关掉并重启整个 Chromium（10 目标 = 10 次冷启动 +
    10 次 30s 侧栏等待），失败窗口极大。现在整轮只开一次浏览器。
    """
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    _click_sidebar_back(page)
    page.wait_for_timeout(400)
    if _sidebar_panel_visible(page):
        return
    _open_message_center(page, budget_s=90)
    if not _sidebar_panel_visible(page):
        # 侧栏打不开属于页面/网络层故障，对所有目标都是必然失败：
        # 交给调度层整轮短路，而不是给 10 个人各写一条失败记录。
        raise InfraError("消息侧栏未能打开（会话列表不可见），稍后自动重试")


def _goto_retry(page, url, attempts=5, **kw):
    """goto 带瞬时网络错误重试（DNS 解析失败/超时等）。
    2026-10-19 加强：attempts 3→5、退避 2-4s→3-6s——机器刚唤醒/网络切换时
    DNS 恢复可能要 20s+，旧参数总窗口 ~8s 不够用。"""
    last = None
    for i in range(attempts):
        try:
            return page.goto(url, **kw)
        except Exception as e:
            last = e
            _log("goto retry %d/%d for %s: %r" % (i + 1, attempts, url[:44], e))
            if i < attempts - 1:
                time.sleep(random.uniform(3.0, 6.0))
    raise InfraError("网络不可达（连续 %d 次导航失败）: %s"
                     % (attempts, str(last).split("\n")[0]))


def purge_old_shots(max_age_h=24):
    """清理截图目录：只保留近一天的截图，更早的自动删除（2026-10-19 用户要求，
    截图只用于失败瞬间的短期诊断，没必要长期占磁盘）。
    在每次截图时与面板启动时各调用一次。"""
    try:
        cutoff = time.time() - max_age_h * 3600
        n = 0
        if config.SHOTS.exists():
            for f in config.SHOTS.iterdir():
                try:
                    if f.is_file() and f.suffix.lower() in ('.png', '.jpg', '.jpeg') \
                            and f.stat().st_mtime < cutoff:
                        f.unlink()
                        n += 1
                except Exception:
                    pass
        if n:
            _log("purged %d screenshot(s) older than %dh" % (n, max_age_h))
    except Exception:
        pass


def _shot(page, tag):
    purge_old_shots()
    name = "%s_%s.png" % (datetime.now().strftime("%Y%m%d_%H%M%S"), tag)
    path = config.SHOTS / name
    try:
        page.screenshot(path=str(path), full_page=False)
        return name
    except Exception:
        return ""


def _sidebar_panel_visible(page):
    """左面板（会话列表区）是否真正可见。
    坑：主站把 im-dialog 常驻 DOM（隐藏态），只查元素存在会误判侧栏已开。"""
    try:
        return page.evaluate(
            r"""
() => {
  const items = document.querySelectorAll("[data-e2e='conversation-item']");
  for (const it of items) {
    const r = it.getBoundingClientRect();
    if (r.width > 50 && r.height > 10) return true;
  }
  return false;
}
"""
        )
    except Exception:
        return False


def _sidebar_container_visible(page):
    """侧栏容器是否在屏幕上显示（rect 有面积）。"""
    try:
        return page.evaluate(
            r"""
() => {
  const dlg = document.querySelector("[data-e2e='im-dialog']");
  if (!dlg) return false;
  const r = dlg.getBoundingClientRect();
  return r.width > 100 && r.height > 100;
}
"""
        )
    except Exception:
        return False


def _click_sidebar_back(page):
    """侧栏停在聊天视图时点「返回」回到会话列表。点击了返回元素返回 True。
    （2026-10-19 下午：撤掉左上角图标启发式——404/坏页面上乱点有误关侧栏
    的风险；实测该状态绝大多数是「列表异步加载中」，耐心等待即可。）"""
    try:
        return page.evaluate(
            r"""
() => {
  const dlg = document.querySelector("[data-e2e='im-dialog']");
  if (!dlg) return false;
  const els = dlg.querySelectorAll("[class*='back'], [class*='Back'], [aria-label*='返回'], [title*='返回'], [data-e2e*='back']");
  for (const el of els) {
    const b = el.getBoundingClientRect();
    if (b.width > 0 && b.height > 0 && b.width < 80 && b.height < 80) {
      el.click();
      return true;
    }
  }
  return false;
}
"""
        )
    except Exception:
        return False


def _goto_homepage(page):
    _goto_retry(page, "https://www.douyin.com/",
                wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)


def _click_message_entry(page):
    """在主页点消息入口按钮。返回是否点到了。"""
    entry = _first_visible(page, MSG_ENTRY_SELECTORS, timeout=1500)
    if entry is not None:
        try:
            entry.click(timeout=1500)
            return True
        except Exception:
            pass
    try:
        cand = page.get_by_text("消息", exact=True)
        for i in range(min(cand.count(), 5)):
            el = cand.nth(i)
            if el.is_visible():
                el.click()
                return True
    except Exception:
        pass
    return False


def _page_is_404(page):
    """抖音 /message 路由会间歇性 404（「页面不见啦」截图实锤，2026-10-19）。
    404 页上侧栏永远打不开，检测到就该立刻回首页走入口。"""
    try:
        return page.get_by_text("页面不见啦").count() > 0
    except Exception:
        return False


def _wait_sidebar_panel(page, timeout_s, poll=1.5):
    """耐心等左面板出现（会话列表是异步慢加载，成功案例 6-14s 自愈）。
    等待期间绝不 reload/点击——任何打扰都会重置加载进度。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _sidebar_panel_visible(page):
            return True
        page.wait_for_timeout(int(poll * 1000))
    return _sidebar_panel_visible(page)


def _open_message_center(page, budget_s=150):
    """打开消息侧栏，直到左面板（会话列表）真正可见。

    2026-10-19 下午复盘（失败瞬间截图实锤）——「侧栏打不开」反复出现的根源：
    抖音 /message 路由**间歇性 404**（「页面不见啦」页），此前的流程先去
    /message、失败后还反复 reload /message，等于一次次撞 404。另外会话
    列表是异步慢加载（成功案例在判定放弃后 6-14s 内自行渲染出来）。

    新策略：
      1. 一律从首页开始点消息入口（首页路径被截图证明 100% 可靠）；
      2. 见 404 立即回首页重来，绝不重载 /message；
      3. 容器已开但列表未渲染 → 先试点返回，然后耐心等 ≤30s，不打扰；
      4. 仍不成 → 重载首页再点入口（最多 2 次）；总预算 budget_s 秒
         （默认 150），超时截图留证。budget_s 参数便于测试注入。"""
    purge_old_shots()
    deadline = time.time() + budget_s
    _goto_retry(page, "https://www.douyin.com/",
                wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    reloads = 0
    while time.time() < deadline:
        if _page_is_404(page):
            _log("open_message_center: douyin 404 page -> back to homepage")
            _goto_retry(page, "https://www.douyin.com/",
                        wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            continue
        if _sidebar_panel_visible(page):
            if reloads:
                _log("open_message_center: panel visible (after %d reload(s))" % reloads)
            break
        if not _sidebar_container_visible(page):
            # 侧栏没显示：点一次消息入口，点开后耐心等列表渲染
            _log("open_message_center: clicking message entry")
            if _click_message_entry(page):
                if _wait_sidebar_panel(page, 30):
                    break
            else:
                _log("open_message_center: no message entry found")
                page.wait_for_timeout(1500)
            continue
        # 容器开着但列表没渲染：先试点返回（万一停在聊天视图），再耐心等
        _click_sidebar_back(page)
        _log("open_message_center: sidebar open, waiting for list render (<=30s)")
        if _wait_sidebar_panel(page, 30):
            break
        if reloads >= 2:
            break
        reloads += 1
        _log("open_message_center: list not rendered -> reload homepage (#%d)" % reloads)
        _goto_retry(page, "https://www.douyin.com/",
                    wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
    if not _sidebar_panel_visible(page):
        _log("open_message_center: left panel NOT visible (gave up)")
        _shot(page, "nosidebar")
    _wait_conv_list_rendered(page, timeout_s=6)


# ---- 联系人提取 ----
# 会话列表提取：用官方 conversation-item 锚点（2026-10 实测）
# 会话项文本结构：昵称 / 火花数(如 🔥206) / 时间 / 最后一条消息（抖音号在会话列表里不存在）
SESSIONS_JS = r"""
() => {
  const seen = new Map();
  document.querySelectorAll("[data-e2e='conversation-item']").forEach(item => {
    try {
      if (!item.offsetParent) return;   // 隐藏项（侧栏没真正打开时）文本会挤成一行，跳过
      const img = item.querySelector('img');
      const lines = (item.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
      if (!lines.length) return;
      const name = lines[0];
      if (!name || name.length > 40) return;
      const avatar = img ? (img.src || '') : '';
      // 群聊特征：头像区是多张成员头像拼的九宫格/叠加图（个人号只有 1 张）
      const isGroup = item.querySelectorAll('img').length >= 2;
      let spark = 0;
      for (let i = 1; i < Math.min(lines.length, 4); i++) {
        const ln = lines[i];
        if (/^\d+(?:\.\d+)?\s*万?$/.test(ln)) {
          spark = parseFloat(ln.replace('万', '')) * (ln.indexOf('万') >= 0 ? 10000 : 1) | 0;
          break;
        }
        if (ln.indexOf('🔥') >= 0 || ln.indexOf('火花') >= 0) {
          const m = ln.match(/([\d.,]+)\s*(万)?/);
          spark = m ? (parseFloat(m[1].replace(/,/g, '')) * (m[2] ? 10000 : 1) | 0) : 1;
          if (!spark) spark = 1;
          break;
        }
      }
      const preview = lines.slice(1, 4).join(' · ').slice(0, 60);
      if (!seen.has(name)) seen.set(name, {name: name, uid: '', avatar: isGroup ? '' : avatar,
                                           group: isGroup,
                                           preview: preview, spark: spark});
    } catch (e) {}
  });
  return Array.from(seen.values()).slice(0, 60);
}
"""

# 用户搜索页（主站 /search?type=user）卡片提取：卡片文本含「抖音号: xxx」
CONTACT_JS = r"""
() => {
  const seen = new Map();
  function harvest(card) {
    if (!card) return null;
    const img = card.querySelector('img');
    if (!img) return null;
    const r = img.getBoundingClientRect();
    if (r.width < 22 || r.height < 22 || r.width > 130) return null;
    const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    if (!lines.length) return null;
    let uid = '';
    for (const ln of lines) {
      const m = ln.match(/抖音号[:：]\s*([A-Za-z0-9_.\-]+)/);
      if (m) { uid = m[1]; break; }
    }
    if (lines[0].length > 40) return null;
    return { name: lines[0], uid: uid, avatar: img.src || '',
             preview: lines.slice(1, 3).join(' · ').slice(0, 60) };
  }
  const cards = document.querySelectorAll(
    '[data-e2e="user-card"], [class*="userCard"], [class*="user-card"],' +
    '[class*="session-item"], [class*="sessionItem"], [class*="sessionItemWrapper"],' +
    '[class*="conv-item"], [class*="conversation-item"], [class*="chatItem"]');
  cards.forEach(c => {
    const u = harvest(c);
    if (u && u.name && !seen.has(u.name + '|' + u.uid)) seen.set(u.name + '|' + u.uid, u);
  });
  // 兜底：结构化卡片没命中时，扫描页面头像 img，向上找最近有文本的祖先容器
  if (seen.size === 0) {
    document.querySelectorAll('img').forEach(img => {
      const r = img.getBoundingClientRect();
      if (r.width < 30 || r.width > 110) return;
      let p = img.parentElement, hops = 0, hit = null;
      while (p && hops < 4) {
        const t = (p.innerText || '').trim();
        if (t && t.length > 1 && t.length < 120) { hit = p; break; }
        p = p.parentElement; hops++;
      }
      if (hit) {
        const u = harvest(hit);
        if (u && u.name && !seen.has(u.name + '|' + u.uid)) seen.set(u.name + '|' + u.uid, u);
      }
    });
  }
  return Array.from(seen.values()).slice(0, 60);
}
"""


def _extract_contacts(page):
    try:
        items = page.evaluate(CONTACT_JS)
        out = []
        for it in items:
            if isinstance(it, dict) and it.get("name"):
                out.append({
                    "name": str(it.get("name", ""))[:40],
                    "uid": str(it.get("uid", "")),
                    "avatar": str(it.get("avatar", "")),
                    "preview": str(it.get("preview", "")),
                })
        return out
    except Exception as e:
        _log("extract contacts error: %r" % e)
        return []


def _extract_sessions(page):
    """从消息侧栏提取会话（conversation-item 锚点，含火花数），失败回退旧 CONTACT_JS。"""
    try:
        items = page.evaluate(SESSIONS_JS)
        if items:
            return items
    except Exception as e:
        _log("extract sessions error: %r" % e)
    return _extract_contacts(page)


def fetch_sessions(max_tries=3):
    """打开消息中心读取全部会话（含 spark 火花数）。返回 (sessions, shot, err)。"""
    with _BROWSER_LOCK:
        shot = ""
        try:
            page = _open(headless=False)
            _open_message_center(page)
            if not has_session_cookie(_context):
                shot = _shot(page, "sessions_nologin")
                return [], shot, "登录态已失效，请先扫码登录"
            page.wait_for_timeout(1500)
            sessions = []
            for i in range(max_tries):
                sessions = _extract_sessions(page)
                if sessions:
                    break
                _scroll_conv_list(page, 800)   # 虚拟列表：滚动加载更多会话
                page.wait_for_timeout(1000)
            # 再从头滚一遍收集（有些会话滚出视口后从 DOM 卸载，分批合并）
            merged = {s["name"]: s for s in sessions}
            if merged:
                page.evaluate(r"""
() => {
  const items = document.querySelectorAll("[data-e2e='conversation-item']");
  if (!items.length) return;
  let el = items[0], hops = 0;
  while (el && hops < 12) {
    if (el.scrollHeight > el.clientHeight + 10) { el.scrollTop = 0; return; }
    el = el.parentElement; hops++;
  }
}
""")
                page.wait_for_timeout(600)
                pos = 0
                for _ in range(12):
                    for s in _extract_sessions(page):
                        merged.setdefault(s["name"], s)
                    if not _scroll_conv_list(page, 400):
                        break
                    pos += 400
                    page.wait_for_timeout(500)
            sessions = list(merged.values())
            shot = _shot(page, "sessions")
            _log("fetch_sessions -> %d items" % len(sessions))
            if not sessions:
                return [], shot, "未能从消息中心提取到会话（可能还没有聊天记录，或页面改版）"
            return sessions, shot, ""
        except Exception as e:
            _log("fetch_sessions error: %r" % e)
            return [], shot, "异常: %r" % e
        finally:
            _close()


def fetch_contacts(max_tries=3):
    """打开消息中心读取会话列表（全量滚动收集）。返回 (contacts, shot, err)。
    注意：会话列表里没有抖音号，uid 需要用 enrich_uid 走主站搜索补全。"""
    sessions, shot, err = fetch_sessions(max_tries=max_tries)
    if err and not sessions:
        return [], shot, err
    return sessions, shot, ""


def search_users(kw):
    """主站用户搜索（type=user）。返回 (contacts, shot, err)。"""
    kw = (kw or "").strip()
    if not kw:
        return [], "", "请输入搜索关键词"
    with _BROWSER_LOCK:
        shot = ""
        try:
            page = _open(headless=False)
            _goto_retry(page, "https://www.douyin.com/search/%s?type=user" % quote(kw),
                        wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3500)
            if not has_session_cookie(_context):
                shot = _shot(page, "search_nologin")
                return [], shot, "登录态已失效，请先扫码登录"
            contacts = []
            for i in range(4):
                contacts = _extract_contacts(page)
                if len(contacts) >= 3:
                    break
                try:
                    page.mouse.wheel(0, 700)
                except Exception:
                    pass
                page.wait_for_timeout(1200)
            shot = _shot(page, "search_" + kw[:10])
            _log("search_users(%r) -> %d items" % (kw, len(contacts)))
            if not contacts:
                return [], shot, "搜索结果为空或页面改版"
            return contacts, shot, ""
        except Exception as e:
            _log("search_users error: %r" % e)
            return [], shot, "异常: %r" % e
        finally:
            _close()


# 综合搜索页的用户名片（含「抖音号: xxx」；type=user 的卡片反而不显示抖音号）
SEARCH_UID_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = walker.currentNode.textContent || '';
    if (!/抖音号[:：]/.test(t)) continue;   // 只要带冒号的（热搜词卡是"xxx抖音号"无冒号，天然排除）
    let el = walker.currentNode.parentElement, hops = 0, best = null;
    while (el && hops < 8) {
      const r = el.getBoundingClientRect();
      if (r.width > 200 && r.height > 60) { best = el; break; }
      el = el.parentElement; hops++;
    }
    if (!best) continue;
    const key = (best.className || '').toString() + '|' + (best.innerText || '').slice(0, 60);
    if (seen.has(key)) continue;
    seen.add(key);
    const txt = (best.innerText || '').trim();
    const m = txt.match(/抖音号[:：]\s*([A-Za-z0-9_.\-]+)/);
    if (m) {
      const name = (txt.split('\n')[0] || '').replace(/[|\s]+$/, '').trim().slice(0, 40);
      out.push({name: name, uid: m[1]});
    }
  }
  return out.slice(0, 10);
}
"""


def _avatar_tokens(url):
    """从头像 URL 提取全局唯一 ID 片段（同一个人在不同场景的头像 URL 共享同一 ID）。"""
    return set(re.findall(r"[A-Za-z0-9_-]{16,}", url or ""))


def _search_uid_once(page, name, avatar=""):
    """搜人拿抖音号，两级策略：
    1) 综合搜索页的用户名片（右侧卡片含「抖音号: xxx」，最快）；
    2) 用户 tab 精确匹配卡片（昵称精确，或头像 ID 与会话头像一致）→ 点进主页读抖音号。"""
    # 路径 1：综合页名片
    try:
        _goto_retry(page, "https://www.douyin.com/search/%s" % quote(name),
                    wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)
        cards = page.evaluate(SEARCH_UID_JS)
        for c in cards:
            if c.get("name") == name and c.get("uid"):
                return c["uid"]
    except Exception as e:
        _log("enrich card path error: %r" % e)
    # 路径 2：用户 tab → 主页
    try:
        _goto_retry(page, "https://www.douyin.com/search/%s?type=user" % quote(name),
                    wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3800)
        contacts = _extract_contacts(page)
        target_tokens = _avatar_tokens(avatar)

        def pick():
            for c in contacts:
                if c.get("name") == name:
                    return c
            if target_tokens:   # 昵称带 emoji 等显示差异时，头像 ID 是唯一铁证
                for c in contacts:
                    if _avatar_tokens(c.get("avatar")) & target_tokens:
                        _log("enrich %s: matched by avatar id, card name=%r"
                             % (name, c.get("name")))
                        return c
            return None

        card = pick()
        if card is None:
            for _ in range(3):   # 精确卡可能排得很靠后，多滚几轮
                try:
                    page.mouse.wheel(0, 900)
                    page.wait_for_timeout(1400)
                    contacts = _extract_contacts(page)
                    card = pick()
                    if card is not None:
                        break
                except Exception:
                    pass
        if card is None:
            _log("enrich %s: no matching user card, candidates=%s"
                 % (name, [c.get("name") for c in contacts][:5]))
            return None
        card_name = card["name"]
        cand = page.get_by_text(card_name, exact=True)
        clicked = False
        for i in range(min(cand.count(), 5)):
            el = cand.nth(i)
            try:
                if el.is_visible(timeout=500):
                    el.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            _log("enrich %s: card %r not clickable" % (name, card_name))
            return None
        page.wait_for_timeout(4000)
        for p in _context.pages:
            if "/user/" in p.url:
                try:
                    txt = p.inner_text("body", timeout=6000)
                except Exception:
                    continue
                m = re.search(r"抖音号[:：]\s*([A-Za-z0-9_.\-]+)", txt)
                if m:
                    return m.group(1)
                break
    except Exception as e:
        _log("enrich profile path error: %r" % e)
    return None


# 聊天窗头部头像点击：标题文本=会话名（备注名），点标题/头像进资料页或弹资料卡
HEADER_PEER_JS = r"""
(name) => {
  const dlg = document.querySelector("[data-e2e='im-dialog']");
  if (!dlg) return false;
  const dr = dlg.getBoundingClientRect();
  const walker = document.createTreeWalker(dlg, NodeFilter.SHOW_ELEMENT);
  const cands = [];
  let el;
  while ((el = walker.nextNode())) {
    if (el.children.length) continue;                    // 叶子文本节点
    const t = (el.textContent || '').trim();
    if (t !== name) continue;                            // 聊天窗标题=会话名
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (r.top < dr.top || r.top > dr.top + 130) continue;  // 顶部标题栏区域
    cands.push(el);
  }
  if (!cands.length) return false;
  // 标题旁边那颗头像（26-70px 的 img），没有就点标题本身
  let target = cands[cands.length - 1];
  let p = target.parentElement, img = null, hops = 0;
  while (p && hops < 3 && !img) {
    for (const i of p.querySelectorAll('img')) {
      const r = i.getBoundingClientRect();
      if (r.width >= 26 && r.width <= 70) { img = i; break; }
    }
    p = p.parentElement; hops++;
  }
  try { (img || target).click(); } catch (e) { return false; }
  return true;
}
"""

# 主页大头像提取（60-200px 的第一张 img）
PROFILE_AVATAR_JS = r"""
() => {
  let best = '';
  document.querySelectorAll('img').forEach(img => {
    if (best) return;
    const r = img.getBoundingClientRect();
    if (r.width >= 60 && r.width <= 200 && r.height >= 60
        && img.src && img.src.startsWith('http')) best = img.src;
  });
  return best;
}
"""


def _uid_via_chat_profile(page, name):
    """策略3：从会话列表打开聊天窗 → 点头部头像进资料页 → 直读抖音号。
    2026-10-20 新增：备注名与抖音昵称不一致（备注「杨胤铖」、实际昵称「胤铖。」）
    时，搜索页永远搜不到；会话入口 100% 精确，这条路是兜底王牌。
    头像由 _find_and_open_conversation（会话项）与资料页（PROFILE_AVATAR_JS）双路抓取。
    返回 uid 或 None。"""
    try:
        _goto_homepage(page)
        page.wait_for_timeout(1200)
        _open_message_center(page)
        if not _sidebar_panel_visible(page):
            _log("enrich %s: chat-profile path skipped (no sidebar)" % name)
            return None
        _find_and_open_conversation(page, name)   # 顺手抓会话项头像
        page.wait_for_timeout(800)
        if not page.evaluate(HEADER_PEER_JS, name):
            _log("enrich %s: chat header peer not found" % name)
            try:
                _click_sidebar_back(page)
            except Exception:
                pass
            return None
        page.wait_for_timeout(3500)
        # 情况A：新开/跳转到 /user/ 主页
        for p in _context.pages:
            if "/user/" in p.url:
                try:
                    txt = p.inner_text("body", timeout=6000)
                except Exception:
                    continue
                m = re.search(r"抖音号[:：]\s*([A-Za-z0-9_.\-]+)", txt)
                if m:
                    try:
                        src = p.evaluate(PROFILE_AVATAR_JS)
                        if src and src not in ("", "about:blank"):
                            AVATAR_HARVEST[name] = src
                    except Exception:
                        pass
                    return m.group(1)
                break
        # 情况B：当前页弹出资料卡
        try:
            txt = page.inner_text("body", timeout=3000) or ""
            m = re.search(r"抖音号[:：]\s*([A-Za-z0-9_.\-]+)", txt)
            if m:
                return m.group(1)
        except Exception:
            pass
        _log("enrich %s: profile opened but no douyin-id text" % name)
        return None
    except Exception as e:
        _log("enrich %s: chat-profile path error: %r" % (name, e))
        return None
    finally:
        try:
            page.keyboard.press("Escape")   # 关掉可能弹出的资料卡，回到干净状态
        except Exception:
            pass


def enrich_uid(items):
    """搜人补全抖音号。items 可为 [昵称] 或 {昵称: 头像URL}（带头像可提高匹配率）。
    2026-10-20：搜索失败后追加策略3（聊天窗点头像进主页直读，专治备注名）；
    顺手抓到的头像统一进 AVATAR_HARVEST，由调用方写回配置。
    返回 {name: {"uid": ...}}，搜不到的人不在结果里。"""
    if isinstance(items, dict):
        pairs = [(k, v or "") for k, v in items.items() if (k or "").strip()]
    else:
        pairs = [(n, "") for n in (items or []) if (n or "").strip()]
    if not pairs:
        return {}
    out = {}
    with _BROWSER_LOCK:
        try:
            page = _open(headless=False)
            if not has_session_cookie(_context):
                _goto_retry(page, "https://www.douyin.com/", wait_until="domcontentloaded",
                            timeout=30000)
                page.wait_for_timeout(2000)
            for i, (name, avatar) in enumerate(pairs):
                try:
                    if not has_session_cookie(_context):
                        _log("enrich: login expired, stop at %d/%d" % (i, len(pairs)))
                        break
                    uid = None
                    for attempt in range(3):
                        uid = _search_uid_once(page, name, avatar)
                        if uid:
                            break
                        if attempt < 2:
                            time.sleep(random.uniform(3.0, 5.0))
                    if not uid:
                        uid = _uid_via_chat_profile(page, name)   # 策略3：备注名兜底
                    if uid:
                        out[name] = {"uid": uid}
                        _log("enrich %s -> uid=%s" % (name, uid))
                    else:
                        _log("enrich %s -> no exact match" % name)
                except Exception as e:
                    _log("enrich %s error: %r" % (name, e))
                time.sleep(random.uniform(2.5, 5.0))
        except Exception as e:
            _log("enrich error: %r" % e)
        finally:
            _close()
    return out


def _send_once(page, name, text, force=False):
    """单次发送。返回 (ok, detail)。

    2026-11-17 重写，修复「同一人一天收到 4 条 / 部分人没发」：
    1) 幂等锁：按下 Enter **之前**先落盘登记发送动作。之后无论校验成功、
       失败还是进程崩溃，当天都不会再对该目标执行第二次发送动作；
    2) 输入框只认聊天窗 Slate 编辑器，空状态零宽空格按「已清空」处理；
    3) 删除「Enter 后输入框仍有文本就再点一次发送按钮」的兜底——它是
       重复投递的直接推手，且实测 Enter 本身已能把消息发出去；
    4) 成功判据：输入框清空 或 含目标文本的消息行数增加。
    """
    day = datetime.now().strftime("%Y-%m-%d")
    if not force and config.already_attempted_today(name):
        _log("send_message -> %s SKIP (already attempted today)" % name)
        return True, "今日已发送过，跳过（幂等保护，避免重复打扰）"

    try:
        _ensure_conversation_list(page)
        if not has_session_cookie(_context):
            raise NotLoggedIn("登录态已失效，请重新扫码登录")
        _find_and_open_conversation(page, name)
        page.wait_for_timeout(400)

        # 会话没真正打开时页面仍可能残留主站的搜索框/评论框。
        # 此处必须失败退出，绝不把文本写进别的输入框再按 Enter。
        if not _mark_input_box(page):
            raise SendFailed("聊天输入框未就绪（会话可能未正确打开），未执行发送")

        typed_ok, observed = _type_into_input(page, text)
        if not typed_ok:
            raise SendFailed("文本未能写入聊天输入框（未执行发送，避免空发）；观察到 %r" % observed)

        before = _chat_bubbles(page)
        before_hits = sum(1 for t in before if text in t)

        # 幂等登记：放在发送动作之前的最后一步。
        config.record_attempt(day, name, text)
        page.keyboard.press("Enter")

        cleared_streak = 0
        samples = []
        for _ in range(24):
            page.wait_for_timeout(500)
            cur = _read_input(page)
            if cur is None:
                # DOM 重建导致标记丢失：重新定位后再读，不能当成「已清空」
                _mark_input_box(page)
                cur = _read_input(page)
            after = _chat_bubbles(page)
            hits = sum(1 for t in after if text in t)
            empty = _input_is_empty(cur)
            cleared_streak = cleared_streak + 1 if empty else 0
            if len(samples) < 8:
                samples.append("in=%s,hit=%d->%d"
                               % ("E" if empty else ("F" if cur is not None else "?"),
                                  before_hits, hits))
            if hits > before_hits:
                _log("send_message -> %s OK (new own message)" % name)
                return True, "已发送（聊天窗确认到新消息）"
            if cleared_streak >= 2:
                _log("send_message -> %s OK (input cleared)" % name)
                return True, "已发送（输入框已清空，客户端已接受发送）"
        tail = _dialog_tail_hint(page)
        raise SendFailed("发送后 12s 内未确认到新消息（已登记发送动作，今天不会自动重发）；采样: %s"
                         % "; ".join(samples)
                         + ("；页面尾部: %s" % tail if tail else ""))
    except InfraError:
        raise
    except SendFailed as e:
        _log("send_message -> %s FAIL: %s" % (name, e))
        return False, str(e)
    except Exception as e:
        from playwright.sync_api import TimeoutError as PWTimeout

        if isinstance(e, PWTimeout):
            return False, "页面加载超时: %s" % e
        _log("send_message error: %r" % e)
        return False, "异常: %r" % e


def begin_batch():
    """整轮开始：取浏览器锁、只启动一次浏览器、一次预检网络与登录态。

    预检失败抛 InfraError，调度层据此中止整轮：不写失败历史、不标记当天已跑，
    环境恢复后自动补跑未完成的目标。修复 10-23 / 11-02 / 11-16 三次全军覆没
    的放大机制——旧版会让 10 个目标各失败一次，最后还把当天标记为已完成。
    """
    _BROWSER_LOCK.acquire()
    try:
        page = _open(headless=False)
        _goto_retry(page, "https://www.douyin.com/", attempts=2,
                    wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1200)
        if not has_session_cookie(_context):
            raise NotLoggedIn("登录态已失效，请重新扫码登录")
        return page
    except InfraError:
        _close()
        _BROWSER_LOCK.release()
        raise
    except Exception as e:
        # 预检阶段的任何异常都按基础设施故障处理，避免冒泡成普通异常
        # 绕过调度层的短路逻辑，导致目标被逐个消耗并写入假失败。
        _close()
        _BROWSER_LOCK.release()
        raise InfraError("发送前预检失败: %s" % str(e).split("\n")[0]) from e


def end_batch():
    """整轮结束：关闭浏览器并释放锁。"""
    _close()
    try:
        _BROWSER_LOCK.release()
    except RuntimeError:
        pass


def send_message(name, text, page=None, force=False):
    """给 name 发送一条 text。返回 (ok, detail)。

    page 非空时复用批次浏览器；force=True 仅用于用户明确要求的手动强制重发，
    正常调度一律走幂等锁（当天最多一次发送动作）。
    """
    if page is not None:
        return _send_once(page, name, text, force=force)
    with _BROWSER_LOCK:
        p = _open(headless=False)
        try:
            return _send_once(p, name, text, force=force)
        finally:
            _close()
