"""
Configuration Template for Stock Dashboard
Copy this to kis_config.py and fill in your credentials.
"""

# KIS API Configuration
KIS_API = {
    "base_url": "https://openapi.koreainvestment.com:9443",
    "app_key": "YOUR_APP_KEY_HERE",
    "app_secret": "YOUR_APP_SECRET_HERE",
    "account_no": "YOUR_ACCOUNT_NO_HERE",
    "virtual": True,  # True for virtual/paper trading
}

# Portfolio Configuration
PORTFOLIO = {
    "initial_capital": 10000000,  # 10 million KRW
    "max_position_size": 0.10,  # 10% per position
    "max_risk_per_trade": 0.02,  # 2% account risk per trade
    "currency": "KRW",
}

# Scanner Configuration
SCANNER = {
    "min_price": 5000,
    "max_price": 100000,
    "min_volume": 100000,  # shares per day
    "scan_interval": 300,  # seconds
}

# Chart Configuration
CHART = {
    "default_timeframe": "daily",
    "lookback_periods": {
        "daily": 252,    # 1 year
        "weekly": 52,    # 1 year
        "monthly": 60,   # 5 years
    },
}

# AI Configuration
AI = {
    "model": "gpt-4",
    "temperature": 0.3,  # Lower = more consistent
    "max_tokens": 1000,
    "rulebook_path": "rulebooks/",
}

# UI Configuration
UI = {
    "theme": "dark",  # or "light"
    "window_width": 1600,
    "window_height": 900,
    "update_interval": 1000,  # milliseconds
}

# Logging Configuration
LOGGING = {
    "level": "INFO",
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    "file": "logs/app.log",
}
