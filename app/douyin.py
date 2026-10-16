"""抖音网页版（主站消息中心）浏览器自动化。

设计原则：
- 登录只用"手机扫码"，登录态存在 browser_data 持久化目录，一次登录长期复用；
- 是否已登录用 cookie（sessionid / sessionid_ss）判断，不依赖页面 DOM，最可靠；
- 发消息走真实浏览器 + 强校验（会话点开输入框出现 + 发送后消息气泡出现），失败自动重试；
- 会话定位用官方 data-e2e 锚点（im-entry / im-dialog / conversation-item），
  搜索兜底只允许侧栏内部搜索框，绝不触碰主站顶部搜索框；
- user_data_dir 同一时刻只能被一个浏览器实例占用，全部会话经 _BROWSER_LOCK 串行化。
"""
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import config

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


class NotLoggedIn(Exception):
    pass


class SendFailed(Exception):
    pass


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

    _playwright = sync_playwright().start()
    _context = _new_context(_playwright, headless=headless, window_pos=window_pos)
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
            page.goto(IM_URL, wait_until="domcontentloaded", timeout=30000)
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


def _open_via_sidebar_search(page, name):
    """在消息侧栏内部搜索并点开会话（绝不碰主站顶部搜索框）。
    结果项带「发消息」按钮时点按钮直达聊天；成功打开聊天窗返回 True。"""
    search = _first_visible(page, SIDEBAR_SEARCH_SELECTORS, timeout=2000)
    if search is None:
        _log("sidebar search: no search box found")
        return False
    try:
        search.click()
        search.fill(name)
        page.keyboard.press("Enter")
        page.wait_for_timeout(2200)
        # 结果项（SearchPanelitembox）：昵称 | 火花数 | 发消息
        dlg = page.locator(IM_DIALOG).first
        try:
            btns = dlg.get_by_text("发消息", exact=True)
            for i in range(min(btns.count(), 5)):
                btn = btns.nth(i)
                try:
                    if not btn.is_visible(timeout=500):
                        continue
                    # 确认按钮所在结果项的文本以目标昵称开头
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
                        _harvest_item_avatar(holder, name)   # 顺手抓头像
                        btn.click()
                        page.wait_for_timeout(1500)
                        if _wait_chat_input(page, 6000) is not None:
                            return True
                except Exception:
                    continue
        except Exception as e:
            _log("sidebar search send-btn error: %r" % e)
        # 结果优先按会话项匹配
        item = _match_conversation_item(page, name)
        if item is not None:
            _harvest_item_avatar(item, name)
            item.click()
            page.wait_for_timeout(1200)
            if _wait_chat_input(page, 6000) is not None:
                return True
        # 侧栏内文本兜底（搜索结果/联系人卡片）
        try:
            cand = dlg.get_by_text(name, exact=True)
            for i in range(min(cand.count(), 5)):
                el = cand.nth(i)
                try:
                    if el.is_visible():
                        el.click()
                        page.wait_for_timeout(1200)
                        if _wait_chat_input(page, 6000) is not None:
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
        _harvest_item_avatar(item, name)   # 顺手抓头像（发送结束统一写回配置）
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


def _shot(page, tag):
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
    """侧栏停在聊天视图时点「返回」回到会话列表。点击了返回元素返回 True。"""
    try:
        return page.evaluate(
            r"""
() => {
  const dlg = document.querySelector("[data-e2e='im-dialog']");
  if (!dlg) return false;
  const els = dlg.querySelectorAll("[class*='back'], [class*='Back'], [aria-label*='返回']");
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


def _open_message_center(page):
    """打开消息侧栏，直到左面板（会话列表）真正可见。
    入口按钮是开关：侧栏若已被页面自动恢复，再点一次反而会关上。
    所以每一步都以「左面板可见」为准：容器没显示→点入口；停在聊天视图→点返回。"""
    page.goto(IM_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    went_home = False
    for attempt in range(4):
        if _sidebar_panel_visible(page):
            if attempt:
                _log("open_message_center: panel visible at attempt %d" % attempt)
            break
        if _sidebar_container_visible(page):
            # 侧栏开着但列表不可见：多半停在聊天视图，点返回
            if not _click_sidebar_back(page):
                _log("open_message_center: sidebar open but panel hidden, no back btn")
            page.wait_for_timeout(2000)
        else:
            # 侧栏没显示：回主页点消息入口（/message 页没有入口按钮）
            if not went_home:
                went_home = True
                _log("open_message_center: /message no sidebar -> homepage entry")
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                          timeout=30000)
                page.wait_for_timeout(2000)
            entry = _first_visible(page, MSG_ENTRY_SELECTORS, timeout=1500)
            clicked = False
            if entry is not None:
                try:
                    entry.click(timeout=1500)
                    clicked = True
                except Exception:
                    pass
            if not clicked:
                try:
                    cand = page.get_by_text("消息", exact=True)
                    for i in range(min(cand.count(), 5)):
                        el = cand.nth(i)
                        if el.is_visible():
                            el.click()
                            clicked = True
                            break
                except Exception:
                    pass
            if not clicked:
                _log("open_message_center: no message entry found (attempt %d)" % attempt)
            page.wait_for_timeout(2200)
    else:
        _log("open_message_center: left panel NOT visible after retries")
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
      if (!seen.has(name)) seen.set(name, {name: name, uid: '', avatar: avatar,
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
            page.goto("https://www.douyin.com/search/%s?type=user" % quote(kw),
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
        page.goto("https://www.douyin.com/search/%s" % quote(name),
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
        page.goto("https://www.douyin.com/search/%s?type=user" % quote(name),
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


def enrich_uid(items):
    """搜人补全抖音号。items 可为 [昵称] 或 {昵称: 头像URL}（带头像可提高匹配率）。
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
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
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


def _send_once(name, text):
    """单次发送尝试。返回 (ok, detail)。"""
    try:
        page = _open(headless=False)
        _open_message_center(page)
        if not has_session_cookie(_context):
            raise NotLoggedIn("登录态已失效，请重新扫码")
        if not _sidebar_panel_visible(page):
            raise SendFailed("消息侧栏未能打开（会话列表不可见），稍后用「立即发送」重试即可")
        _find_and_open_conversation(page, name)
        page.wait_for_timeout(500)

        box = _wait_chat_input(page, 3000)
        if box is None:
            raise SendFailed("找不到聊天输入框（会话已打开但输入框未渲染）")
        box.click()
        page.wait_for_timeout(300)

        before = page.get_by_text(text).count()
        page.keyboard.insert_text(text)
        page.wait_for_timeout(400)
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        # 兜底：Enter 无效时尝试点发送按钮
        btn = _first_visible(page, SEND_BUTTON_SELECTORS, timeout=1200)
        if btn is not None:
            try:
                btn.click()
            except Exception:
                pass

        # 强校验：6 秒内出现含该文本的新消息节点
        ok = False
        for _ in range(12):
            page.wait_for_timeout(500)
            cnt = page.get_by_text(text).count()
            if cnt > before:
                ok = True
                break
        if not ok:
            raise SendFailed("发送后未检测到消息气泡（可能被风控或页面改版）")
        _log("send_message -> %s OK" % name)
        return True, "已发送"
    except NotLoggedIn as e:
        return False, str(e)
    except SendFailed as e:
        _log("send_message -> %s FAIL: %s" % (name, e))
        return False, str(e)
    except Exception as e:
        from playwright.sync_api import TimeoutError as PWTimeout

        if isinstance(e, PWTimeout):
            return False, "页面加载超时: %s" % e
        _log("send_message error: %r" % e)
        return False, "异常: %r" % e
    finally:
        _close()


def send_message(name, text):
    """给 name 发送一条 text。返回 (ok, detail)。
    失败不当场重试（2026-10-16 规则：全部跑完一遍后，失败者由「立即发送」统一补发）。"""
    with _BROWSER_LOCK:
        return _send_once(name, text)
