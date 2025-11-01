import os
from pydantic import BaseModel
class Settings(BaseModel):
    hl_info_url: str = os.getenv("HL_INFO_URL", "https://api.hyperliquid.xyz/info")
    hl_ws_url: str = os.getenv("HL_WS_URL", "wss://api.hyperliquid.xyz/ws")
    network: str = os.getenv("HL_NETWORK", "mainnet")
    master_wallet: str = os.getenv("HL_MASTER_WALLET_ADDRESS", "")
    api_wallet: str = os.getenv("HL_API_AGENT_WALLET_ADDRESS", "")
    api_privkey: str = os.getenv("HL_API_AGENT_PRIVATE_KEY", "")
    pair_base: str = os.getenv("PAIR_BASE", "HYPE").upper()
    pair_quote: str = os.getenv("PAIR_QUOTE", "USDC").upper()
    threshold_bps: float = float(os.getenv("THRESHOLD_BPS", "3"))
    spike_extra_bps_for_ioc: float = float(os.getenv("SPIKE_EXTRA_BPS_FOR_IOC", "7"))
    leverage: float = float(os.getenv("LEVERAGE", "3"))
    alloc_per_trade_usd: float = float(os.getenv("ALLOC_PER_TRADE_USD", "10"))
    min_order_notional_usd: float = float(os.getenv("MIN_ORDER_NOTIONAL_USD", "10"))
    max_trades_per_min: int = int(os.getenv("MAX_TRADES_PER_MIN_PER_PAIR", "3"))
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() in ("1","true","yes")
    perp_maker_bps: float = 1.5
    perp_taker_bps: float = 4.5
    spot_maker_bps: float = 4.0
    spot_taker_bps: float = 7.0
    pg_dsn: str = f"host={os.getenv('POSTGRES_HOST','db')} port={os.getenv('POSTGRES_PORT','5432')} dbname={os.getenv('POSTGRES_DB','hl_arb')} user={os.getenv('POSTGRES_USER','hluser')} password={os.getenv('POSTGRES_PASSWORD','hlpass')}"
    redis_host: str = os.getenv("REDIS_HOST","redis")
    redis_port: int = int(os.getenv("REDIS_PORT","6379"))
    redis_db: int = int(os.getenv("REDIS_DB","0"))
    redis_password: str = os.getenv("REDIS_PASSWORD","")
    smtp_host: str = os.getenv("SMTP_HOST","")
    smtp_port: int = int(os.getenv("SMTP_PORT","587"))
    smtp_user: str = os.getenv("SMTP_USER","")
    smtp_pass: str = os.getenv("SMTP_APP_PASSWORD","")
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN","")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID","")
    deadman_ms: int = int(float(os.getenv("DEADMAN_SECONDS", "5")) * 1000)
    @property
    def redis_kwargs(self) -> dict:
        kwargs = {
            "host": self.redis_host,
            "port": self.redis_port,
            "db": self.redis_db,
        }
        if self.redis_password:
            kwargs["password"] = self.redis_password
        return kwargs
    @property
    def edges_channel(self) -> str:
        return f"edges:{self.pair_base}:{self.pair_quote}"
settings = Settings()
