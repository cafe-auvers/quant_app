"""AI-powered trade review against markdown rulebooks."""
import math
from typing import Dict, List, Optional
from pathlib import Path
from dataclasses import dataclass

from src.utils.config import RULEBOOK_DIR


@dataclass
class TradeSetup:
    """A trade setup for review."""
    symbol: str
    entry_price: float
    stop_loss: float
    take_profit: float
    size_shares: int
    risk_amount: float
    reasoning: str  # User's explanation for the trade


@dataclass
class TradeReview:
    """Result of AI trade review."""
    approved: bool
    confidence: float  # 0.0 to 1.0
    summary: str
    violations: List[str]  # Rules that were violated
    recommendations: List[str]  # Suggestions
    reasoning: str  # Detailed explanation


class TradeReviewer:
    """AI-powered trade setup reviewer."""
    
    def __init__(self, rulebook_dir: str | Path = RULEBOOK_DIR):
        """
        Initialize the trade reviewer.
        
        Args:
            rulebook_dir: Directory containing markdown rulebook files
        """
        self.rulebook_dir = Path(rulebook_dir)
        self.rulebooks: Dict[str, str] = {}
        self._load_rulebooks()
    
    def _load_rulebooks(self) -> None:
        """Load all markdown rulebook files."""
        if not self.rulebook_dir.exists():
            return
        
        for rulebook_file in self.rulebook_dir.glob("*.md"):
            with open(rulebook_file, "r", encoding="utf-8") as f:
                self.rulebooks[rulebook_file.name] = f.read()
    
    def review_trade(self, setup: TradeSetup, use_ai: bool = True) -> TradeReview:
        """
        Review a trade setup against rulebooks.
        
        Args:
            setup: Trade setup to review
            use_ai: Whether to use AI review (requires OpenAI API key)
        
        Returns:
            TradeReview with approval status and feedback
        """
        if use_ai and self.rulebooks:
            try:
                return self._review_with_ai(setup)
            except NotImplementedError:
                # Rulebooks ship with the app while the optional AI adapter is
                # intentionally not configured.  Never let that optional path
                # block a user from receiving the deterministic safety review.
                pass
        return self._review_with_rules(setup)
    
    def _review_with_rules(self, setup: TradeSetup) -> TradeReview:
        """
        Review trade using basic rule parsing.
        
        Args:
            setup: Trade setup to review
        
        Returns:
            TradeReview object
        """
        violations = []
        recommendations = []

        def finite_float(value) -> Optional[float]:
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return number if math.isfinite(number) else None

        entry_price = finite_float(setup.entry_price)
        stop_loss = finite_float(setup.stop_loss)
        risk_amount = finite_float(setup.risk_amount)
        try:
            size_shares = int(setup.size_shares)
        except (TypeError, ValueError):
            size_shares = 0
        reasoning = str(setup.reasoning or "").strip()
        
        # Basic checks
        if size_shares <= 0:
            violations.append("Position size must be greater than zero")

        if entry_price is None or entry_price <= 0:
            violations.append("Entry price must be a finite positive value")
        if stop_loss is None or stop_loss <= 0:
            violations.append("Stop loss must be a finite positive value")
        elif entry_price is not None and entry_price > 0 and stop_loss >= entry_price:
            violations.append("Stop loss must be below entry")

        if risk_amount is None or risk_amount < 0:
            violations.append("Risk amount must be a finite non-negative value")

        if not reasoning:
            recommendations.append("Add a trade thesis before execution")
        
        # Profit management is rule-based for this strategy; fixed take-profit
        # prices and R/R targets are intentionally not required.
        risk = (
            entry_price - stop_loss
            if entry_price is not None
            and entry_price > 0
            and stop_loss is not None
            and 0 < stop_loss < entry_price
            else 0.0
        )

        stop_distance = risk / entry_price if entry_price and risk > 0 else 0
        if stop_distance > 0.15:
            recommendations.append(f"Stop is {stop_distance * 100:.1f}% below entry; consider whether the setup is too wide")
        elif 0 < stop_distance < 0.02:
            recommendations.append(f"Stop is only {stop_distance * 100:.1f}% below entry; check normal volatility")
        
        recommendations.append(
            "Use rule-based exits: sell 1/3 to 1/2 after 3-5 working days if the trade has worked, then exit the rest on a close below the selected 10 EMA or 20 EMA."
        )
        
        approved = len(violations) == 0
        confidence = 0.85 if approved and reasoning else 0.65 if approved else 0.25
        
        return TradeReview(
            approved=approved,
            confidence=confidence,
            summary="Trade setup passes basic checks" if approved else "Trade setup has violations",
            violations=violations,
            recommendations=recommendations,
            reasoning="Rule-based exit model: partial after 3-5 working days; final exit below selected EMA."
        )
    
    def _review_with_ai(self, setup: TradeSetup) -> TradeReview:
        """
        Review trade using AI (OpenAI/Claude).
        
        Args:
            setup: Trade setup to review
        
        Returns:
            TradeReview object
        """
        # TODO: Implement AI review using LangChain + OpenAI
        # This will send the trade setup and rulebooks to GPT-4 for validation
        raise NotImplementedError("AI review not yet implemented")
