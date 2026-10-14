"""Remote hosting must protect reads, writes, exports and event streams."""
import httpx
import pytest
from fastapi import FastAPI
from app.hosting import install_hosting

TOKEN = 'HOSTING_TEST_ONLY_NOT_A_REAL_TOKEN_123456789'


def hosted_app(monkeypatch):
    monkeypatch.setenv('ALPHALAB_ALLOWED_HOSTS', 'api.example.test')
    monkeypatch.setenv('ALPHALAB_CORS_ORIGINS', 'https://usbfl.github.io')
    monkeypatch.setenv('ALPHALAB_ACCESS_TOKEN', TOKEN)
    app = FastAPI()
    install_hosting(app)
    @app.api_route('/api/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE'])
    async def endpoint(path):
        return {'ok': True}
    return app


async def test_remote_auth_and_preflight(monkeypatch):
    app = hosted_app(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://api.example.test') as client:
        for path in ('state', 'export', 'stream', 'connections'):
            response = await client.get('/api/' + path, headers={'Origin': 'https://usbfl.github.io'})
            assert response.status_code == 401
            assert response.headers['Access-Control-Allow-Origin'] == 'https://usbfl.github.io'
        response = await client.options('/api/connections', headers={'Origin': 'https://usbfl.github.io',
            'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,content-type'})
        assert response.status_code == 200
        auth = {'Authorization': 'Bearer ' + TOKEN, 'Origin': 'https://usbfl.github.io'}
        assert (await client.post('/api/connections', headers=auth)).status_code == 200
        response = await client.get('/api/state', headers=auth)
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'no-store'
        assert (await client.get('/api/state?token=' + TOKEN)).status_code == 401
        assert (await client.post('/api/connections', headers={**auth, 'Origin': 'https://evil.example'})).status_code == 403
        assert (await client.get('/api/state', headers={**auth, 'Host': 'evil.example'})).status_code == 400


def test_remote_hosting_fails_closed_without_token(monkeypatch):
    monkeypatch.setenv('ALPHALAB_ALLOWED_HOSTS', 'api.example.test')
    monkeypatch.delenv('ALPHALAB_ACCESS_TOKEN', raising=False)
    with pytest.raises(ValueError, match='ACCESS_TOKEN'):
        install_hosting(FastAPI())
