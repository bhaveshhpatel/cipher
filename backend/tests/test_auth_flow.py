
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_register_login_me_flow():
    email = "test@example.com"
    password = "pw123456"
    r = client.post('/api/auth/register', json={'email': email, 'password': password})
    assert r.status_code in (201, 409)
    t = client.post('/api/auth/token', data={'username': email, 'password': password})
    assert t.status_code == 200
    token = t.json()['access_token']
    me = client.get('/api/auth/me', headers={'Authorization': f'Bearer {token}'})
    assert me.status_code == 200
    assert me.json()['email'] == email
