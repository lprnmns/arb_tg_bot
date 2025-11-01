"""
Runtime configuration manager.

Allows changing bot settings on-the-fly without restarting.
Settings are stored in Redis for persistence.
"""

import json
from typing import Optional, Dict, Any
from redis import Redis

from .config import settings


class RuntimeConfig:
    """Manages runtime configuration with Redis persistence."""

    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        self.prefix = "runtime_config:"
        self._cache = {}

    def get(self, key: str, default: Any = None) -> Any:
        """Get a runtime config value, falling back to default settings."""
        # Check cache first
        if key in self._cache:
            return self._cache[key]

        # Check Redis
        redis_key = f"{self.prefix}{key}"
        value = self.redis.get(redis_key)

        if value is not None:
            # Deserialize
            try:
                parsed = json.loads(value)
                self._cache[key] = parsed
                return parsed
            except (json.JSONDecodeError, TypeError):
                # Try as plain string
                self._cache[key] = value
                return value

        # Fall back to default from settings
        if hasattr(settings, key):
            return getattr(settings, key)

        return default

    def set(self, key: str, value: Any) -> None:
        """Set a runtime config value."""
        redis_key = f"{self.prefix}{key}"

        # Serialize
        if isinstance(value, (dict, list)):
            serialized = json.dumps(value)
        elif isinstance(value, bool):
            serialized = json.dumps(value)
        elif isinstance(value, (int, float)):
            serialized = json.dumps(value)
        else:
            serialized = str(value)

        # Save to Redis
        self.redis.set(redis_key, serialized)

        # Update cache
        self._cache[key] = value

    def delete(self, key: str) -> None:
        """Delete a runtime config value (falls back to default)."""
        redis_key = f"{self.prefix}{key}"
        self.redis.delete(redis_key)
        self._cache.pop(key, None)

    def get_all(self) -> Dict[str, Any]:
        """Get all runtime config values."""
        result = {}

        # Get all runtime keys from Redis
        pattern = f"{self.prefix}*"
        for key in self.redis.scan_iter(match=pattern):
            config_key = key.decode() if isinstance(key, bytes) else key
            config_key = config_key.replace(self.prefix, "")
            result[config_key] = self.get(config_key)

        return result

    def reset_all(self) -> None:
        """Reset all runtime config to defaults."""
        pattern = f"{self.prefix}*"
        for key in self.redis.scan_iter(match=pattern):
            self.redis.delete(key)
        self._cache.clear()


# Global instance (initialized in runner.py)
_runtime_config: Optional[RuntimeConfig] = None


def init_runtime_config(redis_client: Redis) -> RuntimeConfig:
    """Initialize the global runtime config instance."""
    global _runtime_config
    _runtime_config = RuntimeConfig(redis_client)
    return _runtime_config


def get_runtime_config() -> Optional[RuntimeConfig]:
    """Get the global runtime config instance."""
    return _runtime_config


# Trading state management
class TradingState:
    """Manages the bot's trading state (running/stopped)."""

    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        self.state_key = "bot:trading_state"
        self.last_edges_key = "bot:last_edges"

    def is_running(self) -> bool:
        """Check if trading is enabled."""
        value = self.redis.get(self.state_key)
        if value is None:
            # Default: STOPPED (user must manually start trading)
            self.stop()
            return False
        return value.decode() == "running" if isinstance(value, bytes) else value == "running"

    def start(self) -> None:
        """Enable trading."""
        self.redis.set(self.state_key, "running")

    def stop(self) -> None:
        """Disable trading."""
        self.redis.set(self.state_key, "stopped")

    def get_state(self) -> str:
        """Get current state."""
        return "running" if self.is_running() else "stopped"

    def update_edges(self, ps_mm: float, sp_mm: float, mid_ref: float) -> None:
        """Update last seen edges."""
        data = {
            "ps_mm": ps_mm,
            "sp_mm": sp_mm,
            "mid_ref": mid_ref,
            "timestamp": __import__("time").time()
        }
        self.redis.set(self.last_edges_key, json.dumps(data))

    def get_last_edges(self) -> Optional[Dict[str, float]]:
        """Get last seen edges."""
        value = self.redis.get(self.last_edges_key)
        if value:
            try:
                if isinstance(value, bytes):
                    value = value.decode()
                return json.loads(value)
            except (json.JSONDecodeError, AttributeError):
                return None
        return None


_trading_state: Optional[TradingState] = None


def init_trading_state(redis_client: Redis) -> TradingState:
    """Initialize the global trading state instance."""
    global _trading_state
    _trading_state = TradingState(redis_client)
    return _trading_state


def get_trading_state() -> Optional[TradingState]:
    """Get the global trading state instance."""
    return _trading_state
