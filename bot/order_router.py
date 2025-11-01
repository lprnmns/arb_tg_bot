"""Helpers for posting Hyperliquid orders via HTTP.

These utilities isolate the logic for resolving asset indices, building
order payloads and submitting sequential actions (perp first, spot next).

Usage is intentionally minimal so strategy code can call
``place_two_legs`` with already sized orders.
"""

from __future__ import annotations

import httpx
from eth_account import Account

from hyperliquid.info import Info
from hyperliquid.utils.signing import get_timestamp_ms, sign_l1_action

from .config import settings


BASE_URL = settings.hl_info_url.replace("/info", "")
EXCHANGE_URL = f"{BASE_URL}/exchange"


def resolve_indices(base: str = settings.pair_base, quote: str = settings.pair_quote) -> tuple[int, int]:
    """Return (perp_asset, spot_asset) indices for the requested pair."""

    info = Info(BASE_URL, skip_ws=True)
    perp_asset = info.name_to_asset.get(base)
    spot_symbol = f"{base}/{quote}"
    spot_asset = info.name_to_asset.get(spot_symbol)

    if perp_asset is None or spot_asset is None:
        raise RuntimeError(f"Could not resolve asset indices for {base}/{quote}")

    return perp_asset, spot_asset


def build_order(asset: int, is_buy: bool, limit_px: str, size: str, tif: str = "Alo", reduce_only: bool = False, cloid: str | None = None) -> dict:
    order = {
        "a": int(asset),
        "b": bool(is_buy),
        "p": str(limit_px),
        "s": str(size),
        "r": bool(reduce_only),
        "t": {"limit": {"tif": tif}},
    }
    if cloid:
        order["c"] = cloid
    return order


def post_action(wallet: Account, action: dict, is_mainnet: bool, expires_after: int | None = None) -> dict:
    nonce = get_timestamp_ms()
    signature = sign_l1_action(wallet, action, None, nonce, expires_after, is_mainnet)
    body = {
        "action": action,
        "nonce": nonce,
        "signature": signature,
        "vaultAddress": None,
        "expiresAfter": expires_after,
    }
    with httpx.Client(timeout=5.0) as client:
        resp = client.post(EXCHANGE_URL, json=body)
        resp.raise_for_status()
        return resp.json()


def place_two_legs(wallet: Account, is_mainnet: bool, perp_orders: list[dict] | None, spot_orders: list[dict] | None) -> tuple[dict | None, dict | None]:
    """Submit perp orders first, then spot orders. Return both responses."""

    perp_resp = spot_resp = None
    if perp_orders:
        perp_action = {"type": "order", "orders": perp_orders, "grouping": "na"}
        perp_resp = post_action(wallet, perp_action, is_mainnet)
    if spot_orders:
        spot_action = {"type": "order", "orders": spot_orders, "grouping": "na"}
        spot_resp = post_action(wallet, spot_action, is_mainnet)
    return perp_resp, spot_resp
