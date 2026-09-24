import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alphalab.core.book_manager import BookManager  # noqa: E402
from alphalab.core.config import FillConfig, RiskConfig, load_settings  # noqa: E402
from alphalab.core.events import BookSnapshot  # noqa: E402
from alphalab.core.fees import FeeModel  # noqa: E402
from alphalab.data.db import Database  # noqa: E402
from alphalab.sim.broker import SimBroker  # noqa: E402
from alphalab.sim.portfolio import Portfolio  # noqa: E402

C = 100  # one cent in price units


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def settings(tmp_path):
    return load_settings(env={"DATA_DIR": str(tmp_path / "data")})


def make_broker(fill=None, close=None):
    books = BookManager()
    pf = Portfolio()
    broker = SimBroker(books, FeeModel(), fill or FillConfig(order_latency_ms=0, cancel_latency_ms=0), pf, close or {})
    return broker, books, pf


def snap(books, broker, ts, market="M", yes=((45 * C, 10),), no=((50 * C, 10),)):
    ev = BookSnapshot(ts, market, list(yes), list(no))
    broker.before_book(ev)
    books.apply(ev)
    broker.after_book(ev)
    return ev


@pytest.fixture
def rsa_key(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "key.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return key, p
