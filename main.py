# main.py
import os
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---- 环境变量与调试 ----
def _mask(s: str | None) -> str:
    if not s:
        return "None"
    return s[:6] + "..." + s[-4:]

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

# ---- 业务逻辑：中行“美元现汇卖出价”（bocfx 返回单位是每 100 外币）----
from bocfx import bocfx

async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # 正确参数：sort 用 "SE,ASK"（现汇 卖出）
        result = bocfx("USD", "SE,ASK")
        if not result:
            await update.message.reply_text("未获取到汇率数据。")
            return

        # bocfx 返回字符串或数字，表示 CNY / 100 USD
        raw = float(str(result[0]))
        per_usd = raw / 100.0  # 转为 CNY / 1 USD
        await update.message.reply_text(
            f"人民币对美元现汇卖出价：{per_usd:.6f} CNY / 1 USD（牌价：{raw} CNY / 100 USD）"
        )

    except SystemExit:
        # bocfx 在入参非法时会 exit()，这里兜底避免进程退出
        await update.message.reply_text("bocfx 参数异常（已使用 sort='SE,ASK'）。请稍后再试。")
    except Exception as e:
        await update.message.reply_text(f"查询失败：{e}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("你好！发送 /rate 获取人民币对美元的现汇卖出价。")

async def cmd_debug_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "环境检测：\n"
        f"- TELEGRAM_TOKEN 是否存在：{'是' if 'TELEGRAM_TOKEN' in os.environ else '否'}\n"
        f"- TOKEN 是否存在：{'是' if 'TOKEN' in os.environ else '否'}\n"
        f"- BASE_URL 是否存在：{'是' if 'BASE_URL' in os.environ else '否'}\n"
        f"- TOKEN(脱敏)：{_mask(TOKEN)}\n"
        f"- BASE_URL：{BASE_URL or 'None'}\n"
    )

# ---- FastAPI + python-telegram-bot（webhook）----
ptb_app = None  # 延后构建
app = FastAPI()

@asynccontextmanager
async def lifespan(app_fastapi: FastAPI):
    global ptb_app

    if not TOKEN:
        print("!! 未检测到 TOKEN，Bot 不启动。请在 Railway 的 Service → Variables 设置 TELEGRAM_TOKEN（或 TOKEN）。")
        yield
        return

    # 构建应用与命令
    ptb_app = Application.builder().updater(None).token(TOKEN).build()
    ptb_app.add_handler(CommandHandler("start", cmd_start))
    ptb_app.add_handler(CommandHandler("rate", cmd_rate))
    ptb_app.add_handler(CommandHandler("debug_env", cmd_debug_env))

    # 设置 webhook（若缺少 BASE_URL 就不注册，只_
