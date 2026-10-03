# 腾讯云服务器抢购助手

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

腾讯云「上云精选·限时秒杀」轻量应用服务器（38 元/年，4 核 4G 3M）自动下单工具。

活动开始时手动点击「立即抢购」通常无法下单。本工具跳过 DOM 操作，直接在页面上下文内调用 `do-goods` 接口，Cookie 与 CSRF Token 均交由浏览器处理。

![界面截图](screenshot.png)

活动页：https://cloud.tencent.com/act/pro/featured-202607#MS

## 环境准备

需要 Python 3.10 或更高版本。

```bash
pip install -r requirements.txt
```

该命令仅安装 Python 依赖包，不会下载浏览器。本机已安装 Chrome 或 Edge 时脚本会直接复用，运行日志中会出现 `使用浏览器: 本机 Chrome`。两者均未安装时，需要额外下载一份 Chromium：

```bash
python -m playwright install chromium
```

## 使用方法

双击 `腾讯云服务器抢购助手.bat` 启动，或在命令行执行：

```bash
python seckill_ui.py
```

控制台菜单：

| 选项 | 说明 |
| --- | --- |
| [1] 切换地域 | 上海 / 北京 / 广州 |
| [2] 扫码登录 | 弹出浏览器窗口，扫码后自动保存登录态 |
| [3] 开始抢购 | 自动等待最近一场活动 |
| [4] 选择场次 | 指定 10:00 或 15:00 |
| [5] 测试连通 | 校验登录态，不下单 |
| [6] 测试下单 | 会真实创建订单 |

抢购成功后订单保留 1 小时，需在此期间前往腾讯云控制台完成付款，逾期订单自动关闭。

也可跳过菜单，直接指定场次运行：

```bash
python seckill.py --time 15:00
python seckill.py --test        # 校验接口，会创建真实订单
```

## 工作原理

**时间校准**　读取响应头 Date 字段，采样 7 次取中位数，误差约 ±0.1 秒，开抢前 3 分钟再次校准。

**CSRF Token**　由前端动态生成，无法静态读取。脚本注入 hook 拦截页面 fetch 与 XHR 请求，从真实请求中提取。

**请求发送**　按服务器时间提前 3 秒发起请求，10 并发、间隔 20ms，持续 10 秒。

**登录态校验**　使用 `get-vip-info` 接口，该接口不产生订单，可安全重复调用。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `腾讯云服务器抢购助手.bat` | 一键启动入口 |
| `seckill_ui.py` | 控制台菜单与交互 |
| `seckill.py` | 对时、CSRF 捕获与下单逻辑 |
| `requirements.txt` | Python 依赖 |
| `.state.json` | 登录态，扫码后自动生成，不在版本库里 |
| `config.json` | 地域配置，首次运行后自动生成，不在版本库里 |

## 常见问题

**Q1：提示「未捕获到 CSRF token」**

活动页结构可能已调整，hook 无法取得 Token。可检查 `seckill.py` 中的 `CSRF_HOOK_JS`，或提交 issue 并附上页面截图。

**Q2：更换浏览器会影响登录吗？**

不会。Cookie 从 `.state.json` 读取，经 `new_context(storage_state=...)` 载入一个全新的隔离上下文，不涉及本机日常浏览器的用户数据。

**Q3：提示浏览器无法启动**

本机未检测到 Chrome 或 Edge，执行 `python -m playwright install chromium` 后重试。

**Q4：订单未付款**

订单保留 1 小时，请前往控制台付款或手动取消。

## 免责声明

本工具仅供个人学习与研究使用。自动化抢购可能违反腾讯云活动规则，导致账号限流或功能受限，相关风险由使用者自行承担，请勿用于高频滥用。

## License

MIT