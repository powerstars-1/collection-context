"""Progress checks are bounded, read-only, and independent of media history."""
from test_context_management import managed, post
from test_context_scheduling import env, prepared
from collection_context.application.management import ManagementService
from collection_context.workflows.jobs import JobManager


def test_activity_read_never_opens_prepared_media_or_calls_models(managed, env, monkeypatch):
    store = env[0]
    _, identity = prepared(env)
    before = store.snapshot()
    monkeypatch.setattr(ManagementService, '_prepared', lambda *_: (_ for _ in ()).throw(AssertionError('loaded media history')))
    monkeypatch.setattr('collection_context.processing.models.CloudModelClient._send', lambda *_: (_ for _ in ()).throw(AssertionError('called cloud')))
    result = post(managed, 'activity', {})
    assert result.status_code == 200
    data = result.json()['data']
    assert data['active_count'] == 0 and data['model_requests'] == 0
    assert 'prepared' not in data and 'next_offset' not in data
    assert store.snapshot() == before
    assert post(managed, 'activity', {}).json()['data']['library_revision'] == data['library_revision']


def test_activity_reports_queue_and_completion_without_historical_pagination(managed, env):
    store = env[0]
    item, identity = prepared(env)
    batch = env[4].history([identity], idempotency_key='activity-only', max_calls=2, allow_model_calls=True)
    job_id = batch['job_ids'][0]
    first = post(managed, 'activity', {}).json()['data']
    assert first['active_count'] == 1
    assert any(j['job_id'] == job_id and j['state'] == 'queued' for j in first['jobs'])
    JobManager(store).cancel(job_id)
    last = post(managed, 'activity', {}).json()['data']
    assert last['active_count'] == 0
    assert first['completion_revision'] != last['completion_revision']
    assert all('calls' not in j for j in last['jobs'])


def test_activity_rejects_unknown_fields_and_bearer_access(managed):
    assert post(managed, 'activity', {'offset': 0}).json()['ok'] is False
    result = managed[0].post('/v1/management/activity', json={}, headers={'Authorization': 'Bearer ' + managed[3]['token']})
    assert result.status_code == 403
