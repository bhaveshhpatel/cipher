
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def get_token():
    email = 'flow@example.com'
    password = 'pw123456'
    client.post('/api/auth/register', json={'email': email, 'password': password})
    r = client.post('/api/auth/token', data={'username': email, 'password': password})
    return r.json()['access_token']

def test_flow_scan_and_stats():
    token = get_token()
    h = {'Authorization': f'Bearer {token}'}
    r = client.get('/api/flow/scan?ticker=AAPL&limit=10', headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body['ticker'] == 'AAPL'
    assert len(body['events']) <= 10
    s = client.get('/api/stream/stats', headers=h)
    assert s.status_code == 200
    assert 'stats' in s.json()
