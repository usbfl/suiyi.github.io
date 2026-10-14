"""Explicit single-user hosting boundary; local-only defaults stay unchanged."""
import hmac
import os
from urllib.parse import urlparse

from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware


def install_hosting(app):
    from .config import load_env
    load_env()
    hosts = [x.strip() for x in os.getenv('ALPHALAB_ALLOWED_HOSTS', '127.0.0.1,localhost,testserver').split(',') if x.strip()]
    origins = [x.strip().rstrip('/') for x in os.getenv('ALPHALAB_CORS_ORIGINS', '').split(',') if x.strip()]
    token = os.getenv('ALPHALAB_ACCESS_TOKEN', '')
    if not hosts or '*' in hosts or '*' in origins:
        raise ValueError('Set explicit AlphaLab hosts and origins; wildcards are not allowed')
    for origin in origins:
        parsed = urlparse(origin)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('ALPHALAB_CORS_ORIGINS requires HTTPS origins without paths')
    remote = bool(origins) or any(h not in {'127.0.0.1', 'localhost', 'testserver', '::1', '[::1]'} for h in hosts)
    if (remote or token) and len(token) < 32:
        raise ValueError('Hosting requires ALPHALAB_ACCESS_TOKEN with at least 32 characters')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware('http')
    async def access_guard(request, call_next):
        origin = request.headers.get('origin')
        same_origin = origin == f"{request.url.scheme}://{request.headers.get('host')}"
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and origin and not same_origin and origin not in origins:
            return JSONResponse({'detail': '仅允许已配置来源操作'}, status_code=403)
        if request.url.path.startswith('/api/') and token:
            supplied = request.headers.get('authorization', '')
            if not hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
                return JSONResponse({'detail': '需要有效的工作区访问口令'}, status_code=401,
                                    headers={'WWW-Authenticate': 'Bearer'})
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    # Outer CORS layer handles preflight and attaches headers even to 401/403.
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                       allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
                       allow_headers=['Authorization', 'Content-Type'],
                       expose_headers=['Content-Disposition'])
