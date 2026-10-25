import pytest
from fastapi.testclient import TestClient

from voiceops.integrations.pms_client import PMSClient, PMSError, PMSUnavailable
from voiceops.store import Store
from voiceops.tracing import Tracer


@pytest.fixture
def client(pms_app):
    st = Store()
    return PMSClient("http://testserver", "voiceops-agent", "demo-secret", st, Tracer(st), http=TestClient(pms_app),
                     sleep=lambda s: None, breaker_threshold=2)


def test_parses_legacy_formats(client):
    ledger = client.ledger("R-1001")
    assert str(ledger.BalanceDue) == "1450.00" and ledger.Charges[0].DueDate.year > 2000


def test_refreshes_expired_token(client, pms_app):
    client.ledger("R-1001")
    pms_app.state.pms.tokens.clear()  # server-side expiry
    assert client.lease("R-1002").LeaseID == "L-502"


def test_retries_transient_failures(client, pms_app):
    pms_app.state.pms.faults["failure_rate"] = 0.3
    for _ in range(4):
        client.ledger("R-1001")
    retries = [s for s in client.store.q("SELECT attrs FROM spans WHERE kind='http'") if '"attempt": 2' in s["attrs"]]
    assert retries


def test_non_retryable_errors_raise_immediately(client):
    with pytest.raises(PMSError) as e:
        client.ledger("R-9999")
    assert e.value.status == 404 and not isinstance(e.value, PMSUnavailable)


def test_lost_response_is_applied_exactly_once(client, pms_app):
    pms_app.state.pms.faults["lost_response_rate"] = 1.0
    body = {"ResidentID": "R-1001", "Installments": []}
    res = client.write("payment_plan", "/api/v1/payment-plans", body, "key-1", "C-1")
    assert res.status == "queued"  # every response was lost, so we can't claim success
    pms_app.state.pms.faults["lost_response_rate"] = 0.0
    client._open_until = 0
    drained = client.drain_outbox()
    assert drained[0]["status"] == "committed"
    assert pms_app.state.pms.q("SELECT COUNT(*) n FROM payment_plans")[0]["n"] == 1


def test_circuit_breaker_opens_and_fails_fast(client, pms_app):
    pms_app.state.pms.faults["failure_rate"] = 1.0
    for _ in range(2):
        with pytest.raises(PMSUnavailable):
            client.ledger("R-1001")
    before = pms_app.state.pms.request_count
    with pytest.raises(PMSUnavailable) as e:
        client.ledger("R-1001")
    assert e.value.code == "CIRCUIT_OPEN" and pms_app.state.pms.request_count == before


def test_idempotency_key_reuse_with_different_body_is_rejected(client):
    client.write("link", "/api/v1/payment-links", {"ResidentID": "R-1001", "Amount": "10.00"}, "k", None)
    client.store.x("DELETE FROM pending_writes")
    with pytest.raises(PMSError) as e:
        client.write("link", "/api/v1/payment-links", {"ResidentID": "R-1001", "Amount": "20.00"}, "k", None)
    assert e.value.code == "IDEMPOTENCY_CONFLICT"
