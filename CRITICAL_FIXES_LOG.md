# Critical Bug Fixes - Nov 2, 2025

## Issues Identified from Order History

### User Report:
- 2 trades executed but positions not properly closed
- Perp: 3x 0.29 = 0.87 HYPE long remaining
- Spot: 0.58 HYPE remaining
- Telegram showed "Position Closed" but wallet still had open positions

## Root Causes & Fixes

### 🔴 BUG #1: SIZE CALCULATION IGNORING LEVERAGE

**Problem:**
```python
# OLD CODE (WRONG):
target_notional = $12  # alloc_per_trade_usd
perp_size = $12 / $42 = 0.29 HYPE  ❌
spot_size = $12 / $42 = 0.29 HYPE  ❌
```

With 3x leverage:
- Perp: $12 margin → $36 notional (3x leverage)
- Spot: No leverage, but needs to hedge full perp notional
- **Result**: Perp had 3x more exposure than spot hedge!

**Fix** (execution.py:184-207):
```python
# NEW CODE (CORRECT):
target_margin = $12
perp_notional = $12 * 3 = $36  # Account for leverage
perp_size = $36 / $42 = 0.857 HYPE  ✅
spot_size = $36 / $42 = 0.857 HYPE  ✅ (matches perp for proper hedge)
```

**Impact**: Positions now properly hedged 1:1

---

### 🔴 BUG #2: INSUFFICIENT REJECTION DETECTION

**Problem:**
- Old code only checked `response.get("type") != "error"`
- Hyperliquid returns rejected orders with `status: "rejected"` in response data
- One side filled, other rejected → Unhedged position!

**Fix** (execution.py:320-388):
```python
# NEW: More thorough rejection detection
def _is_order_ok(response):
    # Check error type
    if response.get("type") == "error":
        return False
    # Check status in response data
    data = response.get("data", {})
    if isinstance(data, dict):
        statuses = data.get("statuses", [])
        if any(s.get("status") == "rejected" for s in statuses):
            return False
    return True

# If one leg fails, close the successful leg immediately
if not ok and (perp_ok != spot_ok):
    print("⚠️  PARTIAL FILL DETECTED! Closing successful leg...")
    # IOC close to prevent unhedged position
```

**Impact**: Prevents unhedged positions from rejection edge cases

---

### 🔴 BUG #3: FALSE POSITION CLOSING DETECTION

**Problem:**
```python
# OLD CODE (WRONG):
open_orders = info.open_orders(wallet_address)
if not open_orders:
    return {"ok": True}  # Assumes position closed ❌
```

**Issue**:
- ALO orders can expire/cancel without filling
- `open_orders` empty doesn't mean position closed!
- Bot marked position "closed" but orders never filled

**Fix** (execution_alo_close.py:113-159):
```python
# NEW: Check actual position, not just open orders
user_state = info.user_state(wallet_address)

# Check perp position size
perp_position_size = 0.0
for asset_pos in user_state["assetPositions"]:
    perp_position_size = abs(float(pos_data.get("szi", 0)))

# Only consider closed if position size < 0.001
if perp_position_size < 0.001:
    # Cancel any remaining unfilled orders
    if open_orders:
        for order in open_orders:
            ex.cancel(order.get('coin'), order.get('oid'))
    return {"ok": True}
```

**Impact**: Accurate position tracking, no false "closed" statuses

---

## Files Modified

1. **bot/execution.py**
   - Lines 184-207: Fixed size calculation with leverage
   - Lines 320-388: Enhanced rejection detection and partial fill handling

2. **bot/execution_alo_close.py**
   - Lines 113-159: Fixed position verification to check actual balances

---

## Testing Requirements

Before going live:
1. ✅ Verify size calculation: $12 margin × 3x leverage = correct notional
2. ✅ Test rejection handling: Confirm unhedged positions are closed
3. ✅ Test ALO close: Confirm positions only marked closed when actually closed

---

## Expected Results After Fix

**Opening Trade:**
- Perp size: ~0.857 HYPE (was 0.29)
- Spot size: ~0.857 HYPE (was 0.29)
- Perfect 1:1 hedge ✅

**Rejection Handling:**
- If one side rejects → Other side closes immediately with IOC
- No unhedged positions ✅

**Closing Detection:**
- Only marks "closed" when actual position = 0
- Cancels unfilled ALO orders
- Accurate position tracking ✅
