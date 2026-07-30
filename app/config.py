"""集中读取环境变量配置。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Web
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    webhook_secret: str = "CHANGE_ME"
    webhook_header_name: str = ""
    webhook_header_value: str = ""

    # HTTPS 入口
    domain: str = ""
    webhook_port: int = 8443

    # DB
    db_path: str = "./data/monitor.db"

    # TwitterAPI.io
    twitterapi_base_url: str = "https://api.twitterapi.io"
    twitterapi_key: str = ""
    rule_interval_seconds: int = 600

    # Feishu
    feishu_webhook_url: str = ""
    feishu_secret: str = ""
    feishu_alert_webhook_url: str = ""
    feishu_alert_secret: str = ""

    # Apprise（未来扩展）
    apprise_urls: str = ""

    # 按博主路由到不同飞书群（JSON）：
    # [{"handles":["some_handle"],"url":"https://open.feishu.cn/...","secret":"..."}]
    feishu_routes: str = ""

    # 这些博主放宽本地过滤：允许引用+回复（仍不推纯转推）。逗号分隔小写 handle。
    push_allow_reply_quote: str = ""

    # OpenAI（翻译 + AI 总结）
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"
    # 翻译/总结时给 httpx 的可选代理（直连不通时走服务器 xray，如 http://127.0.0.1:xxxx）
    openai_proxy: str = ""
    # AI 总结触发阈值：正文非空白字符数 >此值
    ai_summary_min_chars: int = 100
    # 去重账本上限：tweets 表最多保留多少条（含已推送），超出按时间裁剪
    pushed_retention: int = 5000

    # ---- 昨日信号 早报 ----
    # DeepSeek（全量去噪/分类/主题聚合，便宜）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    # 最终早报写作（用 openai_api_key/base_url）
    report_model: str = "gpt-5.4"
    report_enabled: bool = True
    # 进入 GPT 的重点推文上限（30-80）
    report_select_max: int = 80
    # 趋势记忆回溯天数（连续信号）
    report_trend_days: int = 7
    # 早报触发时间（北京时区小时；北京 9:00）
    report_hour_beijing: int = 9

    # ---- 成本核算 ----
    # 覆盖模型单价（$/1M tokens）JSON：{"gpt-5.4":[in,out],"deepseek-v4-flash":[in,out]}
    llm_prices: str = ""
    twitterapi_tweet_price: float = 0.15   # $/1000 条推文
    twitterapi_profile_price: float = 0.18  # $/1000 个资料
    # 每次规则检查消耗的 credits（实测约 14，可按账单校准）
    twitterapi_check_credits: float = 14.0
    # 1 credit 折算美元（由 $0.15/1000 推文反推 ≈ $0.00001）
    credit_usd: float = 0.00001
    # 成本报告推送间隔（天）
    cost_report_days: int = 7

    # 推送重试
    push_max_retries: int = 5
    push_retry_backoff_minutes: str = "1,5,15,30,60"

    # 定时任务
    backfill_times_per_day: int = 2
    heartbeat_cron_hour: int = 9
    heartbeat_cron_minute: int = 0
    balance_alert_threshold: float = 2.0

    # 日志
    log_level: str = "INFO"

    @property
    def webhook_url(self) -> str:
        port = f":{self.webhook_port}" if self.webhook_port not in (443, 0) else ""
        return f"https://{self.domain}{port}/webhook/twitterapi/{self.webhook_secret}"

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def retry_backoff(self) -> list[int]:
        return [int(x) for x in str(self.push_retry_backoff_minutes).split(",") if x.strip()]

    @property
    def apprise_url_list(self) -> list[str]:
        return [u.strip() for u in self.apprise_urls.split(",") if u.strip()]

    @property
    def relaxed_authors(self) -> set[str]:
        return {h.strip().lstrip("@").lower() for h in self.push_allow_reply_quote.split(",") if h.strip()}

    @property
    def route_map(self) -> dict[str, tuple[str, str]]:
        """handle(小写) -> (webhook_url, secret)。用于按博主分流到不同飞书群。"""
        import json

        out: dict[str, tuple[str, str]] = {}
        if not self.feishu_routes.strip():
            return out
        try:
            for r in json.loads(self.feishu_routes):
                for h in r.get("handles", []):
                    out[h.lstrip("@").lower()] = (r["url"], r.get("secret", ""))
        except Exception:
            pass
        return out

    @property
    def alert_webhook(self) -> str:
        return self.feishu_alert_webhook_url or self.feishu_webhook_url

    @property
    def alert_secret(self) -> str:
        return self.feishu_alert_secret or (self.feishu_secret if not self.feishu_alert_webhook_url else "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
