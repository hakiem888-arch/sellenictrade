"""
Service layer for Momentum Radar v5.5 + Freqtrade Unified Integration.

Provides modular services for configuration & security validation, REST API communication,
background process management, strategy discovery & AI signal adaptation, historical data
management, backtesting, hyperopt optimization, and dry-run/live trading monitoring.
"""

from .config_service import ConfigService, LiveTradingSecurityError
from .freqtrade_client import FreqtradeClient, ApiResponse
from .process_manager import ProcessManager, ManagedJob
from .strategy_service import StrategyService
from .data_service import DataService
from .backtest_service import BacktestService
from .hyperopt_service import HyperoptService
from .trading_service import TradingService

__all__ = [
    "ConfigService",
    "LiveTradingSecurityError",
    "FreqtradeClient",
    "ApiResponse",
    "ProcessManager",
    "ManagedJob",
    "StrategyService",
    "DataService",
    "BacktestService",
    "HyperoptService",
    "TradingService",
]
