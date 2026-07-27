import MetaTrader5 as mt5
import time
import config
from datetime import datetime, timezone



class EntryMixin:
    """
    Mixin class that provides the entry logic for the trading bot.
    It evaluates market ticks against a set of strict criteria (guards) to determine if a new trade should be opened.
    """

    def check_entry_conditions(self, tick, analysis, positions):
        """
        Evaluates the current market tick and technical analysis to determine if a BUY or SELL signal is present.

        Args:
            tick: The current tick from MetaTrader 5 containing bid/ask prices and time.
            analysis: A dictionary containing pre-calculated technical indicators (e.g., Bollinger Bands, Supertrend, Velocity).
            positions: A list of currently open positions.

        Returns:
            tuple: (entry_signal, entry_type, score) where entry_signal is "BUY", "SELL", or "NONE".
                   entry_type describes the reason for the entry (e.g., "Standard", "Re-entry").
                   score is the momentum score object if a signal is generated, else None.
        """
        if not tick or not analysis:
            return "NONE", "NONE", None

        # Prevent re-entering at the exact same price zone after a loss
        last_trade = getattr(self, "last_trade_history", None)
        if last_trade and last_trade.get("profit_points", 0) < 0:
            last_entry = last_trade.get("entry_price")
            if last_entry is not None and abs(tick.bid - last_entry) < 0.50: # Must move at least 0.50 points away
                return "NONE", "NONE", None

        # --- BROKER SYNC COOLDOWN ---
        # Prevent analyzing new setups for 2 seconds after a trade fires.
        # This gives MT5 time to update positions_get(), preventing the engine from starting
        # a new confirmation loop for a trade that is already executing.

        if time.time() - getattr(self, "_last_signal_time", 0) < 2.0:
            return "NONE", "NONE", None
            
        if time.time() - getattr(self, "_last_exit_time", 0) < 2.0:
            return "NONE", "NONE", None


        # GUARD: Sideway Trend Block
        if getattr(self, "last_trend", "NONE") == "NONE":
            self.entry_block_reasons["SIDEWAY_TREND"] += 1
            return "NONE", "NONE", None

        # GUARD: Prevent over-trading by capping the total number of entries per single candle
        if self.trades_this_candle >= getattr(self, "max_trades_candle", 6):
            self.entry_block_reasons["MAX_TRADES_PER_CANDLE"] += 1
            return "NONE", "NONE", None

        # GUARD: Hard stop if we hit the maximum allowed losses in a single candle
        if getattr(self, "losses_this_candle", 0) >= getattr(config, "MAX_LOSSES_PER_CANDLE", 2):
            if self.loop_count % 60 == 0:
                self.log(
                    f"LOSS_LIMIT_CANDLE ({self.losses_this_candle}/{getattr(config, 'MAX_LOSSES_PER_CANDLE', 2)}) — wait next candle",
                    self.Colors.ORANGE,
                )
            self.entry_block_reasons["LOSS_LIMIT_CANDLE"] += 1
            return "NONE", "NONE", None

        # Block entries if consecutive loss limit is reached
        if getattr(self, "candles_to_pause", 0) > 0:
            if self.loop_count % 60 == 0:
                self.log(
                    f"CONSEC_LOSS_PAUSE ({self.candles_to_pause} candles remaining) — too many consecutive losses",
                    self.Colors.ORANGE,
                )
            self.entry_block_reasons["CONSEC_LOSS_PAUSE"] += 1
            return "NONE", "NONE", None

        # --- DAILY PROFIT TARGET ---
        daily_target = getattr(config, "DAILY_PROFIT_TARGET", 500.0)
        if getattr(self, "today_profit", 0.0) >= daily_target:
            if self.loop_count % 30 == 0:
                self.log(
                    f"DAILY_PROFIT_TARGET REACHED (${getattr(self, 'today_profit', 0.0):.2f} / ${daily_target:.2f})",
                    self.Colors.GREEN,
                )
            self.entry_block_reasons["DAILY_PROFIT_TARGET"] += 1
            return "NONE", "NONE", None

        # --- DAILY TRADE LIMIT ---
        if getattr(self, "total_trades_today", 0) >= getattr(config, "MAX_DAILY_TRADES", 6):
            if self.loop_count % 30 == 0:
                self.log(
                    f"DAILY_LIMIT ({self.total_trades_today}/{getattr(config, 'MAX_DAILY_TRADES', 6)})",
                    self.Colors.ORANGE,
                )
            self.entry_block_reasons["DAILY_LIMIT"] += 1
            return "NONE", "NONE", None

        if getattr(self, "final_guard_blocks_this_candle", 0) >= 3:
            self.entry_block_reasons["FINAL_GUARD_PAUSE"] += 1
            return "NONE", "NONE", None

        if getattr(self, "is_executing", False):
            self.entry_block_reasons["IS_EXECUTING"] += 1
            return "NONE", "NONE", None

        # Calculate how many seconds have elapsed since the current candle opened
        candle_open_time = analysis.get("time", 0)
        seconds_into_candle = (
            int(tick.time) - int(candle_open_time) if candle_open_time else 0
        )

        # GUARD: Universal Default Windows
        # Prevent entries at the very start (volatile) or very end (exhausted) of a candle.
        tf = getattr(self, "timeframe", "M5")
        tf_map = {
            "M1": 60,
            "M5": 300,
            "M15": 900,
            "M30": 1800,
        }
        tf_secs = tf_map.get(tf, 300)

        if tf == "M1":
            start_window = 5
            end_window = tf_secs - 5
        else:
            start_window = 5
            end_window = tf_secs - 10

        if seconds_into_candle < start_window:
            self.entry_block_reasons["CANDLE_ENTRY_START"] = (
                self.entry_block_reasons.get("CANDLE_ENTRY_START", 0) + 1
            )
            return "NONE", "NONE", None

        if seconds_into_candle > end_window:
            self.entry_block_reasons["CANDLE_ENTRY_END"] = (
                self.entry_block_reasons.get("CANDLE_ENTRY_END", 0) + 1
            )
            return "NONE", "NONE", None

        live_count = len(positions) if positions else 0
        max_allowed = getattr(config, "MAX_SIMULTANEOUS_POSITIONS", 1)

        max_allowed_buy = max_allowed_sell = max_allowed

        if live_count >= max_allowed and max_allowed > 1:
            self.entry_block_reasons["MAX_POSITIONS"] += 1
            return "NONE", "NONE", None

        state_buy = {
            "timeframe": getattr(self, "timeframe", "M5"),
            "drift": 0.0,
            "last_trend": getattr(self, "last_trend", "NONE"),
            "seconds_into_candle": seconds_into_candle,
        }
        state_sell = {
            "timeframe": getattr(self, "timeframe", "M5"),
            "drift": 0.0,
            "last_trend": getattr(self, "last_trend", "NONE"),
            "seconds_into_candle": seconds_into_candle,
        }

        buy_score = self.strategy.calculate_momentum_score(
            "BUY", tick, analysis, state_buy
        )
        sell_score = self.strategy.calculate_momentum_score(
            "SELL", tick, analysis, state_sell
        )

        analysis["buy_score_total"] = buy_score.total
        analysis["sell_score_total"] = sell_score.total

        if getattr(self, "db", None):
            for d_str, s_obj in [("BUY", buy_score), ("SELL", sell_score)]:
                if s_obj.total >= 60.0:
                    setup_log = {
                        "candle_time": str(analysis.get("time", "")),
                        "direction": d_str,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "score_momentum": s_obj.momentum,
                        "score_trend": s_obj.trend,
                        "score_candle": s_obj.candle,
                        "score_execution": s_obj.execution,
                        "score_total": s_obj.total,
                        "reject_reason": s_obj.block_reason,
                        "decision_stage": (
                            "PASSED" if not s_obj.block_reason else "REJECTED"
                        ),
                        "trade_executed": 0,
                        "ticket": None,
                        "instant_velocity": analysis.get("velocity", 0.0),
                        "velocity_2s": analysis.get("velocity_2s", 0.0),
                        "strategy_version": getattr(
                            config, "STRATEGY_VERSION", "unknown"
                        ),
                    }
                    self.db.log_evaluated_setup(setup_log)

        buy_threshold = buy_score.required_score
        sell_threshold = sell_score.required_score

        if live_count >= max_allowed_buy and not buy_score.block_reason:
            buy_score.block_reason = "MAX_POSITIONS"

        if live_count >= max_allowed_sell and not sell_score.block_reason:
            sell_score.block_reason = "MAX_POSITIONS"

        buy_conditions_met = sell_conditions_met = False
        
        _entry_type = "PULLBACK"
        _confirm_req = getattr(config, "ENTRY_CONFIRM_TICKS", 2)
        
        if not buy_score.block_reason:
            buy_conditions_met = True
        else:
            self.entry_block_reasons[buy_score.block_reason] = (
                self.entry_block_reasons.get(buy_score.block_reason, 0) + 1
            )

        if not sell_score.block_reason:
            sell_conditions_met = True
        else:
            if sell_score.block_reason == "FAKE_SPIKE_AVG_VEL":
                self.log(
                    f"FAKE_SPIKE_AVG_VEL (avg={analysis.get('avg_velocity', 0.0):+.2f})",
                    self.Colors.ORANGE,
                )
            self.entry_block_reasons[sell_score.block_reason] = (
                self.entry_block_reasons.get(sell_score.block_reason, 0) + 1
            )

        buy_trigger = sell_trigger = False

        # --- TICK CONFIRMATION ---
        # Require multiple consecutive ticks (getattr(config, "ENTRY_CONFIRM_TICKS", 3)) where all conditions remain true.
        if buy_conditions_met:
            self.buy_confirm_count += 1
            if self.buy_confirm_count >= _confirm_req:
                buy_trigger = True
                self.log(
                    f"BUY CONFIRMED ({self.buy_confirm_count} ticks)",
                    self.Colors.GREEN,
                )
            else:
                self.log(
                    f"BUY Confirming... (Tick {self.buy_confirm_count}/{_confirm_req} @ {tick.ask:.2f})",
                    self.Colors.YELLOW,
                )
        else:
            if self.buy_confirm_count:
                self.log(f"BUY CONFIRM_RESET — setup lost", self.Colors.ORANGE)
            self.buy_confirm_count = 0

        if sell_conditions_met:
            self.sell_confirm_count += 1
            if self.sell_confirm_count >= _confirm_req:
                sell_trigger = True
                self.log(
                    f"SELL CONFIRMED ({self.sell_confirm_count} ticks)",
                    self.Colors.MAGENTA,
                )
            else:
                self.log(
                    f"SELL Confirming... (Tick {self.sell_confirm_count}/{_confirm_req} @ {tick.bid:.2f})",
                    self.Colors.YELLOW,
                )
        else:
            if self.sell_confirm_count:
                self.log(f"SELL CONFIRM_RESET — setup lost", self.Colors.ORANGE)
            self.sell_confirm_count = 0

        # --- RE-ENTRY POSITION GUARD (HISTORY) ---
        # Removed to allow entries during healthy micro-pullbacks.
        pass

        if buy_trigger or sell_trigger:
            signal = "BUY" if buy_trigger else "SELL"
            avg_velocity = analysis.get("avg_velocity")
            avg_str = f"{avg_velocity:+.2f}" if avg_velocity is not None else "N/A"
            color = self.Colors.GREEN if buy_trigger else self.Colors.MAGENTA
            self.log(
                f"[TRIGGER] {signal} ({_entry_type}) | Body: {analysis.get('prev_body', 0.0):.2f}→{abs(tick.bid - (analysis.get('open') or tick.bid)):.2f} | Vel:{analysis.get('velocity', 0.0):+.2f} | Avg:{avg_str}",
                color,
            )
            if buy_trigger:
                self.buy_confirm_count = 0
                self._last_signal_time = time.time()
            if sell_trigger:
                self.sell_confirm_count = 0
                self._last_signal_time = time.time()
            return signal, _entry_type, (buy_score if buy_trigger else sell_score)

        return "NONE", "NONE", None

    def execute_entry(self, signal, tick, analysis, entry_type="", score=None):
        if self.is_executing:
            return False
        if self.trades_this_candle >= getattr(self, "max_trades_candle", 6):
            self.entry_block_reasons["MAX_TRADES_PER_CANDLE"] += 1
            self.log(
                f"MAX_TRADES_PER_CANDLE ({self.trades_this_candle}/{getattr(self, 'max_trades_candle', 6)})",
                self.Colors.ORANGE,
            )
            return False
        self.is_executing = True
        try:
            symbol_info = mt5.symbol_info(self.symbol)
            if not symbol_info:
                self.log("Failed to get symbol info", self.Colors.RED)
                return False

            fresh_tick = mt5.symbol_info_tick(self.symbol)
            if fresh_tick:
                tick = fresh_tick

            # Final live guard — positions may have changed since check_entry_conditions ran
            live_positions = mt5.positions_get(symbol=self.symbol)
            live_count = len(live_positions) if live_positions else 0

            max_allowed = getattr(config, "MAX_SIMULTANEOUS_POSITIONS", 1)

            if live_count >= max_allowed:
                self.log(
                    f"ENTRY ABORTED — live positions ({live_count}) >= max_allowed ({max_allowed})",
                    self.Colors.ORANGE,
                )
                return False

            entry_price = tick.ask if signal == "BUY" else tick.bid

            volume = getattr(config, "LOT_SIZE", 1.0)
            self.log(f"Lot Size: {volume:.2f}", self.Colors.CYAN)

            order_type = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL

            if hasattr(tick, "ask") and hasattr(tick, "bid"):
                spread = round(tick.ask - tick.bid, 5)
                if spread > getattr(config, "SPREAD_ALLOWANCE", 0.20):
                    self.log(
                        f"WIDE SPREAD ({spread:.2f}) — skipping entry",
                        self.Colors.ORANGE,
                    )
                    return False

            filling_mode = symbol_info.filling_mode
            if filling_mode & 1:
                type_filling = 0
            elif filling_mode & 2:
                type_filling = 1
            else:
                type_filling = 2

            tick_size = symbol_info.trade_tick_size

            entry_vel = analysis.get("velocity", 0.0)
            abs_vel = abs(entry_vel)

            # --- DYNAMIC TP / SL SCALING ---
            tf_settings = getattr(config, "TIMEFRAME_SETTINGS", {}).get(
                getattr(self, "timeframe", "M5"),
                getattr(config, "TIMEFRAME_SETTINGS", {}).get("M5", {}),
            )
            cfg_tp_mod = tf_settings.get("TP_MODERATE", 3.00)
            cfg_tp_str = tf_settings.get("TP_STRONG", 5.00)
            cfg_tp_ult = tf_settings.get("TP_ULTRA_STRONG", 8.00)
            cfg_hard_sl = tf_settings.get("HARD_STOP_LOSS", 2.00)

            _base_tp_mod = cfg_tp_mod
            _base_tp_str = cfg_tp_str
            _base_tp_ult = cfg_tp_ult
            _base_sl_cap = cfg_hard_sl

            if getattr(config, "ENABLE_DYNAMIC_SL_TP", False):
                atr_50 = analysis.get("atr_50", 2.50)
                if atr_50 > 0:
                    _base_tp_mod = max(
                        cfg_tp_mod,
                        atr_50 * getattr(config, "DYNAMIC_TP_BASE_MULTIPLIER", 2.0),
                    )
                    _base_tp_str = max(cfg_tp_str, _base_tp_mod * 1.5)
                    _base_tp_ult = max(cfg_tp_ult, _base_tp_mod * 2.0)

                    calc_sl = atr_50 * getattr(config, "DYNAMIC_SL_ATR_MULTIPLIER", 1.5)
                    min_sl = getattr(config, "MIN_DYNAMIC_SL", 2.00)
                    max_sl = getattr(config, "MAX_DYNAMIC_SL", 8.00)
                    _base_sl_cap = max(min_sl, min(calc_sl, max_sl))

            entry_type = entry_type or analysis.get("last_entry_type", "")
            if entry_type in ["REVERSAL_HAMMER", "REVERSAL_SHOOTING_STAR"]:
                dynamic_tp = _base_tp_str
                tp_label = "REVERSAL"
                self.log(
                    f"🎯 REVERSAL OVERRIDE: Forcing TP_STRONG ({dynamic_tp:.2f} pts)",
                    self.Colors.GREEN,
                )
            elif abs_vel >= 1.00:
                dynamic_tp = _base_tp_ult
                tp_label = "ULTRA"
            elif abs_vel >= 0.70:
                dynamic_tp = _base_tp_str
                tp_label = "STRONG"
            else:
                dynamic_tp = _base_tp_mod
                tp_label = "MODERATE"

            self.log(
                f"Dynamic TP: {dynamic_tp} pts [{tp_label}] (vel: {entry_vel:+.2f})",
                self.Colors.CYAN,
            )

            send_tick = mt5.symbol_info_tick(self.symbol)
            if send_tick:
                tick = send_tick
                entry_price = tick.ask if signal == "BUY" else tick.bid

            current_open = analysis.get("open") or tick.bid
            live_body = tick.bid - current_open
            live_color = (
                "GREEN" if live_body > 0 else "RED" if live_body < 0 else "UNKNOWN"
            )



            risk_pts = min(_base_sl_cap, round(dynamic_tp * getattr(config, "MAX_RISK_TO_TP_RATIO", 2.0), 2))
            hard_sl = round(
                round(
                    (
                        entry_price - risk_pts
                        if signal == "BUY"
                        else entry_price + risk_pts
                    )
                    / tick_size
                )
                * tick_size,
                symbol_info.digits,
            )
            broker_tp = round(
                round(
                    (
                        entry_price + dynamic_tp
                        if signal == "BUY"
                        else entry_price - dynamic_tp
                    )
                    / tick_size
                )
                * tick_size,
                symbol_info.digits,
            )

            # ── BROKER STOPS-LEVEL ENFORCEMENT ──
            # Broker requires SL & TP to be at least (trade_stops_level * point) away
            # from entry. If our calculated values are too close, clamp them outward.
            # Winprofx XAUUSD: stops_level=50, point=0.01 → min dist = 0.50 pts
            min_dist = round(
                (symbol_info.trade_stops_level * symbol_info.point)
                + (symbol_info.point * 15),
                symbol_info.digits,
            )  # +15 pts buffer on top of minimum to avoid edge rejections
            if signal == "BUY":
                sl_limit = round(entry_price - min_dist, symbol_info.digits)
                tp_limit = round(entry_price + min_dist, symbol_info.digits)
                if hard_sl > sl_limit:  # SL too close above limit → push down
                    self.log(
                        f"⚠️ SL {hard_sl:.2f} too close — clamped to broker min {sl_limit:.2f} (dist:{min_dist:.2f})",
                        self.Colors.YELLOW,
                    )
                    hard_sl = sl_limit
                if broker_tp < tp_limit:  # TP too close below limit → push up
                    self.log(
                        f"⚠️ TP {broker_tp:.2f} too close — clamped to broker min {tp_limit:.2f} (dist:{min_dist:.2f})",
                        self.Colors.YELLOW,
                    )
                    broker_tp = tp_limit
            else:  # SELL
                sl_limit = round(entry_price + min_dist, symbol_info.digits)
                tp_limit = round(entry_price - min_dist, symbol_info.digits)
                if hard_sl < sl_limit:  # SL too close below limit → push up
                    self.log(
                        f"⚠️ SL {hard_sl:.2f} too close — clamped to broker min {sl_limit:.2f} (dist:{min_dist:.2f})",
                        self.Colors.YELLOW,
                    )
                    hard_sl = sl_limit
                if broker_tp > tp_limit:  # TP too close above limit → push down
                    self.log(
                        f"⚠️ TP {broker_tp:.2f} too close — clamped to broker min {tp_limit:.2f} (dist:{min_dist:.2f})",
                        self.Colors.YELLOW,
                    )
                    broker_tp = tp_limit

            self.log(
                f"HARD SL: {hard_sl:.2f} ({risk_pts:.2f} pts) | TP: {broker_tp:.2f} (target:{dynamic_tp:.2f}) | BrokerMin:{min_dist:.2f}",
                self.Colors.CYAN,
            )

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": volume,
                "type": order_type,
                "price": entry_price,
                "sl": hard_sl,
                "tp": broker_tp,
                "magic": 123456,
                "deviation": int(getattr(config, "MAX_ENTRY_SLIPPAGE", 0.20) / symbol_info.point),
                "comment": f"{signal}_HardSL",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": type_filling,
            }
            if not mt5.terminal_info() or not mt5.terminal_info().connected:
                from connection import MT5Connection

                if not MT5Connection.initialize_mt5():
                    self.log("❌ ORDER ABORTED — MT5 disconnected", self.Colors.RED)
                    return False

            result = mt5.order_send(request)
            if result is None:
                err = mt5.last_error()
                self.log(
                    f"❌ ORDER FAILED — mt5.order_send returned None | MT5 error: {err}",
                    self.Colors.RED,
                )
                time.sleep(0.3)
                result = mt5.order_send(request)

            if result and result.retcode == 10016:
                self.entry_block_reasons["INVALID_STOPS"] += 1
                self.log(
                    "INVALID_STOPS from broker — entry skipped", self.Colors.ORANGE
                )
                return False

            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                self.total_trades += 1
                self.trades_this_candle += 1

                if getattr(self, "db", None) and score:
                    setup_log = {
                        "candle_time": str(analysis.get("time", "")),
                        "direction": signal,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "score_momentum": score.momentum,
                        "score_trend": score.trend,
                        "score_candle": score.candle,
                        "score_execution": score.execution,
                        "score_total": score.total,
                        "reject_reason": score.block_reason,
                        "decision_stage": "EXECUTED",
                        "trade_executed": 1,
                        "ticket": result.order,
                        "instant_velocity": analysis.get("velocity", 0.0),
                        "velocity_2s": analysis.get("velocity_2s", 0.0),
                        "strategy_version": getattr(
                            config, "STRATEGY_VERSION", "unknown"
                        ),
                    }
                    self.db.log_evaluated_setup(setup_log)

                if result.price:
                    fill_slippage = abs(result.price - entry_price)
                    if fill_slippage >= getattr(config, "MAX_ENTRY_SLIPPAGE", 0.20):
                        self.log(
                            f"⚠️ HIGH SLIPPAGE {fill_slippage:+.2f} — allowing trade to run",
                            self.Colors.ORANGE,
                        )
                    entry_price = result.price

                now_entry = time.time()
                self.formatter.print_tick_context(
                    "PRE-ENTRY",
                    [t for t in self.pre_entry_ticks if t["time"] >= now_entry - 1.0],
                    signal,
                )

                conditions = f"Type: {analysis.get('last_entry_type','N/A')} | V: {analysis.get('velocity',0.0):+.2f} | PB: {analysis.get('prev_body',0.0):.2f}"
                try:
                    self.formatter.print_trade_entry(
                        signal,
                        entry_price,
                        volume,
                        hard_sl,
                        broker_tp,
                        result.order,
                        conditions,
                        self.session_capital,
                        self.total_trades,
                        risk_pts,
                        score=score,
                    )
                except Exception:
                    self.log(
                        f"📋 TRADE: {signal} @ {entry_price:.2f} | SL:{hard_sl:.2f}",
                        self.Colors.CYAN,
                    )

                actual_ticket = result.order
                unified_pos_data = {
                    "entry_price": entry_price,
                    "initial_sl": hard_sl,
                    "initial_tp": broker_tp,
                    "entry_time": datetime.now(timezone.utc),
                    "entry_time_ts": time.time(),
                    "entry_candle_time": analysis.get("time"),
                    "direction": signal,
                    "volume": volume,
                    "entry_velocity": entry_vel,
                    "initial_tp_pts": dynamic_tp,
                    "peak_profit": 0.0,
                    "trail_sl_price": None,
                    "hard_sl_price": hard_sl,
                    "entry_type": entry_type,
                    "score_momentum": score.momentum if score else 0.0,
                    "score_trend": score.trend if score else 0.0,
                    "score_candle": score.candle if score else 0.0,
                    "score_execution": score.execution if score else 0.0,
                    "score_total": score.total if score else 0.0,
                    "velocity_consistency": score.velocity_avg_change if score else 0.0,
                    "velocity_acceleration": (
                        score.velocity_acceleration if score else 0.0
                    ),
                    "score_acceleration": score.accel_score if score else 0.0,
                    "velocity_std": 0.0,
                    "velocity_mean": 0.0,
                    "mfe": 0.0,
                    "mae": 0.0,
                    "adx_14": analysis.get("adx_14", 0.0),
                    "sideways_score": score.sideways_score if score else 0,
                }
                self.position_data[actual_ticket] = unified_pos_data
                return True
            else:
                self.log(
                    f"❌ ORDER FAILED: {result.comment if result else 'Unknown'} (Retcode: {result.retcode if result else 'N/A'})",
                    self.Colors.RED,
                )
                return False

        except Exception as e:
            self.log(f"❌ Error executing trade: {e}", self.Colors.RED)
            return False
        finally:
            self.is_executing = False

    def _modify_sl(self, pos, new_sl_price):
        guard = getattr(self, "_sl_modify_in_progress", None)
        if guard is not None:
            if pos.ticket in guard:
                return False
            guard.add(pos.ticket)
        try:
            symbol_info = mt5.symbol_info(self.symbol)
            tick = mt5.symbol_info_tick(self.symbol)
            if not symbol_info or not tick:
                return False

            tick_size = symbol_info.trade_tick_size
            digits = symbol_info.digits
            stops_level = symbol_info.trade_stops_level
            if stops_level == 0:
                spread = tick.ask - tick.bid
                safe_dist = spread + (symbol_info.point * 8)
            else:
                safe_dist = (stops_level * symbol_info.point) + (symbol_info.point * 8)

            sl_rounded = round(round(new_sl_price / tick_size) * tick_size, digits)

            # Fetch live position to get current broker SL — pos is a stale snapshot
            live_positions = mt5.positions_get(ticket=pos.ticket)
            live_pos = live_positions[0] if live_positions else pos
            current_broker_sl = float(live_pos.sl or 0.0)
            current_broker_tp = float(live_pos.tp or 0.0)

            # Never move SL in the wrong direction
            if current_broker_sl != 0:
                if (
                    pos.type == mt5.POSITION_TYPE_BUY
                    and sl_rounded <= current_broker_sl
                ):
                    return False
                if (
                    pos.type == mt5.POSITION_TYPE_SELL
                    and sl_rounded >= current_broker_sl
                ):
                    return False

            # Clamp to broker's minimum distance for the broker request only.
            # Software SL (caller's trail_sl_price) is set before this call and is not affected.
            broker_sl = sl_rounded
            if pos.type == mt5.POSITION_TYPE_BUY:
                max_allowed = tick.bid - safe_dist
                if broker_sl > max_allowed:
                    broker_sl = round(
                        round(max_allowed / tick_size) * tick_size, digits
                    )
            else:
                min_allowed = tick.ask + safe_dist
                if broker_sl < min_allowed:
                    broker_sl = round(
                        round(min_allowed / tick_size) * tick_size, digits
                    )

            # ── BACKWARDS-MOVE GUARD (after clamping) ──
            # Clamping uses live price, so when price reverses the clamped value
            # can push the broker SL backwards (up for SELL, down for BUY).
            # If that happens, skip the broker update — software SL still tracks correctly.
            if current_broker_sl != 0:
                if pos.type == mt5.POSITION_TYPE_BUY and broker_sl <= current_broker_sl:
                    return False  # skip broker update, software SL still advances
                if (
                    pos.type == mt5.POSITION_TYPE_SELL
                    and broker_sl >= current_broker_sl
                ):
                    return False  # skip broker update, software SL still advances

            # Already at or better — skip broker call
            if current_broker_sl != 0 and abs(broker_sl - current_broker_sl) < (
                tick_size * 0.5
            ):
                return (
                    False  # software SL still advances even if broker SL doesn't move
                )

            result = mt5.order_send(
                {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "symbol": self.symbol,
                    "position": pos.ticket,
                    "sl": broker_sl,
                    "tp": current_broker_tp,
                }
            )
            if result and result.retcode in [mt5.TRADE_RETCODE_DONE, 10025]:
                return sl_rounded  # return intended value, not broker-clamped value
            ret_code = result.retcode if result else "N/A"
            if ret_code == 10016:
                self.log(
                    "⚠️ BROKER SL too close (10016) — software SL active",
                    self.Colors.YELLOW,
                )
                return False  # software SL still tracks correctly
            elif ret_code == 10036:
                self.log(
                    f"⚠️ TRAIL SL IGNORED: Position {pos.ticket} already closed on broker (10036)",
                    self.Colors.YELLOW,
                )
                return False
            elif ret_code != 10025:
                self.log(
                    f"❌ BROKER REJECTED TRAIL SL: {result.comment if result else 'Error'} (Code:{ret_code})",
                    self.Colors.RED,
                )
            return False  # always advance software SL regardless of broker response
        except Exception as e:
            self.log(f"❌ SL Modify Exception: {e}", self.Colors.RED)
            return False
        finally:
            if guard is not None:
                guard.discard(pos.ticket)

    def execute_scale_in(
        self, parent_pos, parent_data, tick, current_live_count, analysis
    ):
        scale_vol = getattr(config, "LOT_SIZE", 0.10)
        parent_vol = round(parent_pos.volume, 2)
        signal = "BUY" if parent_pos.type == mt5.ORDER_TYPE_BUY else "SELL"

        self.log(
            f"Scale-In Lot Sizing (Trade {current_live_count + 1}) → Lot: {scale_vol:.2f}",
            self.Colors.CYAN,
        )
        self.log(
            f"SCALE-IN TRIGGERED: {signal} at +1.00 pt profit. Parent Vol:{parent_vol} → Scale Vol:{scale_vol}",
            self.Colors.CYAN,
        )

        order_type = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL
        entry_price = tick.ask if signal == "BUY" else tick.bid

        symbol_info = mt5.symbol_info(self.symbol)
        filling_mode = symbol_info.filling_mode
        if filling_mode & 1:
            type_filling = 0
        elif filling_mode & 2:
            type_filling = 1
        else:
            type_filling = 2

        tick_size = symbol_info.trade_tick_size
        hard_sl = (
            round(
                (entry_price - 2.00 if signal == "BUY" else entry_price + 2.00)
                / tick_size
            )
            * tick_size
        )

        dynamic_tp = parent_data.get("initial_tp_pts", 3.00)
        broker_tp = (
            round(
                (
                    entry_price + dynamic_tp
                    if signal == "BUY"
                    else entry_price - dynamic_tp
                )
                / tick_size
            )
            * tick_size
        )

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": scale_vol,
            "type": order_type,
            "price": entry_price,
            "sl": hard_sl,
            "tp": broker_tp,
            "deviation": 20,
            "magic": 234000,
            "comment": "Scale-In",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": type_filling,
        }

        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.log(f"SCALE-IN FAILED: {result.comment}", self.Colors.RED)
            return False

        self.log(
            f"SCALE-IN EXECUTED: {signal} {scale_vol} lots @ {result.price}",
            self.Colors.GREEN,
        )

        self.position_data[result.order] = {
            "entry_time": datetime.now(timezone.utc),
            "direction": signal,
            "entry_price": result.price,
            "initial_sl": hard_sl,
            "hard_sl_price": hard_sl,
            "initial_tp": broker_tp,
            "initial_tp_pts": dynamic_tp,
            "trail_sl_price": None,
            "price_lock_sl_price": None,
            "peak_profit": 0.0,
            "last_profit_pts": 0.0,
            "entry_velocity": parent_data.get("entry_velocity", 0.0),
            "volume": scale_vol,
        }
        self.scaled_in_tickets.add(result.order)  # don't scale-in off a scale-in
        self.trades_this_candle += 1
        return True
