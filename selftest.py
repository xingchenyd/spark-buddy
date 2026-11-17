# -*- coding: utf-8 -*-
"""SparkBuddy 自检（不会给任何好友发送消息）。

用法：
    .venv\\Scripts\\python.exe selftest.py            # 离线 + 浏览器启动自检
    .venv\\Scripts\\python.exe selftest.py --live    # 追加：只读打开抖音消息中心

覆盖 2026-11-16 修复的四类根因：
  1. 浏览器可执行文件自动发现（不再硬绑 chromium-1194）；
  2. 输入框定位与严格写入校验（旧版 filled=False 导致空发）；
  3. 消息行增量校验（旧版固定文本计数造成假成功/假失败）；
  4. 基础设施故障短路（不写失败历史）。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import config, douyin, scheduler  # noqa: E402

TEXT = "✨今日火花+1"
HTML = """<!doctype html><html><body>
<div data-e2e="im-dialog">
  <div data-stack-layer="chat" id="chat">
    <div class="MessageBoxContentrowBox" style="min-height:22px">✨今日火花+1</div>
    <div class="MessageBoxContentrowBox" style="min-height:22px">✨今日火花+1</div>
    <div class="MessageBoxContentrowBox" style="min-height:22px">✨今日火花+1</div>
  </div>
  <div contenteditable="true" id="box" style="width:600px;height:44px;border:1px solid #999"></div>
</div>
</body></html>"""

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print("%s %s %s" % ("PASS" if cond else "FAIL", name, extra), flush=True)


def main(live=False):
    # 1. 浏览器发现
    exe = douyin.ensure_browser_exe()
    check("browser executable discovered", bool(exe) and Path(exe).is_file(), exe)
    check("browser is not the dead chromium-1194", "chromium-1194" not in exe)

    # 2. 真正启动一次持久化上下文
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        with tempfile.TemporaryDirectory(prefix="sb_selftest_") as d:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=d, headless=True, executable_path=exe,
                args=["--no-first-run", "--no-default-browser-check"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_content(HTML)
            check("persistent context launches", page.title() is not None)

            # 3. 输入框定位 + 严格写入
            check("input box located", douyin._mark_input_box(page))
            ok, observed = douyin._type_into_input(page, TEXT)
            check("text strictly written into input", ok and TEXT in (observed or ""), repr(observed))

            # 4. 消息行增量校验
            before = douyin._chat_bubbles(page)
            check("history bubbles read", len(before) == 3, "n=%d" % len(before))
            check("no new bubble => not success", not douyin._bubble_added(before, before, TEXT))
            # 旧版 bug 回归：页面别处又出现同样文本（会话预览/虚拟列表加载），
            # 消息行数不变时绝不能判成刚发送成功
            page.evaluate("""() => {
              const d = document.createElement('div');
              d.style.minHeight = '20px'; d.innerText = '✨今日火花+1';
              document.querySelector('[data-e2e="im-dialog"]').appendChild(d);
            }""")
            after = douyin._chat_bubbles(page)
            check("duplicate text elsewhere != sent",
                  not douyin._bubble_added(before, after, TEXT),
                  "msg=%d->%d" % (len(before), len(after)))
            # 模拟真实发送：输入框清空 + 新增一条我方消息行
            page.evaluate("""(t) => {
              const b = document.querySelector('[data-sb-input="1"]');
              b.innerText = '';
              const d = document.createElement('div');
              d.className = 'MessageBoxContentrowBox';
              d.style.minHeight = '22px'; d.innerText = t;
              document.getElementById('chat').appendChild(d);
            }""", TEXT)
            after2 = douyin._chat_bubbles(page)
            check("new own bubble => success", douyin._bubble_added(before, after2, TEXT),
                  "msg=%d->%d" % (len(before), len(after2)))
            check("input cleared => accepted", (douyin._read_input(page) or "").strip() == "")
            # DOM 重建导致标记丢失时必须返回 None（未知），不能被当成「已清空」
            page.evaluate("""() => document.querySelector('[data-sb-input="1"]')
                                  .removeAttribute('data-sb-input')""")
            check("lost input marker => unknown", douyin._read_input(page) is None)
            ctx.close()

        # 无输入框的页面：绝不能按 Enter
        with tempfile.TemporaryDirectory(prefix="sb_selftest_") as d:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=d, headless=True, executable_path=exe,
                args=["--no-first-run", "--no-default-browser-check"])
            p = ctx.pages[0] if ctx.pages else ctx.new_page()
            p.set_content("<html><body><div>no chat here</div></body></html>")
            ok2, _ = douyin._type_into_input(p, TEXT)
            check("no input box => refuse to send", not ok2)
            ctx.close()

    # 5. 基础设施故障短路：不写任何失败历史
    hp = config.HISTORY_PATH
    n_before = sum(1 for _ in hp.open(encoding="utf-8")) if hp.exists() else 0
    orig = douyin.send_message

    def boom(*a, **k):
        raise douyin.InfraError("simulated infra failure")

    douyin.send_message = boom
    try:
        raised = False
        try:
            scheduler._send_round([{"name": "__selftest__"}],
                                  {"min_delay": 3, "max_delay": 4, "message": TEXT},
                                  page=None)
        except douyin.InfraError:
            raised = True
        finally:
            douyin.send_message = orig
        n_after = sum(1 for _ in hp.open(encoding="utf-8")) if hp.exists() else 0
        check("InfraError propagates to scheduler", raised)
        check("InfraError writes no pseudo-failure", n_before == n_after,
              "history %d -> %d" % (n_before, n_after))
    finally:
        douyin.send_message = orig

    # 5b. 预检异常必须归一化为 InfraError，且浏览器锁一定被释放
    orig_open = douyin._open

    def broken_open(*a, **k):
        raise RuntimeError("simulated preflight crash")

    douyin._open = broken_open
    try:
        normalized = False
        try:
            douyin.begin_batch()
        except douyin.InfraError:
            normalized = True
        except Exception as e:
            print("unexpected exception type: %r" % e, flush=True)
        finally:
            douyin._open = orig_open
        check("preflight crash normalized to InfraError", normalized)
        lock_free = douyin._BROWSER_LOCK.acquire(blocking=False)
        if lock_free:
            douyin._BROWSER_LOCK.release()
        check("browser lock released after preflight failure", lock_free)
    finally:
        douyin._open = orig_open

    # 6. 可选：真实只读检查（不发消息）
    if live:
        try:
            page = douyin.begin_batch()
            check("live preflight (network + login)", True)
            douyin.end_batch()
        except Exception as e:
            check("live preflight (network + login)", False, repr(e)[:160])
        sessions, shot, err = douyin.fetch_sessions()
        check("live conversation list readable", bool(sessions) and not err,
              "sessions=%d err=%s" % (len(sessions), err[:100]))

    bad = [n for n, ok in results if not ok]
    print("\n%d/%d checks passed" % (len(results) - len(bad), len(results)), flush=True)
    if bad:
        print("FAILED: " + ", ".join(bad), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(live="--live" in sys.argv))
