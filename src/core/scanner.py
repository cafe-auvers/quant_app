"""Stock scanner with rule-based filtering."""
from typing import List, Dict, Callable
from dataclasses import dataclass
from enum import Enum


class ComparisonOperator(Enum):
    """Comparison operators for rules."""
    GREATER_THAN = ">"
    LESS_THAN = "<"
    EQUAL = "=="
    GREATER_EQUAL = ">="
    LESS_EQUAL = "<="
    NOT_EQUAL = "!="


@dataclass
class ScanRule:
    """A single scanning rule."""
    name: str
    attribute: str  # e.g., "price", "volume", "rsi"
    operator: ComparisonOperator
    threshold: float
    
    def evaluate(self, stock_data: Dict) -> bool:
        """
        Evaluate if stock data passes this rule.
        
        Args:
            stock_data: Dictionary with stock metrics
        
        Returns:
            True if rule is satisfied
        """
        value = stock_data.get(self.attribute)
        if value is None:
            return False
        
        if self.operator == ComparisonOperator.GREATER_THAN:
            return value > self.threshold
        elif self.operator == ComparisonOperator.LESS_THAN:
            return value < self.threshold
        elif self.operator == ComparisonOperator.EQUAL:
            return value == self.threshold
        elif self.operator == ComparisonOperator.GREATER_EQUAL:
            return value >= self.threshold
        elif self.operator == ComparisonOperator.LESS_EQUAL:
            return value <= self.threshold
        elif self.operator == ComparisonOperator.NOT_EQUAL:
            return value != self.threshold
        
        return False


class StockScanner:
    """Rule-based stock scanner."""
    
    def __init__(self):
        """Initialize the scanner."""
        self.rules: List[ScanRule] = []
    
    def add_rule(self, rule: ScanRule) -> None:
        """Add a scanning rule."""
        self.rules.append(rule)
    
    def clear_rules(self) -> None:
        """Clear all rules."""
        self.rules.clear()
    
    def scan(self, stocks: List[Dict]) -> List[Dict]:
        """
        Scan a list of stocks against all rules.
        
        Args:
            stocks: List of stock data dictionaries
        
        Returns:
            Filtered list of stocks that pass all rules
        """
        results = []
        
        for stock in stocks:
            if all(rule.evaluate(stock) for rule in self.rules):
                results.append(stock)
        
        return results
    
    def scan_with_scoring(self, stocks: List[Dict], 
                         scorers: List[Callable[[Dict], float]]) -> List[Dict]:
        """
        Scan stocks and add a score to each.
        
        Args:
            stocks: List of stock data
            scorers: List of functions that score each stock
        
        Returns:
            Filtered and scored stocks, sorted by score descending
        """
        results = self.scan(stocks)
        
        # Add scores
        for stock in results:
            scores = [scorer(stock) for scorer in scorers]
            stock['score'] = sum(scores) / len(scores) if scores else 0.0
        
        # Sort by score descending
        results.sort(key=lambda x: x['score'], reverse=True)
        
        return results

    def set_threshold_rules(self,
                            min_price_history_days: float = 1,
                            min_volume: float = 40000.0,
                            min_dollar_volume: float = 35000.0,
                            min_adr: float = 2.4,
                            min_growth_rank: float = 97.04,
                            min_trend_intensity: float = 90.0) -> None:
        """Set up scanner rules from threshold values."""
        self.clear_rules()
        self.add_rule(ScanRule(
            name="Price history",
            attribute="price_history_days",
            operator=ComparisonOperator.GREATER_EQUAL,
            threshold=min_price_history_days,
        ))
        self.add_rule(ScanRule(
            name="Daily volume",
            attribute="volume",
            operator=ComparisonOperator.GREATER_EQUAL,
            threshold=min_volume,
        ))
        self.add_rule(ScanRule(
            name="Daily dollar volume",
            attribute="dollar_volume",
            operator=ComparisonOperator.GREATER_EQUAL,
            threshold=min_dollar_volume,
        ))
        self.add_rule(ScanRule(
            name="ADR 20-day",
            attribute="adr",
            operator=ComparisonOperator.GREATER_EQUAL,
            threshold=min_adr,
        ))
        self.add_rule(ScanRule(
            name="1-month growth rank",
            attribute="growth_rank",
            operator=ComparisonOperator.GREATER_EQUAL,
            threshold=min_growth_rank,
        ))
        self.add_rule(ScanRule(
            name="Trend intensity",
            attribute="trend_intensity",
            operator=ComparisonOperator.GREATER_EQUAL,
            threshold=min_trend_intensity,
        ))
