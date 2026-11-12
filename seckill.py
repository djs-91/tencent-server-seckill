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
import json
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
# 只读查库存/资格。实测 2026-11-10 22:11（登录态）确认可用，
# 返回 data[0].quota 以 region_id 为键，各地域独立计数。
CHECK_AVAILABLE_PATH = "/dianshi/check-available"

# 地域编号 → 名字。seckill_ui 里的 REGIONS 复用这一份，别再各写各的。
REGION_NAMES = {4: "上海", 8: "北京", 1: "广州"}

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
# 1100106「相同实名主体已产生付费，不满足新用户条件」—— 账号状态，不是程序出错。
# 2026-11-11 10:00 场 preview=1 实测原话：
#   「抱歉，相同实名认证主体已产生付费，不满足新用户条件，欢迎选择其他商品。」
# 注意它比「买过这款首单商品」更宽：是**这个实名主体有过任何付费记录**，
# 就不算新用户了，不限于本商品。而且开窗前 -4.9s / -3.0s 还是 1101001，
# 到 -1.4s 才转成 1100106 —— 说明服务端临近开窗才做这层判定，
# check-available 的 user_available=1 不能代表新用户资格。
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

# ---- 在途数（并发）：不用你设，但也不能瞎爬 ----
#
# 物理关系只有一条：吞吐 ≈ 在途数 ÷ 单条往返。
# 所以「该开多少」没有通用答案，它取决于**服务端在那一刻有没有余量**。
#
# 真实日志里的单条耗时（往返都是 94~151ms 的正常网络，不是网络慢）：
#   11-04 10:00   30 在途 → 32 条/秒 → 单条 0.94 秒（网络往返 94ms，排队 10 倍）
#   11-09 15:00   12 在途 →  6~10 条/秒 → 单条 2.5 秒（网络往返 151ms，排队 16 倍）
#   11-02 15:00   30 在途 → 17 条/秒 → 单条 1.76 秒
# 读法：**服务端在开窗那几秒是被所有人挤满的，谁都得排队。**
# 抢到与否的关键不是「排得多快」，而是「你的请求排在队伍的前面还是后面」。
#
# ⚠️ 一个必须避开的错误推理：拿「12 在途 13 条/秒」和「30 在途 32 条/秒」
# 相除，得出「加在途让吞吐涨了 2.46 倍」——这两个数是**两个人**跑的，
# 前者实测往返 700ms（网络路由绕行，跟并发无关），后者直连 94ms。那差的是网络，不是并发。
# （别把 700ms 归因成"挂代理"，当事人明确说过那场没挂。）
# 仓库里唯一「同一个人连续两场」的对照是 11-09 那天，只有 12 在途一种。
# 所以本文件不靠跨人数据定值，只靠「跑通过的量」当起点，剩下交给运行期实测。
#
# 判据不能是「往返涨没涨」——往返涨了但吞吐也涨，说明服务端在并行处理，
# 那时候该继续加。真饱和的样子是「往返涨了、吞吐不涨」。所以判据取两者的比值：
#       吞吐比 = (在途新 ÷ 往返新) ÷ (在途旧 ÷ 往返旧)
#   ≥ 1.1 → 还有余量，继续加
#   < 1.1 → 饱和，退回场次起点，本场不再加
# 换成一句话：**加完在途，吞吐得跟着涨，不然就退回来。**
ADAPT_UP = 1.5             # 试探步长：在途 ×1.5
ADAPT_GAIN_MIN = 1.1       # 吞吐至少要涨 10% 才算「加得值」
ADAPT_MIN_MS = 250         # 评估周期下限；实际取 max(这个值, 当前往返 × 1.5 —— 详见循环里)
RTT_SAMPLES = 20           # 取最近多少条往返算中位数（抗单条抖动）
CAP_FLOOR = 8              # 最低在途：再少就没法在窗口内占住服务端
CAP_CEIL = 64              # 硬上限：实测 h2 多路复用在 60 条时开始出现 1209ms 拖尾
                           # （30 条跨度只有 123ms），再多只是自己排队

# ---- 按场次分档 ----
#
# 核心事实：**名额只在开窗后几秒内就被抢光**，不是持续放量。
# 实测售罄/成功时刻（全是别人贴的真实日志）：
#   11-02 15:00  开窗 +1.99 秒售罄
#   11-04 10:00  开窗 +3.69 秒抢到（+4.5 秒收工）
#   11-10 15:00  开窗 +7.67 秒售罄
# 所以真正的机会窗口是 **0~5 秒**，把 duration 拉到 20 秒纯属浪费——
# 后面十几秒全在打 1100136，一点用都没有。
#
# 那该做什么？在窗口内把请求量顶到最大。全部已知场次的对照：
#   场次          并发  总请求  408率   有效请求*  结果
#   10-29 10:00    10     20     5%      19      ✅
#   11-04 10:00    30    143    76%      34      ✅
#   11-09 15:00    12     59    76%      14      ❌
#   11-09 15:00    12     69    81%      13      ❌
#   11-?? 10:00    12    166    78%      36      ❌（往返 700ms，网络路由绕行，不是并发/参数问题）
#   11-10 15:00    20     67    66%      16      ❌
#   *有效请求 = 总请求 − 408 − 发不出去的
#
# 关键读法：11-04 被 408 挡掉 109 条（比 11-10 下午的总请求还多），照样抢到。
# **因为它总量大。** 408 是网关按比例随机丢弃，不是封禁，被丢立刻补就行。
# 所以「宁可被挡也要多发」远好于「为了不被挡而少发」——
# 降并发从来只减总量，408 率一点没降（12 并发反而 76~81%，比 30 并发还高）。
#
# cap_start / cap_max 的取值依据（这两个数**不是拍的**）：
#
# 先说一条必须说清的事：**下面每个数字都来自不同的人、不同的网络**，
# 唯一一个有「同一个人连续两场」对照的是 11-09 那天，而那次只有 12 在途一种。
# 所以跨人相除（「12 在途 13 条/秒 vs 30 在途 32 条/秒 = 有余量」）是无效推理，
# 那两个数一个是往返 700ms 的、一个是正常 94ms 的，比的是网络不是并发。
# （700ms 那场当事人说自己没挂代理，所以别再把它写成"挂代理"，真实原因大概率是线路绕行。）
# 这里只保留一条有把握的推论：**起点用跑通过的量**。
#
#   · 10:00  起点 35，让开窗那一刻真的发出 30 条（11-04 抢到那次就是 30 在途、
#            143 条、4.5 秒成功；另 5 条额度要留给开窗前的探针，见 PROBE_CAP 注释）。
#            上限留 64 让自适应去试，试不动会自己退回 35（判据见 calc_cap）。
#            这不是说「64 更好」——没有证据证明 10:00 场在 30 之上还有余量，
#            11-04 那次 30 在途时单条已经 0.94 秒（网络往返只有 94ms），
#            说明当时服务端已经在排队。留着上限只是为了「万一某天没那么挤」。
#   · 15:00  起点 = 上限 = 55，固定不爬。**这个数是按窗口算的，不是拍的**：
#
#            15:00 场的窗口只有 2~4 秒（11-02 开窗 1.99 秒售罄，11-10 是 4 秒出现售罄码），
#            而单条耗时是 1.76~2.5 秒 —— **窗口比一个往返还短**。
#            「回来一个补一个」在下午场基本不工作，把三次真实日志摆一起就看清了：
#              11-04 上午 30 在途、单条 0.94s、窗口 4.5s → 补发 114 条，共 144 条
#              11-02 下午 30 在途、单条 1.76s、窗口 2.0s → 补发   4 条，共  34 条
#              11-09 下午 12 在途、单条 2.5s、窗口 2.0s → 补发   0 条，共  12 条
#            差 4~12 倍，全差在「能不能补发」。
#            换句话说，**下午场的请求数约等于开窗那一批 = 首发条数**。
#
#            这决定了两个场次该往相反方向调：
#              上午场加在途，边际收益低（30 已经靠补发堆到 144 条）
#              下午场加在途，是**纯线性收益**（首发 30 → 50，窗口内就是多 20 条）
#            所以下午场的在途数直接等于「押注量」，给到 55（首发 50，另 5 条被探针占着）：
#              · 首发 50 条在实测拖尾拐点 60 之下（60 条跨度 1209ms，30 条只有 123ms）
#              · 相对 11-02 的 30 多押 67%
#
#            必须说清它解决不了什么：按 76% 被 408 挡算，50 条顶多 12 条有效，
#            离 11-04 抢到那次的 34 条有效还差得远。**下午场在数学上就是难抢**，
#            因为名额 2 秒没、服务端排队、408 按比例丢，三样都不在我们手里。
#            这 20 条是「多押一点」，不是「解决问题」。
#   · 15:00 的 duration 给 12 秒：11-02 是 2 秒售罄、11-10 是 4 秒，样本里没见过更晚的，
#            留 12 秒兜住「某天特别慢」。读到售罄本来也会提前退出，多给不吃亏。
SESSION_PROFILES = {
    # 回读间隔两档都给到十几毫秒：它只影响「回来的位置多快被补上」，
    # 远小于往返（100~900ms），所以不是关键变量，别在它上面反复调。
    # 10:00 给 10 秒：见过跑到第 8 秒服务端还没返售罄的场次，8 秒会把最后那段
    # 还有货的时间掐掉。读到售罄本来就会提前退出，多给不吃亏。
    #
    # 在途数为什么是 35 / 55 而不是 30 / 50：
    # 探针（最多 PROBE_CAP=5 条）和抢购请求共用同一个 inflight 计数，所以放开全速时
    # 能打出去的是 cap − 探针在途。给 35 才能让开窗那一刻真的发出 30 条 —— 30 是唯一
    # 有成功记录的规模；给 55 才能让下午场首发真的到 50。按 cap=30/50 写，开窗瞬间
    # 其实只发出 25/45 条，等于自己少了 5 条头排位置。
    "10:00": {"poll_ms": 15, "duration": 10, "cap_start": 35, "cap_max": CAP_CEIL},
    "15:00": {"poll_ms": 12, "duration": 12, "cap_start": 55, "cap_max": 55},
}

# 隔多久看一次页面里攒下的返回结果（毫秒）。
# 回来一条才能补发一条，所以这个数越小位置周转越快。
# 注意它只影响「补发多及时」，不影响吞吐上限——吞吐 ≈ 在途 ÷ 往返。
# 实测往返 0.9 秒，15ms 和 40ms 都远小于它，所以这个值不是关键变量，别在它上面反复调。
POLL_MS = 15

# 持续发送时间 (秒)，默认档用；各场次档位里另有自己的值
DURATION_SEC = 10

# 没有场次概念时（手动抢进行中的场、测试）用这一档，对齐抢到过的那次
DEFAULT_PROFILE = {"poll_ms": POLL_MS, "duration": DURATION_SEC,
                   "cap_start": 30, "cap_max": CAP_CEIL}


def calc_pre_fire(rtt_ms):
    """按实测往返算「提前多少毫秒放开全速」。

    抽成独立函数是为了能单测——这段逻辑决定第一批量请求是正点到达、
    还是白打在开窗前，不能靠跑一次抢购来验证。

    传 None 表示还没量到往返，用保守的默认单程。
    """
    one_way = ONE_WAY_FALLBACK_MS if not rtt_ms else float(rtt_ms) / 2
    return min(float(PRE_FIRE_MAX_MS),
               max(float(PRE_FIRE_MIN_MS), one_way * PRE_FIRE_MULT))


def calc_cap(cap_now, rtt_med, prev, cap_max, cap_floor=None, cap_safe=None):
    """决定在途数下一步该加、该退、还是不动。

    判据是「吞吐有没有跟着在途一起涨」，不是「往返涨没涨」——
    往返涨了但吞吐也在涨，说明服务端在并行处理，那时候该继续加。

        prev = (加之前那次的在途数, 当时的往返)
        吞吐比 = (cap_now ÷ rtt_med) ÷ (prev_cap ÷ prev_rtt)

    抽成纯函数是为了能单测：这段逻辑决定整场能发多少请求，
    不能靠真抢一场来验证（一天只有两场，还不一定抢得到）。

    cap_safe 是「确认没问题的值」（一般传场次起点：那是唯一有成功记录的量）。
    判饱和后回到它，而不是回到 prev —— prev 可能本身就已经饱和了，
    实测就撞到过这种情况：30→45→64，判饱和后退回 45，而 45 其实也在排队。

    返回 (新的在途数, 动作)，动作是：
        "probe"      第一次，没有对照，试加一次
        "up"         吞吐跟着涨了，继续加
        "hold"       已经到本场上限，不加
        "saturated"  吞吐没跟着涨，退回安全值，本场别再试
        "keep"       样本不足，不动
    """
    floor_ = CAP_FLOOR if cap_floor is None else int(cap_floor)
    try:
        cap_now = int(cap_now)
        cap_max = int(cap_max)
    except Exception:
        return floor_, "keep"
    cap_now = max(floor_, min(cap_max, cap_now))

    if not rtt_med:
        return cap_now, "keep"

    # 第一次评估，还没有「加之前」的对照，只能试着加一次
    if prev is None:
        if cap_now >= cap_max:
            return cap_now, "hold"
        return min(cap_max, max(cap_now + 1, int(cap_now * ADAPT_UP))), "probe"

    prev_cap, prev_rtt = prev
    try:
        prev_cap = max(1.0, float(prev_cap))
        prev_rtt = max(1.0, float(prev_rtt or rtt_med))
        rtt_med = max(1.0, float(rtt_med))
    except Exception:
        return cap_now, "keep"

    if prev_cap >= cap_now:
        # 这一轮没加过（本来就顶在上限上），没有「加了之后吞吐变没变」这件事。
        # 不加这个判断，稳态时 gain 会算成 1.0 < 1.1，把自己误判成饱和退回去。
        return cap_now, "hold"

    gain = (cap_now / rtt_med) / (prev_cap / prev_rtt)
    if gain >= ADAPT_GAIN_MIN:
        # 吞吐确实涨了：服务端还有余量
        if cap_now >= cap_max:
            return cap_max, "hold"
        return min(cap_max, max(cap_now + 1, int(cap_now * ADAPT_UP))), "up"
    # 加了在途、吞吐却几乎没动 = 真饱和。
    # 退回**安全值**（场次起点），不是退回 prev：prev 可能就是导致饱和的那个数。
    # 抢购比的是谁先到，把请求挂在长队里等于主动放弃。
    if cap_safe is None:
        safe = floor_
    else:
        safe = max(floor_, min(cap_max, int(cap_safe)))
    return max(floor_, min(int(prev_cap), safe)), "saturated"


def median(xs):
    """中位数。样本量小的时候比平均值抗单条异常（比如一次 GC 卡了 1 秒）。"""
    vals = sorted(float(x) for x in xs if x is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def session_profile(target_time=None):
    """按场次取并发档位。认不出来就返回默认档，不猜。"""
    if target_time in SESSION_PROFILES:
        return dict(SESSION_PROFILES[target_time])
    return dict(DEFAULT_PROFILE)

# ---- 提前量：必须按每个用户自己的实测往返算，不能写固定值 ----
#
# 这是个纯物理量，跟谁的机器无关：
#   提前 X 毫秒开始全速 → 第一批请求 X 毫秒后出发，单程 T 毫秒后到服务端。
#   · X < T  ：第一批还没到，开窗后才到，晚了
#   · X = T  ：第一批正好开窗那一刻到，最理想
#   · X > T  ：超过 T 的那部分请求会在开窗前就到，白拿 1101001，还消耗限流额度
#
# 所以 X 只能按「自己量到的单程 T」来定，写死 400ms 对谁都不对：
#   · 单程 25ms 的千兆直连：白打 (400-25)/25 ≈ 15 轮，额度全烧光
#   · 单程 350ms 的弱网络：400ms 只提前 50ms，等于几乎没提前
#
# 取 X = 2T，好处是**白打的量恒定**，跟网速无关：
#   白打条数 = 吞吐 × (X−T) = (并发 ÷ T) × T = 并发 = 一个批次的量。
#   也就是说不管快网慢网，开窗前都只白打「一批」，不会多也不会少。
#
# 下限 100ms 是为了盖住对时误差（实测 ±20ms）和调度抖动；
# 上限 900ms 是防极端弱网把额度全烧在开窗前。
PRE_FIRE_MULT = 2.0            # 提前量 = 单程 × 这个倍数（2 倍 = 开窗前白打约一批）
PRE_FIRE_MIN_MS = 100          # 下限：盖住对时误差和调度抖动
PRE_FIRE_MAX_MS = 900          # 上限：防极端弱网烧光额度
ONE_WAY_FALLBACK_MS = 250      # 还没量到往返时的默认单程（README 里模拟用的数）

# 实测往返超过这个值就提示用户网络偏慢（毫秒）。
# 依据：库里所有正常场次的往返都在 95~151ms；唯一一次 700ms 的结果是
# 166 条请求、36 条有效（比抢到那次的 34 条还多）却依然失败 ——
# 说明这种量级的延迟，靠加请求补不回来。
SLOW_RTT_WARN_MS = 400

# 开窗前的探针最多同时在途几条、每次发几条。探针只为焐热连接、量往返、确认没到点。
#
# ⚠️ 这两个数会「吃掉」开窗瞬间的首发名额：在途上限是 cap，而探针也记在同一个
# inflight 里。所以放开全速那一刻，真正能打出去的是 cap − 探针在途（最多 5 条）。
# 想让首发正好是 N 条，cap 就得给 N + PROBE_CAP。SESSION_PROFILES 里的取值就是这么算的
# （10:00 给 35 是为了首发 30，15:00 给 55 是为了首发 50）。
PROBE_CAP = 5

# 408 的处理：**只在日志里报占比，不缩量**。
#
# 曾经有一段「408 占 85% 以上就把在飞上限打 0.55 折」的缩量逻辑，
# 现在删掉了，因为它和上面那套往返自适应直接冲突：
# 自适应按往返把在途爬到 64，缩量又按比例把它打回 35，两个机制互相拆台。
#
# 而且缩量这件事本身就被实测否定过：408 是网关按比例随机丢弃，不是封禁。
# 11-04 抢到那次 408 占 76%（挡掉 109 条），一条都没缩，照样抢到。
# 降并发从来只减总量，408 率一点没降（12 并发反而 76~81%，比 30 并发还高）。
#
# 真要判断「服务端是不是被我们压垮了」，往返变长是比 408 比例更本源的信号：
# 被丢弃的请求回得快，被压住的请求回得慢，两者在往返上分得开。
RL_HIGH_RATIO = 0.85      # 408 占比超过这个值就在日志里提一句（只提，不动作）
RL_MIN_DONE = 6           # 一轮至少回来多少条才做判断，免得被两三条样本误导

# 开窗前探针的间隔（毫秒）。探针只是为了保持连接和确认没到点，不用密。
PROBE_MS = 200

# 持续发送时间 (秒)
# （见上方 DURATION_SEC，已提前到档位定义之前）

# 提前多少毫秒开始抢（负数=提前，相对按服务器时间算出的开抢时刻）
#
# 这段时间分两段：前段每 200ms 一条探针，最后「实测单程 + 30ms」放开全速
# （提前量按探针量到的往返时间现算，不再写死 0.6 秒）。
# 对时改成 get-now 毫秒采样后实测偏差 ±20ms，2 秒足够覆盖
# 对时误差、调度抖动和网络延迟。
ADVANCE_MS = -2000

# ---- do-goods 业务返回码 ----
CODE_OK = 0
CODE_NOT_STARTED = 1101001     # 秒杀尚未开始（没到点 / 本场已结束）
CODE_ALREADY_BOUGHT = 1100106  # 相同实名主体已产生付费，不满足新用户条件（2026-11-11 实测）
CODE_NO_QUALIFY = 1100107      # 不满足活动参与条件
CODE_BAD_PARAM = 1100109       # 商品可选参数不对
CODE_RATE_LIMIT = 408          # 访问频率过快（开抢瞬间大家都快）
CODE_BUSY = 1100205            # 系统繁忙（开抢瞬间服务端排队）
# 接口层限流。比 408 更靠内一层，服务器原话和 1100205 完全一样
# （都是"当前系统繁忙，请稍后重试"）。见过它说明当时连业务层都没进去。
# 行为跟 408/1100205 一样：继续打，不用停。
CODE_RATE_LIMIT_API = 1101408
CODE_CONCURRENT = 1100110      # 正在执行其他操作（同账号有单在提交）
CODE_SOLD_OUT = 1100136        # 当前商品已售罄（本场名额没了）
# 关于 1100136 的实测记录（2026-11-10 22:11，登录态，preview=1）：
#   在**非场次时间**（场次是次日 10:00~12:00）打这个商品，返回的是
#   1101001「抱歉，秒杀尚未开始」，**没有**出现 1100136。
#   所以"未开闸也返售罄"这个说法在本次实测里不成立。
#   它的真实语义（售罄是否可能是暂时的、要不要继续打）仍需在场次内验证。
CODE_FACE_VERIFY = 1100202     # 账号需要人脸核身校验（账号级拦截，窗口内外都拦）
# 账号没有创建实例的权限。服务器原话是
# "You are not authorized to perform operation CreateInstances"，属于腾讯云 CAM 鉴权拒绝。
# 实测对照（2026-11-10）：同一个 preview=1 预检、同是非场次时间，
# 权限正常的账号返回 1101001（尚未开始），只有问题账号返回 600026，
# 且预热单发 1 条就命中 —— 所以它跟场次、并发、网络都无关，是账号属性。
CODE_NO_PERMISSION = 600026

# 「有同款未支付订单，占着限购配额」。**这个码我们没有一手证据**，
# 来源是别人仓库里的一份 AI 生成码表（Allenalonel/tencentyun-seckill 的
# 「腾讯云活动接口错误码全景速查手册.md」，提交说明写着"通过 agent 总结"）。
# 该仓库的实战描述（超时其实已建单、再抢就撞 1101411）是可信的经验，
# 所以我们把它收进来，只用于日志识别，**暂时不进 NO_RETRY_CODES**：
# 万一它的真实语义不是"未付单"，停手会误杀整场；继续发最多是白打，
# 不会更差。等哪天真撞到、拿到服务端原话，再决定要不要改成停手。
CODE_NOT_PAID = 1101411

# 这几个重试没有意义，命中就停，别浪费窗口
# 售罄也算：订单超过支付时限才回补，跟本场没关系，继续发只是白撞限流
NO_RETRY_CODES = {CODE_ALREADY_BOUGHT, CODE_NO_QUALIFY, CODE_BAD_PARAM,
                  CODE_SOLD_OUT, CODE_FACE_VERIFY, CODE_NO_PERMISSION}

# 开抢瞬间常见的排队信号：并发请求里有的排队、有的成功，继续就行
QUEUE_CODES = {CODE_NOT_STARTED, CODE_RATE_LIMIT, CODE_BUSY, CODE_CONCURRENT,
               CODE_RATE_LIMIT_API}

CODE_DESC = {
    CODE_NOT_STARTED: "秒杀尚未开始（未到时间或本场已结束）",
    CODE_ALREADY_BOUGHT: "相同实名主体已产生付费，不满足新用户条件（换账号才行）",
    CODE_NO_QUALIFY: "账号不满足本活动参与条件",
    CODE_BAD_PARAM: "商品参数不对（商品的规格约束变了）",
    CODE_RATE_LIMIT: "访问频率限制（开抢瞬间大家都在抢），继续",
    CODE_BUSY: "系统繁忙（开抢瞬间排队），继续",
    CODE_CONCURRENT: "同账号有订单正在处理，继续",
    CODE_SOLD_OUT: "商品已售罄（本场名额已经没了）",
    CODE_FACE_VERIFY: "账号需要人脸核身校验，先去认证才能下单",
    CODE_NO_PERMISSION: "账号没有创建实例的权限（子账号未授权 / 未实名 / 欠费）",
    CODE_NOT_PAID: "有同款未支付订单占着限购配额？先去控制台看订单（此码语义待实测确认）",
    -1: "请求没发出去（网络断了或页面上下文没了）",

    # ---- 以下为 2026-11-10 22:11 登录态下一手实测到的码（附服务端原话）----
    # 实测方式：do-goods preview=1，不产生订单
    1100105: "商品ID不存在（act_id 写错或活动已换）",
    # 原话: 抱歉，商品ID不存在，请选择其它商品
    600003: "regionId 不是合法地域值",
    # 原话: regionId: invalid value 999
    401: "参数输入错误（必填项的值不合法）",
    # 原话: 参数输入错误（实测 regionId=0 时返回）
    400: "请求参数结构不对（缺字段/字段类型错）",
    # 原话: "goods" must contain at least 1 items / "activity_id" is required
    500: "服务端内部异常（多为参数结构对了但业务不适用）",
    # 原话: 服务异常（实测 check-available 传 {activity_id,goods} 时返回）

    # ---- 以下是第三方程序真实跑出来的码（我能看到日志，但不是我自己一手打出）----
    CODE_RATE_LIMIT_API: "接口层限流（比 408 更靠内一层，原话和 1100205 一模一样）",
    # 来源：issue #13 评论 2026-11-10T12:13:14Z，用户 sundaguanren 贴的 15:00 场日志
    #   [15:00:04.322] 返回 1101408: ... | 当前系统繁忙，请稍后重试，感谢理解与支持！
    #   各码次数: 408×44, 没发出去×7, 1100136×4, 1101408×3
    # 注意：它的服务器原话和 1100205（系统繁忙）**完全相同**，两者可能是不同层
    # 返回的同一个"繁忙"。见过它说明当时连业务层都没进去，属于排队/限流，继续打就行。
    # （我 11-10 22:11 那轮一手实测里没打出它，当时据此误判为"不存在"并删掉了，现已补回。）
}

# 提前多少秒开始预热连接 (在目标时间前)
WARMUP_SECONDS = 20

# 最后多少秒进入纯等待：对时、刷新页面、预热都必须在这之前做完。
# 这几步都要等网络，压在最后几秒会把提前量直接吃掉。
QUIET_SECONDS = 15

# 人脸核身验证入口。接口的 msg 里本来就带这个链接，这里只当兜底用。
FACE_VERIFY_URL = "https://cloud.tencent.com/act/pro/face_authentication"

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


# 前端的 x-csrf-token 是动态生成的，得 hook 住 fetch/XHR 才能拿到。
#
# 实测教训：活动页这一版用 `new Headers({...})` 传头，旧版只判断 h['x-csrf-token']
# 对 Headers 实例取不到（Headers 的下标是空的，只能 h.get()），导致
# window.__latestCsrf 常年为 null，请求带空 token 发出去被服务端回 400 CSRF-ERROR，
# 再被误判成"登录态失效"，于是不停要求重新扫码。
# 所以这里三种形态都要认：Headers 实例、普通对象、[[k,v],...] 数组。
CSRF_HOOK_JS = """
window.__latestCsrf = null;
window.__csrfHits = 0;
(function () {
    function pick(h) {
        if (!h) return null;
        try {
            if (typeof h.get === 'function') {          // Headers 实例
                const v = h.get('x-csrf-token');
                if (v) return v;
            }
        } catch (e) {}
        try {
            if (Array.isArray(h)) {                     // [[k, v], ...]
                for (let i = 0; i < h.length; i++) {
                    const kv = h[i];
                    if (kv && String(kv[0]).toLowerCase() === 'x-csrf-token') return kv[1];
                }
                return null;
            }
            if (typeof h === 'object') {                // 普通对象
                for (const k in h) {
                    if (String(k).toLowerCase() === 'x-csrf-token') return h[k];
                }
            }
        } catch (e) {}
        return null;
    }
    window.__csrfPick = pick;

    const _fetch = window.fetch;
    window.fetch = function () {
        try {
            const v = pick(arguments[1] && arguments[1].headers);
            if (v) { window.__latestCsrf = String(v); window.__csrfHits++; }
        } catch (e) {}
        return _fetch.apply(this, arguments);
    };

    const _set = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
        try {
            if (String(k).toLowerCase() === 'x-csrf-token') {
                window.__latestCsrf = String(v); window.__csrfHits++;
            }
        } catch (e) {}
        return _set.apply(this, arguments);
    };
})();
"""


# ---------- CSRF 兜底抓取 ----------
# JS hook 依赖页面还在跑我们注入的代码，页面内部再用 service worker / iframe
# 之类绕过时就会漏。Playwright 在协议层能看到每一条请求头，所以再从这一侧记一份：
# 只要页面自己发过一次带 x-csrf-token 的请求，这里就一定有值。
def attach_csrf_sniffer(page):
    """在 page 上挂一个请求头嗅探器，把抓到的 csrf 记到 page._csrf_sniff。"""
    page._csrf_sniff = None
    page._csrf_sniff_urls = []

    def _on_request(req):
        try:
            v = req.headers.get("x-csrf-token")
            if v:
                page._csrf_sniff = v
                if len(page._csrf_sniff_urls) < 20:
                    page._csrf_sniff_urls.append(req.url[:120])
        except Exception:
            pass

    try:
        page.on("request", _on_request)
    except Exception:
        pass
    return page


async def ensure_csrf(page, force=False):
    """保证 window.__latestCsrf 有值。

    顺序：从 context cookie 现算 g_tk -> 页面自己的 -> 协议层嗅探到的 -> 页面里现算。
    拿不到就返回 None，调用方不要拿空 token 硬发请求（空 token 会换来
    400 CSRF-ERROR，再被误判成登录失效，这才是"反复要求扫码"的根源）。

    force=True：不信页面缓存，强制按当前 skey 用 g_tk 重算。
    扫码登录后 skey 会换新，而 window.__latestCsrf 里存的是登录**前**
    （匿名时）的旧 token。页面若没自行刷新，旧 token 就一直在，请求会被
    回 CSRF-ERROR，登录检测永远等不到 code=0 —— 这就是"扫完码怎么都
    登不上"的根因。13:08 那次能成功纯属页面碰巧自己刷新了。
    """
    if force:
        calc = await _csrf_from_skey(page)
        if calc:
            try:
                await page.evaluate("(v) => { window.__latestCsrf = v; }", calc)
            except Exception:
                pass
            return calc
        # 重算不出来（页面里没 skey）就用嗅探到的，仍是拿不到才返回 None
        return getattr(page, "_csrf_sniff", None)

    # 首选：直接从 cookie 现算 g_tk。实测服务端认这个（2026-11-12：
    # g_tk(skey) 能过 CSRF 校验，qcmainCSRFToken 过不了），而且不依赖
    # 页面有没有跑过我们的 hook。FIRE_JS 读的是 window.__latestCsrf，
    # 所以算出来后顺手写回页面。
    try:
        tok = await csrf_from_context(page.context)
        if tok:
            try:
                await page.evaluate("(v) => { window.__latestCsrf = v; }", tok)
            except Exception:
                pass
            return tok
    except Exception:
        pass
    try:
        cur = await page.evaluate("() => window.__latestCsrf || null")
        if cur:
            return cur
    except Exception:
        pass

    sniffed = getattr(page, "_csrf_sniff", None)
    if sniffed:
        try:
            await page.evaluate("(v) => { window.__latestCsrf = v; }", sniffed)
        except Exception:
            pass
        return sniffed

    # 最后兜底：按前端的 g_tk 算法，从 skey/p_skey 现算一个
    calc = await _csrf_from_skey(page)
    if calc:
        try:
            await page.evaluate("(v) => { window.__latestCsrf = v; }", calc)
        except Exception:
            pass
        return calc
    return None


async def _csrf_from_skey(page):
    """在页面里读 document.cookie 的 skey/p_skey，按腾讯前端 g_tk 算法算出
    csrf token。实测与页面自己发出的 x-csrf-token 完全一致（2120550790）。

    skey 的 httpOnly=False，所以 JS 能读到；万一读不到返回 None，调用方
    自行降级，绝不拿空 token 去请求。
    """
    try:
        return await page.evaluate("""
            () => {
                const cs = (document.cookie || '').split(';');
                let skey = null;
                for (const c of cs) {
                    const i = c.indexOf('=');
                    if (i < 0) continue;
                    const k = c.slice(0, i).trim();
                    if (k === 'skey' || k === 'p_skey') { skey = c.slice(i + 1); break; }
                }
                if (!skey) return null;
                let e = 5381;
                for (let i = 0; i < skey.length; i++) e += (e << 5) + skey.charCodeAt(i);
                return String(2147483647 & e);
            }
        """)
    except Exception:
        return None


async def _report_state_cookies(ctx):
    """数一下当前 context 里的关键 cookie，返回给人看的描述。

    用来避免"明明没存到登录态却打印已保存"的假象——用户看到这句话就会去
    关浏览器，结果下次又得重扫，这是他反复扫码的最直接来源。
    """
    try:
        cookies = await ctx.cookies()
    except Exception as e:
        return f"读取失败: {str(e)[:40]}"
    names = {c.get("name") for c in cookies}
    keys = [k for k in ("skey", "p_skey", "uin") if k in names]
    if keys:
        return f"{len(cookies)} 条，含 {'/'.join(keys)}"
    return f"{len(cookies)} 条，缺 skey/uin（登录态不完整）"

# ---------- 登录态：保存 / 校验 ----------
#
# 2026-11-12 实测（用一份已经失效的 .state.json 直接打接口）：
#   POST /user/get-vip-info
#     x-csrf-token = qcmainCSRFToken   -> 400 {"code":"CSRF-ERROR"}
#     x-csrf-token = g_tk(skey)        -> 200 {"code":"NOT-LOGINED"}
# 说明 act-api 认的 csrf 就是 g_tk(skey)，跟页面 hook 到的那个值同源。
# 所以 csrf 不该依赖"页面有没有发过请求"，直接从 cookie 现算。
VIP_INFO_URL = API_BASE + "/user/get-vip-info"

# 同一份 state 的另一个实测：拿失效的 skey 打开 cloud.tencent.com，
# 页面自己的 /services/ajax/get_user_info?mc_gtk=<正确值> 会收到
#   Set-Cookie: uin=; Expires=1970 ... / Set-Cookie: skey=;
# 也就是**打开页面这个动作本身**会把登录 cookie 清掉。于是"校验登录态"
# 反而毁了登录态，用户看到的就是"登录状态无法保存、每次都要重扫码"。
# 结论：校验只许打 act-api 的查询接口，不许加载活动页。
LOGIN_COOKIE_NAMES = ("skey", "p_skey", "uin", "tinyid")
STATE_KEEP_DAYS = 30


def gtk_of(skey):
    """按前端 g_tk 算法从 skey 算 csrf token。skey 为空返回 None。"""
    if not skey:
        return None
    e = 5381
    for ch in skey:
        e += (e << 5) + ord(ch)
    return str(2147483647 & e)


def _cookies_of_state(path=STATE_PATH):
    """读 state 文件里的 cookie，返回 {name: value}。读不出来返回空 dict。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    cs = d.get("cookies") or []
    out = {}
    for c in cs:
        if isinstance(c, dict) and c.get("name"):
            out[c["name"]] = c.get("value")
    return out


def state_gtk(path=STATE_PATH):
    ck = _cookies_of_state(path)
    return gtk_of(ck.get("skey") or ck.get("p_skey"))


async def csrf_from_context(ctx):
    """从 context 的 cookie 现算 csrf token。

    比页面上 hook 到的值稳：不依赖页面是否已经发过带 token 的请求，
    也不依赖 hook 有没有被 service worker / iframe 绕过。
    """
    try:
        cookies = await ctx.cookies()
    except Exception:
        return None
    for name in ("skey", "p_skey"):
        for c in cookies:
            if c.get("name") == name:
                tok = gtk_of(c.get("value"))
                if tok:
                    return tok
    return None


async def save_state_safe(ctx, path=STATE_PATH):
    """存登录态并读回校验，新快照缺 skey/uin 时保留旧文件。返回 (ok, desc)。

    先写临时文件、校验通过再替换，避免写一半把原文件搞坏。

    落盘时把 skey/uin/tinyid 这几个会话 cookie（expires=-1）的有效期抻到 30 天：
    只改本地文件里的 expires，不改服务端语义（失效的 skey 照样会被服务端拒），
    但能避免"浏览器一关，会话 cookie 在别处就被当过期"这类凭空丢失。
    """
    tmp = Path(path).with_name(Path(path).name + ".tmp")
    try:
        await ctx.storage_state(path=str(tmp))
    except Exception as e:
        return False, f"写入失败: {str(e)[:40]}"

    def _keys_of(p):
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
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

    if not new_keys and Path(path).exists():
        old_keys, old_n = _keys_of(path)
        if old_keys:
            try:
                tmp.unlink()
            except Exception:
                pass
            log(f"本次快照缺 skey/uin（{n} 条），保留原登录态（{old_n} 条，含 {'/'.join(old_keys)}）")
            return True, f"保留原登录态（{old_n} 条，含 {'/'.join(old_keys)}）"

    if new_keys:
        try:
            d = json.loads(tmp.read_text(encoding="utf-8"))
            until = time.time() + STATE_KEEP_DAYS * 86400
            for c in d.get("cookies") or []:
                if isinstance(c, dict) and c.get("name") in LOGIN_COOKIE_NAMES:
                    if not c.get("expires") or float(c.get("expires") or -1) <= 0:
                        c["expires"] = until
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass  # 改有效期只是加固，失败不影响主流程

    try:
        import os as _os
        _os.replace(str(tmp), str(path))
    except Exception as e:
        return False, f"替换失败: {str(e)[:40]}"

    if new_keys:
        return True, f"{n} 条，含 {'/'.join(new_keys)}"
    return False, f"{n} 条，缺 skey/uin"


async def verify_state(ctx):
    """只打接口、不加载页面地校验登录态。返回 (state, detail)。

    state: 'ok' / 'not-logged' / 'csrf' / 'unknown'
    detail: 服务端原话，便于日志定位。

    'csrf' 说明 cookie 在、只是 token 没对上（重新算即可），**不能当作已登录**，
    更不能当作未登录 —— 但不能像以前那样当成"登录成功"就存盘，那正是
    "存了一份假登录态、下次复验必失败"的来源。
    """
    tok = await csrf_from_context(ctx)
    if not tok:
        return "not-logged", "cookie 里没有 skey（未登录）"
    try:
        r = await ctx.request.post(
            VIP_INFO_URL, data="{}",
            headers={
                "Content-Type": "application/json",
                "x-csrf-token": tok,
                "Origin": "https://cloud.tencent.com",
                "Referer": ACTIVITY_URL,
            },
        )
        body = (await r.text()) or ""
    except Exception as e:
        return "unknown", str(e)[:60]

    try:
        j = json.loads(body)
    except Exception:
        return "unknown", body[:60]

    code = j.get("code")
    text = str(code if code is not None else "").strip().upper()
    msg = str(j.get("msg") or "")[:60]
    if "CSRF" in text:
        return "csrf", msg
    if code in (0, "0") or text in ("OK", "SUCCESS", "SUCC"):
        return "ok", msg
    if "NOT" in text and "LOGIN" in text:
        return "not-logged", msg or text
    return "unknown", msg or text

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
    """对时：调 /dianshi/get-now 取服务器毫秒，按往返时延折半校正。

    取「往返最快的那一次」而不是中位数：网络抖动只会让往返变慢、把偏移算偏，
    不会让它变快，所以最快的那次最接近真实值。往返相差不大时取这几组的中位数。
    get-now 直接给毫秒，实测单次往返 130~470ms，校正后偏差在 ±20ms 内。
    旧实现用响应头 Date 对时，那只有秒精度，实测系统性偏小 0.6~0.7 秒，
    会让整场抢购整体推迟同样的量。
    """
    SAMPLES = 7
    samples = []          # (rtt, offset)
    for _ in range(SAMPLES):
        t0 = time.time() * 1000
        try:
            resp = await api_post(page, "/dianshi/get-now", {})
        except Exception:
            resp = {}
        t1 = time.time() * 1000
        srv = resp.get("data")
        if resp.get("code") == 0 and isinstance(srv, (int, float)) and srv > 0:
            samples.append((t1 - t0, srv - (t0 + t1) / 2))
        await asyncio.sleep(0.05)

    if samples:
        best_rtt = min(r for r, _ in samples)
        near = [o for r, o in samples if r <= best_rtt + 20]   # 和最快那次差不多的
        near.sort()
        offset = int(near[len(near) // 2])
        spread = int(max(o for _, o in samples) - min(o for _, o in samples))
        log(f"服务器时间偏移: {offset:+d}ms "
            f"(get-now ×{len(samples)}, 最快往返 {best_rtt:.0f}ms, 全部波动 {spread}ms)")
        return offset

    log("get-now 对时失败，退回 Date 响应头（秒精度，误差可达 1 秒）")
    return await _sync_time_by_date(page)


async def _sync_time_by_date(page):
    """兜底对时：采样 act-api 响应头 Date 取中位数（只有秒精度）"""
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
    """在页面上下文里 POST act-api：cookie / CSRF / 同源都交给浏览器

    csrf 先由 ensure_csrf 保证拿到；万一仍被判 CSRF-ERROR，重新抓一次再重试一次。
    以前是拿着空 token 直接发，失败就当成"没登录"，把有效登录态误判成失效。
    """
    await ensure_csrf(page)
    r = await _api_post_once(page, path, body)
    if _is_csrf_error(r):
        await asyncio.sleep(0.3)
        await ensure_csrf(page)
        r2 = await _api_post_once(page, path, body)
        if not _is_csrf_error(r2):
            return r2
    return r


def _is_csrf_error(r):
    if not isinstance(r, dict):
        return False
    code = str(r.get("code", "")).strip().upper()
    return "CSRF" in code


async def _api_post_once(page, path, body):
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
    await ensure_csrf(page)
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


async def place_single_order(page, goods):
    """测试下单：只发**一条** preview=0 请求，返回服务端返回体。

    以前的测试下单直接复用整台抢购机器（在途 30→64、3 秒 177 条），
    可在非窗口时段这么打必然被网关按频率打回：实测 2026-11-12 13:21
    的 177 条里 152 条 408、25 条 1100205，一条有效信息都没拿到，
    还白白加深风控。测试只需要一条请求 + 服务端原话。
    """
    results = await do_goods_raw(page, goods, preview=0, concurrency=1)
    return results[0] if results else {"code": -1, "msg": "无返回"}


async def check_qualify(page, goods, regions=None):
    """调 check-available 查「我有没有资格买」和「各地域还剩多少」。

    这个接口只查不买，preview 字段对本接口没有实际意义（实测都是只读）。
    请求结构必须严格是下面这样，多一个字段服务端就报 is not allowed：

        {"activity_id": AID,
         "goods": [{"act_id": ACT, "region_id": [4, 8, 1]}],
         "preview": 0}

    goods 里只有 act_id + region_id 数组，**不能**放 type / goods_param，
    region_id / act_id 也**不能**提到顶层。这是 2026-11-10 实测踩过的坑：
    结构写错时返回 400（"goods" is required）或 500（服务异常），
    会被误读成"接口不支持查库存"。

    返回：
        {"ok": True, "user_available": bool, "idcard_available": bool,
         "total_num": int, "quota": {region_id: 可用总数}, "raw": {...}}
        {"ok": False, "error": "..."}
    """
    rids = [int(r) for r in (regions or [goods.get("goods_param", {}).get("regionId", 8)])]
    body = {
        "activity_id": goods["activity_id"],
        "goods": [{"act_id": goods["act_id"], "region_id": rids}],
        "preview": 0,
    }
    try:
        r = await api_post(page, CHECK_AVAILABLE_PATH, body)
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

    if not isinstance(r, dict):
        return {"ok": False, "error": "返回不是 JSON"}
    if str(r.get("code", "")).strip().upper().find("CSRF") >= 0:
        return {"ok": False, "error": "CSRF 未对齐"}
    if r.get("code") not in (0, None) and r.get("code") != CODE_OK:
        return {"ok": False, "error": f"code={r.get('code')} {str(r.get('msg') or '')[:60]}"}

    data = r.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return {"ok": False, "error": "data 不是预期结构"}
    g = data[0]

    quota = {}
    raw_quota = g.get("quota")
    if isinstance(raw_quota, dict):
        for rid, skus in raw_quota.items():
            n = 0
            if isinstance(skus, dict):
                for info in skus.values():
                    if isinstance(info, dict) and "available" in info:
                        try:
                            n += int(info["available"])
                        except Exception:
                            pass
            quota[rid] = n

    return {
        "ok": True,
        "user_available": g.get("user_available"),
        "idcard_available": g.get("idcard_available"),
        "total_num": g.get("total_num"),
        "quota": quota,
        "raw": g,
    }


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
        log(f"  预热完成（code={code}，{CODE_DESC.get(code, '未识别的返回码，请附服务器原话发 issue')}）")
        log(f"  服务器原话: {str(result.get('msg') or '')[:70]}")
        if code in (CODE_RATE_LIMIT, CODE_RATE_LIMIT_API, CODE_BUSY):
            # 预热是**单发一条**。单发都被打回，说明限的是账号/IP，不是我们的并发。
            log("  ⚠ 单发一条就限流：这是账号/IP 级，不是并发太高，减在途、改参数都没用")
            log("     刚做过测试下单 / 反复登录 / 高频刷新的话，等 5~10 分钟再来")
    return result


def qualify_blocked(v):
    """判断资格字段是否表示"不可买"。

    实测（2026-11-11 场次前，登录态，真实商品）返回的是**数字 1**，不是布尔 True：
        user_available = 1
        idcard_available = 1
        total_num = 1
    所以不能用 `v is False` 判断 —— 服务端真返回 0 时 `0 is False` 在 Python 里是假，
    拦截逻辑会整个失效。这里统一按数值判断，只有明确的"不行"才算拦。
    None / 字段缺失 一律放行：宁可多试一场，也不能因为一个辅助字段误杀用户的抢购。
    """
    if v is None:
        return False
    if isinstance(v, bool):
        return not v
    try:
        return int(v) == 0
    except (TypeError, ValueError):
        return False


async def warmup_qualify(page, goods, regions=None):
    """预热阶段顺带查资格与库存（只读，不会下单）。

    这一步的价值：资格不合格（比如这个实名主体已经买过）时，
    当场就能知道，不用傻等一小时、开抢才发现每条都返 1100106。
    查不通（网络/CSRF/接口变动）只提示，不阻断——不能因为一个辅助检查
    把真正的抢购拦下来。
    """
    q = await check_qualify(page, goods, regions)
    if not q.get("ok"):
        log(f"  资格预检没查通（{q.get('error')}），不影响抢购，继续")
        return None

    ua = q.get("user_available")
    ia = q.get("idcard_available")
    tn = q.get("total_num")
    quota = q.get("quota") or {}

    def _yn(v):
        """1/0、True/False、None 都显示成人话。"""
        if v is None:
            return "未知"
        if isinstance(v, bool):
            return "是" if v else "否"
        try:
            return "是" if int(v) else "否"
        except (TypeError, ValueError):
            return str(v)

    log(f"  资格预检：账号可买={_yn(ua)}  实名可买={_yn(ia)}  每账号限购={tn}")
    if quota:
        parts = []
        for rid in sorted(quota, key=lambda x: str(x)):
            name = REGION_NAMES.get(int(rid), rid) if str(rid).isdigit() else rid
            parts.append(f"{name}={quota[rid]}")
        log(f"  各地域剩余库存：{'  '.join(parts)}")
        if sum(quota.values()) == 0:
            log("  当前显示三个地域都是 0 —— 场次没开时正常（名额还没放出来）")

    if qualify_blocked(ua):
        log("  ★ 账号维度不可买：这个账号已经被判定不符合条件，本场大概率白抢。")
        log("    去活动页看下是不是这个商品已经买过、或账号还有未付款的同款订单。")
    if qualify_blocked(ia):
        log("  ★ 实名维度不可买：同一实名主体已有付费记录（服务器原话「相同实名认证主体"
            "已产生付费，不满足新用户条件」，码 1100106）。")
        log("    换个实名主体才有戏，本场继续抢没有意义。")
    else:
        log("  注意：user_available / idcard_available 都是「是」也不代表新用户资格。")
        log("    2026-11-11 实测：开窗前 1.4 秒 preview=1 才返 1100106，")
        log("    更早两条都是 1101001。这层判定服务端临近开窗才做。")
    return q


FIRE_JS = """
(params) => {
    const st = window.__sk || (window.__sk = {
        inflight: 0, fired: 0, done: 0, codes: {}, msgs: {},
        hit: null, fatal: null, stopped: false, last: null, rtt: null,
        regions: {}, ptr: 0, dts: [],
    });

    // 轮动抢。**注意别把这里当成结论**：quota 确实以 region_id 为键返回，各地域各有
    // 一条 available，但那个数字到底是「本地域自己的库存」还是「总池摊到本地域的数」，
    // 目前没有探明（见 _verify/quota_probe.py 的判定）。所以轮动是「不管哪种情况都不吃亏」
    // 的保守做法，不是「因为各地库存独立所以要多点开花」。
    // 具体做法：每条请求现选地域，某地域回「售罄」就把它摘掉，剩下地域继续，
    // 全摘完才判本场结束。
    const rids = (params.regions && params.regions.length)
        ? params.regions.slice()
        : [params.goods_param.regionId];

    if (params.reset) {
        st.inflight = 0; st.fired = 0; st.done = 0;
        st.codes = {}; st.msgs = {};
        st.hit = null; st.fatal = null; st.stopped = false; st.last = null;
        st.rtt = null; st.ptr = 0; st.regions = {}; st.dts = [];
        rids.forEach(rid => {
            st.regions[rid] = {sent: 0, done: 0, last: null, sold: false,
                               sold_at: null, codes: {}};
        });
    } else {
        rids.forEach(rid => {
            if (!st.regions[rid]) {
                st.regions[rid] = {sent: 0, done: 0, last: null, sold: false,
                                   sold_at: null, codes: {}};
            }
        });
    }
    if (params.stop) st.stopped = true;
    // 换了在途数就把往返样本清空重采：否则判断「新在途下吞吐涨没涨」时，
    // 算进去的还是旧在途时的往返，会误判成「还有余量」继续往上加。
    if (params.reset_rtt) { st.dts = []; }

    function pickRegion() {
        const alive = rids.filter(rid => st.regions[rid] && !st.regions[rid].sold);
        if (!alive.length) return null;
        const rid = alive[st.ptr % alive.length];   // 轮转，天然均摊在存活地域上
        st.ptr++;
        return rid;
    }

    let n = 0;
    while (n < params.batch && st.inflight < params.cap && !st.stopped && !st.hit && !st.fatal) {
        const rid = pickRegion();
        if (rid === null) {
            // 所有地域都售罄了。要等在飞的回来再确认，别在还有可能成功时提前收手
            if (st.inflight === 0 && !st.fatal) st.fatal = params.sold_code;
            break;
        }

        const gp = {};
        for (const k in params.goods_param) gp[k] = params.goods_param[k];
        gp.regionId = rid;

        const body = JSON.stringify({
            activity_id: params.activity_id,
            goods: [{act_id: params.act_id, type: params.type, goods_param: gp}],
            agent_channel: {fromChannel: '', fromSales: '', fromUrl: '', isAgentClient: false},
            preview: params.preview,
        });

        st.inflight++; st.fired++; n++;
        st.regions[rid].sent++;
        const t0 = Date.now();       // 用来量往返：开窗前的提前量按这个现算
        fetch(params.url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json',
                      'x-csrf-token': window.__latestCsrf || ''},
            credentials: 'include',
            body: body,
        }).then(r => r.json()).then(d => {
            st.inflight--; st.done++;
            const dt = Date.now() - t0;
            if (st.rtt === null || dt < st.rtt) st.rtt = dt;
            // 同时留一份最近 N 条的样本：只记最快那条会把排队掩盖掉——
            // 服务端挂住我们的时候，最快的那条依然很快，用它算不出饱和。
            st.dts.push(dt);
            if (st.dts.length > (params.rtt_n || 20)) st.dts.shift();
            // 返回体里没有 code（被限流拦截、走了别的分支）就当没发出去，
            // 别记成 undefined，否则统计表里看不出真相
            const c = (d && d.code !== undefined && d.code !== null) ? d.code : -1;
            st.last = c;
            st.codes[c] = (st.codes[c] || 0) + 1;
            if (d && d.msg) st.msgs[c] = String(d.msg).slice(0, 400);

            const rs = st.regions[rid];
            if (rs) {
                rs.done++; rs.last = c;
                rs.codes[c] = (rs.codes[c] || 0) + 1;
                // 售罄只摘掉这个地域，不当全局致命码。
                // 记下「这个地域第一次收到售罄回包」的 JS 时间戳，收尾时比三地先后。
                // 抢完再查 quota 没意义，那时只剩全 0；这条线索只能在窗口内取。
                // 但它只是收到回包的时间，不等于服务端扣减时刻，判定只给「偏向」。
                if (c === params.sold_code && !rs.sold) {
                    rs.sold = true;
                    rs.sold_at = Date.now();
                }
            }

            if (c === params.ok && !st.hit) st.hit = d;
            if (params.norety.indexOf(c) >= 0 && !st.fatal) st.fatal = c;
        }).catch(e => {
            st.inflight--; st.done++;
            st.last = -1;
            st.codes[-1] = (st.codes[-1] || 0) + 1;
            if (!st.msgs[-1]) st.msgs[-1] = String((e && e.message) || e).slice(0, 60);
        });
    }
    return {fired: st.fired, inflight: st.inflight, done: st.done,
            codes: st.codes, msgs: st.msgs, hit: st.hit, fatal: st.fatal,
            last: st.last, rtt: st.rtt, dts: st.dts.slice(), regions: st.regions,
            region_count: rids.length,
            sold_count: rids.filter(r => st.regions[r] && st.regions[r].sold).length};
}
"""


async def rapid_purchase(page, goods, duration=None, open_in=None, regions=None,
                         profile=None):
    """不等这一批全回来就发下一批：同时在等的到上限就停手，回来一个补一个。

    每秒能发多少只看回来得多快，跟单条请求慢不慢无关；
    成功和致命码从页面里攒下的返回结果读。

    regions：要轮动的地域列表。各地域是不是独立库存还没探明（quota 数字
    全程是 0，抢完再查也只剩全 0），所以默认把请求摊到所有地域上，
    任一处抢到就停；某地域售罄只摘掉它，全部摘完才判本场结束。
    传 None 时只打 goods 里自带的那个地域，行为与以前完全一致。

    profile：场次档位（poll_ms / duration），由 session_profile() 按场次给出。
    传 None 用默认档。**注意档位里没有并发**——在途数运行中按实测往返自动爬坡。

    open_in：距离场次开窗还有多少秒（None=没有窗口概念，直接全速）。
    开窗之前分两段：先每 PROBE_MS 发一条探针（顺带量出往返时间），
    等算出单程、定好提前量以后才放开全速——提前量 = 实测单程 + 余量，
    让第一批请求正好在开窗那一刻到服务端：太早到是白打的 1101001，
    太晚到名额可能已经没了。
    """
    prof = dict(profile or DEFAULT_PROFILE)
    poll_ms = int(prof.get("poll_ms") or POLL_MS)
    if duration is None:
        duration = float(prof.get("duration") or DURATION_SEC)

    # 在途数的起点和上限都来自场次档位（依据见 SESSION_PROFILES 上方注释）。
    # 用户不需要设：10:00 场起点 35 = 首发 30（成功过的规模）+ 5 条留给探针；
    # 15:00 场固定 55 = 首发 50 + 5 条探针，因为下午场补发失效
    # （窗口 2 秒 < 往返还 1.76~2.5 秒），窗口内请求数 ≈ 首发条数。
    cap_max = int(prof.get("cap_max") or CAP_CEIL)
    cap_max = max(CAP_FLOOR, min(CAP_CEIL, cap_max))
    cap_base = int(prof.get("cap_start") or prof.get("inflight") or CAP_FLOOR)
    cap_base = max(CAP_FLOOR, min(cap_max, cap_base))
    cap_now = cap_base
    cap_used_start = cap_base   # 收尾时打「在途从多少爬到多少」用
    cap_peak = cap_base         # 本场爬到过的最高在途数

    open_at = None if open_in is None else time.monotonic() + open_in
    # 已经在窗口里（比如手动抢一场进行中的）就不用探针，直接全速
    ramped = open_at is None or open_in <= 0
    pre_fire = float(PRE_FIRE_MAX_MS)     # 还没量到往返，先按最保守的值占位
    ramp_at = None if open_at is None else open_at - pre_fire / 1000

    # 基线往返：探针阶段只有一条在飞，那时量到的就是纯传播时间。
    # 一旦放开全速就**锁死不再更新** —— 否则满载时「最快那条」会把基线越拉越低，
    # 阈值跟着变严，最后系统把自己误判成一直在排队，白减并发。
    rtt_base = None
    adapt_prev = None         # (加之前的在途, 当时的往返)，算吞吐比要用
    adapt_saturated = False   # 已判定饱和的场次不再往上试（抢购窗口只有几秒，试错太贵）
    adapt_gap = ADAPT_MIN_MS / 1000.0   # 评估周期，拿到基线后改成「至少一个往返」
    adapt_at = time.monotonic() + adapt_gap
    last_adapt_log = None      # 上一次调整时打的日志，用来把连续同向调整合成一条

    # 开抢前把 csrf 落实：FIRE_JS 里发的是 window.__latestCsrf，
    # 这里为空的话，整场几百条请求会全部被服务端判 CSRF-ERROR，白打一整天。
    if not await ensure_csrf(page):
        log("!! 没拿到 csrf token，本次请求可能全部被判 CSRF-ERROR")

    use_regions = list(regions) if regions else [goods.get("goods_param", {}).get("regionId", 8)]

    if not ramped:
        log(f"距开窗 {open_in:.1f}s，先每 {PROBE_MS}ms 一条探针量往返，"
            f"开窗前按实测单程提前铺开")
    if len(use_regions) > 1:
        log(f"开始抢购：轮动 {len(use_regions)} 个地域 {use_regions}，"
            f"在途 {cap_base} 起、最多 {cap_max}（按实测吞吐自动加），"
            f"每 {poll_ms}ms 看一次结果，持续 {duration:g}s")
        log("  某个地域售罄只摘掉它，其余继续；全部售罄才停")
    else:
        log(f"开始抢购：在途 {cap_base} 起、最多 {cap_max}（按实测吞吐自动加），"
            f"每 {poll_ms}ms 看一次结果，持续 {duration:g}s")

    params = {
        "url": DO_GOODS_URL,
        "activity_id": goods["activity_id"],
        "act_id": goods["act_id"],
        "type": goods["type"],
        "goods_param": goods.get("goods_param", {}),
        "regions": use_regions,
        "sold_code": CODE_SOLD_OUT,
        "preview": 0,          # 真抢：会真的下单
        "cap": int(cap_now) if ramped else min(PROBE_CAP, int(cap_now)),
        "batch": int(cap_now) if ramped else 1,
        "ok": CODE_OK,
        "rtt_n": int(RTT_SAMPLES),
        "reset_rtt": False,    # 换了在途数就置 True，让页面把往返样本清空重采
        # 售罄不放进 norety：它只摘地域，全部摘完才由 JS 判本场结束
        "norety": sorted(NO_RETRY_CODES - {CODE_SOLD_OUT}),
        "reset": True,
        "stop": False,
    }

    start = time.monotonic()
    success = False
    last_code = None
    seen_codes = {}
    fired = 0
    err_count = 0
    slow_warned = False      # 网络慢的提示只打一次，别刷屏
    next_progress = start + 2
    result = {}
    done_prev = 0            # 上一轮回来多少条、其中 408 多少条：用来看这一轮是不是整批被打回
    rl_prev = 0
    rl_log_at = 0.0          # 上次打印 408 占比的时刻，用于日志降噪

    while time.monotonic() - start < duration:
        now = time.monotonic()
        if not ramped:
            # 每轮都按最新量到的往返重算「什么时候铺开」：提前量 = 单程 × 2，
            # 保证开窗瞬间第一批正好压到服务端，且白打的量恒定为一个批次。
            # 算法见 calc_pre_fire 与上方常量注释。
            rtt = result.get("rtt")
            if rtt:
                # 探针阶段只有 1 条在飞，此刻的往返就是纯传播时间，用它当基线。
                # 放开全速后（ramped=True）这段不再执行，基线就锁死在这里。
                rtt_base = float(rtt)
            pre_fire = calc_pre_fire(rtt)
            ramp_at = open_at - pre_fire / 1000
            if rtt and float(rtt) > SLOW_RTT_WARN_MS and not slow_warned:
                # 网络慢的时候，参数怎么调都是次要的 —— 先把话说明白，
                # 免得用户以为是自己并发设错了、一直在我们这边找原因。
                slow_warned = True
                log(f"  !! 实测往返 {rtt}ms，偏慢（常见值 100~300ms）")
                log(f"     单程就要 {float(rtt)/2:.0f}ms，开窗那一刻我们已经比快网络的人晚这么多。")
                log(f"     若走了代理/VPN/加速器，或线路绕了境外，换回直连试试，比调任何参数都管用。")
                log(f"     参数会照常自适应（提前 {pre_fire:.0f}ms 铺开），但速度上补不回来。")
            if now >= ramp_at:
                ramped = True
                params["cap"] = int(cap_now)
                params["batch"] = int(cap_now)
                left = open_at - now
                rtt_txt = f"{rtt}ms" if rtt else "还没量到"
                if left > 0:
                    log(f"  开窗前 {pre_fire:.0f}ms 放开全速"
                        f"（实测往返 {rtt_txt}，距开窗 {left:.2f}s）")
                else:
                    log(f"  开窗了，全速发（实测往返 {rtt_txt}）")
            else:
                params["cap"] = min(PROBE_CAP, int(cap_now))
                params["batch"] = 1
        params["reset"] = False
        try:
            result = await page.evaluate(FIRE_JS, params) or {}
            params["reset_rtt"] = False     # 清一次就够了，别每轮都清
        except Exception as e:
            # evaluate 可能撞页面导航/关闭抛错，单次失败不中断整个窗口
            err_count += 1
            if err_count <= 3:
                log(f"  发射失败: {str(e)[:80]}")
            await asyncio.sleep(poll_ms / 1000)
            continue

        fired = int(result.get("fired") or 0)
        msgs = result.get("msgs") or {}
        bad_code = None
        for key, cnt in (result.get("codes") or {}).items():
            try:
                code = int(key)
            except Exception:
                code = key          # 登录态失效时返回的是字符串码，别吞掉
            if code == CODE_OK:
                continue            # 成功走下面的 hit 分支，不当作异常码打印
            if code in seen_codes:
                continue
            # 同一种码只报一次，避免刷屏；但一定带上服务器的原话
            seen_codes[code] = cnt
            if isinstance(code, str) and code not in ("undefined", "null"):
                # NOT-LOGINED 之类：接口根本没走到抢购逻辑，继续发没意义
                desc = "不是抢购结果码，登录态可能失效了"
            else:
                desc = CODE_DESC.get(code, "未识别的返回码，程序会继续；请附服务器原话发 issue")
            log(f"  返回 {code}: {desc} | {msgs.get(key, '')}")
            if isinstance(code, str) and code not in ("undefined", "null"):
                bad_code = code

        # 「最后返回码」取最新回来那一条，不是随便挑一个（同时有多条在路上时，回来的顺序是乱的）
        if result.get("last") is not None:
            last_code = result["last"]

        hit = result.get("hit")
        if hit:
            deal = hit.get("data") or {}
            log("下单成功")
            log(f"   订单号: {(deal.get('deal_names') or ['?'])[0]}")
            log(f"   大单号: {deal.get('big_deal_no') or '?'}")
            success = True
            break

        fatal = result.get("fatal")
        if fatal is not None:
            if fatal == CODE_SOLD_OUT:
                # 走到这里说明**所有**轮动地域都售罄了（单个地域售罄只会把它摘掉）
                n_sold = int(result.get("sold_count") or 0)
                n_all = int(result.get("region_count") or 1)
                if open_at is None:
                    log(f"  所有地域都已售罄（{n_sold}/{n_all}），本场没有名额了，停止。")
                else:
                    ms_from_open = (time.monotonic() - open_at) * 1000
                    log(f"  所有地域都已售罄（{n_sold}/{n_all}），本场没有名额了，停止。"
                        f"（距开窗 {ms_from_open:+.0f} 毫秒）")
            elif fatal == CODE_FACE_VERIFY:
                # 账号级拦截：窗口内外都会拦，重试对本场毫无意义，还会加深风控
                log("  ★ 账号需要人脸核身校验，本场不会再成功了，立即停止。")
                log(f"  去这里认证（扫码后通常即时通过）：{FACE_VERIFY_URL}")
                log("  认证完再等下一场；没认证前继续请求只会加重风控标记。")
                try:
                    import webbrowser
                    webbrowser.open(FACE_VERIFY_URL)
                except Exception:
                    pass
            elif fatal in (CODE_ALREADY_BOUGHT, CODE_NO_QUALIFY):
                log(f"  账号这边过不去（{CODE_DESC.get(fatal, '')}），本场停止。")
            else:
                log("  这类错误重试没有意义，本场停止。")
            last_code = fatal
            break

        if bad_code is not None:
            log("  本场停止，重新扫码登录再来。")
            last_code = bad_code
            break

        now = time.monotonic()
        if now >= next_progress:
            next_progress = now + 2
            rate = fired / max(0.001, now - start)
            log(f"  已发 {fired} 条（{rate:.0f}/秒），"
                f"在途上限 {cap_now}，还在等 {int(result.get('inflight') or 0)} 条"
                f" ({now - start:.1f}s)")

        # ---- 在途数自适应：这一整场能发多少条，全靠它 ----
        # 判据是「加了在途，吞吐有没有跟着涨」，不是「往返涨没涨」。详见 calc_cap。
        # 只在放开全速后做：探针阶段是故意只放 1 条的，拿来判断吞吐会得出错误结论。
        if ramped and now >= adapt_at:
            dts = [x for x in (result.get("dts") or []) if x]
            rtt_med = median(dts)
            if rtt_base is None and dts:
                # 没有探针阶段（手动抢一场进行中的）：拿这批里最快的一条当传播时间。
                rtt_base = min(float(x) for x in dts)
            if rtt_med or rtt_base:
                # 评估周期要盖住「换在途后队列刷新」这件事。
                # 换了在途数，队列里排在前面的是改之前发出去的旧请求（它们先回来），
                # 这时候量到的往返还是旧在途的水平。要等这批排空、新请求开始回来，
                # 看到的才是新在途下的真实往返。
                #
                # 用**当前实测往返**算，不是基线：满载往返可能是基线的十倍
                # （11-04 抢到那次基线 94ms、满载 940ms）。按基线算会让评估周期
                # 短到 250ms，那时回来的还是旧请求，一路误判「还有余量」加到顶。
                ref_rtt = rtt_med if rtt_med else rtt_base
                adapt_gap = max(ADAPT_MIN_MS / 1000.0, float(ref_rtt) / 1000.0 * 1.5)

            # 样本至少要能代表一个在途批次的多数，但采集窗口只留 RTT_SAMPLES 条，
            # 所以取两者的较小值——否则「要 22 条、最多只能有 20 条」会永远等不到。
            enough = min(RTT_SAMPLES, max(3, cap_now // 2))
            if len(dts) < enough:
                # 样本还不够（刚换过在途、刚清空，或者往返特别长），100ms 后再看。
                # 不推后一整个周期，否则 15:00 场那种 2.5 秒往返会白等掉半个窗口。
                adapt_at = now + 0.1
            else:
                adapt_at = now + adapt_gap
                if not adapt_saturated:
                    new_cap, action = calc_cap(cap_base, rtt_med, adapt_prev, cap_max,
                                               cap_safe=cap_used_start)
                    if action in ("probe", "up"):
                        # 记下「加之前」的状态，下一轮用它算吞吐比
                        adapt_prev = (cap_base, rtt_med)
                        cap_base = new_cap
                        cap_peak = max(cap_peak, cap_base)
                        cap_now = cap_base
                        params["cap"] = int(cap_now)
                        params["batch"] = int(cap_now)
                        params["reset_rtt"] = True   # 换在途了，往返要重新采
                        if action != last_adapt_log:
                            last_adapt_log = action
                            log(f"  在途 加到 {cap_now}（当前往返 {rtt_med:.0f}ms，"
                                f"上限 {cap_max}；一个往返后看吞吐跟不跟得上）")
                    elif action == "saturated":
                        # 加了在途、吞吐没跟上 → 服务端饱和。退回加之前的值并锁死，
                        # 抢购窗口只有几秒，反复试探等于烧掉机会。
                        adapt_saturated = True
                        cap_base = new_cap
                        cap_now = cap_base
                        params["cap"] = int(cap_now)
                        params["batch"] = int(cap_now)
                        params["reset_rtt"] = True
                        log(f"  在途 退回 {cap_now}：加了在途吞吐没跟着涨"
                            f"（往返 {rtt_med:.0f}ms），服务端已饱和，本场不再往上试")
                    elif action == "hold" and last_adapt_log != "hold":
                        last_adapt_log = "hold"
                        log(f"  在途停在 {cap_now}（已到本场上限）")

        # 408 只记录，**不缩量**。
        # 历史实测：408 是网关按比例随机丢弃，不是封禁；降低并发从未降低 408 率
        # （12 并发反而 76~81%，比 30 并发还高），掉下去的只是总请求量。
        # 11-04 抢到那次被 408 挡掉 109 条（76%），一条没缩，照样成功。
        # 「该不该减在途」现在交给上面的往返判据：服务端真被压垮时往返会先变长，
        # 那是比 408 比例更本源的信号。两个机制同时改 cap 只会互相打架
        # （这一条是实测出来的：两个都在跑时，自适应爬到 64 又被 408 折扣打回 35）。
        codes_now = result.get("codes") or {}
        rl_now = int(codes_now.get(str(CODE_RATE_LIMIT))
                     or codes_now.get(CODE_RATE_LIMIT) or 0)
        done_now = int(result.get("done") or 0)
        round_done = done_now - done_prev
        round_rl = rl_now - rl_prev
        done_prev, rl_prev = done_now, rl_now
        if round_done >= RL_MIN_DONE and round_rl / round_done >= RL_HIGH_RATIO:
            if now - rl_log_at >= 3.0:      # 每 3 秒最多提一次，不刷屏
                rl_log_at = now
                log(f"  408 占 {round_rl / round_done * 100:.0f}%"
                    f"（按比例丢弃，继续发；要减在途会由往返变长自动触发）")

        if ramped:
            await asyncio.sleep(poll_ms / 1000)
        else:
            # 探针阶段不用密，但别睡过该铺开的时刻
            await asyncio.sleep(min(PROBE_MS / 1000, max(0.005, ramp_at - time.monotonic())))

    # 收尾：先让页面停发，再把还没回来的等一会儿，
    # 免得最后半秒的成功返回被丢掉（数量由服务端按账号限 1，不会因为多发重复下单）
    try:
        params["stop"] = True
        deadline = time.monotonic() + 2.5
        while True:
            result = await page.evaluate(FIRE_JS, params) or {}
            if result.get("hit") and not success:
                deal = (result["hit"].get("data") or {})
                log("下单成功")
                log(f"   订单号: {(deal.get('deal_names') or ['?'])[0]}")
                log(f"   大单号: {(deal.get('big_deal_no') or '?')}")
                success = True
            if success or int(result.get("inflight") or 0) == 0 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.1)
        fired = int(result.get("fired") or fired)
    except Exception:
        pass

    elapsed = time.monotonic() - start
    log(f"抢购结束: {fired} 次请求, {elapsed:.1f}s, {'成功' if success else '未抢到'}")
    cap_txt = f"在途 {cap_used_start}→{cap_peak}"
    if rtt_base:
        cap_txt += f"，基线往返 {rtt_base:.0f}ms"
    log(f"  本场自适应：{cap_txt}；平均 {fired / max(0.001, elapsed):.0f} 条/秒")
    tally = result.get("codes") or {}
    msgs = result.get("msgs") or {}
    if tally:
        def _label(k):
            try:
                k = int(k)          # 页面里回来的字典键是字符串
            except Exception:
                pass
            if k == CODE_OK:
                return "成功"
            if k == -1:
                return "没发出去"
            return str(k)
        parts = sorted(tally.items(), key=lambda kv: -kv[1])
        pieces = []
        for k, v in parts:
            # 每个码都带上服务器原话：认不出的码（比如 1101408）靠这句话才能定性
            msg = str(msgs.get(k) or msgs.get(str(k)) or "").strip()
            pieces.append(f"{_label(k)}×{v}" + (f"（{msg}）" if msg else ""))
        log("  各码次数: " + ", ".join(pieces))

    # 分地域战况：轮动模式下最该看的就是这个——
    # 哪个地域先售罄、哪个地域一条都没发出去、额度是不是被摊平了
    reg_stats = result.get("regions") or {}
    if len(reg_stats) > 1:
        NAME = {4: "上海", 8: "北京", 1: "广州", 9: "新加坡", 999: "非法"}
        bits = []
        for rid, s in sorted(reg_stats.items(), key=lambda kv: str(kv[0])):
            if not isinstance(s, dict):
                continue
            nm = NAME.get(rid, str(rid))
            flag = "已售罄" if s.get("sold") else "仍在打"
            top = ""
            codes = s.get("codes") or {}
            if codes:
                k = max(codes.items(), key=lambda kv: kv[1])[0]
                try:
                    k = int(k)
                except Exception:
                    pass
                top = f" 最多码={k}"
            bits.append(f"{nm} 发{s.get('sent', 0)}回{s.get('done', 0)}({flag}{top})")
        log("  各地域: " + " | ".join(bits))

        # 库存结构判定：只看窗口内各地域「第一次收到售罄回包」的时间。
        # 抢完再去查 quota 是没用的 —— 那时名额早光了，三地必然都是 0。
        # 注意：这是客户端收到回包的时间，不是服务端扣减库存的时刻；
        # 三地请求发起有先后、回包也有排队抖动，所以下面只给「偏向」，不下定论。
        def _nm(rid):
            try:
                return NAME.get(int(rid), str(rid))
            except (TypeError, ValueError):
                return str(rid)

        sold_at = {}
        still_alive = []
        for rid, s in reg_stats.items():
            if not isinstance(s, dict):
                continue
            t = s.get("sold_at")
            if t:
                try:
                    sold_at[_nm(rid)] = float(t)
                except (TypeError, ValueError):
                    pass
            elif not s.get("sold") and (s.get("sent") or 0) > 0:
                still_alive.append(_nm(rid))

        if sold_at and still_alive:
            log(f"  库存判定：{'、'.join(sold_at)}已售罄，"
                f"{'、'.join(still_alive)}仍在打 → 偏向库存按地域分开")
            log("    → 若确实分开，该把请求压到还有货的地域，而不是三点均摊。")
        elif len(sold_at) >= 2:
            spread = max(sold_at.values()) - min(sold_at.values())
            who = sorted(sold_at, key=lambda k: sold_at[k])
            if spread <= 300:
                log(f"  库存判定：{'、'.join(who)} 几乎同时售罄（相差 {spread:.0f}ms）"
                    f" → 偏向一个总池，地域只决定机器开在哪。")
            else:
                log(f"  库存判定：{'、'.join(who)} 售罄先后错开 {spread:.0f}ms"
                    f" → 偏向库存按地域分开。")
        else:
            log("  库存判定：本场没有地域返售罄，这条线索没取到（没售罄、或只打单地域）。")
        # 时基限制写进日志，免得有人把「偏向」当实测结论拿去改策略
        if sold_at:
            log("    （比的是客户端收到售罄回包的时间，不是服务端扣减时刻，只作参考）")
    if not success and last_code is not None:
        if isinstance(last_code, str):
            log(f"  最后返回码 {last_code}: 登录态校验没过，重新扫码登录再试")
        elif last_code == CODE_SOLD_OUT:
            log(f"  最后返回码 {last_code}: {CODE_DESC[last_code]}，只能等下一场")
        elif CODE_DESC.get(last_code):
            log(f"  最后返回码 {last_code}: {CODE_DESC[last_code]}")
            if last_code == CODE_FACE_VERIFY:
                log(f"  → 先去做人脸核身：{FACE_VERIFY_URL}，认证完再抢下一场")
        elif last_code == -1:
            log("  最后返回码 -1: 请求本身没发出去（网络/页面上下文问题）")
        elif last_code in QUEUE_CODES:
            log(f"  最后返回码 {last_code}: {CODE_DESC.get(last_code, '排队信号')}"
                f"。这类码刷屏不影响抢购，没抢到就是名额没了或时间窗不对")
        else:
            log(f"  最后返回码 {last_code}: 未识别的返回码，请附服务器原话发 issue")
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
    attach_csrf_sniffer(page)

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

        # 保存登录状态（写临时文件 → 读回校验 → 替换；缺 skey/uin 不覆盖旧文件）
        _saved, _desc = await save_state_safe(ctx)
        if _saved:
            log(f"登录状态已保存（{_desc}）")
        else:
            log(f"登录状态没能保存（{_desc}）")
        # 用接口验一次真假：不加载页面，避免"校验把 cookie 校验没了"
        _st, _msg = await verify_state(ctx)
        if _st == "ok":
            log("登录态服务端校验通过")
        elif _st == "not-logged":
            log(f"服务端说没登录（{_msg}）：扫码后要在手机上点【确认登录】，"
                f"确认完等 3~5 秒再继续")
        elif _st == "csrf":
            log(f"csrf 没对齐（{_msg}），cookie 在、登录态可能有效，继续")
        else:
            log(f"登录态没能确认（{_st} {_msg}），按有效继续")

        # 同步时间
        offset_ms = await sync_time(page)

        # 商品：--test 用专区商品，正常模式用自动发现的秒杀商品
        goods = TEST_GOODS if test_mode else FLASH_SALE
        log(f"商品(初始): {goods['name']}")

        # 等待前端 CSRF token 就绪（页面导航期间 evaluate 会报上下文销毁，吞掉重试）
        csrf = None
        for _ in range(50):
            try:
                if await page.evaluate("() => !!window.__latestCsrf"):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.2)
        csrf = await ensure_csrf(page)
        if csrf:
            log(f"CSRF token 已就绪（{len(str(csrf))} 位）")
        else:
            log("未捕获到 CSRF token，请求可能被判 CSRF-ERROR")

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
            # 只发一条：测试时段高频打接口只会换来一地 408，测不出任何东西
            res = await place_single_order(page, goods)
            code = res.get("code")
            if code == CODE_OK:
                log("已建单 —— 请去控制台取消订单，不要付款。")
            else:
                log(f"返回 {code}: {CODE_DESC.get(code, '未识别的返回码，请附服务器原话发 issue')}")
                log(f"  服务器原话: {str(res.get('msg') or '')[:80]}")
                if code in QUEUE_CODES:
                    log("  这类码是网关限流/排队，不是下单逻辑问题：等 5~10 分钟再测")
            return

        # 等待抢购时间
        if target_time:
            now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
            target_dt = datetime.strptime(f"{now.date()} {target_time}", "%Y-%m-%d %H:%M")
        else:
            target_dt = find_nearest_time(offset_ms)

        # 自动找最近场次时 target_time 是 None，这里反推出是哪个场次，
        # 否则取并发档位匹配不上，会退化成默认档
        target_time = target_dt.strftime("%H:%M")

        now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
        wait_sec = (target_dt - now).total_seconds()
        log(f"目标: {target_dt.strftime('%Y-%m-%d %H:%M:%S')} (服务器时间)")
        log(f"等待 {wait_sec:.0f}s ({wait_sec/60:.1f}min)")
        log(f"提前量 {abs(ADVANCE_MS)}ms，会在目标时间前 {abs(ADVANCE_MS)/1000:.1f} 秒开始")

        target_ms = target_dt.timestamp() * 1000
        fire_local_ms = target_ms + ADVANCE_MS - offset_ms   # 开始抢的时刻换算成本机时间

        warmed_up = False
        resynced = False
        while True:
            remaining = (target_ms - (time.time() * 1000 + offset_ms)) / 1000

            # 最后 QUIET_SECONDS 秒只等待：对时、刷新、预热都要等网络，
            # 压在最后几秒会把提前量吃掉，所以这段时间一次网络请求都不发。
            if remaining <= QUIET_SECONDS:
                log(f"最后 {remaining:.1f}s，只等待不发请求")
                gap = fire_local_ms - time.time() * 1000
                if gap > 0:
                    await asyncio.sleep(gap / 1000)
                left = target_ms - (time.time() * 1000 + offset_ms)
                log(f"实际提前 {left:.0f}ms（设定 {abs(ADVANCE_MS)}ms，"
                    f"偏差 {left - abs(ADVANCE_MS):+.0f}ms）")
                break

            # 剩余 3 分钟时重新对时一次，修正长时间等待的漂移
            if remaining <= 180 and not resynced:
                log("剩余3分钟，重新对时...")
                offset_ms = await sync_time(page)
                fire_local_ms = target_ms + ADVANCE_MS - offset_ms
                resynced = True
                continue  # 用新偏移重算 remaining

            if remaining <= WARMUP_SECONDS and not warmed_up:
                warmed_up = True
                # 先查资格（只读）：不合格就当场中止，别白等一整场。
                # 注意服务端返回的是 1/0 而不是 True/False，判断必须走 qualify_blocked
                q = await warmup_qualify(page, goods)
                if q and (qualify_blocked(q.get("user_available"))
                          or qualify_blocked(q.get("idcard_available"))):
                    log("开抢前资格预检就不通过，本场中止。")
                    return
                probe = await warmup(page, goods)
                if probe.get("code") in NO_RETRY_CODES:
                    if probe.get("code") == CODE_SOLD_OUT:
                        log("开抢前这个商品就已经售罄了，中止本场")
                    elif probe.get("code") == CODE_NO_PERMISSION:
                        log("开抢前就返回「无权创建实例」，这是账号权限问题，不是场次问题，中止本场。")
                        log("  子账号：让主账号去 访问管理 → 用户 → 授权，加上 QcloudLighthouseFullAccess；")
                        log("          买包年包月还要 QCloudFinanceFullAccess（付款权限）。")
                        log("  主账号：查一下实名认证、有没有欠费、是否被风控限制购买。")
                        log("  这个码每次请求都会返回，继续抢没有意义 —— 跟并发、对时、网络都无关。")
                    else:
                        log("开抢前已知资格不满足，中止本场")
                    return
                continue

            if int(remaining) % 60 == 0:
                log(f"  还剩 {remaining/60:.0f}min")

            await asyncio.sleep(min(1.0, max(0.05, remaining - QUIET_SECONDS)))

        # 执行抢购
        open_in = (target_ms - (time.time() * 1000 + offset_ms)) / 1000
        prof = session_profile(target_time)
        log(f"本场档位（{target_time or '非场次'}）："
            f"在途 {prof['cap_start']}→{prof['cap_max']} / 回读 {prof['poll_ms']}ms / "
            f"持续 {prof['duration']}s")
        success = await rapid_purchase(page, goods, open_in=open_in, profile=prof)

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
            # 退出前也走同一套保护：缺 skey/uin 不覆盖已存好的登录态
            await save_state_safe(ctx)
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
