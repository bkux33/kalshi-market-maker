import base64
import json
import logging

import pytest

from alphalab.core.book_manager import BookManager
from alphalab.core.config import load_settings
from alphalab.core.events import BookDelta, BookSnapshot
from alphalab.core.fees import FeeModel, FeeSchedule, fee_per_contract
from alphalab.core.logs import JsonFormatter, redact
from alphalab.core.orderbook import OrderBook
from alphalab.core.prices import cents_to_units, complement, dollars_to_units, fmt_price, parse_count
from alphalab.kalshi.auth import KalshiAuthError, KalshiSigner

C = 100


# ---------------------------------------------------------------- prices
def test_price_conversions():
    assert dollars_to_units("0.4500") == 4500
    assert dollars_to_units("0.0850") == 850  # deci-cent tick
    assert cents_to_units(45) == 4500
    assert complement(4500) == 5500
    assert parse_count("136.00") == 136.0
    assert fmt_price(4550) == "45.5c" and fmt_price(4500) == "45c"


# ---------------------------------------------------------------- order book
def test_no_bids_become_yes_asks():
    ob = OrderBook("T")
    ob.apply_snapshot([(45 * C, 10), (44 * C, 5)], [(50 * C, 7), (49 * C, 3)], 1)
    assert ob.best_bid() == (4500, 10)
    assert ob.best_ask() == (5000, 7)          # NO bid 50c -> YES ask 50c
    assert ob.asks() == [(5000, 7), (5100, 3)]  # NO bid 49c -> YES ask 51c
    assert ob.spread() == 500 and ob.mid() == 4750
    assert ob.imbalance(1) == pytest.approx(10 / 17)
    assert ob.microprice() == pytest.approx((5000 * 10 + 4500 * 7) / 17)
    assert not ob.is_crossed()


def test_deltas_and_negative_clamp():
    ob = OrderBook("T")
    ob.apply_snapshot([(45 * C, 10)], [(50 * C, 7)], 1)
    ob.apply_delta("yes", 46 * C, 4, 2)
    assert ob.bid_price == 46 * C
    ob.apply_delta("yes", 46 * C, -4, 3)
    assert ob.bid_price == 45 * C and 46 * C not in ob.yes
    r = ob.apply_delta("no", 50 * C, -9, 4)
    assert r.clamped and ob.ask_price is None and ob.inconsistencies == 1


def test_sweep_walks_levels_and_respects_limit_and_consumed():
    ob = OrderBook("T")
    ob.apply_snapshot([(45 * C, 10)], [(50 * C, 3), (49 * C, 4), (48 * C, 100)], 1)
    assert ob.sweep("buy", 5) == [(5000, 3), (5100, 2)]
    assert ob.sweep("buy", 10, limit=5100) == [(5000, 3), (5100, 4)]
    assert ob.sweep("buy", 5, consumed={("ask", 5000): 3}) == [(5100, 4), (5200, 1)]
    assert ob.sweep("sell", 20) == [(4500, 10)]


def test_book_manager_sequence_gap_unsyncs_until_snapshot():
    gaps = []
    bm = BookManager(on_gap=lambda sid, exp, got: gaps.append((sid, exp, got)))
    bm.apply(BookSnapshot(1, "A", [(40 * C, 5)], [(55 * C, 5)], seq=1, sid=7))
    assert bm.apply(BookDelta(2, "A", "yes", 41 * C, 2, seq=2, sid=7))
    assert not bm.apply(BookDelta(3, "A", "yes", 42 * C, 2, seq=4, sid=7))  # seq 3 missing
    assert gaps == [(7, 3, 4)] and not bm.is_synced("A")
    assert not bm.apply(BookDelta(4, "A", "yes", 42 * C, 2, seq=5, sid=7))
    bm.apply(BookSnapshot(5, "A", [(40 * C, 5)], [(55 * C, 5)], seq=6, sid=7))
    assert bm.is_synced("A") and bm.book("A").bid_price == 40 * C


# ---------------------------------------------------------------- fees
def test_fee_formula_and_rounding():
    fm = FeeModel()
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> rounded up to 0.02
    assert fm.fee("X", 5000, 1, is_taker=True) == pytest.approx(0.02)
    assert fm.fee("X", 5000, 100, is_taker=True) == pytest.approx(1.75)
    assert fm.fee("X", 5000, 100, is_taker=False) == pytest.approx(0.44)  # 0.4375 -> 0.44 (conservative maker default)
    assert fm.fee("X", 9900, 10, is_taker=True) == pytest.approx(0.01)   # 0.00693 -> 0.01
    assert fm.fee("X", 5000, 0, is_taker=True) == 0.0
    assert fee_per_contract(5000) == pytest.approx(0.0175)


def test_fee_series_types_and_stress():
    fm = FeeModel()
    fm.register_series("KXA", "quadratic", 1.0)
    fm.register_series("KXB", "quadratic_with_maker_fees", 0.5)
    assert fm.fee("KXA-1", 5000, 100, is_taker=False) == 0.0
    assert fm.fee("KXB-1", 5000, 100, is_taker=False) == pytest.approx(0.22)  # 0.4375*0.5 = 0.21875 -> 0.22
    assert fm.with_stress(2.0).fee("KXA-1", 5000, 100, is_taker=True) == pytest.approx(3.5)
    flat = FeeModel(series_overrides={"F": FeeSchedule(flat_per_contract=0.01)})
    assert flat.fee("F-1", 5000, 7, True) == pytest.approx(0.07)


def test_round_trip_breakeven():
    fm = FeeModel(FeeSchedule(rounding="none"))
    # taker round trip at 50c costs 2 * 0.0175 = 3.5c per contract
    assert fm.breakeven_move_units("X", 5000) == pytest.approx(350)


# ---------------------------------------------------------------- auth & secrets
def test_rsa_pss_signature_verifies(rsa_key):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    key, path = rsa_key
    s = KalshiSigner.from_file("abcd1234efgh", str(path))
    h = s.headers("get", "/trade-api/v2/portfolio/orders?status=resting", timestamp_ms="1700000000000")
    assert h["KALSHI-ACCESS-KEY"] == "abcd1234efgh"
    msg = b"1700000000000GET/trade-api/v2/portfolio/orders"  # query stripped, method upper-cased
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg,
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                            hashes.SHA256())
    assert "PRIVATE" not in repr(s) and "abcd1234efgh" not in repr(s)


def test_bad_key_file_errors_without_leaking(tmp_path):
    p = tmp_path / "k.pem"
    p.write_text("-----BEGIN PRIVATE KEY-----\nnotakey\n-----END PRIVATE KEY-----\n")
    with pytest.raises(KalshiAuthError) as e:
        KalshiSigner.from_file("id", str(p))
    assert "notakey" not in str(e.value)
    with pytest.raises(KalshiAuthError):
        KalshiSigner.from_file("id", str(tmp_path / "missing.pem"))


def test_log_redaction():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEsecret\n-----END RSA PRIVATE KEY-----"
    assert "MIIEsecret" not in redact(f"oops {pem}")
    assert redact({"KALSHI-ACCESS-SIGNATURE": "abc", "x": 1}) == {"KALSHI-ACCESS-SIGNATURE": "[REDACTED]", "x": 1}
    rec = logging.LogRecord("t", logging.INFO, "", 0, "leak %s", (pem,), None)
    rec.fields = {"private_key": pem, "ok": 2}
    out = JsonFormatter().format(rec)
    assert "MIIEsecret" not in out and json.loads(out)["ok"] == 2


def test_settings_secrets_only_from_env(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("kalshi:\n  api_key_id: leaked\n")
    with pytest.raises(ValueError):
        load_settings(str(cfg), env={})
    cfg.write_text("risk:\n  max_order_size: 3\n")
    s = load_settings(str(cfg), env={"KALSHI_API_KEY_ID": "abcdefghijkl", "KALSHI_PRIVATE_KEY_PATH": "/k"})
    assert s.risk.max_order_size == 3 and s.trading_mode == "paper"
    red = json.dumps(s.redacted())
    assert "abcdefghijkl" not in red and "/k" not in red
    with pytest.raises(ValueError):
        load_settings(env={"TRADING_MODE": "yolo"})
    cfg.write_text("nonsense_key: 1\n")
    with pytest.raises(ValueError):
        load_settings(str(cfg), env={})
