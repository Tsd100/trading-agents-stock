import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import market_server as market


class LocalTencentUniverseTests(unittest.TestCase):
    def test_empty_blacklist_does_not_query_quote_providers(self):
        payload = {"stocks": [], "source": "本地空名单"}
        with patch.object(market, "is_intraday_alert_session") as session, \
                patch.object(market, "latest_tushare_trade_date") as tushare:
            self.assertIs(market.hydrate_list_quotes(payload), payload)
            session.assert_not_called()
            tushare.assert_not_called()

    def test_closed_market_policy_uses_newer_tencent_close_date(self):
        now = datetime(2026, 10, 1, 10, 30, tzinfo=timezone(timedelta(hours=8)))
        with patch.object(market, "is_a_share_trading_day", return_value=False), \
                patch.object(market, "latest_local_market_trade_date", return_value="2026-09-29"), \
                patch.object(market, "tencent_index_trade_date", return_value="2026-09-30"):
            policy = market.get_market_data_policy(now)
        self.assertEqual(policy["tradeDate"], "2026-09-30")
        self.assertFalse(policy["live"])

    def test_calendar_fallback_checks_quote_date_instead_of_weekday(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE a_share_trading_calendar (trade_date TEXT, is_open INTEGER)")
        market._a_share_trade_day_cache.pop("2026-10-01", None)
        try:
            with patch.object(market, "open_backtest_db", return_value=db), \
                    patch.object(market, "tushare_pro_request", side_effect=market.MarketDataError("rate limit")), \
                    patch.object(market, "tencent_index_trade_date", return_value="2026-09-30"):
                self.assertFalse(market.is_a_share_trading_day(date(2026, 10, 1)))
        finally:
            market._a_share_trade_day_cache.pop("2026-10-01", None)
            db.close()

    def test_known_index_quote_date_avoids_limited_calendar_api(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE a_share_trading_calendar (trade_date TEXT, is_open INTEGER)")
        market._a_share_trade_day_cache.pop("2026-09-30", None)
        try:
            with patch.object(market, "open_backtest_db", return_value=db), \
                    patch.object(market, "tushare_pro_request") as tushare, \
                    patch.object(market, "tencent_index_trade_date", return_value="2026-09-30"):
                self.assertTrue(market.is_a_share_trading_day(date(2026, 9, 30)))
                tushare.assert_not_called()
        finally:
            market._a_share_trade_day_cache.pop("2026-09-30", None)
            db.close()

    def test_growth_board_includes_current_302_codes(self):
        self.assertTrue("302132".startswith(market.MARKET_SCOPES["chinext"]["prefixes"]))

    def test_alert_cache_lock_allows_nested_trade_calendar_lookup(self):
        with market._cache_lock:
            acquired = market._cache_lock.acquire(blocking=False)
            if acquired:
                market._cache_lock.release()
        self.assertTrue(acquired)

    def test_load_universe_keeps_latest_snapshot_and_industry(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "stocks.db"
            db = sqlite3.connect(db_path)
            try:
                db.execute("CREATE TABLE stock_info (code TEXT, name TEXT, exchange TEXT, first_date TEXT, last_date TEXT)")
                db.execute("CREATE TABLE stock_board_member (code TEXT, board_name TEXT, board_category TEXT)")
                db.executemany("INSERT INTO stock_info VALUES (?,?,?,?,?)", [
                    ("600519", "贵州茅台", "SH", "20010827", "20251231"),
                    ("300750", "宁德时代", "SZ", "20180611", "20251231"),
                    ("000003", "历史退市", "SZ", "19910101", "20001231"),
                ])
                db.execute("INSERT INTO stock_board_member VALUES ('600519','食品饮料','申万一级')")
                db.commit()
            finally:
                db.close()
            with patch.object(market, "STOCK_UNIVERSE_DB_PATH", db_path):
                stocks, as_of = market.load_local_stock_universe()
            self.assertEqual(as_of, "2025-12-31")
            self.assertEqual(set(stocks), {"600519", "300750"})
            self.assertEqual(stocks["600519"]["industry"], "食品饮料")

    def test_tencent_rows_carry_real_quote_date_and_missing_fields(self):
        fields = [""] * 58
        for index, value in {
            1: "贵州茅台", 2: "600519", 3: "1258.62", 4: "1235.58", 5: "1239.53",
            30: "20260930161458", 32: "1.86", 33: "1268.00", 34: "1236.05",
            36: "38331", 37: "479725", 38: "0.31", 43: "2.59",
            44: "15733.78", 45: "15733.78", 49: "1.36",
        }.items():
            fields[index] = value
        raw = ('v_sh600519="' + "~".join(fields) + '";').encode("gb18030")
        class Result:
            stdout = raw
        with patch.object(market.subprocess, "run", return_value=Result()):
            rows = market.fetch_tencent_market_rows(["600519"])
        row = rows["600519"]
        self.assertEqual(row["f124"], 1790756098)
        self.assertEqual(row["f7"], 2.59)
        self.assertEqual(row["f10"], 1.36)
        self.assertIsNone(row["f62"])


if __name__ == "__main__":
    unittest.main()
