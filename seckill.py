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
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout
except ImportError:
    print("缺少 playwright，先执行： pip install playwright")
    sys.exit(1)

# ============================================================
# 配置
# ============================================================

# 活动页。腾讯换活动只换 slug（比如 .../double11-202611），改这一行就够；
# 活动 id 和商品 id 都会从接口现查，不再写死在代码里。
ACTIVITY_URL = "https://cloud.tencent.com/act/pro/double11-202611"

# API 地址
API_BASE = "https://act-api.cloud.tencent.com"
DO_GOODS_URL = API_BASE + "/dianshi/do-goods"

# 兜底活动 id（自动发现失败时才用）。新的排前面，老的留作回落。
#   166111704417621 = 2026年双十一主会场，2026-10-13 ~ 2026-11-30
#   164461404341040 = 2026年7月日常大促，2026-06-30 ~ 2026-10-30（到期后自动跳过）
CANDIDATE_ACTIVITY_IDS = [166111704417621, 164461404341040]

# 轻量 4核4G3M 的通用购买参数（regionId 由用户选的地域覆盖）
GOODS_PARAM = {
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
}

# 抢购商品：轻量4核4G3M 38元/年（原价780元）
FLASH_SALE = {
    "act_id": 1996855673581677,
    "activity_id": 166111704417621,
    "type": "lighthouse_v5",
    "name": "轻量4核4G3M (38元/年)",
    "goods_param": dict(GOODS_PARAM),
}

# 老活动里的同一款商品（38元/年），2026-10-30 23:59:59 到期，仅作回落
FLASH_SALE_LEGACY = {
    "act_id": 1897632168296710,
    "activity_id": 164461404341040,
    "type": "lighthouse_v5",
    "name": "轻量4核4G3M (38元/年·老活动)",
    "goods_param": dict(GOODS_PARAM),
}

# 测试下单用商品：轻量4核4G3M 109元/年（服务器专区），不受秒杀时间窗限制，随时能验
# 注意：它和 38 元那款是同一个「首单」SKU。买过任意一个，另一个也会返回
# 1100106「已购买此产品」—— 那是账号状态，不是程序出错。
TEST_GOODS = {
    "act_id": 1978929380263448,
    "activity_id": 166111704417621,
    "type": "lighthouse_v5",
    "name": "轻量4核4G3M (109元/年·专区)",
    "goods_param": dict(GOODS_PARAM),
}

# 价格校验：38 元那款活动价 3800 分、日常价 78000 分、折扣率 4.8718。
# 这个值不受账号影响 —— 实测账号已经用过首单资格，query-price 依然返回 3800，
# 所以它能当「是不是要抢的那一款」的判别条件。
EXPECT_REAL_COST = 3800
# [5] 测试下单用的 109 元专区商品
TEST_REAL_COST = 10900


def fmt_price(price):
    """把 query-price 的返回转成人话，price 为空表示没查到"""
    if not price:
        return "价格未知"
    real = price.get("real_cost")
    total = price.get("total_cost")
    if real is None:
        return "价格未知"
    txt = f"活动价 {real / 100:.2f} 元"
    if total:
        txt += f" / 日常价 {total / 100:.2f} 元"
    return txt


SECKILL_TIMES = ["10:00", "15:00"]

# 并发请求数
CONCURRENCY = 10

# 持续发送时间 (秒)
DURATION_SEC = 10

# 每轮间隔 (毫秒)
ROUND_INTERVAL_MS = 20

# 提前多少毫秒开火（负数=提前，相对本机推算出的开抢时刻）
#
# 恢复成早期版本的 3 秒：对时永远有误差（采样取中位数，实测 ±0.1s，抖动更大），
# 早发只是换来几条「1101001 秒杀尚未开始」，循环会继续打，不损失任何机会；
# 而晚发是直接错过窗口，补不回来。所以宁可早，代价只是多几百条废请求。
#
# 唯一的副作用是请求量：3 秒 × 500req/s ≈ 1500 条，可能招活动风控
# （接口里 anti_brush_version=v3）。如果日志开始出现限流类返回码，
# 把这里调成 -500 即可，别调成 0。
ADVANCE_MS = -3000

# ---- do-goods 业务返回码 ----
CODE_OK = 0
CODE_NOT_STARTED = 1101001     # 秒杀尚未开始（没到点 / 本场已结束）
CODE_ALREADY_BOUGHT = 1100106  # 已购买此产品，不满足首单条件
CODE_NO_QUALIFY = 1100107      # 不满足活动参与条件
CODE_BAD_PARAM = 1100109       # 商品可选参数不对
CODE_RATE_LIMIT = 408          # 访问频率过快（开抢瞬间大家都快）
CODE_BUSY = 1100205            # 系统繁忙（开抢瞬间服务端排队）
CODE_CONCURRENT = 1100110      # 正在执行其他操作（同账号有单在提交）

# 这几个重试没有意义，命中就停，别浪费窗口
NO_RETRY_CODES = {CODE_ALREADY_BOUGHT, CODE_NO_QUALIFY, CODE_BAD_PARAM}

# 开抢瞬间常见的排队信号：并发请求里有的排队、有的成功，继续就行
QUEUE_CODES = {CODE_NOT_STARTED, CODE_RATE_LIMIT, CODE_BUSY, CODE_CONCURRENT}

CODE_DESC = {
    CODE_NOT_STARTED: "秒杀尚未开始（未到时间或本场已结束）",
    CODE_ALREADY_BOUGHT: "该实名主体已买过这款首单商品，不满足首单条件",
    CODE_NO_QUALIFY: "账号不满足本活动参与条件",
    CODE_BAD_PARAM: "商品参数不对（商品的规格约束变了）",
    CODE_RATE_LIMIT: "访问频率限制（开抢瞬间大家都在抢），继续",
    CODE_BUSY: "系统繁忙（开抢瞬间排队），继续",
    CODE_CONCURRENT: "同账号有订单正在处理，继续",
}

# 提前多少秒开始预热连接 (在目标时间前)
WARMUP_SECONDS = 10

STATE_PATH = Path(__file__).parent / ".state.json"
LOG_PATH = Path(__file__).parent / "seckill.log"

# 启动顺序：先用本机的，都没装就回落到 playwright 下载的 chromium
BROWSER_CHANNELS = ["chrome", "msedge", None]

BROWSER_LABEL = {
    "chrome": "本机 Chrome",
    "msedge": "本机 Edge",
    None: "playwright chromium",
}

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]


async def launch_browser(pw, headless=True, extra_args=None):
    """按 BROWSER_CHANNELS 顺序试，谁能起就用谁。"""
    args = LAUNCH_ARGS + list(extra_args or [])
    last = None

    for channel in BROWSER_CHANNELS:
        kwargs = {"headless": headless, "args": args}
        if channel:
            kwargs["channel"] = channel
        try:
            browser = await pw.chromium.launch(**kwargs)
            log(f"使用浏览器: {BROWSER_LABEL[channel]}")
            return browser
        except Exception as e:
            last = e
            if channel is None:
                log(f"启动失败: {e}")
                log("没装本机 Chrome/Edge 的话，执行 python -m playwright install chromium")

    raise RuntimeError(f"没有可用的浏览器: {last}")


# 前端的 x-csrf-token 是动态生成的，得 hook 住 fetch/XHR 才能拿到
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
    try:
        # 中文 Windows 默认 cp936，编不了 emoji，不指定 encoding 会 UnicodeEncodeError
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except UnicodeEncodeError:
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
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
        log(f"服务器时间偏移: {offset:+d}ms (Date头对时 ×{len(offsets)}中位数 {offsets})")
        return offset

    # 回退：页面 nowTime（静态值，含页面加载延迟，精度较差）
    try:
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
    except Exception:
        offset = 0
    log(f"服务器时间偏移: {offset:+d}ms (nowTime回退)")
    return offset


async def api_post(page, path, body):
    """在页面上下文里 POST act-api：cookie / CSRF / 同源都交给浏览器"""
    return await page.evaluate("""
        async (p) => {
            try {
                const r = await fetch(p.url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json',
                              'x-csrf-token': window.__latestCsrf || ''},
                    credentials: 'include',
                    body: JSON.stringify(p.body),
                });
                return await r.json();
            } catch (e) {
                return {code: -1, msg: String((e && e.message) || e)};
            }
        }
    """, {"url": API_BASE + path, "body": body})


async def discover_activity_id(page):
    """从活动页自身内嵌的数据里读当前活动 id（腾讯换活动时这里会跟着变）"""
    try:
        aid = await page.evaluate("""
            () => {
                const html = document.documentElement.innerHTML;
                let m = html.match(/"big_activity_id"\\s*:\\s*"?(\\d{12,20})"?/);
                if (m) return Number(m[1]);
                m = html.match(/"activity_id"\\s*:\\s*(\\d{12,20})/);
                return m ? Number(m[1]) : 0;
            }
        """)
        return int(aid) if aid else 0
    except Exception as e:
        log(f"读取活动 id 失败: {str(e)[:60]}")
        return 0


async def fetch_price(page, activity_id, act_id, type_, region_id):
    """问一次价格。免登录可用，用来确认这个 act_id 是不是 38 元那款。

    不需要 activity_id 之外的参数，goods 里也不能带 activity_id（会 400）。
    返回 None 表示查不到（接口报错或结构变了），调用方要能容忍。
    """
    r = await api_post(page, "/dianshi/query-price", {
        "activity_id": int(activity_id),
        "goods": [{
            "act_id": int(act_id),
            "type": type_,
            "goods_param": {**GOODS_PARAM, "regionId": region_id},
        }],
        "preview": 0,
    })
    if r.get("code") != CODE_OK:
        return None
    for item in (r.get("data") or []):
        if int(item.get("act_id") or 0) == int(act_id):
            return item
    return None


def _is_lighthouse(sale):
    """场次里的 sales 是不是轻量应用服务器那条"""
    if (sale.get("product_name") or "") == "lighthouse_v5":
        return True
    return "轻量" in (sale.get("goods_name") or "")


# 选中依据的优先级：type 与价格两个条件互相印证。
# type 指 sales[].product_name，轻量应用服务器固定是 "lighthouse_v5"
# （注意别跟 goods_param.type=bundle_budget_mc_lg4_01 混了，那是规格包编号）。
RANK_DESC = {
    0: "type=lighthouse_v5 + 价格对上 38 元",
    1: "type=lighthouse_v5（价格没对上或没查到）",
    2: "价格对上 38 元 + 名字含轻量",
    3: "只有价格对上 38 元，type/名字都没认出来",
    4: "只有名字含轻量",
    9: "type、名字、价格都没对上",
}


def _rank_candidate(sale, price):
    """给场次里的商品打分，越小越像要抢的那款。

    type 来自接口字段，比商品名稳定；价格是独立的一道校验。
    两个都对上最好；只有一个对上时宁可要 type（腾讯自己声明的商品类型）。
    """
    by_type = (sale.get("product_name") or "") == "lighthouse_v5"
    by_name = "轻量" in (sale.get("goods_name") or "")
    by_price = bool(price) and price.get("real_cost") == EXPECT_REAL_COST
    if by_type and by_price:
        return 0
    if by_type:
        return 1
    if by_price and by_name:
        return 2
    if by_price:
        return 3
    if by_name:
        return 4
    return 9


async def discover_flash_sale(page, region_id=8):
    """开抢前现查：当前在售场次 + 对应的 act_id / activity_id

    腾讯每次换活动都会换 act_id，写死在代码里必然过期（换来的是 1101001）。
    这个接口不需要签名，登录后就能调。拿不到才回落到代码里的兜底值。
    返回 (goods, session)，session 为 None 表示用的是兜底值。
    """
    candidates = []
    aid = await discover_activity_id(page)
    if aid:
        candidates.append(aid)
    for a in CANDIDATE_ACTIVITY_IDS:
        if a not in candidates:
            candidates.append(a)

    now = time.time()
    for act in candidates:
        r = await api_post(page, "/dianshi/query-seckill-by-date", {
            "activity_id": act, "preview": 0, "seckill_type": "block",
            "goods_type": "goods", "days": 7,
        })
        if r.get("code") != CODE_OK:
            log(f"  活动 {act}: 查不到场次 (code={r.get('code')})")
            continue

        upcoming, past = [], []
        for date_str, day in ((r.get("data") or {}).get("flashResult") or {}).items():
            for s in (day or []):
                try:
                    start = datetime.strptime(
                        f"{date_str} {s.get('start_time')}", "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    continue
                (upcoming if start.timestamp() > now else past).append((date_str, s, start))
        upcoming.sort(key=lambda x: x[2])
        past.sort(key=lambda x: x[2])

        for date_str, s, start in (upcoming[:1] or past[-1:]):
            sales = s.get("sales") or []
            # 整场商品都问一遍价格。非轻量的商品会报 1100109（goods_param 不属于它们），
            # 拿不到价格就排到最后，不影响判断。
            cands = []
            for sale in sales:
                if sale.get("act_id") is None:
                    continue
                typ = sale.get("product_name") or ""
                goods = {
                    "act_id": int(sale["act_id"]),
                    "activity_id": int(act),
                    "type": typ or "lighthouse_v5",
                    "name": "轻量4核4G3M (38元/年)",
                    "goods_param": {**GOODS_PARAM, "regionId": region_id},
                }
                price = await fetch_price(page, act, goods["act_id"], goods["type"], region_id)
                cands.append((_rank_candidate(sale, price), goods, price, sale))

            if not cands:
                continue  # 这场没拿到任何商品，看下一场
            cands.sort(key=lambda c: c[0])
            rank, goods, price, _sale = cands[0]
            why = RANK_DESC.get(rank, "都没对上")
            if price and price.get("real_cost") != EXPECT_REAL_COST:
                why += f"，价格是 {fmt_price(price)}"

            goods["price"] = price
            goods["price_check"] = why
            session = {
                "id": s.get("id"),
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": f"{date_str} {s.get('end_time')}",
                "started": start.timestamp() <= now,
            }
            return goods, session

    log("自动发现失败，使用代码里的兜底 id（可能已过期，会返回 1101001）")
    log("  排查：1) 活动页是否换了地址 → 改 ACTIVITY_URL")
    log("        2) 是否已换新活动 → 把新 activity_id 加进 CANDIDATE_ACTIVITY_IDS")
    fallback = {**FLASH_SALE, "goods_param": {**FLASH_SALE["goods_param"], "regionId": region_id}}
    price = await fetch_price(page, fallback["activity_id"], fallback["act_id"],
                              fallback["type"], region_id)
    fallback["price"] = price
    if price and price.get("real_cost") != EXPECT_REAL_COST:
        fallback["price_check"] = "兜底 id 价格对不上"
        log(f"  注意：兜底商品是 {fmt_price(price)}，不是 38 元")
    else:
        fallback["price_check"] = "兜底 id" if price else "兜底 id，价格未校验"
    return fallback, None


async def do_goods_raw(page, goods, preview=0, concurrency=1):
    """调 do-goods。preview=1 是预检：服务端完整校验，但不产生订单。
    返回 list（并发时有多条）。"""
    result = await page.evaluate("""
        async (params) => {
            const one = () => fetch(params.url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json',
                          'x-csrf-token': window.__latestCsrf || ''},
                credentials: 'include',
                body: JSON.stringify({
                    activity_id: params.activity_id,
                    goods: [{act_id: params.act_id, type: params.type,
                             goods_param: params.goods_param}],
                    agent_channel: {fromChannel: '', fromSales: '', fromUrl: '',
                                    isAgentClient: false},
                    preview: params.preview,
                }),
            }).then(r => r.json()).catch(e => ({code: -1, msg: String((e && e.message) || e)}));

            const jobs = [];
            for (let i = 0; i < params.concurrency; i++) jobs.push(one());
            return await Promise.all(jobs);
        }
    """, {
        "url": DO_GOODS_URL,
        "activity_id": goods["activity_id"],
        "act_id": goods["act_id"],
        "type": goods["type"],
        "goods_param": goods.get("goods_param", {}),
        "preview": int(preview),
        "concurrency": int(concurrency),
    })
    if isinstance(result, dict):
        return [result]
    return result or []


async def probe_goods(page, goods):
    """preview=1 预检：真实走一遍服务端校验，但不产生订单。返回原始返回体。"""
    results = await do_goods_raw(page, goods, preview=1, concurrency=1)
    return results[0] if results else {"code": -1, "msg": "无返回"}


async def warmup(page, goods):
    """预热连接：提前发一个「预检请求」把 TCP/TLS 建好，到点直接用。
    用 preview=1，不会真的下单。返回服务端返回体，供上层判断是否需要中止。"""
    log("预热连接...")
    result = await probe_goods(page, goods)
    code = result.get("code")
    if code == CODE_OK:
        log("  预热返回成功（说明此刻已在窗口内且资格 OK）")
    elif code in NO_RETRY_CODES:
        log(f"  预热返回 {code}: {CODE_DESC.get(code, '')}")
        log(f"  服务器原话: {str(result.get('msg') or '')[:70]}")
    elif code == CODE_NOT_STARTED:
        log("  预热完成（还没到场次时间，正常）")
    else:
        log(f"  预热完成（code={code}，{CODE_DESC.get(code, '排队信号，不影响')}）")
    return result


async def rapid_purchase(page, goods, duration=DURATION_SEC):
    """并发调 API，每次 evaluate 发一批请求"""
    log(f"开始抢购：并发={CONCURRENCY}，间隔={ROUND_INTERVAL_MS}ms，持续={duration}s")

    start = time.monotonic()
    attempt = 0
    success = False
    err_count = 0
    last_code = None
    seen_codes = {}

    while time.monotonic() - start < duration:
        # 批量发起：一次 evaluate 发 CONCURRENCY 个并发 fetch
        # evaluate 可能撞页面导航/关闭抛错，单轮失败不中断整个抢购窗口
        try:
            # 真抢：preview=0，会真的下单
            batch_result = await do_goods_raw(page, goods, preview=0, concurrency=CONCURRENCY)
        except Exception as e:
            err_count += 1
            if err_count <= 3:
                log(f"  本轮请求异常: {str(e)[:80]}")
            await asyncio.sleep(ROUND_INTERVAL_MS / 1000)
            continue

        attempt += CONCURRENCY

        # 检查结果
        fatal = False
        for r in batch_result:
            code = r.get("code")
            if code is not None:
                last_code = code
            if code == CODE_OK:
                deal = r.get("data", {})
                log("下单成功")
                log(f"   订单号: {(deal.get('deal_names') or ['?'])[0]}")
                log(f"   大单号: {deal.get('big_deal_no') or '?'}")
                success = True
                break
            if code not in seen_codes:
                # 同一种返回码只报一次，避免刷屏；但一定带上服务器的原话
                seen_codes[code] = 1
                log(f"  返回 {code}: {CODE_DESC.get(code, '未知返回码，程序会继续，不影响')}"
                    f" | {str(r.get('msg') or '')[:60]}")
            if code in NO_RETRY_CODES:
                log("  这类错误重试没有意义，本场停止。")
                fatal = True
                break

        if success or fatal:
            break

        if attempt % (CONCURRENCY * 20) == 0:
            elapsed = time.monotonic() - start
            log(f"  已尝试 {attempt} 次 ({elapsed:.1f}s)")

        await asyncio.sleep(ROUND_INTERVAL_MS / 1000)

    elapsed = time.monotonic() - start
    log(f"抢购结束: {attempt} 次请求, {elapsed:.1f}s, {'成功' if success else '未抢到'}")
    if not success and last_code is not None:
        hint = CODE_DESC.get(last_code)
        if hint:
            log(f"  最后返回码 {last_code}: {hint}")
        elif last_code == -1:
            log("  最后返回码 -1: 请求本身没发出去（网络/页面上下文问题）")
        elif last_code in QUEUE_CODES:
            log(f"  最后返回码 {last_code}: {CODE_DESC.get(last_code, '排队信号')}"
                f"。这类码刷屏不影响抢购，没抢到就是名额没了或时间窗不对")
        else:
            log(f"  最后返回码 {last_code}: 未知返回码，把这段日志发个 issue 就行")
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


async def run(target_time=None, test_mode=False):
    pw = await async_playwright().start()
    browser = await launch_browser(pw, headless=False)

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
        log("登录状态已保存")

        # 同步时间
        offset_ms = await sync_time(page)

        # 商品：--test 用专区商品，正常模式用自动发现的秒杀商品
        goods = TEST_GOODS if test_mode else FLASH_SALE
        log(f"商品(初始): {goods['name']}")

        # 等待前端 CSRF token 就绪（页面导航期间 evaluate 会报上下文销毁，吞掉重试）
        for _ in range(50):
            try:
                if await page.evaluate("() => !!window.__latestCsrf"):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.2)
        else:
            log("未捕获到 CSRF token，请求可能失败")

        # 现查当前在售场次与商品 id（腾讯换了活动也能自动跟上，不用改代码）
        region_id = goods["goods_param"].get("regionId", 8)
        if not test_mode:
            found, session = await discover_flash_sale(page, region_id)
            if session:
                log(f"活动 {found['activity_id']} | 场次 {session['start']} ~ "
                    f"{session['end']} (id={session['id']})")
            goods = found
            log(f"商品: {goods['name']} act_id={goods['act_id']} "
                f"activity_id={goods['activity_id']}")
            log(f"  价格: {fmt_price(goods.get('price'))}"
                f"（{goods.get('price_check', '未校验')}）")
        else:
            log(f"测试商品: {goods['name']} act_id={goods['act_id']}")

        if test_mode:
            log("测试下单流程：用 109 元专区商品真下单（preview=0），不付款")
            log("  与 38 元那款不是同一商品，仅同配置；可验证登录/参数/首单资格/建单")
            log("  测完请到控制台取消订单；不要频繁测，反复建单容易触发风控")
            price = await fetch_price(page, goods["activity_id"], goods["act_id"],
                                      goods["type"], goods["goods_param"].get("regionId", 8))
            log(f"  价格: {fmt_price(price)}")
            if price and price.get("real_cost") != TEST_REAL_COST:
                log(f"  注意：这个不是 109 元的测试商品（实际 {fmt_price(price)}），"
                    f"act_id 可能已经变了")
            ok = await rapid_purchase(page, goods, duration=5)
            if ok:
                log("已建单 —— 请去控制台取消订单，不要付款。")
            else:
                log("未成交，看上面的返回码说明。")
            return

        # 等待抢购时间
        if target_time:
            now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
            target_dt = datetime.strptime(f"{now.date()} {target_time}", "%Y-%m-%d %H:%M")
        else:
            target_dt = find_nearest_time(offset_ms)

        now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
        wait_sec = (target_dt - now).total_seconds()
        log(f"目标: {target_dt.strftime('%Y-%m-%d %H:%M:%S')} (服务器时间)")
        log(f"等待 {wait_sec:.0f}s ({wait_sec/60:.1f}min)")
        log(f"提前量 {abs(ADVANCE_MS)}ms，会在目标时间前 {abs(ADVANCE_MS)/1000:.1f} 秒开始")

        warmed_up = False
        resynced = False
        while True:
            now_ms = int(time.time() * 1000) + offset_ms
            remaining = (target_dt.timestamp() * 1000 - now_ms) / 1000

            # 剩余 3 分钟时重新对时一次（Date 头实时对时，安全，修正长时间等待的微小漂移）
            if remaining <= 180 and not resynced:
                log("剩余3分钟，重新对时...")
                offset_ms = await sync_time(page)
                resynced = True
                continue  # 用新偏移重算 remaining

            # remaining 是正数表示还没到点，所以是 <= 300ms 时开始（即提前 300ms）
            if remaining * 1000 <= -ADVANCE_MS:
                log(f"提前 {abs(ADVANCE_MS)}ms 开火")
                break

            if remaining <= 10:
                log(f"{remaining:.1f}s")
            elif int(remaining) % 60 == 0:
                log(f"  还剩 {remaining/60:.0f}min")

            # 提前10秒发一个请求，把连接建好；顺便确认资格，没资格就别白等
            if remaining <= WARMUP_SECONDS and not warmed_up:
                warmed_up = True
                probe = await warmup(page, goods)
                if probe.get("code") in NO_RETRY_CODES:
                    log("开抢前已知资格不满足，中止本场")
                    return

            await asyncio.sleep(0.05 if remaining <= 5 else 1)

        # 执行抢购
        success = await rapid_purchase(page, goods)

        if success:
            log("抢购成功，请在浏览器中完成支付")
            await asyncio.sleep(600)
        else:
            log("未抢到。")

    except KeyboardInterrupt:
        log("用户中断")
    except Exception as e:
        log(f"错误: {e}")
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
    parser.add_argument("--test", action="store_true",
                        help="测试下单流程：用 109 元专区商品真下单（不付款，测完请及时取消）")
    args = parser.parse_args()

    asyncio.run(run(target_time=args.time, test_mode=args.test))


if __name__ == "__main__":
    main()
