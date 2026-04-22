
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def get_token():
    email = 'sim@example.com'
    password = 'pw123456'
    client.post('/api/auth/register', json={'email': email, 'password': password})
    r = client.post('/api/auth/token', data={'username': email, 'password': password})
    return r.json()['access_token']

def test_simulation_validation_and_run():
    token = get_token()
    h = {'Authorization': f'Bearer {token}'}
    bad = client.post('/api/simulation/run', json={'ticker':'TSLA','flow_events':[],'n_agents':7,'n_runs':1}, headers=h)
    assert bad.status_code == 422

def test_websocket_connects_and_receives_message():
    token = get_token()
    with client.websocket_connect(f'/ws/signals?token={token}') as websocket:
        data = websocket.receive_json()
        assert data.get('type') == 'ping'
