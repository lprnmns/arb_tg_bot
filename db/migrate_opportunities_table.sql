-- Migration: Add opportunities table for volatility tracking and strategy testing
-- Run this on existing database: psql -h localhost -U hl_arb_user -d hl_arb_db -f migrate_opportunities_table.sql

-- Opportunity tracking table for volatility analysis and strategy testing
CREATE TABLE IF NOT EXISTS opportunities (
  id BIGSERIAL PRIMARY KEY,

  -- Timing
  detected_at TIMESTAMPTZ NOT NULL,
  detection_latency_ms INTEGER,

  -- Edge and market prices
  edge_bps DOUBLE PRECISION NOT NULL,
  perp_bid DOUBLE PRECISION NOT NULL,
  perp_ask DOUBLE PRECISION NOT NULL,
  spot_bid DOUBLE PRECISION NOT NULL,
  spot_ask DOUBLE PRECISION NOT NULL,

  -- Baseline (20-tick rolling average)
  baseline_perp_bid DOUBLE PRECISION NOT NULL,
  baseline_perp_ask DOUBLE PRECISION NOT NULL,
  baseline_spot_bid DOUBLE PRECISION NOT NULL,
  baseline_spot_ask DOUBLE PRECISION NOT NULL,

  -- Deviations from baseline (in bps)
  perp_bid_deviation_bps DOUBLE PRECISION NOT NULL,
  perp_ask_deviation_bps DOUBLE PRECISION NOT NULL,
  spot_bid_deviation_bps DOUBLE PRECISION NOT NULL,
  spot_ask_deviation_bps DOUBLE PRECISION NOT NULL,

  -- Movement analysis (absolute movement in bps)
  perp_movement_bps DOUBLE PRECISION NOT NULL,
  spot_movement_bps DOUBLE PRECISION NOT NULL,

  -- Volatility classification
  volatility_source TEXT NOT NULL,  -- 'PERP', 'SPOT', or 'BOTH'
  volatility_ratio DOUBLE PRECISION NOT NULL,  -- primary / secondary movement ratio

  -- Strategy simulations
  cost_ioc_both DOUBLE PRECISION NOT NULL,  -- Current strategy cost
  cost_ioc_perp_alo_spot DOUBLE PRECISION NOT NULL,  -- If PERP-driven
  cost_ioc_spot_alo_perp DOUBLE PRECISION NOT NULL,  -- If SPOT-driven

  -- Expected profits (edge - cost)
  expected_profit_ioc_both DOUBLE PRECISION NOT NULL,
  expected_profit_adaptive DOUBLE PRECISION NOT NULL,  -- Best strategy

  -- Performance metrics
  analysis_duration_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS opportunities_detected_at_idx ON opportunities (detected_at);
CREATE INDEX IF NOT EXISTS opportunities_edge_bps_idx ON opportunities (edge_bps);
CREATE INDEX IF NOT EXISTS opportunities_volatility_source_idx ON opportunities (volatility_source);
