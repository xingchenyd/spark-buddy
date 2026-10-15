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
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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
    """等待目标会话项出现；期间把鼠标悬到侧栏上小幅滚动（目标会话可能排在列表底部）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        it = _match_conversation_item(page, name)
        if it is not None:
            return it
        try:
            dlg = page.locator(IM_DIALOG).first
            if dlg.is_visible(timeout=500):
                dlg.hover(timeout=800)
            page.mouse.wheel(0, 240)
        except Exception:
            pass
        page.wait_for_timeout(600)
    return None


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
    成功打开聊天窗返回 True。"""
    search = _first_visible(page, SIDEBAR_SEARCH_SELECTORS, timeout=2000)
    if search is None:
        _log("sidebar search: no search box found")
        return False
    try:
        search.click()
        search.fill(name)
        page.keyboard.press("Enter")
        page.wait_for_timeout(2200)
        # 结果优先按会话项匹配
        item = _match_conversation_item(page, name)
        if item is not None:
            item.click()
            page.wait_for_timeout(1200)
            if _wait_chat_input(page, 6000) is not None:
                return True
        # 侧栏内文本兜底（搜索结果/联系人卡片）
        try:
            dialog = page.locator(IM_DIALOG).first
            cand = dialog.get_by_text(name, exact=True)
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


def _open_message_center(page):
    """打开消息中心侧栏：先直接访问 /message，侧栏没出现则回退主站点「消息」图标。"""
    page.goto(IM_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    # 判定消息侧栏是否真的打开：im-dialog 容器可见（不能用主站顶部搜索框判定！）
    if _first_visible(page, [IM_DIALOG], timeout=1500) is None:
        _log("open_message_center: /message fallback -> homepage entry")
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        entry = _first_visible(page, MSG_ENTRY_SELECTORS, timeout=2000)
        if entry is not None:
            entry.click()
            page.wait_for_timeout(2000)
        else:
            # 兜底：按可见文本「消息」点击（右上角导航）
            try:
                cand = page.get_by_text("消息", exact=True)
                for i in range(min(cand.count(), 5)):
                    item = cand.nth(i)
                    try:
                        if item.is_visible():
                            item.click()
                            page.wait_for_timeout(2000)
                            break
                    except Exception:
                        continue
            except Exception:
                pass


# ---- 联系人提取（会话列表 / 用户搜索）----
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


def fetch_contacts(max_tries=3):
    """打开消息中心读取会话列表。返回 (contacts, shot, err)。"""
    with _BROWSER_LOCK:
        shot = ""
        try:
            page = _open(headless=False)
            _open_message_center(page)
            if not has_session_cookie(_context):
                shot = _shot(page, "contacts_nologin")
                return [], shot, "登录态已失效，请先扫码登录"
            page.wait_for_timeout(1500)
            contacts = []
            for i in range(max_tries):
                contacts = _extract_contacts(page)
                if contacts:
                    break
                try:
                    page.mouse.wheel(0, 400)
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            shot = _shot(page, "contacts")
            _log("fetch_contacts -> %d items" % len(contacts))
            if not contacts:
                return [], shot, "未能从消息中心提取到会话（可能还没有聊天记录，或页面改版）"
            return contacts, shot, ""
        except Exception as e:
            _log("fetch_contacts error: %r" % e)
            return [], shot, "异常: %r" % e
        finally:
            _close()


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


def _send_once(name, text):
    """单次发送尝试。返回 (ok, detail)。"""
    try:
        page = _open(headless=False)
        _open_message_center(page)
        if not has_session_cookie(_context):
            raise NotLoggedIn("登录态已失效，请重新扫码")
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
    except PWTimeout as e:
        return False, "页面加载超时: %s" % e
    except Exception as e:
        _log("send_message error: %r" % e)
        return False, "异常: %r" % e
    finally:
        _close()


def send_message(name, text):
    """给 name 发送一条 text，失败自动重试一次（登录失效除外）。
    返回 (ok, detail)：成功/最终失败原因。"""
    with _BROWSER_LOCK:
        ok, detail = _send_once(name, text)
        if ok:
            return True, detail
        if "登录态已失效" in detail:
            return False, detail
        _log("send_message: retry once for %s (first fail: %s)" % (name, detail))
        time.sleep(3)
        ok2, detail2 = _send_once(name, text)
        if ok2:
            return True, "已发送（首次失败，重试成功）"
        return False, "重试后仍失败: %s" % detail2
