# main.py
import os
from contextlib import asynccontextmanager
from http import HTTPStatus
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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

# ---- 业务逻辑：中行“美元现汇卖出价”（bocfx 返回单位：每100 USD）----
from bocfx import bocfx

def _try_pick_number(x):
    """从 bocfx 返回的数据结构里尽力取出一个数字（字符串或数字都可）"""
    if x is None:
        return None
    # 直接就是数字或可转浮点的字符串
    try:
        return float(str(x))
    except Exception:
        pass
    # 列表/元组：找第一个可转浮点的元素
    if isinstance(x, (list, tuple)):
        for item in x:
            try:
                return float(str(item))
            except Exception:
                continue
        return None
    # 字典：尝试常见 key
    if isinstance(x, dict):
        candidate_keys = ["SE,ASK", "SE_ASK", "SE ASK", "SEASK", "SE-ASK"]
        for k in candidate_keys:
            v = x.get(k)
            try:
                return float(str(v))
            except Exception:
                continue
        # 再兜底遍历所有值
        for v in x.values():
            try:
                return float(str(v))
            except Exception:
                continue
    return None

async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # 依次尝试几种最常见/最合理的调用方式
        attempts = [
            ("USD", "SE,ASK"),      # 首选：现汇 卖出
            ("USD,CNY", "SE,ASK"),  # 有些版本需要显式给出两个币种
            ("USD", None),          # 不传 sort（bocfx 可能返回全部四种）
        ]
        raw = None
        last_err = None

        for farg, sarg in attempts:
            try:
                res = bocfx(farg, sarg) if sarg else bocfx(farg)
                raw = _try_pick_number(res)
                if raw is not None:
                    break
            except SystemExit:
                # bocfx 内部对参数不合法会 exit，这里吞掉继续尝试下一种
                last_err = "bocfx SystemExit"
                continue
            except Exception as e:
                last_err = str(e)
                continue

        if raw is None:
            msg = "未获取到汇率数据。"
            if last_err:
                msg += f"（{last_err}）"
            await update.message.reply_text(msg)
            return

        # bocfx/中行单位为每 100 USD 的人民币价 → 换算为“每 1 USD”
        per_usd = raw / 100.0
        await update.message.reply_text(
            f"人民币对美元现汇卖出价：{per_usd:.6f} CNY / 1 USD（牌价：{raw} CNY / 100 USD）"
        )

    except SystemExit:
        # 再兜底一次，避免把进程干掉
        await update.message.reply_text("bocfx 参数异常（已做兼容重试），请稍后再试。")
    except Exception as e:
        await update.message.reply_text(f"查询失败：{e}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("你好！发送 /rate 获取人民币对美元的现汇卖出价。")

async def cmd_debug_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "环境检测：\n"
        f"- TELEGRAM_TOKEN 存在：{'是' if TOKEN else '否'}\n"
        f"- BASE_URL 存在：{'是' if BASE_URL else '否'}\n"
        f"- TOKEN(脱敏)：{_mask(TOKEN)}\n"
        f"- BASE_URL：{BASE_URL or 'None'}\n"
    )

# ---------- FastAPI + python-telegram-bot（webhook） ----------
ptb_app = None
app = FastAPI()

@asynccontextmanager
async def lifespan(app_fastapi: FastAPI):
    global ptb_app

    if not TOKEN:
        print("!! 未检测到 TELEGRAM_TOKEN，Bot 不启动。请在 Railway Service → Variables 设置。")
        yield
        return

    ptb_app = Application.builder().updater(None).token(TOKEN).build()
    ptb_app.add_handler(CommandHandler("start", cmd_start))
    ptb_app.add_handler(CommandHandler("rate", cmd_rate))
    ptb_app.add_handler(CommandHandler("debug_env", cmd_debug_env))

    # 注册 webhook：对 token 做 URL 编码，避免 ':' → '%3A' 导致路由不匹配
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

# 重新挂载带 lifespan 的 app
app = FastAPI(lifespan=lifespan)

# 注意这里允许 path 形式并在对比前 unquote，从而识别 %3A
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
