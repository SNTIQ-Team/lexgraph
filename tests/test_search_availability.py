import pytest
from fastapi import HTTPException
from api import main

@pytest.fixture
def available(monkeypatch):
    monkeypatch.setattr(main, '_retrospective_manifest', lambda **kwargs: {'fixture': True})
    monkeypatch.setattr(main, 'resolve_changes', lambda *args, **kwargs: {'matched': 0, 'events': []})
    monkeypatch.setattr(main, '_load', lambda *args: [])

def test_successful_zero_is_distinct_from_missing_archive(available, monkeypatch):
    ok = main._append_temporal_search({'result_total': 2}, 'x')
    assert ok['status'] == 'ok' and ok['change_total'] == 0
    monkeypatch.setattr(main, '_retrospective_manifest', lambda **kwargs: None)
    missing = main._append_temporal_search({'result_total': 2}, 'x')
    assert missing['status'] == 'partial' and missing['change_total'] is None
    assert missing['components']['changes']['reason'] == 'not_in_corpus'
    assert missing['result_total'] == 2 and missing['result_total_is_partial']

@pytest.mark.parametrize('where', ['_retrospective_manifest','resolve_changes'])
def test_manifest_and_archive_integrity_errors_survive(available, monkeypatch, where):
    def fail(*args, **kwargs):
        raise main.RetrospectiveIntegrityError('fixture hash mismatch')
    monkeypatch.setattr(main, where, fail)
    result = main._append_temporal_search({}, 'x')
    assert result['change_total'] is None
    assert result['components']['changes']['reason'] == 'integrity_check_failed'

def test_decision_failure_does_not_erase_successful_history(available, monkeypatch):
    monkeypatch.setattr(main, 'resolve_changes', lambda *args, **kwargs: {'matched': 3, 'events': [{'id':'one'}]})
    def fail(*args):
        raise HTTPException(404, 'missing decisions')
    monkeypatch.setattr(main, '_load', fail)
    result = main._append_temporal_search({'result_total': 2}, 'x')
    assert result['status'] == 'partial' and result['decision_total'] is None
    assert result['change_total'] == 3 and result['result_total'] == 5
    assert result['components']['decisions']['status'] == 'unavailable'

def test_wrapped_manifest_integrity_failure_retains_reason(available, monkeypatch):
    def fail(**kwargs):
        try:
            raise main.RetrospectiveIntegrityError('hash mismatch')
        except main.RetrospectiveIntegrityError as cause:
            raise HTTPException(503, 'unavailable') from cause
    monkeypatch.setattr(main, '_retrospective_manifest', fail)
    assert main._append_temporal_search({}, 'x')['components']['changes']['reason'] == 'integrity_check_failed'

def test_http_rejects_ignored_dates_and_duplicate_constraints():
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    for suffix in ['&at=2020-01-01','&valid_at=2020-01-01','&known_at=2020-01-01','&jurisdiction=EU','&q=y']:
        assert client.get('/search?q=x'+suffix).status_code == 422
    assert client.get('/capabilities').json()['search']['norms'] == 'current_only'

def test_deployed_composition_root_keeps_constraint_checks():
    from fastapi.testclient import TestClient
    from api.server import server
    response = TestClient(server, root_path='/lex').get('/search?q=x&known_at=2020-01-01')
    assert response.status_code == 422
    assert response.json()['status'] == 'unsupported_temporal_request'
