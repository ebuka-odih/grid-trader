"""
Trading Agent — the LLM-powered decision layer for grid trading.

Uses NVIDIA NIM API to make intelligent trading decisions:
 1. PRE-TRADE: Which coin, what direction, how wide the grid
 2. MID-TRADE: Adjust grid, shift levels, hedge
 3. CLOSE: When to exit (not just PnL target — reads the market)
 4. POST-TRADE: Learn from results, update strategy

The agent is called at DECISION POINTS, not on every tick.
It's too slow/expensive for tick-by-tick — the algo handles the fast stuff.

v2: Supports shared OpenAI client (one for all grids), retry with
    exponential backoff, and fallback model for resilience.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI

from config import TARGET_PNL_LOW, TARGET_PNL_HIGH, MAX_DRAWDOWN_PCT, BASE_ORDER_SIZE_USDT

logger = logging.getLogger("trading_agent")


# ── Data Structures ──────────────────────────────────────────────

@dataclass
class PreTradeDecision:
    """Agent's decision before deploying a grid."""
    symbol: str
    direction: str  # "long", "short", "neutral"
    confidence: float  # 0-1
    upper: float
    lower: float
    num_grids: int
    leverage: int
    reasoning: str
    market_regime: str  # "trending_up", "trending_down", "ranging", "volatile"
    narrative: str  # brief market context


@dataclass
class MidTradeDecision:
    """Agent's decision while a grid is running."""
    action: str  # "hold", "shift_up", "shift_down", "tighten", "widen", "close", "hedge"
    shift_pct: float = 0.0  # % to shift grid (for shift actions)
    new_leverage: int = 0  # 0 = no change
    reasoning: str = ""
    confidence: float = 0.0


@dataclass
class CloseDecision:
    """Agent's decision on whether to close a grid."""
    should_close: bool
    urgency: str  # "immediate", "soon", "no_rush"
    reasoning: str
    confidence: float = 0.0


@dataclass
class PostTradeLearning:
    """Agent's analysis after a completed trade cycle."""
    what_worked: str
    what_failed: str
    suggestion: str
    pattern_observed: str


# ── Shared Client Factory ────────────────────────────────────────

def create_shared_client() -> OpenAI:
    """
    Create a shared OpenAI client for NVIDIA NIM API.
    Call this ONCE in MultiGridManager and pass to all TradingAgent instances.
    """
    api_key = os.getenv("NVIDIA_API_KEY", "")
    if not api_key:
        raise ValueError("NVIDIA_API_KEY not set in environment")

    client = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=api_key,
        timeout=30.0,  # 30s default timeout
        max_retries=2,  # SDK-level retries for connection errors
    )
    logger.info("🤖 Shared OpenAI client created (NVIDIA NIM)")
    return client


# ── Trading Agent ────────────────────────────────────────────────

class TradingAgent:
    """
    Dedicated LLM-powered trading agent.
    Uses NVIDIA NIM API for fast, cheap inference.

    v2: Accepts shared OpenAI client. Creates own only if not provided.
    Includes retry with exponential backoff and fallback model.
    """

    # Retry config
    MAX_RETRIES = 3
    RETRY_DELAYS = [1, 2, 4]  # exponential backoff in seconds

    # Retryable HTTP status codes
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, client: Optional[OpenAI] = None, model: Optional[str] = None):
        """
        Initialize trading agent.

        Args:
            client: Shared OpenAI client. If None, creates a new one.
            model: Model name. If None, reads from env var TRADING_AGENT_MODEL.
        """
        if client is not None:
            self.client = client
            self._owns_client = False
        else:
            self.client = create_shared_client()
            self._owns_client = True

        # Model selection — use a fast, cheap model for trading decisions
        # meta/llama-3.3-70b-instruct is fast + good at structured output
        self.model = model or os.getenv("TRADING_AGENT_MODEL", "meta/llama-3.3-70b-instruct")

        # Fallback model — smaller, faster, more reliable
        self.fallback_model = os.getenv(
            "TRADING_AGENT_FALLBACK_MODEL",
            "meta/llama-3.1-8b-instruct"
        )

        # Conversation memory for this trading session
        self._history: list[dict] = []
        self._trade_count = 0

        logger.info(f"🤖 Trading Agent initialized | model={self.model} | fallback={self.fallback_model} | shared_client={not self._owns_client}")

    def _call_llm(self, system_prompt: str, user_prompt: str, max_tokens: int = 512) -> str:
        """
        Call the NVIDIA LLM with structured prompts.
        Includes retry with exponential backoff and fallback model.

        Retry logic:
        - Max 3 retries with 1s, 2s, 4s delays
        - Retry on: connection errors, 5xx, rate limits (429)
        - Don't retry on: auth errors (401/403), invalid requests (400)
        - After all retries fail, try fallback model once
        - On complete failure, return empty string
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Try primary model with retries
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.3,  # Low temp for consistent trading decisions
                    max_tokens=max_tokens,
                    timeout=30.0,
                )
                content = response.choices[0].message.content.strip()
                logger.info(f"🤖 LLM response ({len(content)} chars) | model={self.model} | attempt={attempt + 1}")
                return content

            except Exception as e:
                last_error = e
                error_str = str(e)
                status_code = getattr(e, "status_code", None)

                # Check if we should retry
                should_retry = False
                if status_code in self.RETRYABLE_STATUS_CODES:
                    should_retry = True
                elif "rate_limit" in error_str.lower() or "429" in error_str:
                    should_retry = True
                elif "timeout" in error_str.lower() or "connection" in error_str.lower():
                    should_retry = True
                elif status_code in {401, 403, 400}:
                    # Auth/bad request — don't retry
                    logger.error(f"🤖 LLM call failed (non-retryable): {e}")
                    break

                if should_retry and attempt < self.MAX_RETRIES - 1:
                    delay = self.RETRY_DELAYS[attempt]
                    logger.warning(f"🤖 LLM call failed (attempt {attempt + 1}/{self.MAX_RETRIES}), retrying in {delay}s: {e}")
                    time.sleep(delay)
                elif should_retry:
                    logger.error(f"🤖 LLM call failed after {self.MAX_RETRIES} retries: {e}")

        # All retries exhausted — try fallback model
        logger.warning(f"🤖 Primary model failed, trying fallback: {self.fallback_model}")
        try:
            response = self.client.chat.completions.create(
                model=self.fallback_model,
                messages=messages,
                temperature=0.3,
                max_tokens=max_tokens,
                timeout=30.0,
            )
            content = response.choices[0].message.content.strip()
            logger.info(f"🤖 Fallback LLM response ({len(content)} chars) | model={self.fallback_model}")
            return content
        except Exception as e:
            logger.error(f"🤖 Fallback model also failed: {e}")

        # Complete failure
        logger.error(f"🤖 All LLM calls failed (primary + fallback). Last error: {last_error}")
        return ""

    def _parse_json(self, text: str) -> Optional[dict]:
        """Extract JSON from LLM response (handles markdown code blocks)."""
        import re
        json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if json_match:
            text = json_match.group(1)
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            brace_match = re.search(r'\{[\s\S]*\}', text)
            if brace_match:
                try:
                    return json.loads(brace_match.group(0))
                except json.JSONDecodeError:
                    pass
            logger.warning(f"Failed to parse JSON from LLM: {text[:200]}")
            return None

    # ── Decision 1: PRE-TRADE ────────────────────────────────────

    def decide_pre_trade(self, top_coins: list[dict]) -> Optional[PreTradeDecision]:
        """
        Agent picks which coin to trade, what direction, and grid parameters.
        Receives top-5 coins from the scanner with all metrics.
        """
        self._trade_count += 1

        system_prompt = """You are an expert crypto grid trading agent. Your job is to select the best coin for grid trading and decide the direction and grid parameters.

You must respond with valid JSON only. No explanations outside the JSON.

Market regimes:
- "trending_up": Price making higher highs — lean LONG (more buy levels below, sell levels above)
- "trending_down": Price making lower lows — lean SHORT (more sell levels above, buy levels below)
- "ranging": Price oscillating in a range — NEUTRAL grid (balanced buy/sell)
- "volatile": Large swings both ways — wider grid, lower leverage

Direction rules:
- "long": Bias buy levels (70% buy, 30% sell) — price likely to go up
- "short": Bias sell levels (70% sell, 30% buy) — price likely to go down
- "neutral": Balanced grid (50/50) — ranging market

JSON format:
{
  "symbol": "COIN/USDT:USDT",
  "direction": "long|short|neutral",
  "confidence": 0.0-1.0,
  "upper": float,
  "lower": float,
  "num_grids": int,
  "leverage": int,
  "reasoning": "brief explanation",
  "market_regime": "trending_up|trending_down|ranging|volatile",
  "narrative": "1-sentence market context"
}

IMPORTANT: Choose the coin with the BEST risk/reward for grid trading RIGHT NOW. Consider recent price action, mean reversion quality, and volume."""

        coins_text = json.dumps(top_coins, indent=2)
        user_prompt = f"""Here are the top {len(top_coins)} coins from the scanner. Pick the best one for grid trading right now.

Scanner results (ranked by algorithmic score):
{coins_text}

Trade #{self._trade_count} | Target PnL: ${TARGET_PNL_LOW}-${TARGET_PNL_HIGH} | Max drawdown: {MAX_DRAWDOWN_PCT}%

Respond with JSON only."""

        raw = self._call_llm(system_prompt, user_prompt, max_tokens=400)
        if not raw:
            return None

        parsed = self._parse_json(raw)
        if not parsed:
            logger.warning("🤖 Pre-trade: failed to parse LLM response, falling back to top-1")
            return None

        try:
            decision = PreTradeDecision(
                symbol=parsed["symbol"],
                direction=parsed.get("direction", "neutral"),
                confidence=float(parsed.get("confidence", 0.5)),
                upper=float(parsed["upper"]),
                lower=float(parsed["lower"]),
                num_grids=int(parsed["num_grids"]),
                leverage=int(parsed["leverage"]),
                reasoning=parsed.get("reasoning", ""),
                market_regime=parsed.get("market_regime", "ranging"),
                narrative=parsed.get("narrative", ""),
            )
            logger.info(
                f"🤖 PRE-TRADE: {decision.symbol} | dir={decision.direction} | "
                f"regime={decision.market_regime} | conf={decision.confidence:.2f} | "
                f"grid={decision.lower:.4f}-{decision.upper:.4f} | "
                f"grids={decision.num_grids} | lev={decision.leverage}x"
            )
            logger.info(f"🤖 Reasoning: {decision.reasoning}")
            logger.info(f"🤖 Narrative: {decision.narrative}")

            self._history.append({
                "type": "pre_trade",
                "decision": decision.__dict__,
                "timestamp": time.time(),
            })

            return decision
        except (KeyError, ValueError) as e:
            logger.error(f"🤖 Pre-trade parse error: {e} | raw: {parsed}")
            return None

    # ── Decision 2: MID-TRADE ────────────────────────────────────

    def decide_mid_trade(self, grid_status: dict) -> MidTradeDecision:
        """
        Agent evaluates a running grid and decides whether to adjust.
        Called every 2-3 minutes during active trading.
        """
        system_prompt = """You are an expert crypto grid trading agent. A grid is currently running and you need to decide if any adjustments are needed.

Respond with valid JSON only:

{
  "action": "hold|shift_up|shift_down|tighten|widen|close|hedge",
  "shift_pct": 0.0,
  "new_leverage": 0,
  "reasoning": "brief explanation",
  "confidence": 0.0-1.0
}

Actions:
- "hold": Grid is fine, no changes needed
- "shift_up": Move grid up by shift_pct% (price trending up, grid too low)
- "shift_down": Move grid down by shift_pct% (price trending down, grid too high)
- "tighten": Narrow the grid range (market calming down)
- "widen": Widen the grid range (market getting more volatile)
- "close": Close the grid now (conditions deteriorated)
- "hedge": Open opposing position (direction changed against us)

Guidelines:
- If price is near grid edge and trending out, SHIFT the grid
- If fills are slow (low activity), consider TIGHTENing
- If losing money and trend is against us, consider CLOSE or HEDGE
- Default to HOLD if uncertain — don't over-manage"""

        user_prompt = f"""Current grid status:
{json.dumps(grid_status, indent=2)}

What should we do? Respond with JSON only."""

        raw = self._call_llm(system_prompt, user_prompt, max_tokens=300)
        if not raw:
            return MidTradeDecision(action="hold", reasoning="LLM call failed", confidence=0.0)

        parsed = self._parse_json(raw)
        if not parsed:
            return MidTradeDecision(action="hold", reasoning="Failed to parse LLM response", confidence=0.0)

        try:
            decision = MidTradeDecision(
                action=parsed.get("action", "hold"),
                shift_pct=float(parsed.get("shift_pct", 0.0)),
                new_leverage=int(parsed.get("new_leverage", 0)),
                reasoning=parsed.get("reasoning", ""),
                confidence=float(parsed.get("confidence", 0.0)),
            )
            logger.info(
                f"🤖 MID-TRADE: {decision.action} | conf={decision.confidence:.2f} | "
                f"shift={decision.shift_pct:.1f}% | lev={decision.new_leverage}"
            )
            logger.info(f"🤖 Reasoning: {decision.reasoning}")

            self._history.append({
                "type": "mid_trade",
                "decision": decision.__dict__,
                "timestamp": time.time(),
            })

            return decision
        except (KeyError, ValueError) as e:
            logger.error(f"🤖 Mid-trade parse error: {e}")
            return MidTradeDecision(action="hold", reasoning="Parse error", confidence=0.0)

    # ── Decision 3: CLOSE ────────────────────────────────────────

    def decide_close(self, grid_status: dict, close_trigger: str) -> CloseDecision:
        """
        Agent decides whether to close a grid now.
        Called when PnL target is close, drawdown approaching, or timeout.
        """
        system_prompt = """You are an expert crypto grid trading agent. A close decision is needed.

Respond with valid JSON only:

{
  "should_close": true/false,
  "urgency": "immediate|soon|no_rush",
  "reasoning": "brief explanation",
  "confidence": 0.0-1.0
}

Close if:
- PnL target reached and momentum is fading
- Market regime changed (trending against our direction)
- Close trigger is "drawdown" — close immediately
- Multiple fills completed and profit is decent

Don't close if:
- Grid is actively filling and PnL is improving
- Market is still ranging nicely
- Only a few fills so far — give it more time"""

        user_prompt = f"""Grid status:
{json.dumps(grid_status, indent=2)}

Close trigger: {close_trigger}

Should we close? Respond with JSON only."""

        raw = self._call_llm(system_prompt, user_prompt, max_tokens=200)
        if not raw:
            return CloseDecision(
                should_close=(close_trigger == "drawdown"),
                urgency="immediate" if close_trigger == "drawdown" else "no_rush",
                reasoning="LLM fallback",
                confidence=0.0,
            )

        parsed = self._parse_json(raw)
        if not parsed:
            return CloseDecision(should_close=False, urgency="no_rush", reasoning="Parse failed", confidence=0.0)

        try:
            decision = CloseDecision(
                should_close=bool(parsed.get("should_close", False)),
                urgency=parsed.get("urgency", "no_rush"),
                reasoning=parsed.get("reasoning", ""),
                confidence=float(parsed.get("confidence", 0.0)),
            )
            logger.info(
                f"🤖 CLOSE: {'YES' if decision.should_close else 'NO'} | "
                f"urgency={decision.urgency} | conf={decision.confidence:.2f}"
            )
            logger.info(f"🤖 Reasoning: {decision.reasoning}")
            return decision
        except (KeyError, ValueError) as e:
            logger.error(f"🤖 Close parse error: {e}")
            return CloseDecision(should_close=False, urgency="no_rush", reasoning="Parse error", confidence=0.0)

    # ── Decision 4: POST-TRADE ───────────────────────────────────

    def analyze_post_trade(self, cycle_result: dict) -> Optional[PostTradeLearning]:
        """
        Agent learns from a completed trade cycle.
        Identifies patterns, what worked, what failed, and suggests improvements.
        """
        system_prompt = """You are an expert crypto grid trading agent analyzing a completed trade cycle.

Respond with valid JSON only:

{
  "what_worked": "what went well in this trade",
  "what_failed": "what went wrong or could be improved",
  "suggestion": "concrete suggestion for next trade",
  "pattern_observed": "market pattern noticed (e.g., 'AAVE ranges well in Asian session')"
}

Be specific and actionable. Focus on things that can be parameterized:
- Grid width (too wide/narrow)
- Leverage (too high/low)
- Direction bias (should have been long/short/neutral)
- Timing (better times to trade)
- Coin selection (type of coin that works best)"""

        user_prompt = f"""Completed trade cycle:
{json.dumps(cycle_result, indent=2)}

Trade history this session: {len(self._history)} decisions

What did we learn? Respond with JSON only."""

        raw = self._call_llm(system_prompt, user_prompt, max_tokens=300)
        if not raw:
            return None

        parsed = self._parse_json(raw)
        if not parsed:
            return None

        try:
            learning = PostTradeLearning(
                what_worked=parsed.get("what_worked", ""),
                what_failed=parsed.get("what_failed", ""),
                suggestion=parsed.get("suggestion", ""),
                pattern_observed=parsed.get("pattern_observed", ""),
            )
            logger.info(f"🤖 POST-TRADE LEARNING:")
            logger.info(f"  ✅ Worked: {learning.what_worked}")
            logger.info(f"  ❌ Failed: {learning.what_failed}")
            logger.info(f"  💡 Suggestion: {learning.suggestion}")
            logger.info(f"  🔍 Pattern: {learning.pattern_observed}")

            self._history.append({
                "type": "post_trade",
                "learning": learning.__dict__,
                "timestamp": time.time(),
            })

            return learning
        except (KeyError, ValueError) as e:
            logger.error(f"🤖 Post-trade parse error: {e}")
            return None

    # ── Utility ──────────────────────────────────────────────────

    def get_session_summary(self) -> dict:
        """Get a summary of all agent decisions this session."""
        return {
            "trade_count": self._trade_count,
            "total_decisions": len(self._history),
            "decisions_by_type": {
                t: sum(1 for h in self._history if h["type"] == t)
                for t in ["pre_trade", "mid_trade", "post_trade"]
            },
            "model": self.model,
            "fallback_model": self.fallback_model,
        }
