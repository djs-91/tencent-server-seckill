#!/usr/bin/env python3
"""
腾讯云服务器抢购助手（控制台版）— bat 一键启动
============================================
活动: 上云精选·限时秒杀（轻量应用服务器 38元/年）
网址: https://cloud.tencent.com/act/pro/featured-202607#MS
功能:
  1. 选择服务器地域（上海/北京/广州）
  2. 扫码登录（弹窗扫码，自动检测，登录态可复用）
  3. 一键抢购（headless 无窗口，心跳监测浏览器存活）
  4. 抢到后大字 🎉 提示

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

REGIONS = {4: "上海", 8: "北京", 1: "广州"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
VIP_URL = "https://act-api.cloud.tencent.com/user/get-vip-info"


# ---------- 基础工具 ----------
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
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


def bark_push(title, body):
    cfg = load_config()
    key = cfg.get("bark_key", "").strip()
    if not key:
        return
    try:
        import requests
        requests.post(f"https://api.day.app/{key}/{title}/{body}", timeout=5)
        log("📱 Bark 推送已发送")
    except Exception as e:
        log(f"⚠️ Bark 推送失败: {e}")


# ---------- 登录 ----------
async def _check_login(page):
    """无害验证：get-vip-info 查询接口，绝不产生订单"""
    r = await page.evaluate("""
        (url) => fetch(url, {method:'POST',
            headers:{'Content-Type':'application/json','x-csrf-token':window.__latestCsrf||''},
            credentials:'include', body:'{}'}).then(r=>r.json()).catch(e=>({code:-1,msg:e.message}))
    """, VIP_URL)
    return r.get("code") != "NOT-LOGINED", r


async def do_login():
    """弹出浏览器扫码 → 自动检测登录 → 保存状态"""
    c("=" * 56, Fore.YELLOW)
    c("扫码登录", Fore.YELLOW)
    c("=" * 56, Fore.YELLOW)
    c("浏览器即将弹出（仅登录用，登录成功自动关闭）", Fore.CYAN)
    c("请在弹出窗口里：点右上角【登录】→ 微信扫码", Fore.CYAN)
    c("（本机已保存过登录态可跳过本步骤）", Fore.WHITE)

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    ctx = await browser.new_context(viewport={"width": 1400, "height": 900}, user_agent=UA)
    await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    await ctx.add_init_script(sf.CSRF_HOOK_JS)
    page = await ctx.new_page()
    page.set_default_timeout(30000)

    log("打开活动页...")
    await page.goto(sf.ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)
    for _ in range(25):
        if await page.evaluate("() => !!window.__latestCsrf"):
            break
        await asyncio.sleep(0.2)

    log("请扫码... (10分钟超时)")
    deadline = asyncio.get_event_loop().time() + 600
    ok = False
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(3)
        try:
            logged, r = await _check_login(page)
            if logged:
                ok = True
                break
        except Exception as e:
            log(f"检测异常: {e}")
    if ok:
        await ctx.storage_state(path=str(STATE_PATH))
        c("✅ 登录成功，登录态已保存！", Fore.GREEN)
        log("✅ 登录成功（get-vip-info 验证通过）")
    else:
        c("❌ 10分钟超时未登录", Fore.RED)
    await browser.close()
    await pw.stop()
    return ok


# ---------- 抢购 ----------
async def ensure_login():
    """确保有登录态；没有则引导扫码登录"""
    if STATE_PATH.exists():
        return True
    c("❌ 未找到登录态（首次使用需先扫码登录）", Fore.RED)
    y = input("  现在扫码登录？(y/n): ").strip().lower()
    if y == "y":
        await do_login()
    if STATE_PATH.exists():
        return True
    c("  未登录，操作已取消", Fore.RED)
    return False


async def run_grab(region_id, target_time=None):
    """headless 抢购 + 心跳监测 + 反馈"""
    if not await ensure_login():
        return False
    goods = {**sf.FLASH_SALE, "goods_param": {**sf.FLASH_SALE["goods_param"], "regionId": region_id}}
    c("=" * 56, Fore.YELLOW)
    c(f"开始抢购 | 地域: {REGIONS.get(region_id, region_id)} | 商品: {goods['name']}", Fore.YELLOW)
    c("浏览器已隐藏运行（headless）", Fore.CYAN)
    c("=" * 56, Fore.YELLOW)

    pw = await sf.async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )

    # 心跳监测线程（控制台）
    stop_hb = {"v": False}
    def heartbeat():
        while not stop_hb["v"]:
            try:
                alive = browser.is_connected()
            except Exception:
                alive = False
            sys.stdout.write(f"\r🖥️ 浏览器状态: {'运行中' if alive else '❌ 已关闭'} | 按 Ctrl+C 停止    ")
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

        log("加载活动页...")
        await page.goto(sf.ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # 登录检查（无害）
        try:
            logged, r = await _check_login(page)
            if not logged:
                c("❌ 登录态无效（NOT-LOGINED）", Fore.RED)
                y = input("  是否现在重新扫码登录？(y/n): ").strip().lower()
                if y == "y":
                    if await do_login():
                        c("  ✅ 重新登录成功，请回到菜单重新选择场次开始抢购。", Fore.GREEN)
                    else:
                        c("  ❌ 重新登录失败", Fore.RED)
                return False
            log("✅ 登录态有效")
        except Exception as e:
            c(f"❌ 登录检查失败: {e}", Fore.RED)
            return False

        await ctx.storage_state(path=str(STATE_PATH))

        # 等 CSRF token
        for _ in range(50):
            if await page.evaluate("() => !!window.__latestCsrf"):
                break
            await asyncio.sleep(0.2)

        # 对时
        offset_ms = await sf.sync_time(page)

        # 目标时间
        if target_time:
            now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
            target_dt = datetime.strptime(f"{now.date()} {target_time}", "%Y-%m-%d %H:%M")
        else:
            target_dt = sf.find_nearest_time(offset_ms)

        now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
        wait_sec = (target_dt - now).total_seconds()
        log(f"⏰ 目标: {target_dt.strftime('%Y-%m-%d %H:%M:%S')} (服务器时间)")
        log(f"⏳ 等待 {wait_sec:.0f}s ({wait_sec/60:.1f}min)")

        # 等待循环（含 3 分钟重对时 + 心跳）
        warmed_up = False
        resynced = False
        while True:
            if not browser.is_connected():
                c("\n❌ 浏览器已被关闭！程序中止。", Fore.RED)
                return False
            now_ms = int(time.time() * 1000) + offset_ms
            remaining = (target_dt.timestamp() * 1000 - now_ms) / 1000

            if remaining <= 180 and not resynced:
                log("🔄 剩余3分钟，重新对时...")
                offset_ms = await sf.sync_time(page)
                resynced = True
                continue

            if remaining * 1000 <= -sf.ADVANCE_MS:
                log(f"🚀 提前 {abs(sf.ADVANCE_MS)}ms 开始！")
                break

            if remaining <= 10:
                log(f"⚡ {remaining:.1f}s")
            elif int(remaining) % 60 == 0:
                log(f"  还剩 {remaining/60:.0f}min")

            if remaining <= sf.WARMUP_SECONDS and not warmed_up:
                await sf.warmup(page, goods)
                warmed_up = True

            await asyncio.sleep(0.05 if remaining <= 5 else 1)

        # 抢购
        success = await sf.rapid_purchase(page, goods)
        stop_hb["v"] = True
        if success:
            c("\n" + "🎉" * 20, Fore.GREEN)
            c("🎉🎉🎉 抢购成功！请在腾讯云控制台付款（1小时内）！", Fore.GREEN)
            c("🎉" * 20, Fore.GREEN)
        else:
            c("\n❌ 未抢到，本场结束。", Fore.RED)
        return success
    except KeyboardInterrupt:
        c("\n用户中断", Fore.YELLOW)
    except Exception as e:
        c(f"\n❌ 错误: {e}", Fore.RED)
        import traceback
        traceback.print_exc()
    finally:
        stop_hb["v"] = True
        try:
            await ctx.storage_state(path=str(STATE_PATH))
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
        await pw.stop()
    return False


# ---------- 菜单 ----------
def show_menu():
    cfg = load_config()
    region = cfg.get("region", 8)
    state_exists = STATE_PATH.exists()
    c("=" * 56, Fore.YELLOW)
    c("        腾讯云服务器抢购助手", Fore.YELLOW + Style.BRIGHT)
    c("  活动: 上云精选·限时秒杀（轻量应用服务器 38元/年）", Fore.WHITE)
    c("  https://cloud.tencent.com/act/pro/featured-202607#MS", Fore.WHITE)
    c("=" * 56, Fore.YELLOW)
    c(f"  服务器地域: {REGIONS.get(region, region)}", Fore.CYAN)
    c(f"  登录状态  : {'已保存(使用时自动校验)' if state_exists else '未登录'}", Fore.CYAN)
    c("-" * 56, Fore.WHITE)
    c("  [1] 选择服务器地域（上海/北京/广州）", Fore.WHITE)
    c("  [2] 扫码登录（首次或登录过期时）", Fore.WHITE)
    c("  [3] 开始抢购（自动找最近场次 10:00/15:00）", Fore.WHITE)
    c("  [4] 选择场次抢购（10:00 / 15:00）", Fore.WHITE)
    c("  [5] 测试连通（验证登录+参数，不抢购）", Fore.WHITE)
    c("  [6] 测试下单（真实下单，慎用！）", Fore.RED)
    c("  [0] 退出", Fore.WHITE)
    c("-" * 56, Fore.WHITE)
    c("⚠️ 免责声明: 本工具仅供个人学习使用；自动化抢购可能违反", Fore.YELLOW)
    c("   活动规则，可能导致账号限流/受限，风险自担；请勿高频滥用。", Fore.YELLOW)
    c("=" * 56, Fore.YELLOW)


async def test_connect(region_id):
    """测试：headless 打开页面 + 验证登录（无害，不抢购）"""
    if not await ensure_login():
        return False
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = await browser.new_context(storage_state=str(STATE_PATH), user_agent=UA)
    await ctx.add_init_script(sf.CSRF_HOOK_JS)
    page = await ctx.new_page()
    await page.goto(sf.ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)
    for _ in range(25):
        if await page.evaluate("() => !!window.__latestCsrf"):
            break
        await asyncio.sleep(0.2)
    logged, r = await _check_login(page)
    c(f"  登录态: {'✅ 有效' if logged else '❌ 无效 (' + str(r.get('code')) + ')'}", Fore.GREEN if logged else Fore.RED)
    c(f"  地域: {REGIONS.get(region_id)} (regionId={region_id})", Fore.CYAN)
    if logged:
        c("  测试通过，参数就绪，可以开抢。", Fore.GREEN)
    else:
        c("  ❌ 登录态无效！需要重新扫码登录。", Fore.RED)
    await browser.close()
    await pw.stop()
    if not logged:
        y = input("  是否现在重新扫码登录？(y/n): ").strip().lower()
        if y == "y":
            await do_login()
    return logged


async def test_order(region_id):
    """测试下单：用服务器专区商品真实下单（会产生订单，慎用！）"""
    if not await ensure_login():
        return
    c("⚠️" * 20, Fore.RED)
    c("⚠️ 风险提示：测试下单会【真实创建订单】！", Fore.RED)
    c("⚠️ 订单不付款会在 1 小时内自动关闭，也可到控制台手动取消", Fore.RED)
    c("⚠️" * 20, Fore.RED)
    y = input("  确认测试下单？输入 YES 继续: ").strip()
    if y != "YES":
        c("  已取消", Fore.YELLOW)
        return
    goods = {**sf.TEST_GOODS, "goods_param": {**sf.TEST_GOODS["goods_param"], "regionId": region_id}}
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = await browser.new_context(storage_state=str(STATE_PATH), user_agent=UA)
    await ctx.add_init_script(sf.CSRF_HOOK_JS)
    page = await ctx.new_page()
    page.set_default_timeout(30000)
    await page.goto(sf.ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)
    for _ in range(50):
        if await page.evaluate("() => !!window.__latestCsrf"):
            break
        await asyncio.sleep(0.2)
    logged, _ = await _check_login(page)
    if not logged:
        c("  ❌ 登录态无效（NOT-LOGINED）", Fore.RED)
        await browser.close()
        await pw.stop()
        y = input("  是否现在重新扫码登录？(y/n): ").strip().lower()
        if y == "y":
            await do_login()
        return
    c("  开始测试下单（服务器专区商品，持续5秒）...", Fore.CYAN)
    success = await sf.rapid_purchase(page, goods, duration=5)
    if success:
        c("  🎉 测试下单成功！（记得去控制台取消该订单）", Fore.GREEN)
    else:
        c("  测试完成（未下单成功，可查看上方日志判断原因）", Fore.YELLOW)
    await browser.close()
    await pw.stop()


def main():
    while True:
        show_menu()
        try:
            choice = input("请输入: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        cfg = load_config()
        if choice == "1":
            c("  地域选项: [4]上海 [8]北京 [1]广州", Fore.CYAN)
            r = input("  选择地域编号: ").strip()
            if r in ("4", "8", "1"):
                cfg["region"] = int(r)
                save_config(cfg)
                c(f"  ✅ 地域已设为 {REGIONS[int(r)]}", Fore.GREEN)
            else:
                c("  ❌ 无效输入", Fore.RED)
        elif choice == "2":
            asyncio.run(do_login())
        elif choice == "3":
            asyncio.run(run_grab(cfg.get("region", 8)))
        elif choice == "4":
            c("  场次选项: [1] 10:00 场  [2] 15:00 场", Fore.CYAN)
            t = input("  选择场次编号: ").strip()
            if t == "1":
                asyncio.run(run_grab(cfg.get("region", 8), target_time="10:00"))
            elif t == "2":
                asyncio.run(run_grab(cfg.get("region", 8), target_time="15:00"))
            else:
                c("  ❌ 无效输入（请输入 1 或 2）", Fore.RED)
        elif choice == "5":
            asyncio.run(test_connect(cfg.get("region", 8)))
        elif choice == "6":
            asyncio.run(test_order(cfg.get("region", 8)))
        elif choice == "0":
            break
        else:
            c("  ❌ 无效选项", Fore.RED)
        input("\n按回车继续...")
    c("再见！", Fore.YELLOW)


if __name__ == "__main__":
    main()
