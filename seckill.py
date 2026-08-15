#!/usr/bin/env python3
"""
腾讯云服务器抢购终极方案 — 浏览器内注入JS直接调API
==================================================
在浏览器上下文中用 fetch() 调用 do-goods API，
浏览器自动处理 cookie、CSRF、所有认证。

比纯 HTTP 快（不需要自己处理认证），比 DOM 点击快（不等面板渲染）。

使用:
  python3 seckill.py                  # 抢最近一场
  python3 seckill.py --time 10:00     # 指定时间
  python3 seckill.py --loop           # 持续运行
  python3 seckill.py --test           # 测试(用服务器专区商品)
"""

import asyncio
import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout
except ImportError:
    print("pip3 install playwright && playwright install chromium")
    sys.exit(1)

# ============================================================
# 配置
# ============================================================

ACTIVITY_URL = "https://cloud.tencent.com/act/pro/featured-202607"

# API 地址
DO_GOODS_URL = "https://act-api.cloud.tencent.com/dianshi/do-goods"

# 抢购商品
FLASH_SALE = {
    "act_id": 1897632168296710,
    "activity_id": 164461404341040,
    "type": "lighthouse_v5",
    "name": "轻量4核4G3M (38元/年)",
    "goods_param": {
        "BlueprintId": "LINUX_UNIX",
        "area": 1,
        "ddocUnionConnect": 0,
        "goodsNum": 1,
        "imageId": "lhbp-eqora508",
        "scenario": "0",
        "timeSpanUnit": "12m",
        "zone": "",
        "regionId": 8,  # 地域: 4=上海 8=北京 1=广州 (area=1 内地)
        "type": "bundle_budget_mc_lg4_01",
    },
}

# 测试商品 (服务器专区)
TEST_GOODS = {
    "act_id": 1865957483429697,
    "activity_id": 164461404341040,
    "type": "lighthouse_v5",
    "name": "轻量4核4G3M (服务器专区)",
    "goods_param": {
        "BlueprintId": "LINUX_UNIX",
        "area": 1,
        "ddocUnionConnect": 0,
        "goodsNum": 1,
        "imageId": "lhbp-eqora508",
        "scenario": "0",
        "timeSpanUnit": "12m",
        "zone": "",
        "regionId": 8,  # 地域: 4=上海 8=北京 1=广州 (area=1 内地)
        "type": "bundle_budget_mc_lg4_01",
    },
}

SECKILL_TIMES = ["10:00", "15:00"]

# 并发请求数
CONCURRENCY = 10

# 持续发送时间 (秒)
DURATION_SEC = 10

# 每轮间隔 (毫秒)
ROUND_INTERVAL_MS = 20

# 提前多少毫秒开始发请求 (负数=提前，-3000=提前3秒)
ADVANCE_MS = -3000

# 提前多少秒开始预热连接 (在目标时间前)
WARMUP_SECONDS = 10

STATE_PATH = Path(__file__).parent / ".state.json"
LOG_PATH = Path(__file__).parent / "seckill.log"

# 捕获前端真实请求用的 x-csrf-token（前端动态生成，只能 hook fetch/XHR 获取）
CSRF_HOOK_JS = """
window.__latestCsrf = null;
const _fetch = window.fetch;
window.fetch = function(...args) {
    try {
        const h = (args[1] && args[1].headers) || {};
        if (h['x-csrf-token']) window.__latestCsrf = h['x-csrf-token'];
    } catch(e) {}
    return _fetch.apply(this, args);
};
const _open = XMLHttpRequest.prototype.open;
const _set = XMLHttpRequest.prototype.setRequestHeader;
XMLHttpRequest.prototype.open = function(m, u) {
    return _open.apply(this, arguments);
};
XMLHttpRequest.prototype.setRequestHeader = function(k, v) {
    if (String(k).toLowerCase() === 'x-csrf-token') window.__latestCsrf = v;
    return _set.apply(this, arguments);
};
"""


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}")
    with open(LOG_PATH, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


async def sync_time(page):
    """对时：7次采样 act-api 响应头 Date 取中位数（秒精度噪声±500ms → 收敛到±0.2s），失败回退页面 nowTime"""
    import email.utils
    SAMPLES = 7
    offsets = []

    async def one_sample():
        fut = asyncio.get_event_loop().create_future()

        def on_resp(resp):
            if fut.done():
                return
            if "act-api" in resp.url:
                d = resp.headers.get("date") or resp.headers.get("Date")
                if d:
                    try:
                        dt = email.utils.parsedate_to_datetime(d)
                        fut.set_result(int(dt.timestamp() * 1000) - int(time.time() * 1000))
                    except Exception:
                        pass

        page.on("response", on_resp)
        try:
            # 主动触发一次 act-api 查询请求（get-vip-info，无害查询接口）
            await page.evaluate("""
                (url) => fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'x-csrf-token': window.__latestCsrf || ''},
                    credentials: 'include', body: '{}'
                }).then(r => r.json()).catch(e => ({}))
            """, "https://act-api.cloud.tencent.com/user/get-vip-info")
            off = await asyncio.wait_for(fut, timeout=3)
            offsets.append(off)
        except Exception:
            pass
        finally:
            page.remove_listener("response", on_resp)

    for _ in range(SAMPLES):
        await one_sample()
        await asyncio.sleep(0.15)

    if offsets:
        offsets.sort()
        offset = int(offsets[len(offsets) // 2])
        log(f"⏰ 服务器时间偏移: {offset:+d}ms (Date头对时 ×{len(offsets)}中位数 {offsets})")
        return offset

    # 回退：页面 nowTime（静态值，含页面加载延迟，精度较差）
    offset = await page.evaluate("""
        () => {
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const m = (s.textContent||'').match(/"nowTime"\\s*:\\s*(\\d{13})/);
                if (m) return parseInt(m[1]) - Date.now();
            }
            return 0;
        }
    """)
    log(f"⏰ 服务器时间偏移: {offset:+d}ms (nowTime回退)")
    return offset


async def api_do_goods(page, goods, attempt):
    """在浏览器上下文中调用 do-goods API"""
    result = await page.evaluate("""
        async (params) => {
            try {
                const resp = await fetch(params.url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'x-csrf-token': window.__latestCsrf || ''},
                    credentials: 'include',
                    body: JSON.stringify({
                        activity_id: params.activity_id,
                        goods: [{act_id: params.act_id, type: params.type, goods_param: params.goods_param}],
                        agent_channel: {fromChannel: '', fromSales: '', fromUrl: '', isAgentClient: false},
                        preview: 0,
                    }),
                });
                return await resp.json();
            } catch(e) {
                return {code: -1, msg: e.message};
            }
        }
    """, {
        "url": DO_GOODS_URL,
        "activity_id": goods["activity_id"],
        "act_id": goods["act_id"],
        "type": goods["type"],
        "goods_param": goods.get("goods_param", {}),
    })

    code = result.get("code")
    msg = result.get("msg", "")

    if code == 0:
        deal = result.get("data", {})
        log(f"🎉🎉🎉 下单成功！#{attempt}")
        log(f"   订单号: {deal.get('deal_names', ['?'])[0]}")
        log(f"   大单号: {deal.get('big_deal_no', '?')}")
        return True
    elif attempt <= 5 or code != 1435936:
        if attempt <= 10:
            log(f"  #{attempt} code={code} msg={msg[:60]}")
    return False


async def warmup(page, goods):
    """预热连接：提前发一个请求建立TCP/TLS，到点直接用"""
    log("🔥 预热连接...")
    result = await api_do_goods(page, goods, 0)
    log(f"  预热完成 (结果: {'成功' if result else '未到时间，正常'})")


async def rapid_purchase(page, goods, duration=DURATION_SEC):
    """并发疯狂调用API — 批量模式，单次evaluate发多个请求"""
    log(f"🔥 开始抢购！并发={CONCURRENCY}，间隔={ROUND_INTERVAL_MS}ms，持续={duration}s")

    start = time.monotonic()
    attempt = 0
    success = False

    while time.monotonic() - start < duration:
        # 批量发起：一次 evaluate 发 CONCURRENCY 个并发 fetch
        batch_result = await page.evaluate("""
            async (params) => {
                const results = [];
                const promises = [];
                for (let i = 0; i < params.concurrency; i++) {
                    promises.push(
                        fetch(params.url, {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json', 'x-csrf-token': window.__latestCsrf || ''},
                            credentials: 'include',
                            body: JSON.stringify({
                                activity_id: params.activity_id,
                                goods: [{act_id: params.act_id, type: params.type, goods_param: params.goods_param}],
                                agent_channel: {fromChannel: '', fromSales: '', fromUrl: '', isAgentClient: false},
                                preview: 0,
                            }),
                        })
                        .then(r => r.json())
                        .catch(e => ({code: -1, msg: e.message}))
                    );
                }
                return await Promise.all(promises);
            }
        """, {
            "url": DO_GOODS_URL,
            "activity_id": goods["activity_id"],
            "act_id": goods["act_id"],
            "type": goods["type"],
            "goods_param": goods.get("goods_param", {}),
            "concurrency": CONCURRENCY,
        })

        attempt += CONCURRENCY

        # 检查结果
        for r in batch_result:
            code = r.get("code")
            if code == 0:
                deal = r.get("data", {})
                log(f"🎉🎉🎉 下单成功！")
                log(f"   订单号: {deal.get('deal_names', ['?'])[0]}")
                log(f"   大单号: {deal.get('big_deal_no', '?')}")
                success = True
                break

        if success:
            break

        if attempt % (CONCURRENCY * 20) == 0:
            elapsed = time.monotonic() - start
            log(f"  已尝试 {attempt} 次 ({elapsed:.1f}s)")

        await asyncio.sleep(ROUND_INTERVAL_MS / 1000)

    elapsed = time.monotonic() - start
    log(f"抢购结束: {attempt} 次请求, {elapsed:.1f}s, {'✅成功' if success else '❌未抢到'}")
    return success


def find_nearest_time(offset_ms=0):
    """找最近的抢购场次"""
    now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
    for t in SECKILL_TIMES:
        candidate = datetime.strptime(f"{now.date()} {t}", "%Y-%m-%d %H:%M")
        if candidate > now + timedelta(seconds=5):
            return candidate
    tomorrow = now.date() + timedelta(days=1)
    return datetime.strptime(f"{tomorrow} {SECKILL_TIMES[0]}", "%Y-%m-%d %H:%M")


async def run(target_time=None, test_mode=False, loop_mode=False):
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )

    if STATE_PATH.exists():
        ctx = await browser.new_context(
            storage_state=str(STATE_PATH),
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
    else:
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )

    await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    await ctx.add_init_script(CSRF_HOOK_JS)
    page = await ctx.new_page()
    page.set_default_timeout(30000)

    try:
        # 加载页面
        log("加载活动页...")
        await page.goto(ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # 检查登录
        if "login" in page.url.lower():
            log("请在浏览器中登录...")
            try:
                await page.wait_for_url("**/console/**", timeout=300000)
                await page.goto(ACTIVITY_URL, wait_until="domcontentloaded")
                await asyncio.sleep(3)
            except PwTimeout:
                pass

        # 保存登录状态
        await ctx.storage_state(path=str(STATE_PATH))
        log("✅ 登录状态已保存")

        # 同步时间
        offset_ms = await sync_time(page)

        goods = TEST_GOODS if test_mode else FLASH_SALE
        log(f"商品: {goods['name']}")

        # 等待前端 CSRF token 就绪
        for _ in range(50):
            if await page.evaluate("() => !!window.__latestCsrf"):
                break
            await asyncio.sleep(0.2)
        else:
            log("⚠️ 未捕获到 CSRF token，请求可能失败")

        if test_mode:
            log("测试模式: 直接调用 do-goods")
            success = await rapid_purchase(page, goods, duration=5)
            if success:
                log("🎉 测试成功！API格式正确")
            else:
                log("测试完成。查看返回的code判断是否格式正确。")
            return

        # 等待抢购时间
        if target_time:
            now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
            target_dt = datetime.strptime(f"{now.date()} {target_time}", "%Y-%m-%d %H:%M")
        else:
            target_dt = find_nearest_time(offset_ms)

        now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
        wait_sec = (target_dt - now).total_seconds()
        log(f"⏰ 目标: {target_dt.strftime('%Y-%m-%d %H:%M:%S')} (服务器时间)")
        log(f"⏳ 等待 {wait_sec:.0f}s ({wait_sec/60:.1f}min)")
        log(f"⚡ 提前量: {abs(ADVANCE_MS)}ms (会在目标时间前{abs(ADVANCE_MS)/1000:.1f}秒开始)")

        warmed_up = False
        resynced = False
        while True:
            now_ms = int(time.time() * 1000) + offset_ms
            remaining = (target_dt.timestamp() * 1000 - now_ms) / 1000

            # 剩余 3 分钟时重新对时一次（Date 头实时对时，安全，修正长时间等待的微小漂移）
            if remaining <= 180 and not resynced:
                log("🔄 剩余3分钟，重新对时...")
                offset_ms = await sync_time(page)
                resynced = True
                continue  # 用新偏移重算 remaining

            # ADVANCE_MS 提前量: 负数=提前开始（remaining 为正=还没到点，剩余秒数）
            # 提前3秒 = remaining <= 3.0s 时触发（即 -ADVANCE_MS = 3000ms）
            if remaining * 1000 <= -ADVANCE_MS:
                log(f"🚀 提前 {abs(ADVANCE_MS)}ms 开始！")
                break

            if remaining <= 10:
                log(f"⚡ {remaining:.1f}s")
            elif int(remaining) % 60 == 0:
                log(f"  还剩 {remaining/60:.0f}min")

            # 预热：提前10秒发一个请求建立连接
            if remaining <= WARMUP_SECONDS and not warmed_up:
                await warmup(page, goods)
                warmed_up = True

            await asyncio.sleep(0.05 if remaining <= 5 else 1)

        # 执行抢购
        success = await rapid_purchase(page, goods)

        if success:
            log("🎉 抢购成功！请在浏览器中完成支付。")
            # 等用户支付
            await asyncio.sleep(600)
        else:
            log("未抢到。")

    except KeyboardInterrupt:
        log("用户中断")
    except Exception as e:
        log(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            await ctx.storage_state(path=str(STATE_PATH))
        except:
            pass
        await browser.close()
        await pw.stop()


def main():
    parser = argparse.ArgumentParser(description="腾讯云服务器抢购终极方案")
    parser.add_argument("--time", type=str, help="HH:MM")
    parser.add_argument("--test", action="store_true", help="测试模式")
    parser.add_argument("--loop", action="store_true", help="持续运行")
    args = parser.parse_args()

    asyncio.run(run(target_time=args.time, test_mode=args.test, loop_mode=args.loop))


if __name__ == "__main__":
    main()
