# main.py
import os
import math
from contextlib import asynccontextmanager
from http import HTTPStatus
from urllib.parse import quote, unquote
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters
)

# 第三方依赖
from bocfx import bocfx
import httpx
from bs4 import BeautifulSoup

# ---------- 环境变量 ----------
def _mask(s: str | None) -> str:
    if not s:
        return "None"
    return s[:6] + "..." + s[-4:]

TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TOKEN")
BASE_URL = os.environ.get("BASE_URL")
PORT = int(os.environ.get("PORT", "8000"))

print("=== Boot ===")
print("TELEGRAM_TOKEN set?:", bool(TOKEN))
print("BASE_URL set?:", bool(BASE_URL))
print("PORT:", PORT)
print("================")

# ---------- 常量 & 内存状态 ----------
BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
RATE_TTL = 120  # 秒
PENDING_TTL = 120  # 秒，等待费率输入超时
# 缓存：per_usd(Decimal, 每1USD)、pub_time(str|None)、raw_100(Decimal, 每100USD)
_rate_cache = {"per_usd": None, "pub_time": None, "cached_at": None, "raw_100": None}
# 待费率会话 & 费率记忆
pending_fee = {}   # chat_id -> {"amount_usd": Decimal, "created_at": datetime, "last_fee": Decimal|None}
last_fee_mem = {}  # chat_id -> Decimal

# ---------- 工具函数 ----------
def _clean_number(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.replace(",", "").strip())
    except Exception:
        return None

def _first_number_deep(x):
    """在任意结构中递归提取第一个可转 float 的数；提取不到返回 None"""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x) if math.isfinite(float(x)) else None
    if isinstance(x, str):
        return _clean_number(x)
    if isinstance(x, (list, tuple)):
        for item in x:
            v = _first_number_deep(item)
            if v is not None:
                return v
        return None
    if isinstance(x, dict):
        for v in x.values():
            w = _first_number_deep(v)
            if w is not None:
                return w
    return None

def _fmt_money(d: Decimal, places: int = 4) -> str:
    """金额/汇率统一保留4位小数，带千分位"""
    q = Decimal(10) ** -places
    v = d.quantize(q, rounding=ROUND_HALF_UP)
    return f"{v:,.{places}f}"

def _parse_amount_to_decimal(text: str) -> Decimal | None:
    try:
        t = text.replace(",", "").strip()
        if not t:
            return None
        val = Decimal(t)
        if val <= 0:
            return None
        if val > Decimal("1000000000"):
            return None
        return val
    except InvalidOperation:
        return None

# —— 新增：把带中文单位的金额转成 Decimal（支持：亿/万/千/百/十，支持链式：1万2千3百）——
def _parse_amount_chinese(text: str) -> Decimal | None:
    """
    支持形式：
      - 50万 -> 500000
      - 3.5万 -> 35000
      - 2亿 -> 200000000
      - 1万2千 -> 12000
      - 1万2千3百50 -> 12350
    仅支持阿拉伯数字 + 单位（亿/万/千/百/十）的组合；不解析汉字数字（如“五十万”）。
    """
    import re
    if not text:
        return None
    # 保留数字、点、逗号和单位字符，其余删除（去掉“美元/美金/USD”等）
    cleaned = re.sub(r"[^0-9\.\,亿万千百十]", "", text)
    if not cleaned:
        return None

    # 先尝试纯数字直接转（避免“500000”这类被误判）
    try:
        pure = cleaned.replace(",", "")
        if re.fullmatch(r"\d+(?:\.\d+)?", pure):
            val = Decimal(pure)
            return val if Decimal("0") < val <= Decimal("1000000000") else None
    except Exception:
        pass

    # 解析带单位的片段并求和
    unit_map = {"亿": Decimal("100000000"), "万": Decimal("10000"),
                "千": Decimal("1000"), "百": Decimal("100"), "十": Decimal("10"), "": Decimal("1")}
    # 匹配若干个 “数字 + 可选单位” 片段
    parts = re.findall(r"(\d+(?:\.\d+)?)([亿万千百十]?)", cleaned)
    if not parts:
        return None

    total = Decimal("0")
    try:
        for num_str, unit in parts:
            num = Decimal(num_str)
            total += num * unit_map.get(unit, Decimal("1"))
    except Exception:
        return None

    # 边界保护
    if total <= 0 or total > Decimal("1000000000"):
        return None
    return total.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

def _parse_amount_any(text: str) -> Decimal | None:
    """先按纯数字解析，失败再按中文单位解析"""
    val = _parse_amount_to_decimal(text)
    if val is not None:
        return val
    return _parse_amount_chinese(text)

def _parse_percent_to_decimal(text: str) -> Decimal | None:
    try:
        t = text.replace("%", "").strip()
        val = Decimal(t)
        if val < 0 or val > Decimal("100"):
            return None
        return val
    except InvalidOperation:
        return None

def _now_tz():
    # 中国时间
    return datetime.now(timezone(timedelta(hours=8)))

# ---------- 抓取中行牌价（官方优先，bocfx 兜底），带缓存 ----------
async def fetch_boc_official_usd_se_ask_httpx():
    """
    抓取中国银行官网“美元 现汇卖出价”
    返回： (per_usd(Decimal), pub_time(str|None), raw_100(Decimal)) 或 (None, None, None)
    """
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/122.0.0.0 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=12, headers=headers) as client:
            r = await client.get(BOC_URL)
        text = r.text
        if not text or "<table" not in text:
            r.encoding = "gb18030"
            text = r.text

        soup = BeautifulSoup(text, "lxml")
        tables = soup.find_all("table")
        for table in tables:
            header_tr = table.find("tr")
            if not header_tr:
                continue
            ths = [th.get_text(strip=True) for th in header_tr.find_all(["th", "td"])]
            if not ths:
                continue

            col_ask = None
            col_time = None
            for i, name in enumerate(ths):
                if ("现汇卖出" in name) and col_ask is None:
                    col_ask = i
                if ("发布时间" in name or "发布日期" in name or "Pub" in name) and col_time is None:
                    col_time = i

            if col_ask is None:
                continue

            for tr in table.find_all("tr")[1:]:
                tds = tr.find_all("td")
                if not tds:
                    continue
                cc_name = tds[0].get_text(strip=True) if len(tds) > 0 else ""
                if cc_name and ("美元" in cc_name or "USD" in cc_name.upper()):
                    ask_text = tds[col_ask].get_text(strip=True) if col_ask < len(tds) else ""
                    ask_raw = _clean_number(ask_text)
                    if ask_raw is None:
                        continue
                    raw_100 = Decimal(str(ask_raw))
                    per_usd = (raw_100 / Decimal("100"))
                    pub_time = None
                    if col_time is not None and col_time < len(tds):
                        pub_time = tds[col_time].get_text(strip=True) or None
                    return per_usd, pub_time, raw_100
        return None, None, None
    except Exception as e:
        print("DEBUG fetch_boc_official error:", e)
        return None, None, None

def fetch_bocfx_usd_se_ask():
    """
    bocfx 兜底：返回 (per_usd(Decimal), pub_time(None), raw_100(Decimal)) 或 (None,None,None)
    """
    attempts = [("USD", "SE,ASK"), ("USD,CNY", "SE,ASK"), ("USD", None)]
    for farg, sarg in attempts:
        try:
            res = bocfx(farg, sarg) if sarg else bocfx(farg)
            val = _first_number_deep(res)
            if val is not None and math.isfinite(val):
                raw_100 = Decimal(str(val))
                per_usd = raw_100 / Decimal("100")
                return per_usd, None, raw_100
        except BaseException:
            continue
    return None, None, None

async def get_usd_per_usd_with_cache():
    """返回 (per_usd, pub_time, raw_100)；若缓存过期则刷新。"""
    try:
        if _rate_cache["per_usd"] and _rate_cache["cached_at"]:
            if (_now_tz() - _rate_cache["cached_at"]).total_seconds() < RATE_TTL:
                return _rate_cache["per_usd"], _rate_cache["pub_time"], _rate_cache["raw_100"]

        per_usd, pub_time, raw_100 = await fetch_boc_official_usd_se_ask_httpx()
        if per_usd is None:
            per_usd, pub_time, raw_100 = fetch_bocfx_usd_se_ask()
        if per_usd is not None:
            _rate_cache["per_usd"] = per_usd
            _rate_cache["pub_time"] = pub_time
            _rate_cache["raw_100"] = raw_100
            _rate_cache["cached_at"] = _now_tz()
        return per_usd, pub_time, raw_100
    except Exception as e:
        print("DEBUG get_usd_per_usd_with_cache:", e)
        return None, None, None

# ---------- /rate（含“汇率”别名） ----------
async def cmd_rate_core(update: Update, context: ContextTypes.DEFAULT_TYPE):
    per_usd, pub_time, raw_100 = await get_usd_per_usd_with_cache()
    if per_usd is None:
        await update.message.reply_text("暂时未获取到中国银行牌价。")
        return
    time_str = pub_time if pub_time else "未知"
    msg = (
        f"现汇卖出价（SE,ASK）：{_fmt_money(per_usd)} CNY / 1 USD\n"
        f"牌价：{_fmt_money(raw_100)} CNY / 100 USD\n"
        f"挂牌时间（北京时间，UTC+8）：{time_str}\n"
        f"来源：{BOC_URL}"
    )
    await update.message.reply_text(msg)

async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_rate_core(update, context)

async def alias_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # “汇率”或“/汇率”文本别名
    await cmd_rate_core(update, context)

# ---------- /convert（仅美金->人民币） ----------
async def start_convert_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: Decimal):
    chat_id = update.effective_chat.id
    last = last_fee_mem.get(chat_id)
    pending_fee[chat_id] = {
        "amount_usd": amount,
        "created_at": _now_tz(),
        "last_fee": last,
    }
    if last is not None:
        await update.message.reply_text(
            f"上次费率为 {last}% ，是否沿用？发送“是”直接计算，或发送新的百分比（如 2.3），发送“取消”退出。"
        )
    else:
        await update.message.reply_text("请输入手续费率（百分比）。例如 2.3 表示 2.3%。发送“取消”退出。")

async def cmd_convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/convert 金额（单位：美金）。例如：/convert 500000")
        return
    amount = _parse_amount_to_decimal(args[0])
    if amount is None:
        await update.message.reply_text("请输入合法的金额（仅数字，最大 1e9）。例如：/convert 500000")
        return
    await start_convert_flow(update, context, amount)

async def alias_convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    中文别名：匹配 “兑换 <金额>”，金额可为：
      - 纯数字：500000 / 12345.67
      - 带中文单位：50万 / 3.5万 / 2亿 / 1万2千 等
    """
    import re
    text = (update.message.text or "").strip()
    # 捕获“兑换”后的全部内容，交给解析器处理
    m = re.match(r"^兑换\s*(.+?)\s*$", text)
    if not m:
        await update.message.reply_text("用法：兑换 金额（单位：美金）。例如：兑换 500000 / 兑换 50万")
        return
    token = m.group(1)

    amt = _parse_amount_any(token)
    if amt is None:
        await update.message.reply_text("请输入合法的金额，例如：兑换 500000 或 兑换 50万")
        return

    await start_convert_flow(update, context, amt)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    非命令文本：用于费率输入，也支持“汇率”别名（已在 alias_rate 单独处理）
    """
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # 别名触发：汇率 / /汇率（优先被 alias_rate 的 Regex 捕获，这里兜底）
    if text in {"汇率", "/汇率"}:
        await cmd_rate_core(update, context)
        return

    # 仅在等待费率时处理
    state = pending_fee.get(chat_id)
    if not state:
        return

    if (_now_tz() - state["created_at"]).total_seconds() > PENDING_TTL:
        pending_fee.pop(chat_id, None)
        await update.message.reply_text("已超时取消。请重新发送 /convert 金额。")
        return

    if text in {"取消", "cancel", "Cancel"}:
        pending_fee.pop(chat_id, None)
        await update.message.reply_text("已取消。")
        return

    if text in {"是", "Yes", "yes", "Y", "y"} and state.get("last_fee") is not None:
        fee_pct = state["last_fee"]
    else:
        fee = _parse_percent_to_decimal(text)
        if fee is None:
            await update.message.reply_text("请输入合法的百分比（例如 2.3），或发送“取消”。")
            return
        fee_pct = fee
        last_fee_mem[chat_id] = fee_pct  # 记忆

    amount_usd = state["amount_usd"]
    pending_fee.pop(chat_id, None)

    per_usd, pub_time, raw_100 = await get_usd_per_usd_with_cache()
    if per_usd is None:
        await update.message.reply_text("暂时未获取到中国银行牌价。")
        return

    time_str = pub_time if pub_time else "未知"
    usd = Decimal(amount_usd)

    # 计算（人民币以每1美元价计算）
    cny_no_fee = (usd * Decimal(per_usd)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    fee_ratio = (Decimal(fee_pct) / Decimal("100"))
    fee_cny = (cny_no_fee * fee_ratio).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    fee_usd = (fee_cny / Decimal(per_usd)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    total_cny = (cny_no_fee + fee_cny).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    total_usd = (usd + fee_usd).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    total_rate = (total_cny / usd).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    # A 外发复制版：仅 美金 / 人民币（最终含手续费） / 简洁汇率数字 + 时间 + 来源
    msg_a = (
        f"美金：{_fmt_money(usd)} 美元\n"
        f"人民币：{_fmt_money(total_cny)} 元\n"
        f"使用汇率：{_fmt_money(Decimal(per_usd))}\n"
        f"挂牌时间（北京时间，UTC+8）：{time_str}\n"
        f"来源：{BOC_URL}"
    )
    await update.message.reply_text(msg_a)

    # B 明细版：自用（保持完整细目 + 总额换算汇率）
    msg_b = (
        f"美金：{_fmt_money(usd)} 美元\n"
        f"人民币：{_fmt_money(cny_no_fee)} 元（不含手续费）\n"
        f"手续费：{_fmt_money(fee_usd)} 美元 / { _fmt_money(fee_cny)} 元\n"
        f"合计：{_fmt_money(total_usd)} 美元 / { _fmt_money(total_cny)} 元\n"
        f"使用汇率及时间：{_fmt_money(Decimal(per_usd))}（挂牌时间：{time_str}，来源：{BOC_URL}）\n"
        f"总额换算汇率：{_fmt_money(total_rate)}"
    )
    await update.message.reply_text(msg_b)

# ---------- FastAPI + webhook ----------
ptb_app = None
app = FastAPI()

@asynccontextmanager
async def lifespan(app_fastapi: FastAPI):
    global ptb_app
    if not TOKEN:
        print("!! 未检测到 TELEGRAM_TOKEN，Bot 不启动。")
        yield
        return

    ptb_app = Application.builder().updater(None).token(TOKEN).build()

    # /rate + 中文别名
    ptb_app.add_handler(CommandHandler("rate", cmd_rate))
    ptb_app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^/?\s*汇率\s*$"), alias_rate))

    # /convert + 中文“兑换”
    ptb_app.add_handler(CommandHandler("convert", cmd_convert))
    ptb_app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^兑换\s*\S+.*$"), alias_convert))

    # 费率输入/取消/沿用的自由文本
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if BASE_URL:
        webhook_url = f"{BASE_URL.rstrip('/')}/webhook/{quote(TOKEN, safe='')}"
        try:
            await ptb_app.bot.set_webhook(webhook_url)
            print("Webhook:", webhook_url)
        except Exception as e:
            print("Webhook 设置失败：", e)
    else:
        print("警告：未设置 BASE_URL，未注册 webhook。")

    async with ptb_app:
        await ptb_app.start()
        yield
        await ptb_app.stop()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook/{token:path}")
async def telegram_webhook(token: str, request: Request):
    if not TOKEN or unquote(token) != TOKEN:
        return Response(status_code=HTTPStatus.FORBIDDEN)
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return Response(status_code=HTTPStatus.OK)

@app.get("/")
async def health():
    return {"status": "ok", "has_token": bool(TOKEN), "has_base_url": bool(BASE_URL)}

@app.get("/__env")
async def env_probe():
    return {
        "TELEGRAM_TOKEN_in_env": bool(TOKEN),
        "BASE_URL_in_env": bool(BASE_URL),
        "PORT": PORT,
        "TOKEN_masked": _mask(TOKEN),
        "BASE_URL": BASE_URL or None,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
