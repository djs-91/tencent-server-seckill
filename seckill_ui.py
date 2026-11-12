#!/usr/bin/env python3
"""
腾讯云服务器抢购助手（控制台版）— bat 一键启动
============================================
活动: 上云精选·限时秒杀（轻量应用服务器 38元/年）
网址: https://cloud.tencent.com/act/pro/featured-202607#MS

菜单里选地域、扫码登录、开抢。登录态存在 .state.json，下次直接复用。

关于登录态（踩坑记录，改之前先看这段）：
  - 腾讯的 skey 是会话 cookie（expires = -1）。Playwright 的 storageState()
    **会**导出它，new_context(storage_state=...) 也**能**恢复它 —— 已实测。
    所以"关浏览器就必然要重扫"不是机制限制。
  - 真正容易出问题的是两个时机/判定：
    (1) 扫码成功后 cookie 有几秒延迟，存早了就是一份没有 skey 的空壳；
        -> 用 _wait_login_cookies() 等 skey 落地再存。
    (2) 扫码后 skey 会换新，而页面里缓存的 csrf 还是登录**前**的旧 token，
        服务端于是回 CSRF-ERROR。它既不是"已登录"，也不是"失效"，而是必须
        拿当前 skey 用 g_tk 重算才能消掉的状态 -> _check_login() 里 force 重算。
        在这个状态存 state 服务端不认（issue #13 的假成功就是这么来的）。
免责声明: 仅供个人学习使用；自动化抢购可能违反活动规则，
可能致账号限流/受限，风险自担；请勿高频滥用。
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
except ImportError:
    class _F: RED = GREEN = YELLOW = CYAN = WHITE = MAGENTA = RESET = ""
    class _S: BRIGHT = RESET_ALL = ""
    Fore, Style = _F, _S

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

import seckill as sf  # 复用核心逻辑

CONFIG_PATH = BASE / "config.json"
STATE_PATH = sf.STATE_PATH
LOG_PATH = BASE / "seckill_ui.log"

REGIONS = sf.REGION_NAMES   # 复用 seckill.py 那一份，别各写各的（改一处就够）
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
VIP_URL = "https://act-api.cloud.tencent.com/user/get-vip-info"


# ---------- 基础工具 ----------
# 心跳行宽度：打印正式日志前先擦掉这一行，否则会和日志叠在同一行
HB_WIDTH = 72


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    sys.stdout.write("\r" + " " * HB_WIDTH + "\r")
    print(f"[{ts}] {msg}")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def c(msg, color=Fore.CYAN):
    print(color + msg)


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"region": 8}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 登录 ----------
_NOT_LOGGED_CODES = {"NOT-LOGINED", "NOT_LOGINED", "NOTLOGINED", "ERROR", "FAIL", "UNAUTHORIZED"}
# 只有明确表示成功的 code 才算已登录，其余未识别的一律按未登录处理
_OK_CODES = {"OK", "SUCCESS", "SUCC"}


def _login_state(r):
    """判断 get-vip-info 的返回，'ok' / 'not-logged' / 'csrf' / 'unknown'。

    不能只看 code != 'NOT-LOGINED'：请求失败时前端 fetch 会被 catch 成
    {code: -1}，那不等于 NOT-LOGINED，判成"登录成功"就会误报。

    CSRF-ERROR 单独成一类，但它**不代表已登录**。三方实测：
      带对 csrf  -> code=0
      csrf 陈旧/错误 -> CSRF-ERROR
      连 skey 都没有 -> NOT-LOGINED
    它的意思是「请求带的 csrf 与当前 skey 对不上」，必须拿当前 skey 重算
    token 才能消掉（见 _check_login 的 force 重算）。既不能判成功（会存下
    假登录态），也不能判失效（会逼人白重扫）。
    """
    if not isinstance(r, dict) or "code" not in r:
        return "unknown"
    code = r["code"]
    if code is None or isinstance(code, bool):
        return "unknown"
    if isinstance(code, (int, float)):
        # 只认 0 为成功。正数可能是业务码（1100205 系统繁忙、408 限流、
        # 200 这种 HTTP 风格值），以前一律判 ok，等于把"系统繁忙"当成
        # "登录成功"—— 这是假成功的另一个入口，必须堵掉。
        if code == 0:
            return "ok"
        return "not-logged" if code < 0 else "unknown"
    text = str(code).strip().upper()
    if not text:
        return "unknown"
    if "CSRF" in text:
        return "csrf"          # token 对不上，skey 其实在
    if text in _NOT_LOGGED_CODES:
        return "not-logged"
    if text in _OK_CODES or text == "0":
        return "ok"
    # 不认识的码一律 unknown：既不报成功（防假成功），也不报失效（防误逼重扫）
    return "unknown"


def _i32(x):
    """模拟 JS 的 ToInt32：截到 32 位并转成有符号。"""
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def gtk_of(skey):
    """按腾讯前端的 g_tk 算法从 skey 算出 csrf token（纯 Python，不开页面）。

    必须逐位对齐 JS，不能只写 `e += (e << 5) + ord(c)`：
      · `e << 5` 在 JS 里先 ToInt32 再左移，会溢出成负数；
      · e 本身是 double，累加几十轮就超过 2^53 会丢精度，Python int 不会。
    所以这里 e 用 float 走 IEEE754 加法和 ToInt32 截断，与浏览器完全一致。
    实测：305 组随机串（长度 1~300，含空串）与页面 JS 结果 100% 相同。
    """
    if not skey:
        return None
    e = 5381.0
    for ch in skey:
        e = e + float(_i32(_i32(int(e)) << 5)) + float(ord(ch))
    return str(2147483647 & _i32(int(e)))


async def csrf_from_context(ctx):
    """从 context 的 cookie 里取 skey 现算 csrf，不依赖页面、也不加载页面。"""
    try:
        cookies = await ctx.cookies()
    except Exception:
        return None
    for want in ("skey", "p_skey"):
        for ck in cookies:
            if ck.get("name") == want and ck.get("value"):
                tok = gtk_of(ck["value"])
                if tok:
                    return tok
    return None


async def verify_state(ctx, timeout=15):
    """只打 get-vip-info 校验登录态，**绝不加载任何页面**。返回 (state, r)。

    【为什么不能用页面校验】
    实测（2026-11-12）：拿一份失效 skey 打开 cloud.tencent.com，页面自己请求
    /services/ajax/get_user_info 时，服务端会回 Set-Cookie: skey=; uin=; tinyid=;
    —— 三个登录 cookie 当场被清空（21 条 -> 18 条，skey/uin/tinyid 全没）。
    也就是说「打开活动页校验登录态」这个动作本身在销毁登录态：本来只是暂时
    失效、重试或稍后还能用的，被这么一验就彻底没了，只能重扫。
    有效登录态不受影响（实测 33 -> 34 条，三个都在），但失效时就是雪上加霜。

    只打接口则完全不动 cookie（实测前后条数一致）。csrf 用 g_tk(skey) 现算，
    与页面 hook 抓到的值完全一致（同一算法，305 组实测逐位相同）。
    """
    gtk = await csrf_from_context(ctx)
    if not gtk:
        return "unknown", {"__err": "context 里没有 skey"}
    try:
        resp = await ctx.request.post(
            VIP_URL,
            data="{}",
            headers={"Content-Type": "application/json", "x-csrf-token": gtk},
            timeout=int(timeout * 1000),
        )
        r = await resp.json()
    except Exception as e:
        return "unknown", {"__err": str(e)[:80]}
    return _login_state(r), r


async def verify_state_file(state_path=None):
    """用磁盘上的 .state.json 开一个无头 context 做只读校验，用完即关。

    不需要页面、不会碰 cookie，校验完把 context 扔掉，不落盘。
    返回 (state, r, ctx_cookie_count)。
    """
    from playwright.async_api import async_playwright
    path = Path(state_path) if state_path else STATE_PATH
    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await sf.launch_browser(pw, headless=True)
        ctx = await browser.new_context(storage_state=str(path), user_agent=UA)
        state, r = await verify_state(ctx)
        try:
            n = len(await ctx.cookies())
        except Exception:
            n = -1
        await ctx.close()
        return state, r, n
    except Exception as e:
        return "unknown", {"__err": str(e)[:80]}, -1
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


async def _check_login(page):
    """无害验证：get-vip-info 查询接口，绝不产生订单。返回 (state, r)

    state: 'ok' / 'not-logged' / 'csrf' / 'unknown'
    """
    await sf.ensure_csrf(page)
    r = await page.evaluate("""
        (url) => fetch(url, {method:'POST',
            headers:{'Content-Type':'application/json','x-csrf-token':window.__latestCsrf||''},
            credentials:'include', body:'{}'})
            .then(r => r.json())
            .catch(e => ({__err: String(e)}))
    """, VIP_URL)
    state = _login_state(r)
    if state == "csrf":
        # 换成**当前** skey 重算 csrf 再试一次。
        # 这里必须 force：扫码后 skey 已换新，但 window.__latestCsrf 还是
        # 登录前抓到的旧 token，不强制重算的话重试多少次都是 CSRF-ERROR。
        await asyncio.sleep(0.4)
        await sf.ensure_csrf(page, force=True)
        r2 = await page.evaluate("""
            (url) => fetch(url, {method:'POST',
                headers:{'Content-Type':'application/json','x-csrf-token':window.__latestCsrf||''},
                credentials:'include', body:'{}'})
                .then(r => r.json())
                .catch(e => ({__err: String(e)}))
        """, VIP_URL)
        state2 = _login_state(r2)
        if state2 != "csrf":
            return state2, r2
    return state, r


async def _wait_csrf(page, tries=50):
    """等 CSRF token 就绪。返回 token 或 None，别拿空 token 去请求。

    腾讯云活动页加载后还会自我重定向/重载，导航期间 evaluate 会报
    "Execution context was destroyed"，吞掉重试。"""
    for _ in range(tries):
        try:
            tok = await sf.ensure_csrf(page)
            if tok:
                return tok
        except Exception:
            pass  # 导航中，等页面稳下来再试
        await asyncio.sleep(0.2)
    return None


async def _save_state(ctx):
    """存登录态，并读回校验关键 cookie 是否真的落盘。

    返回 (ok, desc)。desc 给人看，形如 "21 条，含 skey/uin"。

    两条保护：
    1. 先写临时文件、校验通过才替换，避免写一半把原文件搞坏。
    2. 新快照缺 skey/uin 而原文件有，就保留原文件 —— 否则一次网络抖动
       就能把用户刚扫好的登录态覆盖成空壳，下次又得重扫。
    """
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    try:
        await ctx.storage_state(path=str(tmp))
    except Exception as e:
        log(f"保存登录态失败: {e}")
        return False, f"写入失败: {str(e)[:40]}"

    def _keys_of(path):
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return None, 0
        cs = [c for c in (d.get("cookies") or []) if isinstance(c, dict)]
        names = {c.get("name") for c in cs}
        return [k for k in ("skey", "p_skey", "uin") if k in names], len(cs)

    new_keys, n = _keys_of(tmp)
    if new_keys is None:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False, "读回失败（临时文件不可解析）"

    if not new_keys and STATE_PATH.exists():
        old_keys, old_n = _keys_of(STATE_PATH)
        if old_keys:
            try:
                tmp.unlink()
            except Exception:
                pass
            log(f"本次快照缺 skey/uin（{n} 条），保留原登录态（{old_n} 条，含 {'/'.join(old_keys)}）")
            return True, f"保留原登录态（{old_n} 条，含 {'/'.join(old_keys)}）"

    try:
        os.replace(str(tmp), str(STATE_PATH))
    except Exception as e:
        return False, f"替换失败: {str(e)[:40]}"

    if new_keys:
        return True, f"{n} 条，含 {'/'.join(new_keys)}"
    return False, f"{n} 条，缺 skey/uin"


async def _has_login_cookie(ctx):
    """context 里有没有真正的登录凭证（skey/p_skey + uin）。

    这才是"登录成功"的硬标准。光看 get-vip-info 的返回不可靠：
    code 可能是 200 之类的 HTTP 风格值，被当成成功，但 cookie 根本没下发。
    实测用户真实 .state.json：19 条 cookie、有 loginType=wx / qcloud_uid，
    却一个 skey 都没有 —— 就是"接口说成功、cookie 没到位"时存下的空壳。
    """
    try:
        names = {ck.get("name") for ck in await ctx.cookies()}
    except Exception:
        return False, set()
    has = ("skey" in names or "p_skey" in names) and "uin" in names
    return has, names


async def _wait_login_cookies(ctx, timeout=12.0):
    """等 skey/uin 真正下发到 context 再往下走。

    扫码成功到 cookie 落地之间有几秒延迟，固定 sleep 1.5s 经常踩空，
    存出来就是一份没有 skey 的空壳 —— 用户看到"已保存"就关浏览器，
    下次还得重扫。这里改成盯着 ctx.cookies() 直到 skey 出现。
    """
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        ok, _ = await _has_login_cookie(ctx)
        if ok:
            return True
        await asyncio.sleep(0.5)
    return False


async def do_login():
    """弹浏览器扫码，检测到登录后存 cookie。"""
    c("=" * 56, Fore.YELLOW)
    c("扫码登录", Fore.YELLOW)
    c("=" * 56, Fore.YELLOW)
    c("浏览器即将弹出（仅登录用）", Fore.CYAN)
    c("请在弹出窗口里：点右上角【登录】→ 微信扫码", Fore.CYAN)
    c("（本机已保存过登录态可跳过本步骤）", Fore.WHITE)
    c("扫码成功后会等几秒让 cookie 落地，这期间请不要关闭浏览器。", Fore.WHITE)

    pw = None
    browser = None
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await sf.launch_browser(pw, headless=False)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900}, user_agent=UA)
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        await ctx.add_init_script(sf.CSRF_HOOK_JS)
        page = await ctx.new_page()
        page.set_default_timeout(30000)
        sf.attach_csrf_sniffer(page)

        log("打开活动页...")
        await page.goto(sf.ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        if not await _wait_csrf(page):
            log("未捕获到 CSRF token，登录检测可能不准确")

        log("请扫码... (10分钟超时)")
        deadline = asyncio.get_event_loop().time() + 600
        ok = False
        hinted = False
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)
            try:
                state, r = await _check_login(page)
                has_cookie, names = await _has_login_cookie(ctx)

                # 唯一可靠的成功判据：服务端认（get-vip-info 返回 OK）。
                #
                # 曾经用「cookie 里有 skey+uin」当硬标准，实测证明会有假阳性：
                # 扫码后 skey 已经下发，但微信那步没点"确认登录"，服务端照样
                # 回 NOT-LOGINED、页面右上角照样是"登录"按钮、进 console 会被
                # 打回登录页。拿这种 skey 存 state 就是存了个假登录态。
                if state == "ok":
                    ok = True
                    break

                # 【教训】这里曾经加过一条判据：「有 skey/uin + 服务端回
                # CSRF-ERROR = 已登录」。它是 6cea7e9 引入的，也是这次
                # 「扫码怎么都登不上」的根源之一，已彻底删除。
                #
                # CSRF-ERROR 的真实含义是「请求带的 csrf 与当前 skey 对不上」。
                # 三方实测（_verify/probe_stale_csrf.py）：带对 token 返回
                # code=0；token 错/空返回 CSRF-ERROR；连 skey 都没有才是
                # NOT-LOGINED。它既不是"已登录"，也不是"等一会儿就好"，而
                # 是必须拿**当前** skey 重算 token 才能消掉的状态。
                #
                # 在这个状态存 state，服务端不认：issue #13 实测 08:58:44 判
                # "已登录"、08:58:53 复验就 not-logged；本地 13:15 复现同样结果。
                # 假成功会让用户以为登好了，开抢时又报"登录态已失效"，于是
                # 重扫、再假成功，形成死循环——这正是大家反馈的现象。
                #
                # 所以判据回到唯一可靠的那一条：服务端 get-vip-info 明确返回
                # code=0。csrf 不在这里特殊处理，由 _check_login 内部强制重算
                # token 后重试来消解。
                if has_cookie and not hinted:
                    hinted = True
                    if state == "csrf":
                        c("已检测到扫码，正在建立登录态…请在手机上点【确认登录】。", Fore.YELLOW)
                    else:
                        c("已检测到扫码，但服务端还不认。请在手机上点【确认登录】。", Fore.YELLOW)
                    log(f"有 skey/uin 但服务端返回 {state}，继续等用户在手机上确认")
            except Exception as e:
                log(f"检测异常: {e}")

        if ok:
            # 等 skey 真正下发再存：扫码成功后 cookie 有几秒延迟，
            # 存早了就是一份没有 skey 的空壳，下次还得重扫。
            if not await _wait_login_cookies(ctx, timeout=12):
                log("12 秒内没等到完整登录 cookie（skey/uin）")
            saved, desc = await _save_state(ctx)
            if saved:
                log(f"state 已保存：{desc}")
                # 用存下来的这份 state 重新开一次，服务端认了才算真的登录。
                # 绝不能"存下来就算成功"：issue #13 就是这样假成功的——
                # 打印了"登录成功"，9 秒后复验却是 not-logged。
                #
                # 复验同样走「只打接口、不加载页面」：
                # 一是不用再开窗口（无头也不用起，直接省掉一个浏览器），
                # 二是避免加载页面在登录态有问题时把 skey/uin/tinyid 清掉。
                re_state = None
                try:
                    re_state, _vr, _vn = await verify_state_file()
                except Exception as e:
                    c(f"复验出错: {e}", Fore.YELLOW)

                if re_state == "ok":
                    c(f"登录成功，登录态已保存（{desc}）", Fore.GREEN)
                    log("state 复验通过（重启后可直接沿用）")
                elif re_state == "csrf":
                    c(f"登录态未能确认（csrf 未对齐），已保存 {desc}", Fore.YELLOW)
                    c("  建议再执行一次 [2] 确认；开抢前也会自动复验。", Fore.WHITE)
                    log("state 复验：CSRF-ERROR，未能确认登录")
                    ok = False
                elif re_state is None:
                    c(f"登录态已保存（{desc}），但复验没跑成，未能确认。", Fore.YELLOW)
                    log("state 复验未执行")
                    ok = False
                else:
                    c(f"登录没有成功：服务端仍返回未登录（{re_state}）", Fore.RED)
                    c("  存下来的 cookie 服务端不认，请重新扫码。", Fore.WHITE)
                    c("  扫码后一定要在手机上点【确认登录】。", Fore.WHITE)
                    log(f"state 复验没过: {re_state}")
                    ok = False
            else:
                # 关键：不再假报"已保存"。存不全就明说，并给出可执行的建议。
                c(f"扫码成功，但登录态没能完整保存（{desc}）", Fore.YELLOW)
                c("  原因：腾讯的 skey 是会话 cookie，有时在保存那一刻还没下发完整。", Fore.WHITE)
                c("  建议：稍等 3~5 秒再执行一次 [2]，让 cookie 下发完整（多数第二次就能存全）。", Fore.WHITE)
                log(f"state 未完整保存：{desc}")
        else:
            c("10分钟超时未登录", Fore.RED)

        if ok:
            c("登录态已处理完毕。按回车关闭这个浏览器窗口，然后回到菜单选 [3] 开抢。", Fore.CYAN)
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass
        return ok
    except Exception as e:
        c(f"扫码登录过程出错: {str(e)[:120]}", Fore.RED)
        log(f"do_login 异常: {e}")
        return False
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


# ---------- 抢购 ----------
async def ensure_login():
    """没登录态就引导扫码。文件在不代表 cookie 还有效，所以要真验一次。

    判定原则（很重要，关系到会不会白让人扫码）：
      只有服务端明确回 NOT-LOGINED 才算失效。
      CSRF-ERROR / unknown / 请求异常 都不作为"失效"依据：前者是 token 对不上
      （skey 其实在，_check_login 内部会 force 重算），中者是没见过的返回码，
      后者只是网络抖了。把它们当失效，就是"明明登录着还被逼着重扫"的根源。
      反过来也绝不判成功——成功只认 code=0，其余一律不报"登好了"。
    """
    if not STATE_PATH.exists():
        c("未找到登录态，首次使用需要先扫码登录", Fore.RED)
        y = input("  现在扫码登录？(y/n): ").strip().lower()
        if y == "y":
            await do_login()
        if STATE_PATH.exists():
            c("已保存登录态（每次操作前都会自动校验有效性）", Fore.YELLOW)
            return True
        c("  未登录，操作已取消", Fore.RED)
        return False

    # 文件在、但里面根本没有 skey/uin：不用再开浏览器白跑 20 秒，
    # 直接说要重扫。这种文件是历史版本"存早了"留下的空壳，必然不可用。
    if _state_missing_skey():
        c(f"保存的登录态不完整（{_state_summary()}）", Fore.RED)
        c("  这份文件里没有 skey，等于没登录。", Fore.WHITE)
        y = input("  现在重新扫码登录？(y/n): ").strip().lower()
        if y == "y":
            return await do_login()
        c("  未登录，操作已取消", Fore.RED)
        return False

    # 只用接口校验，**不加载任何页面**。
    # 加载页面会在登录态失效时把 skey/uin/tinyid 一起清掉（实测 21 条 -> 18 条，
    # 三个全没），等于校验动作本身把登录态毁了，用户只能重扫。
    state, r, cookie_n = await verify_state_file()
    if state in ("csrf", "unknown"):
        # 可能是网络抖动或 EdgeOne 挑战，隔几秒再试一次，
        # 否则会误判失效、让人白扫一次码
        await asyncio.sleep(3)
        state, r, cookie_n = await verify_state_file()
    detail = str(r.get("code") or r.get("__err") or "")[:40]
    log(f"登录态校验（只打接口）：{state}"
        f"{' ' + detail if detail else ''}，context {cookie_n} 条 cookie")

    if state == "ok":
        log("登录态有效")
        return True

    if state in ("csrf", "unknown"):
        # 无法证明失效 -> 不打扰用户，继续干活；真失效的话开抢前那次复验会报出来
        log(f"登录态未能确认（{state}{': ' + detail if detail else ''}），按有效继续")
        c(f"登录态无法确认（{state}），但也不代表失效，继续尝试。", Fore.YELLOW)
        return True

    c(f"保存的登录态已失效（服务端返回 {detail or 'NOT-LOGINED'}）", Fore.RED)
    y = input("  现在重新扫码登录？(y/n): ").strip().lower()
    if y == "y":
        return await do_login()
    c("  未登录，操作已取消", Fore.RED)
    return False


async def run_grab(region_id, target_time=None, rotate=True):
    """headless 抢购，顺便用心跳显示浏览器还活着没

    rotate=True 时三个地域轮着发。这里刻意不下「库存按地域独立」的结论——
    quota 虽然以 region_id 为键返回，但数字含义还没探明，且抢完再查只剩全 0。
    轮动是保守做法：两种情况都不吃亏。库存结构的线索只在窗口内取，见
    seckill.py 收尾的「库存判定」。
    """
    if not await ensure_login():
        return False
    goods = {**sf.FLASH_SALE, "goods_param": {**sf.FLASH_SALE["goods_param"], "regionId": region_id}}

    # 本机可下单的地域（不含海外/非法值），轮动时全用上
    grab_regions = [4, 8, 1] if rotate else [region_id]

    c("=" * 56, Fore.YELLOW)
    if rotate:
        c(f"开始抢购 | 三地轮动并发：{'/'.join(REGIONS[r] for r in grab_regions)} | 商品: {goods['name']}", Fore.YELLOW)
        c("某个地域售罄就不再去它那里，三个都售罄才停", Fore.WHITE)
    else:
        c(f"开始抢购 | 地域: {REGIONS.get(region_id, region_id)} | 商品: {goods['name']}", Fore.YELLOW)
    c("浏览器已隐藏运行（headless）", Fore.CYAN)
    c("=" * 56, Fore.YELLOW)

    pw = await sf.async_playwright().start()
    browser = await sf.launch_browser(pw, headless=True)

    # 心跳：跑抢购要等很久，给个提示浏览器还活着
    stop_hb = {"v": False}
    def heartbeat():
        while not stop_hb["v"]:
            try:
                alive = browser.is_connected()
            except Exception:
                alive = False
            sys.stdout.write(("\r浏览器状态: %s | Ctrl+C 停止" % ("运行中" if alive else "已关闭")).ljust(HB_WIDTH))
            sys.stdout.flush()
            time.sleep(5)
    import threading
    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()

    try:
        ctx = await browser.new_context(storage_state=str(STATE_PATH), viewport={"width": 1920, "height": 1080}, user_agent=UA)
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        await ctx.add_init_script(sf.CSRF_HOOK_JS)
        page = await ctx.new_page()
        page.set_default_timeout(30000)
        sf.attach_csrf_sniffer(page)

        log("加载活动页...")
        await page.goto(sf.ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        try:
            state, r = await _check_login(page)
            if state == "not-logged":
                c(f"登录态无效（{r.get('code') or r.get('__err')}）", Fore.RED)
                y = input("  是否现在重新扫码登录？(y/n): ").strip().lower()
                if y == "y":
                    if await do_login():
                        c("  重新登录成功，请回到菜单重新选择场次开始抢购。", Fore.GREEN)
                    else:
                        c("  重新登录失败", Fore.RED)
                return False
            if state == "ok":
                log("登录态有效")
            else:
                log(f"登录态未能确认（{state}），按有效继续；真失效会在开抢前复验时报出来")
                c(f"登录态无法确认（{state}），继续尝试。", Fore.YELLOW)
        except Exception as e:
            c(f"登录检查失败: {e}", Fore.RED)
            return False

        _ok, _desc = await _save_state(ctx)
        log(f"登录态刷新（{_desc}）")

        if not await _wait_csrf(page):
            c("未捕获到 CSRF token，请求可能被拒绝", Fore.YELLOW)

        # 商品 id 现查：腾讯换活动就换 act_id，写死在代码里必然过期
        goods_box = {"goods": goods}

        async def refresh_goods():
            found, session = await sf.discover_flash_sale(page, region_id)
            if session:
                log(f"活动 {found['activity_id']} | 场次 {session['start']} ~ "
                    f"{session['end']} (id={session['id']})")
            goods_box["goods"] = found
            log(f"商品: {found['name']} act_id={found['act_id']} "
                f"activity_id={found['activity_id']}")
            log(f"  价格: {sf.fmt_price(found.get('price'))}"
                f"（{found.get('price_check', '未校验')}）")

        offset_ms = await sf.sync_time(page)
        await refresh_goods()

        if target_time:
            now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
            target_dt = datetime.strptime(f"{now.date()} {target_time}", "%Y-%m-%d %H:%M")
        else:
            target_dt = sf.find_nearest_time(offset_ms)

        # 自动找最近场次时 target_time 还是 None，这里从算出的时刻反推出是哪个场，
        # 否则取并发档位时匹配不上，就退化成默认档了
        target_time = target_dt.strftime("%H:%M")

        now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
        wait_sec = (target_dt - now).total_seconds()
        log(f"目标: {target_dt.strftime('%Y-%m-%d %H:%M:%S')} (服务器时间)")
        log(f"等待 {wait_sec:.0f}s ({wait_sec/60:.1f}min)")
        log(f"提前量 {abs(sf.ADVANCE_MS)}ms")

        target_ms = target_dt.timestamp() * 1000
        fire_local_ms = target_ms + sf.ADVANCE_MS - offset_ms   # 开始抢的时刻换算成本机时间

        warmed_up = False
        resynced = False
        while True:
            if not browser.is_connected():
                c("\n浏览器已被关闭，程序中止。", Fore.RED)
                return False
            remaining = (target_ms - (time.time() * 1000 + offset_ms)) / 1000

            # 最后 QUIET_SECONDS 秒只等待：对时、刷新页面、预热都要等网络，
            # 压在最后几秒会把提前量吃掉，所以这段时间一次网络请求都不发。
            if remaining <= sf.QUIET_SECONDS:
                log(f"最后 {remaining:.1f}s，只等待不发请求")
                gap = fire_local_ms - time.time() * 1000
                if gap > 0:
                    await asyncio.sleep(gap / 1000)
                left = target_ms - (time.time() * 1000 + offset_ms)
                log(f"实际提前 {left:.0f}ms（设定 {abs(sf.ADVANCE_MS)}ms，"
                    f"偏差 {left - abs(sf.ADVANCE_MS):+.0f}ms）")
                break

            # 剩余 3 分钟：刷新页面 + 复验登录 + 重新对时。
            # 挂机数小时的会话，CSRF token / 登录态都可能过期，
            # 开抢前必须让页面重新生成 token，并确认登录仍然有效。
            # 这一步必须在进纯等待之前做完，来不及就跳过刷新，不占提前量。
            if remaining <= 180 and not resynced:
                if remaining > sf.WARMUP_SECONDS:
                    log("剩余3分钟，刷新页面并重新对时...")
                    csrf_ok = False
                    for attempt in (1, 2):
                        try:
                            if attempt == 1:
                                await page.reload(wait_until="domcontentloaded", timeout=30000)
                            else:
                                await page.goto(sf.ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000)
                            await asyncio.sleep(2)
                            csrf_ok = await _wait_csrf(page, tries=50)
                            if csrf_ok:
                                break
                            log(f"  第{attempt}次刷新后未捕获到 CSRF，重试...")
                        except Exception as e:
                            log(f"  第{attempt}次刷新失败: {str(e)[:80]}")
                    if csrf_ok:
                        try:
                            # 登录判定走接口而不是页面：页面刚 reload 过，
                            # 它自己的 JS 状态、hook 时机都还没稳，拿它判登录
                            # 容易误报；接口只认 code，不受页面状态干扰。
                            state, r = await verify_state(ctx)
                            if state == "ok":
                                log("登录态复验通过")
                            elif state == "not-logged":
                                c(f"!! 登录态已失效（{r.get('code') or r.get('__err')}），请中断后重新扫码", Fore.RED)
                            else:
                                log(f"登录复验未能确认（{state}），继续（不代表登录失效）")
                        except Exception as e:
                            log(f"登录复验异常: {str(e)[:80]}")
                    else:
                        c("!! 刷新后仍未捕获到 CSRF token，开抢可能失败，请留意", Fore.RED)
                else:
                    log("临近开抢，跳过页面刷新，只重新对时...")
                offset_ms = await sf.sync_time(page)
                fire_local_ms = target_ms + sf.ADVANCE_MS - offset_ms
                try:
                    await refresh_goods()
                except Exception as e:
                    log(f"重新拉取场次失败（沿用上次结果）: {str(e)[:80]}")
                resynced = True
                continue

            if remaining <= sf.WARMUP_SECONDS and not warmed_up:
                warmed_up = True
                # 先查资格（只读）：不合格就当场中止，别白等一整场。
                # 注意服务端返回的是 1/0 而不是 True/False，判断必须走 qualify_blocked
                q = await sf.warmup_qualify(page, goods_box["goods"], grab_regions)
                if q and (sf.qualify_blocked(q.get("user_available"))
                          or sf.qualify_blocked(q.get("idcard_available"))):
                    stop_hb["v"] = True
                    c("\n开抢前资格预检不通过，本场中止。", Fore.RED)
                    return False
                probe = await sf.warmup(page, goods_box["goods"])
                if probe.get("code") in sf.NO_RETRY_CODES:
                    stop_hb["v"] = True
                    if probe.get("code") == sf.CODE_SOLD_OUT:
                        c("\n开抢前这个商品就已经售罄了，中止本场。", Fore.RED)
                    else:
                        c("\n开抢前已知资格不满足，中止本场。", Fore.RED)
                    return False
                continue

            if int(remaining) % 60 == 0:
                log(f"  还剩 {remaining/60:.0f}min")

            await asyncio.sleep(min(1.0, max(0.05, remaining - sf.QUIET_SECONDS)))

        open_in = (target_ms - (time.time() * 1000 + offset_ms)) / 1000
        # 按场次取档位：10:00 场和 15:00 场的拥挤程度不一样（见 seckill.py 注释）。
        # 档位里只有回读间隔和持续时间，并发由程序按实测往返自动爬坡。
        prof = sf.session_profile(target_time)
        log(f"本场档位（{target_time or '非场次'}）：在途 {prof['cap_start']}→{prof['cap_max']} / "
            f"回读 {prof['poll_ms']}ms / 持续 {prof['duration']}s")
        success = await sf.rapid_purchase(page, goods_box["goods"], open_in=open_in,
                                          regions=grab_regions, profile=prof)
        stop_hb["v"] = True
        if success:
            c("\n" + "=" * 40, Fore.GREEN)
            c("抢购成功，请在腾讯云控制台付款（1小时内）", Fore.GREEN)
            c("=" * 40, Fore.GREEN)
        else:
            c("\n未抢到，本场结束。", Fore.RED)
        return success
    except KeyboardInterrupt:
        c("\n用户中断", Fore.YELLOW)
    except Exception as e:
        c(f"\n错误: {e}", Fore.RED)
        import traceback
        traceback.print_exc()
    finally:
        stop_hb["v"] = True
        try:
            _ok, _desc = await _save_state(ctx)
            log(f"退出前刷新登录态（{_desc}）")
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
        await pw.stop()
    return False


# ---------- 菜单 ----------
def _state_missing_skey():
    """文件存在但里面没有 skey/p_skey -> True。损坏文件也按"缺"处理。"""
    if not STATE_PATH.exists():
        return False
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return True
    cs = [c for c in (data.get("cookies") or []) if isinstance(c, dict)]
    names = {c.get("name") for c in cs}
    return not ("skey" in names or "p_skey" in names)


def _state_summary():
    """读 .state.json 报一句实话：到底存到了 skey 没有。

    以前只判断"文件在不在"，文件在但里面没有 skey 时也显示"已保存"，
    用户就放心去关浏览器了，下次又得重扫。字符串必须如实反映内容。
    """
    if not STATE_PATH.exists():
        return "未登录"
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return "文件损坏，需重扫"
    cs = [c for c in (data.get("cookies") or []) if isinstance(c, dict)]
    names = {c.get("name") for c in cs}
    if "skey" in names or "p_skey" in names:
        return f"已保存（{len(cs)} 条 cookie，用时自动校验）"
    return f"不完整（{len(cs)} 条，缺 skey），建议重新扫码"


def show_menu():
    cfg = load_config()
    region = cfg.get("region", 8)
    rotate = cfg.get("rotate", True)
    state_desc = _state_summary()
    c("=" * 56, Fore.YELLOW)
    c("        腾讯云服务器抢购助手", Fore.YELLOW + Style.BRIGHT)
    c("  活动: 上云精选·限时秒杀（轻量应用服务器，每天 10:00 / 15:00）", Fore.WHITE)
    c("  商品 id 每期活动会变，开始抢购时自动现查，无需手动改", Fore.WHITE)
    c("=" * 56, Fore.YELLOW)
    c(f"  登录状态  : {state_desc}", Fore.CYAN)
    p10 = sf.session_profile("10:00")
    p15 = sf.session_profile("15:00")
    c(f"  同时请求  : 10:00场 {p10['cap_start']}→{p10['cap_max']} · "
      f"15:00场 {p15['cap_start']}→{p15['cap_max']}（按实测吞吐自动加，不用设）",
      Fore.CYAN)
    c(f"  持续时间  : 10:00场 {p10['duration']}s · 15:00场 {p15['duration']}s", Fore.CYAN)
    c("-" * 56, Fore.WHITE)
    c(f"  [1] 地域设置（当前: {'三地轮动并发' if rotate else '只抢' + REGIONS.get(region, str(region))}）", Fore.WHITE)
    c("  [2] 扫码登录（首次或登录过期时）", Fore.WHITE)
    c("  [3] 开始抢购（自动找最近场次 10:00/15:00）", Fore.WHITE)
    c("  [4] 选择场次抢购（10:00 / 15:00）", Fore.WHITE)
    c("  [5] 测试下单流程（不付款；测完取消订单，不要反复测）", Fore.WHITE)
    c("  [0] 退出", Fore.WHITE)
    c("-" * 56, Fore.WHITE)
    c("提示: 扫码后紧接着开抢，中间不要关闭弹出的浏览器窗口。", Fore.CYAN)
    c("免责声明: 本工具仅供个人学习使用；自动化抢购可能违反", Fore.YELLOW)
    c("  活动规则，可能导致账号限流/受限，风险自担；请勿高频滥用。", Fore.YELLOW)
    c("=" * 56, Fore.YELLOW)


def region_menu():
    """地域设置：三地轮动并发，或只抢其中一个。原来拆成 [1] 和 [6] 两个入口，太远，合到这里。"""
    cfg = load_config()
    rotate = cfg.get("rotate", True)
    region = cfg.get("region", 8)
    c("-" * 56, Fore.WHITE)
    c("  地域设置", Fore.CYAN)
    c("  [1] 三地轮动并发（默认，推荐）", Fore.WHITE)
    c("  [2] 只抢上海", Fore.WHITE)
    c("  [3] 只抢北京", Fore.WHITE)
    c("  [4] 只抢广州", Fore.WHITE)
    c("  [0] 返回，不改", Fore.WHITE)
    cur = "三地轮动并发" if rotate else f"只抢{REGIONS.get(region, region)}"
    c(f"  当前: {cur}", Fore.YELLOW)
    c("-" * 56, Fore.WHITE)
    try:
        k = input("  请选择: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if k == "1":
        cfg["rotate"] = True
        cfg.pop("region", None)          # 轮动时主地域没意义，别留个死值误导自己
        save_config(cfg)
        c("  已设为三地轮动并发：上海、北京、广州一起抢，某个地域售罄就不再去它那里。", Fore.GREEN)
    elif k in ("2", "3", "4"):
        rid = {"2": 4, "3": 8, "4": 1}[k]
        cfg["rotate"] = False
        cfg["region"] = rid
        save_config(cfg)
        c(f"  已设为只抢{REGIONS[rid]}。另外两个地域不再发请求。", Fore.GREEN)
    elif k == "0":
        c("  未改动。", Fore.YELLOW)
    else:
        c("  无效输入，未改动。", Fore.RED)


async def place_single_order(ctx, goods, preview=0, timeout=20):
    """只发**一条** do-goods，不打开任何页面。返回 (code, msg, raw)。

    实测（2026-11-12）：用 ctx.request.post 直接打 act-api，带 g_tk(skey) 算的
    csrf 返回 200 正常业务码，不带则 400 CSRF-ERROR —— 说明请求完全走通，
    服务端不校验 Referer/Origin，也就不需要为了一次试单去加载活动页。

    这一点很重要：加载活动页会跑抢购那种量级的 JS，而且登录态有问题时还会
    把 skey/uin/tinyid 清掉。试单只需要一个请求，不该付这些代价。
    """
    gtk = await csrf_from_context(ctx)
    if not gtk:
        return None, "context 里没有 skey，无法下单", {}
    body = {
        "activity_id": int(goods["activity_id"]),
        "goods": [{
            "act_id": int(goods["act_id"]),
            "type": goods["type"],
            "goods_param": goods.get("goods_param", {}),
        }],
        "agent_channel": {"fromChannel": "", "fromSales": "", "fromUrl": "",
                          "isAgentClient": False},
        "preview": int(preview),
    }
    try:
        resp = await ctx.request.post(
            sf.DO_GOODS_URL,
            data=json.dumps(body),
            headers={"Content-Type": "application/json", "x-csrf-token": gtk},
            timeout=int(timeout * 1000),
        )
        r = await resp.json()
    except Exception as e:
        return None, f"请求失败: {str(e)[:100]}", {}
    return r.get("code"), r.get("msg") or "", r


# do-goods 的返回码怎么念给人听。只列实测见过的，其余显示原码让用户带原话反馈。
GOODS_CODE_DESC = {
    sf.CODE_OK: "下单成功（已建单）",
    sf.CODE_NOT_STARTED: "秒杀尚未开始（这个商品受时间窗限制）",
    sf.CODE_RATE_LIMIT: "访问频率过快：账号/IP 正在限流，减并发也没用，等 1~2 小时",
    sf.CODE_BUSY: "系统繁忙：服务端在排队",
    sf.CODE_SOLD_OUT: "本期已售罄",
    sf.CODE_FACE_VERIFY: "账号需要人脸核身，去认证后才能买",
    1100106: "检测到相同实名主体已购买该商品，不满足首单条件（这是资格问题，不是程序问题）",
}


async def test_once(region_id):
    """测试下单：用接口单发一条 do-goods，不加载页面、不跑抢购机器。

    【为什么改成这样】
    28de579 曾经把它改成 rapid_purchase(duration=3)，等于把整台抢购机器
    （在途 30→64、每 15ms 回读）直接用来"测一下"。实测有人 3.7 秒打出 360 条
    真下单请求、100% 是 408，账号被打进风控，之后真抢更难。
    试单的目的只是"确认能不能走通、能不能建单"，一条请求足够。
    """
    if not await ensure_login():
        return
    c("!" * 58, Fore.YELLOW)
    c("  测试下单：用 109 元专区商品真下单（preview=0），不付款。", Fore.WHITE)
    c("  **只发 1 条请求，不加载页面、不跑抢购那套并发**，不会因此被限流。", Fore.GREEN)
    c("  能确认：登录态、参数结构、首单资格、能不能建单。", Fore.WHITE)
    c("  不付款的订单 1 小时后自动关闭。抢之前去订单页取消掉。", Fore.YELLOW)
    c("!" * 58, Fore.YELLOW)
    try:
        # 大小写都认：曾有人输 yes 被当成取消，以为程序坏了（issue #14）
        y = input("  确认真下单？输入 YES 继续: ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        return
    if y != "YES":
        c("  已取消", Fore.YELLOW)
        return

    goods = {**sf.TEST_GOODS,
             "goods_param": {**sf.TEST_GOODS["goods_param"], "regionId": region_id}}
    c(f"  测试商品: {goods['name']} act_id={goods['act_id']}", Fore.CYAN)
    c(f"  地域: {REGIONS.get(region_id, region_id)}", Fore.CYAN)

    pw = None
    browser = None
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await sf.launch_browser(pw, headless=True)
        # 注意：只建 context，**不 new_page、不 goto**，所以没有任何页面和窗口
        ctx = await browser.new_context(storage_state=str(STATE_PATH), user_agent=UA)

        # 先用接口确认登录态（不碰页面）
        state, vr, _n = await verify_state(ctx)
        if state == "not-logged":
            c(f"  登录态无效（{vr.get('code') or vr.get('__err')}），先扫码登录再试。", Fore.RED)
            return
        if state != "ok":
            c(f"  登录态未能确认（{state}），继续尝试这一条。", Fore.YELLOW)

        # 价格先查（只读接口，不产生任何写操作）
        try:
            gtk = await csrf_from_context(ctx)
            pr = await ctx.request.post(
                sf.API_BASE + "/dianshi/query-price",
                data=json.dumps({
                    "activity_id": int(goods["activity_id"]),
                    "goods": [{"act_id": int(goods["act_id"]), "type": goods["type"],
                               "goods_param": {**goods["goods_param"]}}],
                    "preview": 0,
                }),
                headers={"Content-Type": "application/json", "x-csrf-token": gtk or ""},
                timeout=15000,
            )
            pj = await pr.json()
            items = (pj.get("data") if isinstance(pj.get("data"), list) else None) or []
            price = next((x for x in items if int(x.get("act_id") or 0) == int(goods["act_id"])), None)
            c(f"  价格: {sf.fmt_price(price)}", Fore.CYAN)
            if price and price.get("real_cost") != sf.TEST_REAL_COST:
                c(f"  注意：不是 109 元那款（实际 {sf.fmt_price(price)}），act_id 可能已变", Fore.RED)
        except Exception as e:
            c(f"  价格查询失败（不影响下单验证）: {str(e)[:60]}", Fore.YELLOW)

        c("  发送 1 条下单请求…", Fore.CYAN)
        code, msg, raw = await place_single_order(ctx, goods, preview=0)
        if code is None:
            c(f"  请求没发出去：{msg}", Fore.RED)
            log(f"试单请求失败: {msg}")
            return

        desc = GOODS_CODE_DESC.get(code, "未识别的返回码，请把服务器原话发 issue")
        if code == sf.CODE_OK:
            c(f"  ★ 下单成功（code={code}）：{desc}", Fore.GREEN)
            c("  请去控制台把这个订单取消掉，不要付款。", Fore.GREEN)
            deal = (raw.get("data") or {})
            if isinstance(deal, dict) and deal.get("deal_names"):
                c(f"  订单号: {deal['deal_names'][0]}", Fore.GREEN)
        else:
            c(f"  返回 code={code}：{desc}", Fore.YELLOW)
            if msg:
                c(f"  服务器原话: {msg}", Fore.WHITE)
        log(f"试单（单发 1 条）: code={code} msg={str(msg)[:80]}")
    except Exception as e:
        log(f"测试下单异常: {str(e)[:120]}")
        return
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


def main():
    while True:
        show_menu()
        try:
            choice = input("请输入: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        cfg = load_config()
        if choice == "1":
            region_menu()
        elif choice == "2":
            asyncio.run(do_login())
        elif choice == "3":
            asyncio.run(run_grab(cfg.get("region", 8), rotate=cfg.get("rotate", True)))
        elif choice == "4":
            c("  场次选项: [1] 10:00 场  [2] 15:00 场", Fore.CYAN)
            t = input("  选择场次编号: ").strip()
            if t == "1":
                asyncio.run(run_grab(cfg.get("region", 8), target_time="10:00", rotate=cfg.get("rotate", True)))
            elif t == "2":
                asyncio.run(run_grab(cfg.get("region", 8), target_time="15:00", rotate=cfg.get("rotate", True)))
            else:
                c("  无效输入（请输入 1 或 2）", Fore.RED)
        elif choice == "5":
            asyncio.run(test_once(cfg.get("region", 8)))
        elif choice == "0":
            break
        else:
            c("  无效选项", Fore.RED)
        try:
            input("\n按回车继续...")
        except (EOFError, KeyboardInterrupt):
            break
    c("再见！", Fore.YELLOW)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl+C 中断（如等待场次时想提前退出）：不再打整页 traceback
        c("\n已中断退出。登录态等进度保存在本地，下次可直接使用。", Fore.YELLOW)
