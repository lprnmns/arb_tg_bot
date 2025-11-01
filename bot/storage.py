import psycopg2
from .config import settings
def pg_conn():
    return psycopg2.connect(settings.pg_dsn)
def insert_edge(ts, base, spot_index, ps_mm_bps, sp_mm_bps, mid_ref, recv_ms, send_ms):
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO edges (ts, base, spot_index, edge_ps_mm_bps, edge_sp_mm_bps, mid_ref, recv_ms, send_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (ts, base, spot_index, ps_mm_bps, sp_mm_bps, mid_ref, recv_ms, send_ms)
        )
def insert_trade(ts, base, direction, threshold_bps, mm_best_bps, notional_usd, role, request_id, request_json, response_json, status):
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trades (ts, base, direction, threshold_bps, mm_best_bps, notional_usd, role, request_id, request_json, response_json, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (ts, base, direction, threshold_bps, mm_best_bps, notional_usd, role, request_id, request_json, response_json, status)
        )
        return cur.fetchone()[0]

def insert_position(opened_at, base, direction, open_edge_bps, perp_size, spot_size, perp_entry_px, spot_entry_px, timeout_seconds, trade_id=None):
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO positions
            (opened_at, base, direction, open_edge_bps, perp_size, spot_size, perp_entry_px, spot_entry_px, status, timeout_seconds, trade_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (opened_at, base, direction, open_edge_bps, perp_size, spot_size, perp_entry_px, spot_entry_px, 'OPEN', timeout_seconds, trade_id)
        )
        return cur.fetchone()[0]

def get_open_positions():
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, opened_at, base, direction, open_edge_bps, perp_size, spot_size, perp_entry_px, spot_entry_px, timeout_seconds FROM positions WHERE status = 'OPEN'")
        return cur.fetchall()

def close_position(position_id, closed_at, close_edge_bps, perp_exit_px, spot_exit_px, realized_pnl):
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE positions
            SET closed_at = %s, close_edge_bps = %s, perp_exit_px = %s, spot_exit_px = %s, realized_pnl = %s, status = 'CLOSED'
            WHERE id = %s""",
            (closed_at, close_edge_bps, perp_exit_px, spot_exit_px, realized_pnl, position_id)
        )
