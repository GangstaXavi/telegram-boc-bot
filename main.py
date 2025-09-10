import os
print("=== DEBUG: ALL ENV VARS ===")
for k, v in os.environ.items():
    print(f"{k}={v}")
print("=== END DEBUG ===")

TOKEN = os.environ.get("TELEGRAM_TOKEN")
BASE_URL = os.environ.get("BASE_URL")
PORT = int(os.environ.get("PORT", "8000"))

print("DEBUG TELEGRAM_TOKEN:", TOKEN)
print("DEBUG BASE_URL:", BASE_URL)

# main.py
import os
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# 导入 bocfx
from bocfx import bocfx

# 环境变量
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BASE_URL = os.environ.get("BASE_URL")
PORT = int(os.environ.get("PORT", "8000"))

if not TOKEN:
    raise RuntimeError("请设置 TELEGRAM_TOKEN 环境变量")

# 初始化 telegram bot 应用
ptb_app = Application.builder().updater(None).token(TOKEN).build()

# /start 指令
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("欢迎！发送 /汇率 获取人民币对美元现汇卖出价。")

# /汇率 指令
async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # 使用 bocfx 获取人民币对美元现汇卖出价
        result = bocfx("USD", "SE_ASK")
        if result:
            rate = result[0]
            await update.message.reply_text(f"人民币对美元现汇卖出价：{rate} CNY per USD")
        else:
            await update.message.reply_text("未获取到汇率数据。")
    except Exception as e:
        await update.message.reply_text(f"查询失败：{e}")

# 注册指令
ptb_app.add_handler(CommandHandler("start", cmd_start))
ptb_app.add_handler(CommandHandler("汇率", cmd_rate))

# FastAPI
app = FastAPI()

@asynccontextmanager
async def lifespan(app_fastapi: FastAPI):
    if BASE_URL:
        webhook_url = f"{BASE_URL.rstrip('/')}/webhook/{TOKEN}"
        await ptb_app.bot.set_webhook(webhook_url)
        print("Webhook 已设置:", webhook_url)
    async with ptb_app:
        await ptb_app.start()
        yield
        await ptb_app.stop()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != TOKEN:
        return Response(status_code=HTTPStatus.FORBIDDEN)
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return Response(status_code=HTTPStatus.OK)

@app.get("/")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
