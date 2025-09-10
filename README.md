# Telegram BOC FX Bot

一个查询中国银行实时汇率的 Telegram Bot。  
基于 [bocfx](https://github.com/bobleer/bocfx) 和 `python-telegram-bot`，部署在 Railway。

## 功能
- 输入 `/汇率` → 返回人民币对美元的现汇卖出价
- 输入 `/start` → 欢迎提示

## 部署步骤
1. Fork 本仓库到你的 GitHub。
2. 在 [Railway](https://railway.app) 新建项目，选择 GitHub 部署。
3. 在 Railway **Variables** 添加：
   - `TELEGRAM_TOKEN` = 你的 BotFather Token
   - `BASE_URL` = Railway 自动分配的域名（例如 `https://xxx.up.railway.app`）
4. 部署完成后，在 Telegram 输入 `/汇率` 即可查询。

## 本地测试
```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```
然后可配合 `ngrok` 调试 webhook。
