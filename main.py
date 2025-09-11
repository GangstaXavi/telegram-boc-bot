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
    if x is None:
        return None
    try:
        v = float(str(x).strip())
        if math.isfinite(v):
            return v
    except Exception:
        pass
    if isinstance(x, (list, tuple)):
        for item in x:
            v = _first_number_deep(item)
            if v is not None:
                return v
        return None
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
    url = "https://www.boc.cn/sourcedb/whpj/"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
        r.encoding = r.encoding or "utf-8"

        # === 打印调试信息 ===
        print("=== DEBUG BOC HTML START ===")
        print(r.text[:8000])  # 打印前8000字符
        print("=== DEBUG BOC HTML END ===")

        soup = BeautifulSoup(r.text, "lxml")
        table = soup.find("table")
        if not table:
            return None

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
    except Exception as e:
        print("DEBUG fetch_boc_official error:", e)
        return None

def fetch_bocfx_usd_se_ask():
    attempts = [
        ("USD", "SE,ASK"),
        ("USD,CNY", "SE,ASK"),
        ("USD", None),
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
            last_err = "bocfx SystemExit"
            continue
        except Exception as e:
            last_err = f"bocfx 异常：{e}"
            continue
    return None

# ---------- Telegram 指令 ----------
async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw_100 = fetch_bocfx_usd_se_ask()
        if raw_100 is None:
            raw_100 = await fetch_boc_official_usd_se_ask_httpx()

        if raw_100 is None:
            await update.message.reply_text("未获取到中国银行牌价。")
            return

        per_usd = float(raw_100) / 100.0
        await update.message.reply_text(
            f"人民币对美元现汇卖出价：{per_usd:.6f} CNY / 1 USD\n"
            f"（中行牌价：{raw_100} CNY / 100 USD）"
        )

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
    ptb_app.add_handler(CommandHandler("start", cmd_start))
    ptb_app.add_handler(CommandHandler("rate", cmd_rate))
    ptb_app.add_handler(CommandHandler("debug_env", cmd_debug_env))

    if BASE_URL:
        webhook_url = f"{BASE_URL.rstrip('/')}/webhook/{quote(TOKEN, safe='')}"
        await ptb_app.bot.set_webhook(webhook_url)
        print("Webhook:", webhook_url)
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
