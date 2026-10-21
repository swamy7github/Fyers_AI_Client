# Ghanshyam Tech Intraday Option-Buying Framework — Rule Extraction

Source: `Ghanshyam Tech Options Trading Guide.pdf` (14 pages, repo root).
Page numbers below are the PDF page numbers. Numbers that the PDF renders as
equation images (dropped by text extraction) were read from rasterised pages
and are marked `[img]`.

Everything in `ghanshyam_options/` implements the rules in this file; the
YAML key that controls each rule is given in the right-hand column so nothing
is hard-coded in strategy code.

---

## 1. Foundations (p1–p2)

| # | Rule | Page | Config key |
|---|------|------|------------|
| F1 | Intraday **option buying only**, index options (Nifty 50, Bank Nifty). No overnight carry. | p1, p12 | `risk.square_off` per session |
| F2 | Edge comes from synchronising **spot velocity with gamma**; typical holding window **3–15 minutes**; cut quickly if the move does not come (theta). | p1 | `strategies.*.max_hold_minutes` |
| F3 | Framework discards lagging indicators in favour of **raw price action** at inflection points (stop clusters / liquidity vacuums). | p1 | — (no indicator-based entries) |
| F4 | **Never buy OTM.** Execute only **ATM or slightly ITM** strikes with initial **delta ≥ 0.50** `[img p2]`. | p2 | `strike.mode` (`ATM_ITM` / `ITM1`), `strike.min_delta` |

## 2. Structural mapping — LDCHL (Last Day Close / High / Low) (p2–p3)

| # | Rule | Page | Config key |
|---|------|------|------------|
| S1 | Mark **PDH, PDL, PDC** before the open (08:45–09:14). | p2, p9 | computed from previous session bars |
| S2 | **PDH**: CE buying on confirmed *multi-minute acceptance above*; PE buying on clear *upper-wick rejection*. | p2 | `strategies.ldchl_levels` |
| S3 | **PDL**: PE buying on *sustained breakdown*; CE buying on *lower-wick rejection* (liquidity absorption). | p2 | `strategies.ldchl_levels` |
| S4 | **PDC = primary trend filter**: sustained trading above PDC → **CE-only** bias; below → **PE-only** bias. | p3 | `bias.acceptance_bars`, per-strategy `require_bias_alignment` |
| S5 | **Opening 15-minute range** = intraday volatility corridor; boundary breaches dictate continuation scalps. | p3 | `strategies.congestion_base.use_opening_range` |
| S6 | **Intraday congestion base**: horizontal consolidation of **≥ 20–45 minutes**; entry strictly on boundary violation **with volume confirmation**. | p3 | `strategies.congestion_base` (`min_base_minutes`, `volume_factor`) |
| S7 | Above PDC: Calls only, on pullbacks into support or breakouts above local highs. Below PDC: Puts only on retests of supply / breakdowns. | p3 | bias filter |
| S8 | **Equilibrium trap**: price trapped in the narrow band between PDC and PDH (or PDL) → **no trade**. | p3 | `bias.trap_band_points` (per instrument) |
| S9 | Bank Nifty breakouts must be confirmed by synchronised momentum in **HDFC Bank and ICICI Bank**; divergence ⇒ low-probability trap. | p3, p10–11 | `instruments.BANKNIFTY.constituents`, `confluence.constituent_confirmation` (soft filter; needs constituent bars) |

## 3. Setups (p4–p7). Timeframes: 5m primary, 15m for continuation, 1m/5m for 15:00.

### 3.1 Inside Candle Compression (p4, p6)
* Mother candle followed by an inside candle whose **entire high–low range is contained** in the mother's range.
* 5m = scalp, 15m = trend continuation.
* Uptrend (bias up): CE the instant a subsequent candle **breaches the inside-candle high** (conservative: mother high). Downtrend: PE when inside-candle low is violated.
* SL = **opposite extreme of the inside candle** (BN: typically 25–40 pts of risk).
* Target = **1.5× to 2.5× the pattern range height** `[img p4]`; matrix says R:R "1 : 1.5 to 1 : …" (second number truncated in the PDF) `[img p6]`.
* Config: `strategies.inside_candle` (`timeframe`, `trigger: inside|mother`, `target_range_multiple: 2.0`, `require_bias_alignment: true`).

### 3.2 Bullish Hammer (p4, p6)
* Pre-condition: prior decline of **≥ 7–8 consecutive bearish 5m candles**, OR an uncorrected slide of **150–200 pts (BN) / 70–100 pts (Nifty)**.
* Must form **at a major structural level** (PDL, multi-day support, lower channel boundary).
* Morphology: **lower shadow ≥ 2× real body**; green body preferred but secondary.
* Entry: breach of the **hammer high**. SL: **1–2 pts below the hammer low**. Target: minimum **1:2** R:R `[img p6: "1:2 to 1:…"]`.
* Config: `strategies.hammer` (`min_consecutive`, `min_run_points` per instrument, `wick_body_ratio: 2.0`, `sl_buffer_points`, `target_r: 2.0`, `level_tolerance_points`).

### 3.3 Bearish Shooting Star (p4–p5, p6)
* Mirror of 3.2: prior rally of 7–8 consecutive bullish candles or 150–200+ pts (BN) **into resistance / PDH**.
* **Upper shadow ≥ 2× body**, minimal or no lower shadow.
* Entry: ATM PE on breach of the **star low**. Invalidation: spot above the **wick high** (SL 1–2 pts above). Target ≥ 1:2.
* Config: `strategies.shooting_star` (same keys as hammer, plus `max_lower_wick_ratio`).

### 3.4 09:20 Morning Range Breakout (p5, p6, p10)
* At 09:20:00 mark the **high/low of the 09:15–09:20 candle**. No orders during 09:15–09:20.
* If the opening candle is **wider than 200–250 pts (BN)** the setup is **invalidated** (range exhaustion).
* Enter in the breakout direction the moment the **09:20 candle breaches either boundary**.
* SL = **opposite extreme** (narrow range) or **50% midpoint** (wider range). Target: **40–80 spot points** rapid scalp `[img p6]`.
* Config: `strategies.morning_0920` (`max_range_points`, `wide_range_points` (midpoint SL threshold), `target_points`, `window`).

### 3.5 14:20 Trend Resumption / Afternoon Pulse (p5, p6, p11)
* 11:30–13:30 is a decay window: **no new entries** (see 5.3). Monitor the **13:30–14:15 consolidation base**.
* Evaluate the close of the **14:15–14:20 candle**: if it **breaches the midday band** (or retests key S/R), buy at the **14:20 bar open**.
* SL = midday-base swing high/low. R:R given as "1 : …" (number truncated) `[img p6]` → configurable, default 1:2.
* Config: `strategies.afternoon_1420` (`base_start: "13:30"`, `base_end: "14:15"`, `trigger_bar: "14:15"`, `target_r`).

### 3.6 15:00 Liquidity Sweep & Squeeze (p5, p6, p11)
* Primarily **expiry sessions**. Mark high/low of **14:30–14:58** (1m/5m chart).
* At the **14:59/15:00 open**, breakout scalp on boundary breach. SL = **midpoint** of the 14:30–14:58 base.
* Pure gamma scalp, target **15–30 % of premium** `[img p6]`, held **3–10 minutes**, squared off well before the close.
* Config: `strategies.squeeze_1500` (`expiry_days_only`, `base_start`, `base_end`, `target_premium_pct: 0.20`, `max_hold_minutes: 10`).

### 3.7 Trendline Break & Retest (p5, p6–p7)
* Trendlines across swing highs/lows on 5m/15m with **≥ 3 touch points**.
* Breakouts are never chased: require **consolidation against the line before the break**, or a **break followed by a low-volume retest**.
* Buy ATM only when the **retest is confirmed by taking out the breakout candle's extreme**; SL **behind the retest swing**. R:R "1 : …" (truncated) → default 1:2.
* Config: `strategies.trendline` (`pivot_strength`, `min_touches: 3`, `touch_tolerance_atr`, `retest_max_bars`, `low_volume_factor`).

### 3.8 Summary matrix (p6–p7) `[img]`

| Setup | TF | Trigger | SL | Expected R:R |
|---|---|---|---|---|
| Inside candle | 5m/15m | breach of inside H/L | opposite inside extreme | 1:1.5 to 1:… |
| Bullish hammer | 5m | tick above hammer high | 1–2 pts below hammer low | 1:2 to 1:… |
| Shooting star | 5m | tick below star low | 1–2 pts above star high | 1:2 to 1:… |
| 09:20 expansion | 5m | breach of 09:15–09:20 range | opposite extreme or 50 % | 40–80 spot pts |
| 14:20 pulse | 5m | new bar open at 14:20 after candle close | midday base swing | 1:… |
| 15:00 squeeze | 1m/5m | boundary violation at 14:59/15:00 | midpoint of base | 15–30 % premium |
| Trendline break & retest | 5m/15m | swing violation after clean retest | behind retest bar | 1:… |

## 4. Capital allocation and circuit breakers (p7–p9) `[img p7, p8]`

| # | Rule | Page | Config key |
|---|------|------|------------|
| R1 | **Active tranche = 20–25 %** of total equity is the maximum deployable across open positions in a session; **75–80 % reserve** stays in cash. | p7, p8 | `capital.active_tranche_pct: 0.25` |
| R2 | Risk per trade capped at a **predefined %** of the tranche (number not given in the PDF). | p7 | `capital.risk_per_trade_pct: 0.01` (assumption, of total capital) |
| R3 | **Max 2–3 trades per day**; zero trades is correct when no high-confluence setup exists. | p7, p8 | `risk.max_trades_per_day: 3` |
| R4 | **Consecutive-loss lockout**: if the first two trades hit SL, stop for the day. | p7, p8 | `risk.max_consecutive_losses: 2` |
| R5 | **Max daily loss 2–3 %** of total account equity → lockout. | p7, p8 | `risk.max_daily_loss_pct: 0.02` |
| R6 | **80/20 profit retention**: "A green session must never close red". Protect **70–80 %** of realised profit; any later trade may risk at most **20–30 % of banked gains**; if it is stopped out, trading ends for the day. (₹10,000 banked → max further loss ₹2,000–3,000 → take-home ≥ ₹7,000.) | p8, p9 | `risk.profit_retention_pct: 0.80`, `risk.secondary_risk_pct_of_banked: 0.25` |
| R7 | Trail stop-losses tightly on open trades during the midday window to lock in profit. | p11 | `risk.trail_to_breakeven_after_r: 1.0` (assumption on mechanics) |

## 5. Intraday execution matrix (p9–p12)

| Phase | Time (IST) | Rule | Config key |
|---|---|---|---|
| Pre-market | 08:45–09:14 | Mark PDH/PDL/PDC; S/R zones within 500 pts; gap check; BN constituent charts. | computed at day start |
| 1 Initial auction | 09:15–09:20 | Track first candle, **no orders**; log H/L at 09:20; BN opening bar > 250 pts ⇒ cancel morning breakout. | `strategies.morning_0920` |
| 2 Structural momentum | 09:21–11:30 | PDC bias enforcement; inside candle; hammer/star exhaustion; BN component confirmation. | strategy windows |
| 3 Midday decay avoidance | 11:31–13:30 | **No new option-buying entries**; trail SLs; enforce 2-SL lockout. | `signals.no_entry_windows.NSE` |
| 4 Afternoon expansion | 13:31–15:10 | 14:20 setup; 15:00 expiry scalp; enforce 80/20 secondary-risk cap. | strategy windows |
| 5 Mandatory square-off | 15:11–15:30 | **Close all long option positions before 15:15**; never carry overnight. | `sessions.NSE.square_off: "15:15"` |
| Post-market | 15:45–16:30 | Journal entry/exit, planned vs realised R:R, slippage, rule compliance. | `journal/trade_log.py` columns `planned_r`, `realised_r`, `slippage` |

## 6. What the PDF does **not** specify (assumptions, all configurable)

* **MCX commodities** — the guide is index-only. The same price-action setups are applied to MCX option-on-futures with MCX session timings (09:00–23:30 IST), the "09:20" opening-range rule mapped to the first 5-minute candle after the MCX open (09:00–09:05), and the 14:20/15:00 index-specific setups disabled for MCX by default. Point thresholds for MCX instruments are scaled guesses in `settings.yaml` and must be tuned.
* **Risk per trade %** (R2) — 1 % of total capital by default.
* **Second number of the R:R ratios** (truncated in the PDF) — defaults: inside candle 2.0× range, hammer/star 1:2, 14:20 1:2, trendline 1:2.
* **"Sustained" acceptance above/below a level** — 2 consecutive closed bars by default (`bias.acceptance_bars`).
* **Equilibrium-trap band width** — per-instrument points (`bias.trap_band_points`).
* **Trailing mechanics** — SL to breakeven after +1R, then trail by the spot ATR fraction (`risk.trail_atr_fraction`).
* **Nifty thresholds** where only Bank Nifty is given — Nifty ≈ Bank-Nifty / 2 (the PDF's own hammer rule uses 70–100 vs 150–200).
* **Expiry days (2026)** — NIFTY weekly expiry on **Tuesday**, NIFTY and BANKNIFTY monthly expiry on the **last Tuesday**, BANKNIFTY weekly discontinued — taken from the user's `fyersclient/nifty_fyers_symbol_generator.py`; MCX option expiries are explicit lists in YAML (verify against the Fyers symbol master).
* **Option premium for past dates** — Fyers history rarely serves expired option contracts, so the backtester prices options with a Black-Scholes synthetic model (per-instrument IV assumption, intraday theta) and tags every such trade `premium_model=synthetic`. Trades priced from real option candles are tagged `market`.
