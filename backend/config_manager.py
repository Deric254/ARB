import sqlite3
import json
import os
from cryptography.fernet import Fernet
from typing import Optional, Dict

DB_PATH = os.path.join(os.path.dirname(__file__), 'config.db')

class ConfigManager:
    def __init__(self):
        self._key = self._get_or_create_key()
        self._cipher = Fernet(self._key)
        self._init_db()

    def _get_or_create_key(self) -> bytes:
        key_path = os.path.join(os.path.dirname(__file__), '.key')
        if os.path.exists(key_path):
            with open(key_path, 'rb') as f:
                return f.read()
        key = Fernet.generate_key()
        with open(key_path, 'wb') as f:
            f.write(key)
        return key

    def _init_db(self):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()

    def set(self, key: str, value: str):
        encrypted = self._cipher.encrypt(value.encode()).decode()
        conn = sqlite3.connect(DB_PATH)
        conn.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', (key, encrypted))
        conn.commit()
        conn.close()

    def get(self, key: str, default: str = '') -> str:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute('SELECT value FROM config WHERE key = ?', (key,)).fetchone()
        conn.close()
        if row:
            return self._cipher.decrypt(row[0].encode()).decode()
        return default

    def get_all(self) -> Dict[str, str]:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute('SELECT key, value FROM config').fetchall()
        conn.close()
        return {k: self._cipher.decrypt(v.encode()).decode() for k, v in rows}

    def delete(self, key: str):
        conn = sqlite3.connect(DB_PATH)
        conn.execute('DELETE FROM config WHERE key = ?', (key,))
        conn.commit()
        conn.close()

    def save_exchange_keys(self, exchange: str, api_key: str, api_secret: str, passphrase: str = ''):
        self.set(f'{exchange}_api_key', api_key)
        self.set(f'{exchange}_api_secret', api_secret)
        if passphrase:
            self.set(f'{exchange}_passphrase', passphrase)

    def get_exchange_keys(self, exchange: str) -> Dict[str, str]:
        return {
            'api_key': self.get(f'{exchange}_api_key'),
            'api_secret': self.get(f'{exchange}_api_secret'),
            'passphrase': self.get(f'{exchange}_passphrase')
        }

    def save_trading_config(self, cfg: dict):
        self.set('trading_config', json.dumps(cfg))

    def get_trading_config(self) -> dict:
        raw = self.get('trading_config')
        if raw:
            return json.loads(raw)
        return {
            'symbol': 'BTC/USDT',
            'target_volume': '0.001',
            'min_profit_usd': '15.0',
            'max_slippage_pct': '0.1',
            'vwap_depth': 20,
            'poll_interval_ms': 50,
            'max_daily_trades': 50,
            'max_daily_loss_usd': '500',
            'cooldown_seconds': 30,
            'max_drawdown_pct': '0.05',
            'paper_trading': True,
            'demo_mode': True
        }

    def save_branding(self, name: str, slogan: str):
        self.set('brand_name', name)
        self.set('brand_slogan', slogan)

    def get_branding(self) -> Dict[str, str]:
        return {
            'name': self.get('brand_name', 'ARB Pro'),
            'slogan': self.get('brand_slogan', 'High-Frequency Cross-Exchange Arbitrage')
        }
