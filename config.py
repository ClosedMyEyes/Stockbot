"""
config.py — Central configuration for the orchestrator.
Edit this file to set your universe, risk params, and connections.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# =============================================================================
# IBKR CONNECTION
# =============================================================================
IBKR_HOST       = "127.0.0.1"
IBKR_PORT       = 7497          # 7497 = TWS paper trading; 7496 = live
IBKR_CLIENT_ID  = 10            # unique client ID for the orchestrator

# =============================================================================
# SIGNALSTACK
# =============================================================================
SIGNALSTACK_WEBHOOK_URL = "https://api.signalstack.com/webhook"
SIGNALSTACK_API_KEY     = "YOUR_SIGNALSTACK_API_KEY"   # set via env var in prod

# =============================================================================
# RISK PARAMETERS
# =============================================================================
MAX_SIMULTANEOUS_POSITIONS  = 6       # max open trades across all strategies at once
DAILY_LOSS_LIMIT_DOLLARS    = 1000.0  # portfolio-level hard stop (sum of all per-strategy DDs)
MAX_POSITIONS_PER_SYMBOL    = 1       # only one strategy may hold a given symbol at a time

# Per-strategy risk settings.
#   risk_per_trade : dollar risk per trade (1R)
#   max_dd         : daily drawdown limit for this strategy; hitting it halts
#                    that strategy for the rest of the session (others keep running)
STRATEGY_RISK: Dict[str, dict] = {
    "gf_small_multi": {"risk_per_trade": 125.0, "max_dd":  750.0},
    "gap_fill_big":   {"risk_per_trade": 245.0, "max_dd":  1000.0},
    "gap_fill_large": {"risk_per_trade":  48.0, "max_dd":  250.0},
    "impulse_short":  {"risk_per_trade":  42.0, "max_dd":  225.0},
    "gap_fill_small": {"risk_per_trade":  67.0, "max_dd":  400.0},
    "orb_short":      {"risk_per_trade":  20.0, "max_dd":  100.0},
}

# Same-symbol conflict priority (index 0 = highest priority).
# When two strategies want the same symbol on the same bar, the one with
# the lower index wins the slot. Existing open positions are never preempted.
STRATEGY_PRIORITY: List[str] = [
    "gf_small_multi",
    "gap_fill_big",
    "gap_fill_large",
    "impulse_short",
    "gap_fill_small",
    "orb_short",
]

# =============================================================================
# SESSION TIMING
# =============================================================================
RTH_START = "09:30"
RTH_END   = "15:59"
EOD_BAR   = "15:59"
PREMARKET_ROUTINE_TIME = "09:15"   # when to run ATR/gap pre-calc

# =============================================================================
# SYMBOL UNIVERSES PER STRATEGY
# Keys must match strategy IDs in strategies/__init__.py
# =============================================================================
STRATEGY_UNIVERSES: Dict[str, List[str]] = {

    # ── orb_short ─────────────────────────────────────────────────────────────
    # Universe rebuilt from backtested S&P 500 screen (April 2026).
    # Core rules:
    #   INCLUDE: Metals/mining (non-oil), casinos, semiconductors, commercial
    #            banks, consumer-disc retail/auto/hotels, internet megacap,
    #            enterprise tech, select comm-services.
    #   EXCLUDE: Energy (all), pharma/biotech, consumer staples, investment
    #            banks, pure SaaS/streaming, industrial machinery, China ADRs.
    #   VALIDATE: backtested stop-rate < ~48% (symbol respects VWAP as resistance).
    #
    # Tier A — directly validated in long backtest (1998-2026):
    #   FCX (+24.5R), LVS (+11.7R), QCOM (+11.3R), WFC (+10.3R), ORCL (+9.9R),
    #   GE (+9.2R), F (+7.8R), AMZN (+5.3R), LOW (+4.5R), GOOGL (+2.5R),
    #   MSFT (+1.2R). Entry-time filter ≤11:00, obs_range_min_pct=0.75 applied.
    #
    # Tier B — S&P 500 screen, same sector/character as Tier A winners.
    #   Require play-money backtest before promoting to full sizing.
    #
    "orb_short": [
        # ── Tier A: proven ─────────────────────────────────────────────────
        "FCX",    # Copper mining — strategy's anchor, +24.5R, 61% WR
        "LVS",    # Casino — VWAP absorption archetype, +11.7R, 61% WR
        "QCOM",   # Semiconductors — cyclical institutional, +11.3R
        "WFC",    # Commercial bank — VWAP-respecting, +10.3R, 56% WR
        "ORCL",   # Enterprise tech — structured opening range, +9.9R
        "GE",     # Aerospace — strong post-2020, +9.2R (monitor for consistency)
        "F",      # Auto — cyclical consumer disc, +7.8R, 50% WR / high avg win
        "AMZN",   # E-commerce megacap — 58% WR, 58% TP rate
        "LOW",    # Home improvement retail — lowest stop rate (22%), +4.5R
        "GOOGL",  # Internet megacap — highest WR in backtest (65%), +2.5R
        "MSFT",   # Tech megacap — consistent, +1.2R, 60% WR

        # ── Tier B: S&P 500 screen, play-money test ────────────────────────
        # Semiconductors — QCOM analogs (cyclical, tangible product, institutional flow)
        "AMD",    # High-beta semis, very high daily volume
        "MU",     # Cyclical DRAM — deep boom/bust cycle, strong absorption setups
        "AVGO",   # Semis/networking chips — large-cap institutional anchor
        "AMAT",   # Semis equipment — cyclical capex proxy, strong vol patterns

        # Auto — F analogs (physical assets, cyclical consumer disc)
        "GM",     # Automobile manufacturer — F analog, high institutional flow

        # Casinos / Leisure — LVS analogs (physical assets, trapped institutional money)
        "MGM",    # Las Vegas casino — direct LVS competitor, same setup character
        "WYNN",   # High-end casino — smaller float, wider ranges

        # Hotels — high institutional participation, real-asset-backed
        "MAR",    # Marriott — large-cap hotel, VWAP-respecting institutional name
        "HLT",    # Hilton — MAR analog

        # Cruise lines — high-beta consumer discretionary
        "CCL",    # Carnival — volatile, deep institutional absorption on gap days
        "RCL",    # Royal Caribbean — CCL analog, slightly higher price

        # Consumer disc retail — LOW analogs (physical store, cyclical)
        "BBY",    # Best Buy — electronics retail, structured opening volume
        "TGT",    # Target — higher beta than WMT (which is excluded as staples)
        "AZO",    # AutoZone — auto-parts retail, less macro-driven than oil names

        # Internet megacap — GOOGL analogs
        "META",   # Social media megacap — GOOG analog, high institutional daily flow

        # Commercial banks — WFC analogs (loan-driven, NOT investment banking)
        "BAC",    # Bank of America — WFC analog, high daily volume
        "COF",    # Capital One — consumer finance, higher beta than WFC
        "JPM",    # JPMorgan — marginal in backtest but high ADV, monitor carefully

        # Financial exchanges — cyclical, structured, not event-driven
        "SCHW",   # Charles Schwab — brokerage/exchange character, high daily volume
        "CME",    # CME Group — exchange, highly institutional, structured vol patterns
    ],

    # ── impulse_short ─────────────────────────────────────────────────────────
    # Expanded from 20 → ~75 symbols. Sectors confirmed positive in backtest:
    # Financials (avg_R 0.469), Materials (0.313), ConsDisc (0.220),
    # Telecom/CommSvcs (0.168), Healthcare (0.125), ETF (0.106), Tech selective (0.047).
    # Dead sectors (Energy, Industrials, ConsStaples) and dead symbols excluded.
    # Bootstrap P(avg_R > 0) = 100% on 5,000 samples across 367-trade dataset.
    # Expected max drawdown: ~8.7R vs 7.5R current (marginal increase).
    # Removed from original list: EMC (delisted), FB (→ META), GOOG (→ GOOGL),
    #   KO and PM (ConsStaples — confirmed dead sector in data).
    "impulse_short": [
        # Financials — best sector, avg_R 0.469
        "AIG", "AXP", "BAC", "BK", "BLK", "BRK.B", "BX",
        "C", "CB", "CME", "COF",
        "GS", "ICE", "JPM", "MA", "MCO", "MET",
        "PGR", "SCHW", "SPGI", "TRV", "V", "WFC",

        # Materials — rank 2, avg_R 0.313
        "APD", "CF", "DD", "DOW", "FCX", "LIN", "MOS", "NEM", "NUE",

        # Consumer Discretionary — most total R, avg_R 0.220
        "ABNB", "AMZN", "AZO", "BBY", "BIDU", "BKNG",
        "CCL", "CMG", "DECK", "DIS", "EBAY", "EXPE",
        "F", "GM", "HD", "LOW", "MAR", "MCD",
        "NKE", "ORLY", "RCL", "SBUX", "TGT", "TJX", "TSLA", "YUM",

        # Telecom / Communication Services — avg_R 0.168
        "ATVI", "CHTR", "CMCSA", "EA", "META", "T", "TMUS", "VZ",

        # Healthcare — avg_R 0.125 (avoid pure biotech: AMGN, GILD excluded)
        "ABBV", "ABT", "BMY", "BSX", "CI", "CVS",
        "ELV", "JNJ", "LLY", "MDT", "MRK", "PFE",
        "SYK", "TMO", "UNH", "ZTS",

        # ETF — small sample but 66.7% WR
        "GLD", "IWM", "PSE", "QQQ", "SPY", "XLF", "XLK",

        # Tech — lowest avg_R of positive sectors (0.047), selective only
        # High-liquidity names only; INTC, IBM, QCOM, CRM excluded (confirmed losers)
        "AAPL", "ADBE", "AMD", "AMAT", "AVGO",
        "CSCO", "GOOGL", "HPQ", "INTU", "MSFT",
        "NOW", "NVDA", "ORCL", "SNPS",
    ],

    # ── gap_fill_large ────────────────────────────────────────────────────────
    # Long-side gap fill. Gap-down 2.5–5%, session_extreme stop, entry ≤ 11:00.
    # Edge comes from EOD runners: institutionally-owned names that gap down on
    # macro/sector news and trend all day when the fill stalls.
    # Filters: gap_atr_ratio 0.6–1.3, gap_vol_ratio_min 1.0, gap_vol_ratio_max 3.5.
    #
    # Selection rules (derived from 1998–2026 backtest):
    #   INCLUDE: Enterprise software, diversified industrials, large-cap biotech/
    #            pharma, big-box retail, copper/materials, markets-facing financials,
    #            auto manufacturers. These produce large EOD runners (avg 3–8R)
    #            when gaps stall — the payoff that drives the strategy's edge.
    #   EXCLUDE: Commodity energy producers (XOM, CVX, COP — gaps reverse too
    #            quickly or not at all), retail banks (C, GS, WFC — credit-driven
    #            gaps chop), pure telecom (VZ — insufficient gap frequency),
    #            consumer staples (gap frequency too low for 2.5% min).
    #   CAUTION: Chinese ADRs (BIDU — valid but lower EOD runner quality).
    #            HAL included but ceiling on EOD size is lower than software names.
    #
    # Proven (backtested 1998–2026, positive total_R):
    "gap_fill_large": [
        # ── Enterprise software / cloud (best subsector: MSFT 7.8R EOD avg, ORCL 5.5R) ──
        "MSFT",   # anchor — 1.92 avg_R, 30% WR, EOD runners avg 7.8R
        "ORCL",   # anchor — 1.97 avg_R, 45% WR, EOD runners avg 5.5R
        "IBM",    # anchor — 1.32 avg_R; gaps on macro/sector not stock-specific
        "CRM",    # proven positive; enterprise SaaS, institutional bid on gap-down days
        "ADBE",   # MSFT/ORCL analog — enterprise creative software, S&P since 1997,
                  # deep institutional ownership, trends hard when gaps stall
        "INTU",   # MSFT analog — financial software, same institutional bid character
        "ADSK",   # ORCL analog — design/engineering software, long S&P history (1989)
        "CSCO",   # proven positive (borderline); high ADV, sector-driven gaps

        # ── Diversified industrials / heavy equipment (CAT 0.75 avg_R, GE 0.61) ──
        "CAT",    # anchor — heavy equipment, gaps on China/PMI news, trends directionally
        "GE",     # anchor (GE Aerospace post-2024 split) — macro-driven, institutional
        "DE",     # CAT direct analog — Deere, agricultural/heavy machinery,
                  # same macro gap drivers (global growth, commodities), S&P since 1957
        "HON",    # GE analog — diversified industrial conglomerate, S&P since 1957,
                  # gaps on macro and sector rotation, institutional participation
        "ETN",    # CAT/GE analog — Eaton, electrical/industrial, S&P since 1957,
                  # cyclical with clean gap-fill character
        "EMR",    # GE analog — Emerson Electric, electrical components, S&P since 1965
        "CMI",    # CAT analog — Cummins, heavy engines/machinery, S&P since 1965

        # ── Large-cap biotech / pharma (AMGN 0.75 avg_R, PFE 1.12 avg_R) ──────────
        "AMGN",   # anchor — 1.13 avg_R, 57% WR; binary-ish gaps that fill or trend hard
        "PFE",    # anchor — 1.12 avg_R, 44% WR; large-cap pharma, defined gap character
        "GILD",   # proven positive post-ATR filter; biotech with clinical/macro gaps
        "BIIB",   # AMGN direct analog — large-cap biotech, S&P since 2003,
                  # gaps on FDA/pipeline news with similar institutional follow-through
        "BMY",    # PFE direct analog — Bristol Myers, large-cap pharma, S&P since 1957,
                  # dividend-paying, institutional, gaps on pipeline/macro

        # ── Big-box / home improvement retail (LOW 0.73 avg_R, WMT 0.92 avg_R) ────
        "LOW",    # anchor — home improvement, strong EOD runners on housing data gaps
        "WMT",    # anchor — big-box, gaps on consumer sentiment/macro
        "TGT",    # LOW/WMT direct analog — Target, home improvement + general retail,
                  # same gap drivers (consumer confidence, same-store sales), S&P since 1957
        "MCD",    # anchor — 0.84 avg_R; QSR gaps on consumer/macro, directional close

        # ── Copper / materials (FCX 0.49 avg_R, highest trade frequency) ───────────
        "FCX",    # anchor — best trade frequency (2.1/yr), gaps on China/commodity data

        # ── Markets-facing financials (MS 1.06 avg_R) ────────────────────────────
        "MS",     # anchor — 1.06 avg_R; IB gaps on markets/rates, trends hard
        "BLK",    # MS analog — BlackRock, asset management, gaps on markets/rates news,
                  # highly institutional, S&P since 2011

        # ── Auto manufacturers (F 0.46 avg_R) ────────────────────────────────────
        "F",      # anchor — cyclical consumer disc, gaps on macro/production data
        "GM",     # F direct analog — General Motors, same gap drivers and character,
                  # S&P since 2013 (sufficient history)

        # ── E-commerce / consumer tech (AMZN 0.35 avg_R) ─────────────────────────
        "AMZN",   # anchor — 0.35 avg_R; institutional megacap, gaps on macro/retail data
        "NFLX",   # anchor — 0.46 avg_R; streaming, gaps on subscriber/macro news

        # ── Other proven (positive in backtest, lower avg_R) ─────────────────────
        "HAL",    # oilfield services (not commodity producer) — 0.18 avg_R; lower EOD
                  # ceiling vs software names but consistently positive
        "HPQ",    # proven positive; hardware, institutional, sector-driven gaps
        "BAC",    # borderline retail bank — 0.27 avg_R; high ADV, monitor carefully
        "JPM",    # borderline — 0.19 avg_R; highest ADV in financials, monitor
        "AAPL",   # proven positive; hardware megacap, 0.12 avg_R, ATR filter critical
        "T",      # proven positive; telecom, 0.77 avg_R but only 0.4 trades/yr
        "OXY",    # E&P energy — borderline (0.23 avg_R); more stock-specific than XOM/CVX

        # ── Chinese ADR — lower conviction ────────────────────────────────────────
        "BIDU",   # proven positive (0.19 avg_R) but lower EOD quality; keep small
    ],
    # ── gap_fill_small ────────────────────────────────────────────────────────
    # Short-side gap fill. Gap-up 2.5–5%, gap_open_buffer stop, entry on first
    # reversal bar ≤ 11:00 (15:30 max scan). Edge comes from fat EOD runners
    # and TP hits: avg EOD close +2.66R, avg TP hit +5.44R across 131 trades.
    #
    # Winner profile (1998–2026 backtest, 131 trades, +102R, −7R max DD):
    #   avg gap_pct: 3.28%  |  avg gap_atr_ratio: 0.354  |  avg bars_to_entry: 2.2
    #   EOD rate: 25%  |  TP rate: 14%  |  WR: 36.6%  |  avg R: +0.78
    #
    # Selection rules:
    #   INCLUDE: High-beta, liquid, institutionally-covered names that gap on
    #            macro/sector news (not company-specific events). The gap must
    #            be driven by overnight sentiment — these fill once RTH order
    #            flow reasserts. Target: avg_gap > 3%, gap_atr_ratio 0.2–0.5.
    #   INCLUDE sectors: Investment banks/brokers (GS, MS archetype), large-cap
    #            biotech (GILD archetype), gaming/leisure (LVS archetype),
    #            high-beta consumer disc (AMZN, NFLX archetype), E&P/oil services
    #            (COP, HAL archetype), volatile tech (CSCO, ORCL archetype).
    #   EXCLUDE: Consumer staples, utilities, REITs, industrial machinery,
    #            China ADRs (after-hours gap risk), defensive healthcare.
    #            Slow-moving names don't gap 2.5%+ often enough on macro news.
    #
    # Proven (backtested 1998–2026, positive total_R, sorted by avg_R):
    "gap_fill_small": [
        # ── Directly backtested winners ────────────────────────────────────────
        # Investment banks / broker-dealers (gap on rates/macro, fill cleanly)
        "GS",     # anchor — 3.08 avg_R, 50% WR; gaps on rates/deal flow, fills hard
        "MS",     # anchor — 0.68 avg_R; same macro gap character as GS

        # Large-cap biotech (gap on FDA/pipeline/macro, strong mean-reversion)
        "GILD",   # anchor — 2.17 avg_R, 50% WR; best single ticker in backtest
        "AMGN",   # anchor — 3.21 avg_R (small sample); biotech gap archetype

        # High-beta consumer discretionary
        "NFLX",   # anchor — 0.69 avg_R, 46% WR; 13 trades, consistent positive
        "AMZN",   # anchor — 0.63 avg_R, 35% WR; institutional megacap
        "LVS",    # anchor — 0.54 avg_R, 31% WR; casino, physical asset-backed
        "DIS",    # anchor — 1.29 avg_R, 50% WR; leisure/media, macro gaps

        # Energy E&P / oil services (gap on commodity/macro news)
        "COP",    # anchor — 1.57 avg_R, 67% WR; E&P, commodity macro gaps
        "HAL",    # anchor — 0.46 avg_R, 33% WR; oil services, sector-driven
        "XOM",    # anchor — 2.73 avg_R (small sample); integrated energy

        # Volatile legacy tech (sector-driven gaps, strong fill character)
        "CSCO",   # anchor — 2.60 avg_R, 33% WR; networking, sector gaps
        "ORCL",   # anchor — 0.36 avg_R, 14% WR; enterprise tech, structured gaps
        "BIDU",   # anchor — 0.42 avg_R, 17% WR; high-beta internet (note: ADR risk)

        # Other proven
        "F",      # anchor — 0.30 avg_R, 42% WR; cyclical auto, macro gaps
        "HPQ",    # anchor — positive; legacy hardware, sector-driven
        "JNJ",    # anchor — 0.34 avg_R, 50% WR; pharma gap archetype (small sample)
        "LOW",    # anchor — positive; home improvement, macro gaps
        "MCD",    # anchor — positive; QSR, consumer sentiment gaps

        # ── S&P 500 screen: investment bank / broker-dealer analogs ────────────
        # GS/MS archetype: gap hard on rates/macro/deal flow, institutional fill.
        # Key trait: NOT commercial banks (loan-driven) — markets-facing P&L.
        "AIG",    # large insurer — macro-sensitive, high institutional, similar gap profile
        "MET",    # MetLife — rates-driven gaps, same fill character as GS
        "PRU",    # Prudential — MET analog, large-cap markets-facing insurer
        "BX",     # Blackstone — asset manager, extremely gap-prone on rates/risk-off
        "AXP",    # American Express — consumer finance, high beta, institutional flow

        # ── S&P 500 screen: large-cap biotech analogs ─────────────────────────
        # GILD/AMGN archetype: binary-ish gaps (FDA/pipeline/macro) that fill or
        # trend hard. Large float required — thin biotechs gap on idio news only.
        "BIIB",   # Biogen — AMGN direct analog, large-cap biotech, S&P since 2003
        "REGN",   # Regeneron — GILD analog, high-priced, institutional biotech
        "VRTX",   # Vertex — large-cap, macro/sector gap character
        "MRNA",   # Moderna — highly volatile biotech, institutional, post-2021 S&P

        # ── S&P 500 screen: casino / gaming analogs ───────────────────────────
        # LVS archetype: physical asset-backed, heavy institutional + retail money,
        # macro/tourism gaps that fill once open-print selling exhausts.
        "MGM",    # MGM Resorts — direct LVS competitor, Las Vegas, same setup character
        "WYNN",   # Wynn Resorts — smaller float, wider ranges, LVS analog

        # ── S&P 500 screen: cruise / leisure (high-beta consumer disc) ─────────
        # Same macro gap driver as LVS/DIS: overnight risk-off → gap → fill.
        "RCL",    # Royal Caribbean — large-cap cruise, institutional, high ADV
        "CCL",    # Carnival — highest ADV in cruise sector, deep institutional absorption

        # ── S&P 500 screen: airlines (macro-driven, high-beta) ─────────────────
        # Gap on fuel prices, travel demand data, macro risk-off — fill on open print.
        "DAL",    # Delta — largest US airline by revenue, highest institutional ownership
        "UAL",    # United — DAL analog, high daily volume

        # ── S&P 500 screen: E&P / oil services analogs ─────────────────────────
        # COP/HAL archetype: commodity-macro gaps on oil price / rig count / inventory.
        "DVN",    # Devon Energy — E&P, COP analog, S&P since 2000
        "EOG",    # EOG Resources — E&P, high institutional, same macro gap drivers
        "BKR",    # Baker Hughes — oil services, direct HAL competitor/analog

        # ── S&P 500 screen: high-beta internet / consumer disc ─────────────────
        # AMZN/NFLX archetype: institutional megacap, gaps on macro/sector,
        # strong fill when consumer sentiment / risk appetite reasserts.
        "BKNG",   # Booking Holdings — travel/consumer disc, high beta, institutional
        "EBAY",   # eBay — e-commerce, high beta, macro gap character like AMZN
    ],
    # ── gap_fill_small_multi ──────────────────────────────────────────────────
    # Same short-side gap-fill strategy as gap_fill_small but allows multiple
    # trades per day. Expanded universe adds semi, bank, industrial, and
    # consumer-tech analogs screened from the S&P 500.
    "gap_fill_small_multi": [
        # ── Directly backtested winners (same core as gap_fill_small) ──────────
        # Investment banks / broker-dealers
        "GS",     # anchor — 3.08 avg_R, 50% WR; gaps on rates/deal flow, fills hard
        "MS",     # anchor — 0.68 avg_R; same macro gap character as GS

        # Large-cap biotech
        "GILD",   # anchor — 2.17 avg_R, 50% WR; best single ticker in backtest
        "AMGN",   # anchor — 3.21 avg_R (small sample); biotech gap archetype

        # High-beta consumer discretionary
        "NFLX",   # anchor — 0.69 avg_R, 46% WR; 13 trades, consistent positive
        "AMZN",   # anchor — 0.63 avg_R, 35% WR; institutional megacap
        "LVS",    # anchor — 0.54 avg_R, 31% WR; casino, physical asset-backed
        "DIS",    # anchor — 1.29 avg_R, 50% WR; leisure/media, macro gaps

        # Energy E&P / oil services
        "COP",    # anchor — 1.57 avg_R, 67% WR; E&P, commodity macro gaps
        "HAL",    # anchor — 0.46 avg_R, 33% WR; oil services, sector-driven
        "XOM",    # anchor — 2.73 avg_R (small sample); integrated energy

        # Volatile legacy tech
        "CSCO",   # anchor — 2.60 avg_R, 33% WR; networking, sector gaps
        "ORCL",   # anchor — 0.36 avg_R, 14% WR; enterprise tech, structured gaps
        "BIDU",   # anchor — 0.42 avg_R, 17% WR; high-beta internet (note: ADR risk)

        # Other proven
        "F",      # anchor — 0.30 avg_R, 42% WR; cyclical auto, macro gaps
        "HPQ",    # anchor — positive; legacy hardware, sector-driven
        "JNJ",    # anchor — 0.34 avg_R, 50% WR; pharma gap archetype (small sample)
        "LOW",    # anchor — positive; home improvement, macro gaps
        "MCD",    # anchor — positive; QSR, consumer sentiment gaps

        # ── Proven in backtest — previously omitted ────────────────────────────
        "BAC",    # Bank of America — 17 trades, +18.4R, 53% WR; top commercial bank
        "C",      # Citigroup — 30 trades, +16.1R, 43% WR; global macro exposure
        "WFC",    # Wells Fargo — 18 trades, +2.7R; marginal but positive, high ADV

        "INTC",   # Intel — 9 trades, +4.8R, 33% WR; legacy semis, sector gaps
        "QCOM",   # Qualcomm — 34 trades, +9.1R, 56% WR; mobile/5G, cyclical macro

        "CAT",    # Caterpillar — 6 trades, +1.9R; heavy machinery, China/PMI gaps
        "GE",     # GE Aerospace — 18 trades, +7.4R, 39% WR; industrial macro gaps

        "AAPL",   # Apple — 23 trades, +6.3R, 43% WR; hardware megacap, macro gaps
        "T",      # AT&T — 5 trades, +2.9R, 60% WR; telecom, rates-driven gaps

        # ── S&P 500 screen: investment bank / broker-dealer analogs ────────────
        "AIG",    # large insurer — macro-sensitive, high institutional, similar gap profile
        "MET",    # MetLife — rates-driven gaps, same fill character as GS
        "PRU",    # Prudential — MET analog, large-cap markets-facing insurer
        "BX",     # Blackstone — asset manager, extremely gap-prone on rates/risk-off
        "AXP",    # American Express — consumer finance, high beta, institutional flow

        # ── S&P 500 screen: large-cap biotech analogs ─────────────────────────
        "BIIB",   # Biogen — AMGN direct analog, large-cap biotech, S&P since 2003
        "REGN",   # Regeneron — GILD analog, high-priced, institutional biotech
        "VRTX",   # Vertex — large-cap, macro/sector gap character
        "MRNA",   # Moderna — highly volatile biotech, institutional, post-2021 S&P

        # ── S&P 500 screen: casino / gaming analogs ───────────────────────────
        "MGM",    # MGM Resorts — direct LVS competitor, Las Vegas, same setup character
        "WYNN",   # Wynn Resorts — smaller float, wider ranges, LVS analog

        # ── S&P 500 screen: cruise / leisure ──────────────────────────────────
        "RCL",    # Royal Caribbean — large-cap cruise, institutional, high ADV
        "CCL",    # Carnival — highest ADV in cruise sector, deep institutional absorption

        # ── S&P 500 screen: airlines ──────────────────────────────────────────
        "DAL",    # Delta — largest US airline by revenue, highest institutional ownership
        "UAL",    # United — DAL analog, high daily volume

        # ── S&P 500 screen: E&P / oil services analogs ─────────────────────────
        "DVN",    # Devon Energy — E&P, COP analog, S&P since 2000
        "EOG",    # EOG Resources — E&P, high institutional, same macro gap drivers
        "BKR",    # Baker Hughes — oil services, direct HAL competitor/analog

        # ── S&P 500 screen: high-beta internet / consumer disc ─────────────────
        "BKNG",   # Booking Holdings — travel/consumer disc, high beta, institutional
        "EBAY",   # eBay — e-commerce, high beta, macro gap character like AMZN

        # ── S&P 500 screen: semiconductor analogs (multi only) ─────────────────
        # INTC/QCOM archetype: highly cyclical, institutional coverage, gap on
        # China trade / PMI / memory-cycle news — not product launches.
        "MU",     # Micron — DRAM cycle, extreme beta, gap-prone on memory price data
        "AMD",    # Advanced Micro Devices — INTC direct competitor, very high beta
        "AMAT",   # Applied Materials — semis equipment, CAT-like cyclical behavior
        "ADI",    # Analog Devices — QCOM analog, industrial/auto semis, institutional
        "NVDA",   # Nvidia — megacap semis, enormous ADV, macro/AI-sentiment gaps

        # ── S&P 500 screen: commercial bank analogs (multi only) ───────────────
        # BAC/C archetype: loan-book P&L, rates-driven gaps, institutional fill.
        "JPM",    # JP Morgan — highest ADV in sector, borderline markets-facing
        "COF",    # Capital One — consumer finance, higher beta than WFC, high vol
        "USB",    # US Bancorp — mid-size commercial bank, BAC analog, institutional

        # ── S&P 500 screen: cyclical industrial analogs (multi only) ───────────
        # CAT/GE archetype: heavy equipment, global growth/China macro sensitivity.
        "DE",     # Deere — agricultural/construction machinery, direct CAT analog
        "HON",    # Honeywell — diversified industrial conglomerate, GE analog
        "MMM",    # 3M — industrial conglomerate, S&P since 1957, low idio gap risk
        "ETN",    # Eaton — electrical industrial, CAT/GE character, macro-driven

        # ── S&P 500 screen: consumer tech / internet megacap (multi only) ──────
        "META",   # Meta Platforms — internet megacap, AMZN/NFLX analog, very high ADV
        "TSLA",   # Tesla — consumer disc/auto, extreme beta, high institutional flow
    ],

    # ── gap_fill_big ──────────────────────────────────────────────────────────
    # Gap-up fade short. Gaps 1.5–5%, gap_atr_ratio 0.7–1.0, stop at
    # session open + 0.1%, entry on first reversal bar ≤ 11:00.
    #
    # Selection criteria:
    #   INCLUDE: Low-beta, old-economy, dividend-paying. Gap-ups are
    #            market-driven (sympathy with SPY), not stock-specific news.
    #            Trade frequency ≤ ~18 qualifying gaps in test window.
    #   EXCLUDE: Big tech, investment banks, pharma/biotech, commodity
    #            exploration, high-momentum names, beta > 1.1.
    #
    # Tier A — directly validated (1998-2026 backtest, 1.0 ATR band):
    #   T (+9.8R, 75% WR), F (+18.9R), LOW (+11.1R), CVX (+12.2R),
    #   CSCO (+4.7R), MCD (+8.7R), HAL (+8.0R), AMGN (+3.8R),
    #   HD (+5.5R), HPQ (+3.4R), IBM (+2.2R), KO (+2.1R),
    #   AMZN (+3.2R), COP (+2.7R), MS (+2.1R, marginal — monitor).
    #
    # Tier B — S&P 500 screen, same sector/character as Tier A.
    #   Require paper-money backtest before full sizing.
    #   Priority sectors: Utilities (highest conviction), Consumer Staples
    #   expansion, Necessity Retail, Refiners, Legacy Tech, Auto.
    #
    "gap_fill_big": [
        # ── Tier A: proven (original backtest) ─────────────────────────────
        "T",      # Telecom — highest WR (75%), +9.8R, very low beta
        "F",      # Auto — highest total R (+18.9R), cyclical consumer disc
        "LOW",    # Home improvement retail — +11.1R, 63.6% WR
        "CVX",    # Integrated energy major — +12.2R, 50% WR
        "CSCO",   # Legacy/infrastructure tech — +4.7R, 60% WR
        "MCD",    # Consumer staples — highest WR in set (81.8%), +8.7R
        "HAL",    # Energy services — +8.0R, 50% WR (not exploration)
        "AMGN",   # Biotech — +3.8R, 62.5% WR; biotech exception (large-cap, mean-reverting)
        "HD",     # Home improvement retail — +5.5R, 57.1% WR
        "HPQ",    # Legacy tech (hardware) — +3.4R
        "IBM",    # Legacy enterprise tech — +2.2R
        "KO",     # Consumer staples — +2.1R, 57.1% WR
        "AMZN",   # E-commerce — +3.2R; fires rarely = gap is unusual = reverts
        "COP",    # Integrated energy — +2.7R (not pure exploration)
        "MS",     # Finance — +2.1R, marginal; monitor stop rate closely

        # ── Tier B: Utilities — highest-conviction additions ────────────────
        # Textbook low-beta, yield-driven, mean-reverting. A 1.5% gap on
        # NEE or DUK is genuinely anomalous — almost always market-driven.
        "NEE",    # NextEra Energy — largest US utility, S&P 500 core
        "DUK",    # Duke Energy — regulated electric, very stable cash flows
        "SO",     # Southern Company — regulated multi-state utility
        "AEP",    # American Electric Power — high-dividend, low volatility
        "XEL",    # Xcel Energy — clean energy utility, low beta
        "ED",     # Consolidated Edison — NYC utility, 50+ year dividend history

        # ── Tier B: Consumer Staples expansion ─────────────────────────────
        # MCD and KO proven. Same character: non-cyclical, dividend, gap-ups
        # are market-wide noise that fade once RTH order flow reasserts.
        "COST",   # Costco — large-cap staples retail, very low beta
        "GIS",    # General Mills — packaged foods, steady dividend
        "CL",     # Colgate-Palmolive — household products, near-zero beta
        "CLX",    # Clorox — household products, historically mean-reverting
        "CHD",    # Church & Dwight — consumer staples, low float turnover
        "CPB",    # Campbell's — packaged foods, very slow-moving
        "SYY",    # Sysco — food distribution, defensive, institutional

        # ── Tier B: Necessity Retail ────────────────────────────────────────
        # LOW and HD proven. Same physical-store, non-momentum character.
        "TGT",    # Target — broadline retail, higher beta than WMT (excluded)
        "KR",     # Kroger — grocery, near-staples character, very low beta
        "DG",     # Dollar General — necessity retail, defensive

        # ── Tier B: Legacy / Infrastructure Tech ────────────────────────────
        # CSCO, IBM, HPQ proven. "Boring" tech: dividend, old economy,
        # gaps are market-driven not product-launch/earnings-driven.
        "TXN",    # Texas Instruments — analog chips, near-utility character
        "GLW",    # Corning — specialty glass/fiber, old economy materials-tech
        "HPE",    # Hewlett Packard Enterprise — HPQ analog (spun off 2015)
        "ADI",    # Analog Devices — TXN analog, low beta semis

        # ── Tier B: Refiners (Integrated Energy, not Exploration) ───────────
        # CVX and COP proven. Refiners are margin-driven, not oil-price-driven
        # in the same binary way as exploration names (OXY, FCX excluded).
        "PSX",    # Phillips 66 — refining/midstream, low correlation to oil
        "MPC",    # Marathon Petroleum — largest US refiner by capacity
        "VLO",    # Valero — pure-play refiner, mean-reverting character

        # ── Tier B: Auto expansion ──────────────────────────────────────────
        # F proven. Physical-asset, cyclical consumer disc, not momentum.
        "GM",     # General Motors — F analog, institutional ownership driven
    ],
}

# =============================================================================
# STRATEGY PARAMETERS
# These mirror the argparse defaults in the original scripts.
# Override per-strategy here; the strategy class reads from this dict.
# =============================================================================
STRATEGY_PARAMS: Dict[str, dict] = {
    "orb_short": {
        "observe_bars":         15,
        "vol_delta_min":        2.0,
        "vol_down_min_pct":     3.5,
        "vwap_entry_pct":       0.20,
        "vwap_drift_min_pct":   0.3,
        "vwap_drift_max_pct":   1.5,
        "obs_range_min_pct":    0.75,   # upgraded from 0.0 — enforces TP >= ~1R
        "retest_timeout":       0,
        "tp_mode":              "obs_level",
        "tp_mult":              3.5,
        "sl_buffer_pct":        0.8,
        "min_r_pct":            0.20,
        "slippage_pct":         0.08,
        "entry_gap_max_pct":    0.15,
        "entry_time_min":       "09:55",
        "entry_time_max":       "11:00",   # upgraded from 14:00 — late entries drag EV
        "skip_monday":          True,
        "skip_friday":          True,
        "skip_months":          [8, 9],
        "vol_regime_min":       0.6,
        "vol_regime_max":       1.6,
        "trail_activation_r":   0,
        "trail_lock_r":         0,
        "hold_cap_bars":        0,
        "hold_cap_exit_r":      -0.2,
    },
    "impulse_short": {
        "atr_impulse_mult":         0.9,
        "impulse_size_pct_min":     0.7,
        "impulse_min_bars":         0,
        "deep_retrace_min":         0.60,
        "deep_retrace_max":         0.95,
        "retest_pct":               0.10,
        "retest_max_bars":          15,
        "stop_buffer_mult":         0.10,
        "tp_mode":                  "fixed",
        "tp_fixed_mult":            1.0,
        "min_r_pct":                0.08,
        "slippage_pct":             0.05,
        "breakout_ibs_min":         0.95,
        "min_failure_body_pct":     85,
        "max_pullback_vol_ratio":   1.8,
        "breakout_min":             0.75,
        "entry_time_start":         "09:30",
        "entry_time_end":           "15:55",
        "ema_period":               20,
        "ema_slope_bars":           0,
    },
    "gap_fill_large": {
        "gap_min_pct":              2.5,
        "gap_max_pct":              5.0,
        "direction":                "long",
        "gap_fill_target_pct":      3.0,
        "stop_type":                "session_extreme",
        "sl_buffer_pct":            0.15,
        "entry_time_max":           "11:00",
        "entry_gap_max_pct":        0.10,
        "max_bars_to_entry":        3,
        "min_gap_fill_at_entry":    -0.05,
        "min_r_pct":                0.10,
        "slippage_pct":             0.08,
        "hold_cap_bars":            0,
        "hold_cap_exit_r":          -0.3,
        "skip_monday":              False,
        "skip_friday":              False,
        "skip_months":              [],
        "vol_regime_min":           0.5,
        "vol_regime_max":           99.0,
        "gap_vol_ratio_min":        1.0,
        "gap_vol_ratio_max":        3.5,    # cap panic/news gaps; 0 = off
        "gap_atr_ratio_min":        0.6,
        "gap_atr_ratio_max":        1.3,    # script default; filters structurally huge gaps
    },
    "gap_fill_small": {
        "gap_min_pct":              2.5,
        "gap_max_pct":              5.0,
        "direction":                "short",
        "gap_fill_target_pct":      3.0,
        "stop_type":                "gap_open_buffer",
        "sl_buffer_pct":            0.1,
        "entry_time_max":           "15:30",
        "entry_gap_max_pct":        0.15,
        "max_bars_to_entry":        0,
        "min_gap_fill_at_entry":    0.2,
        "min_r_pct":                0.10,
        "slippage_pct":             0.08,
        "hold_cap_bars":            0,
        "hold_cap_exit_r":          -0.3,
        "skip_monday":              False,
        "skip_friday":              False,
        "skip_months":              [],
        "vol_regime_min":           0.2,
        "vol_regime_max":           3.0,
        "gap_vol_ratio_min":        0,
        "gap_atr_ratio_min":        0.2,
        "gap_atr_ratio_max":        0.5,
    },
    # gap_fill_small_multi: same logic, multiple trades per day allowed,
    # expanded universe (see STRATEGY_UNIVERSES above).
    "gap_fill_small_multi": {
        "gap_min_pct":              2.0,
        "gap_max_pct":              5.0,
        "direction":                "short",
        "gap_fill_target_pct":      3.0,
        "stop_type":                "gap_open_buffer",
        "sl_buffer_pct":            0.1,
        "entry_time_max":           "09:59",
        "entry_gap_max_pct":        0.15,
        "max_bars_to_entry":        5,
        "min_gap_fill_at_entry":    0.4,
        "min_r_pct":                0.10,
        "slippage_pct":             0.08,
        "hold_cap_bars":            0,
        "hold_cap_exit_r":          -0.3,
        "skip_monday":              False,
        "skip_friday":              False,
        "skip_months":              [],
        "vol_regime_min":           0.2,
        "vol_regime_max":           3.0,
        "gap_vol_ratio_min":        0,
        "gap_atr_ratio_min":        0.2,
        "gap_atr_ratio_max":        0.5,
        # ── Session management (multi-trade) ──────────────────────────────────
        "max_trades_per_session":   0,              # 0 = unlimited; cap to 2 or 3 to limit exposure
        "reentry_cooldown_bars":    5,              # bars to wait after any exit before re-scanning
        "reentry_min_gap_fill":     0.4,            # stricter fill % required for trade 2+ (stop has expanded)
        "reentry_stop_type":        "session_extreme",  # "inherit" | "session_extreme" | "gap_open_buffer"
    },
    "gap_fill_big": {
        "gap_min_pct":              1.5,
        "gap_max_pct":              5.0,
        "direction":                "short",
        "gap_fill_target_pct":      3.0,
        "stop_type":                "gap_open_buffer",
        "sl_buffer_pct":            0.1,
        "entry_time_max":           "11:00",
        "entry_gap_max_pct":        0.15,
        "max_bars_to_entry":        0,
        "min_gap_fill_at_entry":    0.2,
        "min_r_pct":                0.10,
        "slippage_pct":             0.08,
        "hold_cap_bars":            0,
        "hold_cap_exit_r":          -0.3,
        "skip_monday":              False,
        "skip_friday":              False,
        "skip_months":              [],
        "vol_regime_min":           0.7,
        "vol_regime_max":           3.0,
        "gap_vol_ratio_min":        0,
        "gap_atr_ratio_min":        0.7,
        "gap_atr_ratio_max":        1.0,
    },
}

# =============================================================================
# LOGGING
# =============================================================================
LOG_DIR              = "logs"
TRADE_LOG_CSV        = "logs/trade_log.csv"
SIGNAL_LOG_CSV       = "logs/signal_log.csv"
CONFLICT_LOG_CSV     = "logs/conflict_log.csv"
DAILY_SUMMARY_CSV    = "logs/daily_summary.csv"

# =============================================================================
# DASHBOARD
# =============================================================================
DASHBOARD_PORT   = 8050      # local Dash/Flask port
DASHBOARD_ENABLE = True
