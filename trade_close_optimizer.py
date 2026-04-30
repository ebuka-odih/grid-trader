"""
Fee-Aware Trade Closing Optimizer

This module provides fee-aware trade closing logic for the grid trader.
Accounts for fees, spreads, and market conditions before closing trades.
"""

import logging
from typing import Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger("trade_closer")


# Bybit Fee Schedule (Linear Perpetuals)
MAKER_FEE_RATE = 0.0002   # 0.02%
TAKER_FEE_RATE = 0.00055  # 0.055%


# Default Configuration
DEFAULT_SPREAD_THRESHOLD_PCT = 0.1  # 0.1% spread warning
MAX_SPREAD_THRESHOLD_PCT = 0.3      # 0.3% spread - use limit orders


@dataclass
class CloseDecision:
    """Result of close decision analysis."""
    should_close: bool
    close_type: str  # "market", "limit", "hold"
    net_pnl: float
    gross_pnl: float
    fee_estimate: float
    reason: str
    suggested_price: Optional[float] = None


@dataclass  
class GridStatus:
    """Current grid status for close analysis."""
    fills: int
    realized_pnl: float
    unrealized_pnl: float
    allocated_margin: float
    order_size_usdt: float
    avg_entry_price: float
    current_bid: float
    current_ask: float
    current_mark: float
    direction: str  # "long", "short", "neutral"
    grid_levels: int
    filled_levels: int
    age_seconds: float


class TradeCloseOptimizer:
    """
    Fee-aware trade closing optimizer.
    
    Accounts for:
    - Trading fees (maker/taker schedule)
    - Bid-ask spread
    - Net vs gross PnL
    - Market conditions
    """
    
    def __init__(
        self,
        maker_fee: float = MAKER_FEE_RATE,
        taker_fee: float = TAKER_FEE_RATE,
        spread_threshold: float = DEFAULT_SPREAD_THRESHOLD_PCT,
        max_spread_threshold: float = MAX_SPREAD_THRESHOLD_PCT,
        target_pnl_pct_low: float = 2.0,
        target_pnl_pct_high: float = 4.0,
        max_drawdown_pct: float = 5.0,
        min_net_profit_usdt: float = 0.0,
        fee_buffer_multiplier: float = 1.0,
    ):
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.spread_threshold = spread_threshold
        self.max_spread_threshold = max_spread_threshold
        self.target_pnl_pct_low = target_pnl_pct_low
        self.target_pnl_pct_high = target_pnl_pct_high
        self.max_drawdown_pct = max_drawdown_pct
        self.min_net_profit_usdt = max(0.0, float(min_net_profit_usdt))
        self.fee_buffer_multiplier = max(1.0, float(fee_buffer_multiplier))
    
    def calculate_fees(self, fills: int, order_size_usdt: float) -> dict:
        """
        Calculate estimated fees for grid trading.
        
        Grid trades typically:
        - Open with maker orders (limit orders placed)
        - Close with taker orders (market orders)
        
        Args:
            fills: Number of grid fills completed
            order_size_usdt: Size per grid level in USDT
            
        Returns:
            Dict with fee breakdown
        """
        # Conservative: assume all close as taker (0.055%)
        # Grid has fills count * 2 (buys + sells)
        # But net position only ~half need closing
        
        total_trades = fills  # Each fill is one trade
        position_closes = fills / 2  # Roughly half need closing
        
        opening_fees = total_trades * order_size_usdt * self.maker_fee
        closing_fees = position_closes * order_size_usdt * self.taker_fee
        
        return {
            "opening_fees": opening_fees,
            "closing_fees": closing_fees,
            "total_fees": opening_fees + closing_fees,
            "total_fees_pct": ((opening_fees + closing_fees) / 
                              (total_trades * order_size_usdt)) * 100 if total_trades > 0 else 0,
        }
    
    def calculate_spread(self, bid: float, ask: float) -> dict:
        """
        Calculate spread metrics from order book.
        
        Args:
            bid: Best bid price
            ask: Best ask price
            
        Returns:
            Dict with spread metrics
        """
        if bid <= 0 or ask <= 0 or ask <= bid:
            return {"spread": 0, "spread_pct": float('inf'), "mid": 0}
        
        spread = ask - bid
        mid = (ask + bid) / 2
        spread_pct = (spread / mid) * 100
        
        return {
            "spread": spread,
            "spread_pct": spread_pct,
            "mid": mid,
        }
    
    def should_close(
        self,
        status: GridStatus,
    ) -> CloseDecision:
        """
        Comprehensive close decision with fee and spread awareness.
        
        Args:
            status: Current grid status including prices, PnL, fills
            
        Returns:
            CloseDecision with recommendations
        """
        # Calculate PnL
        gross_pnl = status.realized_pnl + status.unrealized_pnl
        
        # Calculate fees. For small high-leverage wallets, closing too early can
        # turn a visually green trade into a net loss after maker/taker costs.
        # Use a configurable fee buffer so targets must clear fees plus slack.
        fees = self.calculate_fees(status.fills, status.order_size_usdt)
        buffered_fee_estimate = fees["total_fees"] * self.fee_buffer_multiplier
        net_pnl = gross_pnl - buffered_fee_estimate
        required_net_profit = max(
            self.min_net_profit_usdt,
            status.allocated_margin * self.target_pnl_pct_low / 100,
        )
        
        # Calculate spread
        spread_info = self.calculate_spread(status.current_bid, status.current_ask)
        
        # Calculate targets
        target_low = status.allocated_margin * self.target_pnl_pct_low / 100
        target_high = status.allocated_margin * self.target_pnl_pct_high / 100
        drawdown_limit = status.allocated_margin * self.max_drawdown_pct / 100
        
        # Fast path: no fills
        if status.fills == 0:
            return CloseDecision(
                should_close=False,
                close_type="hold",
                net_pnl=net_pnl,
                gross_pnl=gross_pnl,
                fee_estimate=0,
                reason="No fills yet - holding",
            )
        
        # Check spread - if too wide, don't close with market order
        if spread_info["spread_pct"] > self.max_spread_threshold:
            logger.warning(
                f"⚠️ FATTY MARKET CLOSING - Spread={spread_info['spread_pct']:.3f}% "
                f"({status.current_bid}/{status.current_ask})"
            )
            # Use limit order instead (near mid price)
            if gross_pnl >= target_low and net_pnl >= required_net_profit:
                return CloseDecision(
                    should_close=True,
                    close_type="limit",
                    net_pnl=net_pnl,
                    gross_pnl=gross_pnl,
                    fee_estimate=buffered_fee_estimate,
                    reason=f"Target hit ({gross_pnl:.2f}/{target_low:.2f}) - use limit due to wide spread ({spread_info['spread_pct']:.2f}%)",
                    suggested_price=spread_info["mid"],
                )
        
        # Profit target check. Close only when net profit covers fees plus the
        # configured small-wallet buffer; otherwise keep cycling because gross
        # green PnL is not enough after execution costs.
        if gross_pnl >= target_low and net_pnl < required_net_profit:
            return CloseDecision(
                should_close=False,
                close_type="hold",
                net_pnl=net_pnl,
                gross_pnl=gross_pnl,
                fee_estimate=buffered_fee_estimate,
                reason=(
                    f"Gross target hit but net PnL ${net_pnl:.4f} is below "
                    f"required net target ${required_net_profit:.4f} after fees"
                ),
            )

        if gross_pnl >= target_low and gross_pnl < target_high:
            # Low target hit
            return CloseDecision(
                should_close=True,
                close_type="market",
                net_pnl=net_pnl,
                gross_pnl=gross_pnl,
                fee_estimate=buffered_fee_estimate,
                reason=f"Low net target hit: net=${net_pnl:.4f} >= ${required_net_profit:.4f}; gross=${gross_pnl:.2f} >= ${target_low:.2f} (target={self.target_pnl_pct_low}%)",
            )
        
        if gross_pnl >= target_high:
            # High target hit - definitely close
            return CloseDecision(
                should_close=True,
                close_type="market",
                net_pnl=net_pnl,
                gross_pnl=gross_pnl,
                fee_estimate=buffered_fee_estimate,
                reason=f"High net target hit: net=${net_pnl:.4f} >= ${required_net_profit:.4f}; gross=${gross_pnl:.2f} >= ${target_high:.2f} (target={self.target_pnl_pct_high}%)",
            )
        
        # Drawdown check: for this strategy we do not crystallize negative PnL
        # just because a filled position is underwater. Funded challenge rule:
        # hold losing positions until they recover positive, then close/recycle.
        if gross_pnl < 0:
            return CloseDecision(
                should_close=False,
                close_type="hold",
                net_pnl=net_pnl,
                gross_pnl=gross_pnl,
                fee_estimate=buffered_fee_estimate,
                reason=f"Negative PnL ${gross_pnl:.4f} - holding until position returns positive",
            )
        
        # Partial profit with good fills - consider closing early
        if gross_pnl > 0 and net_pnl >= required_net_profit and status.filled_levels >= status.grid_levels * 0.7:
            # 70%+ of grid filled and profitable - close
            return CloseDecision(
                should_close=True,
                close_type="limit",
                net_pnl=net_pnl,
                gross_pnl=gross_pnl,
                fee_estimate=buffered_fee_estimate,
                reason=f"Grid {status.filled_levels}/{status.grid_levels} filled and ${gross_pnl:.2f} profit - close with limit",
                suggested_price=spread_info["mid"],
            )
        
        # Stale check - long time, no progress
        if status.age_seconds > 1800 and status.filled_levels < 3:
            return CloseDecision(
                should_close=True,
                close_type="limit",
                net_pnl=net_pnl,
                gross_pnl=gross_pnl,
                fee_estimate=buffered_fee_estimate,
                reason=f"Stale grid: {status.age_seconds/60:.0f}min, {status.filled_levels} fills - closing",
                suggested_price=spread_info["mid"],
            )
        
        # Hold position
        return CloseDecision(
            should_close=False,
            close_type="hold",
            net_pnl=net_pnl,
            gross_pnl=gross_pnl,
            fee_estimate=buffered_fee_estimate,
            reason=f"Target: ${target_low:.2f}, Current: ${gross_pnl:.2f}, Spread: {spread_info['spread_pct']:.3f}%",
        )
    
    def get_close_recommendation_summary(
        self,
        status: GridStatus,
    ) -> dict:
        """Get human-readable close recommendation."""
        decision = self.should_close(status)
        
        return {
            "action": "CLOSE" if decision.should_close else "HOLD",
            "close_type": decision.close_type,
            "gross_pnl_usdt": round(decision.gross_pnl, 4),
            "net_pnl_usdt": round(decision.net_pnl, 4),
            "fees_usdt": round(decision.fee_estimate, 4),
            "reason": decision.reason,
            "suggested_price": decision.suggested_price,
        }


# Example usage and test
if __name__ == "__main__":
    optimizer = TradeCloseOptimizer()
    
    # Test scenario: profitable grid
    status = GridStatus(
        fills=5,
        realized_pnl=1.8,
        unrealized_pnl=0.3,
        allocated_margin=20.0,
        order_size_usdt=2.0,
        avg_entry_price=100.0,
        current_bid=101.0,
        current_ask=101.02,
        current_mark=101.01,
        direction="neutral",
        grid_levels=10,
        filled_levels=5,
        age_seconds=300,
    )
    
    decision = optimizer.should_close(status)
    print(f"\nTest Result:")
    print(f"  Should close: {decision.should_close}")
    print(f"  Close type: {decision.close_type}")
    print(f"  Gross PnL: ${decision.gross_pnl:.2f}")
    print(f"  Net PnL: ${decision.net_pnl:.2f} (after fees: ${decision.fee_estimate:.2f})")
    print(f"  Reason: {decision.reason}")
