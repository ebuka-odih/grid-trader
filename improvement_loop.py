"""
Improvement Loop v2 — trade journal (SQLite) with portfolio-level + cross-margin pattern tracking.

v2 Changes:
- Agent memory bridge: records portfolio-level patterns (not just per-grid)
- Cross-margin cascade tracking: detects when correlated positions move together
- Learning records from agent post-trade analysis
- Portfolio-level stats: correlation risk events, wallet PnL curves
- Historical context injection for agent prompts
"""

import logging
import json
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, Boolean, JSON, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from config import DEFAULT_LEVERAGE, DEFAULT_NUM_GRIDS

logger = logging.getLogger("improvement_loop")

Base = declarative_base()


class TradeRecord(Base):
    """SQLAlchemy model for individual trade records."""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    grid_id = Column(String(100), index=True)
    symbol = Column(String(50), index=True)
    side = Column(String(10))
    price = Column(Float)
    qty = Column(Float)
    realized_pnl = Column(Float, default=0.0)
    timestamp = Column(DateTime, default=datetime.utcnow)
    order_id = Column(String(100))


class GridCycleRecord(Base):
    """SQLAlchemy model for a full grid cycle (open → close)."""
    __tablename__ = "grid_cycles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    grid_id = Column(String(100), unique=True, index=True)
    symbol = Column(String(50), index=True)
    upper_price = Column(Float)
    lower_price = Column(Float)
    num_grids = Column(Integer)
    leverage = Column(Integer)
    total_pnl = Column(Float)
    realized_pnl = Column(Float, default=0.0)
    unrealized_pnl_at_close = Column(Float, default=0.0)
    fills_count = Column(Integer, default=0)
    duration_seconds = Column(Float, default=0.0)
    started_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)
    close_reason = Column(String(50))
    was_profitable = Column(Boolean, default=False)
    # v2: Cross-margin fields
    wallet_balance_at_close = Column(Float, default=0.0)
    wallet_exposure_pct_at_close = Column(Float, default=0.0)
    direction = Column(String(10), default="neutral")
    adjusted_leverage = Column(Integer, default=0)
    adjusted_order_size = Column(Float, default=0.0)


class AgentLearningRecord(Base):
    """v2: Records agent post-trade learning for memory bridge."""
    __tablename__ = "agent_learning"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), index=True)
    what_worked = Column(Text)
    what_failed = Column(Text)
    suggestion = Column(Text)
    pattern_observed = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)


class PortfolioRiskEvent(Base):
    """v2: Records portfolio-level risk events (emergency closures, exposure breaches)."""
    __tablename__ = "portfolio_risk_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(50), index=True)  # "emergency_close", "exposure_breach", "correlation_alert", "cascade_detected"
    symbol = Column(String(50), nullable=True)
    details = Column(Text)
    wallet_balance = Column(Float, default=0.0)
    total_exposure_pct = Column(Float, default=0.0)
    timestamp = Column(DateTime, default=datetime.utcnow)


class CascadePatternRecord(Base):
    """v2: Records cross-margin cascade patterns — when correlated positions move together."""
    __tablename__ = "cascade_patterns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    correlation_group = Column(String(50), index=True)
    symbols_involved = Column(Text)  # JSON array of symbols
    direction = Column(String(10))  # "all_long" or "all_short" or "mixed"
    total_pnl_impact = Column(Float, default=0.0)
    trigger_event = Column(String(100))  # e.g., "BTC_dump", "ETH_pump"
    duration_seconds = Column(Float, default=0.0)
    timestamp = Column(DateTime, default=datetime.utcnow)


class ImprovementLoop:
    """Tracks trade performance and suggests parameter improvements. v2: Portfolio-aware."""

    def __init__(self, db_path: str = "sqlite:///trades.db"):
        self.engine = create_engine(db_path, echo=False)
        Base.metadata.create_all(self.engine)
        self._migrate_existing_schema()
        self.Session = sessionmaker(bind=self.engine)
        logger.info(f"📊 Trade journal v2 initialized: {db_path}")

    def _migrate_existing_schema(self):
        """Add v2 columns to existing SQLite DBs without dropping trade history."""
        if self.engine.dialect.name != "sqlite":
            return

        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        if "grid_cycles" not in existing_tables:
            return

        existing_columns = {
            col["name"] for col in inspector.get_columns("grid_cycles")
        }
        model_columns = GridCycleRecord.__table__.columns
        missing_columns = [
            col for col in model_columns
            if col.name not in existing_columns and not col.primary_key
        ]
        if not missing_columns:
            return

        with self.engine.begin() as conn:
            for column in missing_columns:
                column_type = column.type.compile(dialect=self.engine.dialect)
                conn.execute(text(
                    f"ALTER TABLE grid_cycles ADD COLUMN {column.name} {column_type}"
                ))
                logger.info(f"📊 Migrated grid_cycles: added column {column.name}")

    # ── Record Trades ──────────────────────────────────────────

    def record_fill(self, grid_id: str, symbol: str, side: str, price: float,
                    qty: float, realized_pnl: float = 0.0, order_id: str = ""):
        """Record a single fill/execution."""
        session = self.Session()
        try:
            trade = TradeRecord(
                grid_id=grid_id, symbol=symbol, side=side,
                price=price, qty=qty, realized_pnl=realized_pnl,
                order_id=order_id,
            )
            session.add(trade)
            session.commit()
            logger.info(f"📝 Fill recorded: {side} {qty} @ {price:.4f} pnl=${realized_pnl:.4f}")
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to record fill: {e}")
        finally:
            session.close()

    def record_cycle_start(self, grid_id: str, symbol: str, upper: float,
                           lower: float, num_grids: int, leverage: int,
                           direction: str = "neutral", adjusted_leverage: int = 0,
                           adjusted_order_size: float = 0.0):
        """Record the start of a grid cycle, including v2 runtime metadata for open grids."""
        session = self.Session()
        try:
            cycle = GridCycleRecord(
                grid_id=grid_id, symbol=symbol,
                upper_price=upper, lower_price=lower,
                num_grids=num_grids, leverage=leverage,
                direction=direction,
                adjusted_leverage=adjusted_leverage or leverage,
                adjusted_order_size=adjusted_order_size,
            )
            session.add(cycle)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to record cycle start: {e}")
        finally:
            session.close()

    def record_cycle_close(self, grid_id: str, total_pnl: float, realized_pnl: float,
                           unrealized_pnl: float, fills: int, duration: float,
                           close_reason: str, wallet_balance: float = 0.0,
                           wallet_exposure_pct: float = 0.0, direction: str = "neutral",
                           adjusted_leverage: int = 0, adjusted_order_size: float = 0.0):
        """Record the close of a grid cycle with v2 wallet context."""
        session = self.Session()
        try:
            cycle = session.query(GridCycleRecord).filter_by(grid_id=grid_id).first()
            if cycle:
                cycle.total_pnl = total_pnl
                cycle.realized_pnl = realized_pnl
                cycle.unrealized_pnl_at_close = unrealized_pnl
                cycle.fills_count = fills
                cycle.duration_seconds = duration
                cycle.closed_at = datetime.utcnow()
                cycle.close_reason = close_reason
                cycle.was_profitable = total_pnl > 0
                # v2 fields
                cycle.wallet_balance_at_close = wallet_balance
                cycle.wallet_exposure_pct_at_close = wallet_exposure_pct
                cycle.direction = direction
                cycle.adjusted_leverage = adjusted_leverage
                cycle.adjusted_order_size = adjusted_order_size
                session.commit()
                logger.info(f"📝 Cycle closed: {grid_id} | pnl=${total_pnl:.4f} | reason={close_reason}")
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to record cycle close: {e}")
        finally:
            session.close()

    # ── v2: Agent Learning Records ─────────────────────────────

    def record_learning(self, symbol: str, what_worked: str, what_failed: str,
                        suggestion: str, pattern: str):
        """Record agent post-trade learning for memory bridge."""
        session = self.Session()
        try:
            record = AgentLearningRecord(
                symbol=symbol,
                what_worked=what_worked,
                what_failed=what_failed,
                suggestion=suggestion,
                pattern_observed=pattern,
            )
            session.add(record)
            session.commit()
            logger.info(f"🧠 Learning recorded: {symbol} | {suggestion[:80]}")
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to record learning: {e}")
        finally:
            session.close()

    def get_learnings(self, symbol: Optional[str] = None, last_n: int = 20) -> list[dict]:
        """Get recent agent learnings, optionally filtered by symbol."""
        session = self.Session()
        try:
            query = session.query(AgentLearningRecord)
            if symbol:
                query = query.filter_by(symbol=symbol)
            query = query.order_by(AgentLearningRecord.timestamp.desc()).limit(last_n)
            records = query.all()

            return [
                {
                    "symbol": r.symbol,
                    "what_worked": r.what_worked,
                    "what_failed": r.what_failed,
                    "suggestion": r.suggestion,
                    "pattern": r.pattern_observed,
                    "timestamp": str(r.timestamp),
                }
                for r in records
            ]
        finally:
            session.close()

    # ── v2: Portfolio Risk Events ──────────────────────────────

    def record_risk_event(self, event_type: str, symbol: Optional[str], details: str,
                          wallet_balance: float = 0.0, total_exposure_pct: float = 0.0):
        """Record a portfolio-level risk event."""
        session = self.Session()
        try:
            event = PortfolioRiskEvent(
                event_type=event_type,
                symbol=symbol,
                details=details,
                wallet_balance=wallet_balance,
                total_exposure_pct=total_exposure_pct,
            )
            session.add(event)
            session.commit()
            logger.info(f"🚨 Risk event recorded: {event_type} | {details[:80]}")
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to record risk event: {e}")
        finally:
            session.close()

    def get_risk_events(self, event_type: Optional[str] = None, last_n: int = 20) -> list[dict]:
        """Get recent portfolio risk events."""
        session = self.Session()
        try:
            query = session.query(PortfolioRiskEvent)
            if event_type:
                query = query.filter_by(event_type=event_type)
            query = query.order_by(PortfolioRiskEvent.timestamp.desc()).limit(last_n)
            records = query.all()

            return [
                {
                    "event_type": r.event_type,
                    "symbol": r.symbol,
                    "details": r.details,
                    "wallet_balance": r.wallet_balance,
                    "exposure_pct": r.total_exposure_pct,
                    "timestamp": str(r.timestamp),
                }
                for r in records
            ]
        finally:
            session.close()

    # ── v2: Cascade Pattern Tracking ───────────────────────────

    def record_cascade_pattern(self, correlation_group: str, symbols_involved: list[str],
                               direction: str, total_pnl_impact: float,
                               trigger_event: str, duration_seconds: float = 0.0):
        """Record a cross-margin cascade pattern — when correlated positions move together."""
        session = self.Session()
        try:
            record = CascadePatternRecord(
                correlation_group=correlation_group,
                symbols_involved=json.dumps(symbols_involved),
                direction=direction,
                total_pnl_impact=total_pnl_impact,
                trigger_event=trigger_event,
                duration_seconds=duration_seconds,
            )
            session.add(record)
            session.commit()
            logger.info(f"🌊 Cascade pattern recorded: {correlation_group} | {trigger_event} | pnl=${total_pnl_impact:.4f}")
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to record cascade pattern: {e}")
        finally:
            session.close()

    def get_cascade_patterns(self, correlation_group: Optional[str] = None, last_n: int = 20) -> list[dict]:
        """Get recorded cascade patterns for context injection."""
        session = self.Session()
        try:
            query = session.query(CascadePatternRecord)
            if correlation_group:
                query = query.filter_by(correlation_group=correlation_group)
            query = query.order_by(CascadePatternRecord.timestamp.desc()).limit(last_n)
            records = query.all()

            return [
                {
                    "correlation_group": r.correlation_group,
                    "symbols": json.loads(r.symbols_involved) if r.symbols_involved else [],
                    "direction": r.direction,
                    "total_pnl_impact": r.total_pnl_impact,
                    "trigger_event": r.trigger_event,
                    "timestamp": str(r.timestamp),
                }
                for r in records
            ]
        finally:
            session.close()

    # ── v2: Historical Context Bridge ──────────────────────────

    def get_agent_context(self, symbol: str, include_portfolio: bool = True) -> str:
        """
        Build a context string to inject into agent prompts.
        Combines per-symbol learnings, portfolio risk events, and cascade patterns.

        This is the AGENT MEMORY BRIDGE — it gives the LLM historical context
        about this symbol AND the portfolio so it can make better decisions.
        """
        parts = []

        # Per-symbol learnings
        learnings = self.get_learnings(symbol=symbol, last_n=5)
        if learnings:
            parts.append(f"## Recent learnings for {symbol}:")
            for l in learnings[:3]:
                parts.append(f"- Worked: {l['what_worked']}")
                parts.append(f"- Failed: {l['what_failed']}")
                parts.append(f"- Suggestion: {l['suggestion']}")

        # Portfolio-level context
        if include_portfolio:
            # Recent risk events
            risk_events = self.get_risk_events(last_n=5)
            if risk_events:
                parts.append("\n## Recent portfolio risk events:")
                for e in risk_events[:3]:
                    parts.append(f"- {e['event_type']}: {e['details']}")

            # Recent cascade patterns
            cascades = self.get_cascade_patterns(last_n=5)
            if cascades:
                parts.append("\n## Cross-margin cascade patterns observed:")
                for c in cascades[:3]:
                    parts.append(
                        f"- Group '{c['correlation_group']}': {c['trigger_event']} → "
                        f"${c['total_pnl_impact']:.4f} impact ({c['direction']})"
                    )

        return "\n".join(parts) if parts else ""

    # ── Performance Analysis ───────────────────────────────────

    def get_stats(self, symbol: Optional[str] = None, last_n: int = 50) -> dict:
        """Get performance statistics."""
        session = self.Session()
        try:
            query = session.query(GridCycleRecord)
            if symbol:
                query = query.filter_by(symbol=symbol)
            query = query.order_by(GridCycleRecord.closed_at.desc()).limit(last_n)
            cycles = query.all()

            if not cycles:
                return {"total_cycles": 0, "win_rate": 0, "avg_pnl": 0}

            wins = sum(1 for c in cycles if c.was_profitable)
            total_pnl = sum(c.total_pnl for c in cycles if c.total_pnl)
            avg_duration = sum(c.duration_seconds for c in cycles if c.duration_seconds) / max(len(cycles), 1)

            # Per-symbol breakdown
            symbol_stats = {}
            for c in cycles:
                if c.symbol not in symbol_stats:
                    symbol_stats[c.symbol] = {"count": 0, "wins": 0, "pnl": 0}
                symbol_stats[c.symbol]["count"] += 1
                symbol_stats[c.symbol]["wins"] += 1 if c.was_profitable else 0
                symbol_stats[c.symbol]["pnl"] += c.total_pnl or 0

            # v2: Direction breakdown
            direction_stats = {}
            for c in cycles:
                dir_key = c.direction or "neutral"
                if dir_key not in direction_stats:
                    direction_stats[dir_key] = {"count": 0, "wins": 0, "pnl": 0}
                direction_stats[dir_key]["count"] += 1
                direction_stats[dir_key]["wins"] += 1 if c.was_profitable else 0
                direction_stats[dir_key]["pnl"] += c.total_pnl or 0

            stats = {
                "total_cycles": len(cycles),
                "win_rate": wins / len(cycles) * 100,
                "total_pnl": round(total_pnl, 4),
                "avg_pnl": round(total_pnl / len(cycles), 4),
                "avg_duration_min": round(avg_duration / 60, 1),
                "best_cycle": round(max((c.total_pnl for c in cycles if c.total_pnl), default=0), 4),
                "worst_cycle": round(min((c.total_pnl for c in cycles if c.total_pnl), default=0), 4),
                "per_symbol": {
                    s: {
                        "count": v["count"],
                        "win_rate": round(v["wins"] / v["count"] * 100, 1),
                        "pnl": round(v["pnl"], 4),
                    }
                    for s, v in symbol_stats.items()
                },
                "per_direction": {
                    d: {
                        "count": v["count"],
                        "win_rate": round(v["wins"] / v["count"] * 100, 1),
                        "pnl": round(v["pnl"], 4),
                    }
                    for d, v in direction_stats.items()
                },
            }
            return stats
        finally:
            session.close()

    # ── Auto-Tune Suggestions ──────────────────────────────────

    def suggest_params(self) -> dict:
        """
        Analyze past performance and suggest parameter adjustments.
        v2: Includes direction and wallet context in suggestions.
        """
        stats = self.get_stats()
        suggestions = {
            "leverage": DEFAULT_LEVERAGE,
            "num_grids": DEFAULT_NUM_GRIDS,
            "reason": [],
        }

        if stats["total_cycles"] < 5:
            suggestions["reason"].append("Not enough data yet (need 5+ cycles). Using defaults.")
            return suggestions

        # Win rate too low → reduce leverage
        if stats["win_rate"] < 50:
            suggestions["leverage"] = max(3, DEFAULT_LEVERAGE - 3)
            suggestions["reason"].append(f"Win rate {stats['win_rate']:.0f}% < 50% → reduce leverage")

        # Average PnL too low → increase grid count (more trades)
        if stats["avg_pnl"] < 0.5:
            suggestions["num_grids"] = min(25, DEFAULT_NUM_GRIDS + 5)
            suggestions["reason"].append(f"Avg PnL ${stats['avg_pnl']:.2f} < $0.50 → more grids for more fills")

        # Avg PnL high → can afford fewer grids (less risk)
        if stats["avg_pnl"] > 3.0:
            suggestions["num_grids"] = max(5, DEFAULT_NUM_GRIDS - 3)
            suggestions["reason"].append(f"Avg PnL ${stats['avg_pnl']:.2f} > $3.00 → fewer grids, less risk")

        # Best performing symbol
        if stats["per_symbol"]:
            best = max(stats["per_symbol"].items(), key=lambda x: x[1]["pnl"])
            suggestions["preferred_symbol"] = best[0]
            suggestions["reason"].append(f"Best symbol: {best[0]} (pnl=${best[1]['pnl']:.2f})")

        # v2: Direction analysis
        if "per_direction" in stats and stats["per_direction"]:
            best_dir = max(stats["per_direction"].items(), key=lambda x: x[1]["win_rate"])
            if best_dir[1]["win_rate"] > 60:
                suggestions["direction_bias"] = best_dir[0]
                suggestions["reason"].append(
                    f"Best direction: {best_dir[0]} ({best_dir[1]['win_rate']:.0f}% win rate)"
                )

        logger.info(f"🧠 Suggestions: lev={suggestions['leverage']}x grids={suggestions['num_grids']} | {'; '.join(suggestions['reason'])}")
        return suggestions
