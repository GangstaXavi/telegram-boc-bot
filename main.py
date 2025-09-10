# main.py
import os
import math
from contextlib import asynccontextmanager
from http import HTTPStatus
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# 第三方
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

# ---------- 工具函数 ----------
def _first_number_deep(x):
    """在任意结构中递归提取第一个可转 float 的数；提取不到返回 None"""
    if x is None:
        return None
    # 直接是数字或可转成数字的字符串
    try:
        v = float(str(x).strip())
        if math.isfinite(v):
            return v
    except Exception:
        pass
    # list/tuple
    if isinstance(x, (list, tuple)):
        for item in x:
            v = _first_number_deep(item)
            if v is not None:
                return v
        return None
    # dict：优先常见键，其次遍历所有值
    if isinstance(x, dict):
        preferred = ["SE,ASK", "SE_ASK", "现汇卖出", "现汇 卖出", "SEASK", "SE-ASK"]
        for k in preferred:
            if k in x:
                v = _first_number_deep(x[k])
                if v is not None:
                    return v
        for v in x.values():
            w = _first_number_deep(v)
            if w is not None:
                return w
    return None

async def fetch_boc_official_usd_se_ask_httpx():
    """
    兜底：直接抓中国银行官网 https://www.boc.cn/sourcedb/whpj/
    返回 CNY/100 USD 的浮点数 或 None
    """
    url = "https://www.boc.cn/sourcedb/whpj/"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
        r.encoding = r.encoding or "utf-8"
        soup = BeautifulSoup(r.text, "lxml")

        table = soup.find("table")
        if not table:
            return None

        # 表头：定位“现汇卖出价”的列
        header_tr = table.find("tr")
        ths = [th.get_text(strip=True) for th in header_tr.find_all(["th", "td"])]
        candidates = ["现汇卖出价", "现汇卖出", "卖出价"]
        se_ask_col = None
        for i, name in enumerate(ths):
            if any(key in name for key in candidates):
                se_ask_col = i
                break
        if se_ask_col is None:
            return None

        # 查找“美元”一行
        for tr in table.find_all("tr")[1:]:
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if not tds:
                continue
            first_col = tds[0]
            if "美元" in first_col or "USD" in first_col.upper():
                val_text = tds[se_ask_col]
                if not val_text or val_text in ("-", "—", "–"):
                    return None
                return float(val_text)
        return None
    except Exception:
        return None

def fetch_bocfx_usd_se_ask():
    """
    先尝试用 bocfx 取“美元 现汇卖出价”，返回 CNY/100 USD 的数或 None。
    多路尝试 + 结构自适应解析，不抛 IndexError。
    """
    attempts = [
        ("USD", "SE,ASK"),      # 常规：现汇 卖出
        ("USD,CNY", "SE,ASK"),  # 某些部署需要显式币对
        ("USD", None),          # 不传 sort，返回全集，从结构中抽取
    ]
    last_err = None
    for farg, sarg in attempts:
        try:
            res = bocfx(farg, sarg) if sarg else bocfx(farg)
            val = _first_number_deep(res)
            if val is not None:
                return val
            last_err = f"bocfx 返回不可解析：{type(res).__name__}"
        except SystemExit:
            last_err = "bocfx SystemExit（参数不被接受）"
            continue
        except Exception as e:
            last_err = f"bocfx 异常：{e}"
            continue
    return None

# ---------- Telegram 指令 ----------
async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # 1) 先用 bocfx
        raw_100 = fetch_bocfx_usd_se_ask()

        # 2) bocfx 失败则兜底抓 BOC 官网（保证与中行牌价一致）
        if raw_100 is None:
            raw_100 = await fetch_boc_official_usd_se_ask_httpx()

        if raw_100 is None:
            await update.message.reply_text("未获取到中国银行牌价。")
            return

        # 中行单位：CNY / 100 USD → 同时回显原牌价
        per_usd = float(raw_100) / 100.0
        await update.message.reply_text(
            f"人民币对美元现汇卖出价：{per_usd:.6f} CNY / 1 USD\n"
            f"（中行牌价：{raw_100} CNY / 100 USD）"
        )

    except SystemExit:
        await update.message.reply_text("bocfx 参数异常（已做兼容重试），请稍后再试。")
    except Exception as e:
        await update.message.reply_text(f"查询失败：{e}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("你好！发送 /rate 获取人民币对美元的现汇卖出价。")

async def cmd_debug_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "环境检测：\n"
        f"- TELEGRAM_TOKEN：{'已设置' if TOKEN else '未设置'}\n"
        f"- BASE_URL：{BASE_URL or '未设置'}\n"
        f"- TOKEN(脱敏)：{_mask(TOKEN)}\n"
    )

# ---------- FastAPI + PTB（webhook，含 token : 编码修复） ----------
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
    ptb_app.add_handler(CommandHandler("start", cmd_start))
    ptb_app.add_handler(CommandHandler("rate", cmd_rate))
    ptb_app.add_handler(CommandHandler("debug_env", cmd_debug_env))

    if BASE_URL:
        webhook_url = f"{BASE_URL.rstrip('/')}/webhook/{quote(TOKEN, safe='')}"
        try:
            await ptb_app.bot.set_webhook(webhook_url)
            print("Webhook:", webhook_url)
        except Exception as e:
            print("Webhook 设置失败：", e)
    else:
        print("警告：未设置 BASE_URL，未注册 webhook（/webhook/<TOKEN>）。")

    async with ptb_app:
        await ptb_app.start()
        yield
        await ptb_app.stop()

# 只实例化一次
app = FastAPI(lifespan=lifespan)

# 注意允许 path 形式并 unquote，匹配 %3A
@app.post("/webhook/{token:path}")
async def telegram_webhook(token: str, request: Request):
    if not TOKEN:
        return Response(status_code=HTTPStatus.FORBIDDEN)
    incoming = unquote(token)
    if incoming != TOKEN:
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
