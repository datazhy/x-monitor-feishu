"""FastAPI 应用：HTTPS webhook 接收端 + 健康检查 + 内嵌调度。

设计要点（对应方案“必加保险”）：
- webhook 收到后先入库再返回 2xx，飞书推送异步后台做，避免重试风暴。
- delivery_id 幂等防 webhook 重试；tweet_id 唯一索引防重复推送。
- 路径里带长随机 secret；可选再校验自定义请求头。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException, Request, Response

from . import db, webhook
from .config import get_settings
from .scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=get_settings().log_level,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("app.main")

app = FastAPI(title="X 推文监控 -> 飞书推送", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    start_scheduler()
    log.info("服务启动完成")


@app.on_event("shutdown")
def _shutdown() -> None:
    stop_scheduler()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/webhook/twitterapi/{secret}")
async def receive_webhook(secret: str, request: Request) -> Response:
    settings = get_settings()

    # 1. 路径 secret 校验
    if secret != settings.webhook_secret or settings.webhook_secret == "CHANGE_ME":
        raise HTTPException(status_code=404, detail="not found")

    # 2. 可选自定义请求头校验
    if settings.webhook_header_name:
        if request.headers.get(settings.webhook_header_name.lower()) != settings.webhook_header_value:
            raise HTTPException(status_code=401, detail="bad header")

    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    delivery_id = webhook.extract_delivery_id(headers, raw_body)

    # 3. delivery 幂等：重复投递直接 200，不再处理
    is_first = db.record_delivery(delivery_id, status="received")
    if not is_first:
        log.info("重复投递 %s，跳过", delivery_id)
        return Response(status_code=200, content="duplicate")

    # 4. 解析 + 入库（tweet_id 去重），尽快返回
    tweets = webhook.parse_tweets(raw_body)
    new_count = 0
    for t in tweets:
        try:
            if db.insert_tweet(t):
                new_count += 1
        except Exception:
            log.exception("入库失败 tweet=%s", t.get("tweet_id"))

    db.update_delivery(delivery_id, status="processed", tweet_count=new_count)
    db.log_event("webhook", True, f"delivery={delivery_id} 新增 {new_count}/{len(tweets)} 条")
    log.info("webhook %s: 收到 %d 条，新增 %d 条", delivery_id, len(tweets), new_count)

    # 推送由后台 scheduler 异步处理 -> 这里立即返回 2xx
    return Response(status_code=200, content="ok")
