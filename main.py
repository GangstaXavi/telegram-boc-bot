# main.py
import os
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---- 环境变量 ----
def _mask(s: str | None) -> str:
    if not s:
        return "None"
    return s[:6] + "..." + s[-4:]

# 优先读 TELEGRAM_TOKEN，兜底读 TOKEN
TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TOKEN")
BASE_URL = os.environ.get("BASE_URL")
PORT = int(os.environ.get("PORT", "8000"))

print("=== ENV KEYS AT STARTUP ===")
print("Has TELEGRAM_TOKEN:", "TELEGRAM_TOKEN" in os.environ)
print("Has TOKEN:", "TOKEN" in os.environ)
print("Has BASE_URL:", "BASE_URL" in os.environ)
print("PORT:", PORT)
print("Loaded TOKEN(masked):", _mask(TOKEN))
print("Loaded BASE_URL:", BASE_URL)
print("============================")

# ---- 业务逻辑：中行“美元现汇卖出价” ----
from bocfx import bocfx

async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        result = bocfx("USD", "SE_ASK")  # 现汇卖出价
        if result:
            rate = result[0]
            await update.message.reply_text(f"人民币对美元现汇卖出价：{rate} CNY per USD（BOC）")
        else:
            await update.message.reply_text("未获取到汇率数据。")
    except Exception as e:
        await update.message.reply_text(f"查询失败：{e}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("你好！发送 /rate 获取人民币对美元现汇卖出价。")

async def cmd_debug_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "环境检测：\n"
        f"- TELEGRAM_TOKEN 是否存在：{'是' if 'TELEGRAM_TOKEN' in os.environ else '否'}\n"
        f"- TOKEN 是否存在：{'是' if 'TOKEN' in os.environ else '否'}\n"
        f"- BASE_URL 是否存在：{'是' if 'BASE_URL' in os.environ else '否'}\n"
        f"- TOKEN(脱敏)：{_mask(TOKEN)}\n"
        f"- BASE_URL：{BASE_URL or 'None'}\n"
    )

# ---- FastAPI + PTB（webhook）----
app = FastAPI()
ptb_app = None  # 延后构建，等 lifespan 时再根据 TOKEN 决定

@asynccontextmanager
async def lifespan(app_fastapi: FastAPI):
    global ptb_app

    if not TOKEN:
        print("!! 未检测到 TOKEN，Bot 不启动。请在 Railway 的 Service → Variables 中设置 TELEGRAM_TOKEN（或 TOKEN）。")
        yield
        return

    ptb_app = Application.builder().updater(None).token(TOKEN).build()

    # 只注册合法命令：/rate、/start、/debug_env
    ptb_app.add_handler(CommandHandler("rate", cmd_rate))
    ptb_app.add_handler(CommandHandler("start", cmd_start))
    ptb_app.add_handler(CommandHandler("debug_env", cmd_debug_env))

    # 注册 webhook（若未提供 BASE_URL，则仅启动 bot、不注册 webhook）
    if BASE_URL:
        webhook_url = f"{BASE_URL.rstrip('/')}/webhook/{TOKEN}"
        try:
            await ptb_app.bot.set_webhook(webhook_url)
            print("Webhook 已设置:", webhook_url)
        except Exception as e:
            print("Webhook 设置失败：", e)
    else:
        print("警告：未设置 BASE_URL，未注册 webhook（/webhook/<TOKEN>）。")

    async with ptb_app:
        await ptb_app.start()
        yield
        await ptb_app.stop()

# 重新挂载带 lifespan 的 app，确保只实例化一次
app = FastAPI(lifespan=lifespan)

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if not TOKEN or token != TOKEN:
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
        "TELEGRAM_TOKEN_in_env": "TELEGRAM_TOKEN" in os.environ,
        "TOKEN_in_env": "TOKEN" in os.environ,
        "BASE_URL_in_env": "BASE_URL" in os.environ,
        "PORT": PORT,
        "TOKEN_masked": _mask(TOKEN),
        "BASE_URL": BASE_URL or None,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
