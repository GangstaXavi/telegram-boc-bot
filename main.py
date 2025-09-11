# main.py
import os
import math
from contextlib import asynccontextmanager
from http import HTTPStatus
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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

# ---------- 工具函数 ----------
def _clean_number(text: str | None) -> float | None:
    """将形如 '1,234.56' 的文本安全转成 float；失败返回 None"""
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
    # 直接数/可转数字的字符串
    if isinstance(x, (int, float)):
        return float(x) if math.isfinite(float(x)) else None
    if isinstance(x, str):
        return _clean_number(x)
    # list/tuple
    if isinstance(x, (list, tuple)):
        for item in x:
            v = _first_number_deep(item)
            if v is not None:
                return v
        return None
    # dict：遍历所有值
    if isinstance(x, dict):
        for v in x.values():
            w = _first_number_deep(v)
            if w is not None:
                return w
    return None

async def fetch_boc_official_usd_se_ask_httpx() -> float | None:
    """
    直接抓取中国银行官网 https://www.boc.cn/sourcedb/whpj/
    返回 CNY/100 USD 的浮点数；失败返回 None
    """
    url = "https://www.boc.cn/sourcedb/whpj/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=12, headers=headers) as client:
            r = await client.get(url)
        # 编码兜底：优先服务器声明；若缺失则假设 utf-8；解析失败再回退 gb18030
        text = r.text
        if not text or ("<table" not in text and r.encoding and r.encoding.lower() != "gb18030"):
            r.encoding = "gb18030"
            text = r.text

        soup = BeautifulSoup(text, "lxml")

        # 可能存在多个表，逐一定位包含“现汇卖出”的表
        tables = soup.find_all("table")
        for table in tables:
            header_tr = table.find("tr")
            if not header_tr:
                continue
            ths = [th.get_text(strip=True) for th in header_tr.find_all(["th", "td"])]
            if not ths:
                continue

            # 找到“现汇卖出价”列号（名称有时显示为“现汇卖出”）
            col_index = None
            for i, name in enumerate(ths):
                if "现汇卖出" in name:
                    col_index = i
                    break
            if col_index is None:
                continue

            # 遍历数据行，第一列一般为货币名称
            for tr in table.find_all("tr")[1:]:
                tds = tr.find_all("td")
                if not tds:
                    continue
                first_col = tds[0].get_text(strip=True)
                if not first_col:
                    continue
                if ("美元" in first_col) or ("USD" in first_col.upper()):
                    val_text = tds[col_index].get_text(strip=True) if col_index < len(tds) else ""
                    val = _clean_number(val_text)
                    if val is not None:
                        return val
        return None
    except Exception as e:
        print("DEBUG fetch_boc_official error:", e)
        return None

def fetch_bocfx_usd_se_ask() -> float | None:
    """
    用 bocfx 获取 CNY/100 USD；拦截一切异常（含 SystemExit），失败返回 None
    """
    attempts = [
        ("USD", "SE,ASK"),
        ("USD,CNY", "SE,ASK"),
        ("USD", None),
    ]
    for farg, sarg in attempts:
        try:
            res = bocfx(farg, sarg) if sarg else bocfx(farg)
            val = _first_number_deep(res)
            if val is not None and math.isfinite(val):
                return float(val)
        except BaseException:
            # 注意：SystemExit/KeyboardInterrupt 继承自 BaseException，不会漏网
            continue
    return None

# ---------- Telegram 指令 ----------
async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # 先走官方页面（确保与中行牌价一致），再退回 bocfx
        raw_100 = await fetch_boc_official_usd_se_ask_httpx()
        if raw_100 is None:
            raw_100 = fetch_bocfx_usd_se_ask()

        if raw_100 is None:
            await update.message.reply_text("未获取到中国银行牌价。")
            return

        # 单位：CNY / 100 USD → 同时回显原牌价
        per_usd = float(raw_100) / 100.0
        await update.message.reply_text(
            f"人民币对美元现汇卖出价：{per_usd:.6f} CNY / 1 USD\n"
            f"（中行牌价：{raw_100} CNY / 100 USD）"
        )

    except BaseException as e:
        # 防御性兜底：任何异常（含 SystemExit）都不让它 500
        try:
            await update.message.reply_text(f"查询失败：{e}")
        except Exception:
            pass

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
