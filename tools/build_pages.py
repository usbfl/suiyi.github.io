"""Build only public frontend assets, with repository-subpath-safe URLs."""
import argparse
import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def build(output, api_base=''):
    output = output.resolve()
    if output == ROOT or ROOT.is_relative_to(output) or output.is_relative_to(ROOT / 'static'):
        raise ValueError('Output must not overwrite source')
    if api_base:
        url = urlparse(api_base)
        if url.scheme != 'https' or not url.netloc or url.path not in ('', '/') or url.query or url.fragment or url.username or url.password:
            raise ValueError('API base must be an HTTPS origin without path or credentials')
    if output.exists():
        raise ValueError('Output already exists; use a fresh build directory')
    output.mkdir(parents=True)
    shutil.copytree(ROOT / 'static', output / 'static')
    html = (ROOT / 'static/index.html').read_text(encoding='utf-8')
    html = html.replace('<html lang="zh-CN">', '<html lang="zh-CN" data-hosting="github-pages">')
    (output / 'index.html').write_text(html, encoding='utf-8')
    (output / 'static/index.html').unlink()
    (output / 'static/deploy-config.js').write_text(
        'window.ALPHALAB_CONFIG = ' + json.dumps({'apiBase': api_base.rstrip('/')}) + ';\n', encoding='utf-8')
    (output / '.nojekyll').touch()
    print(f'Built GitHub Pages frontend: {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '_site')
    parser.add_argument('--api-base', default='')
    parser.add_argument('--root-entry', action='store_true', help='Refresh the main/root Pages entry from the shared HTML')
    args = parser.parse_args()
    if args.root_entry:
        html = (ROOT / 'static/index.html').read_text(encoding='utf-8')
        (ROOT / 'index.html').write_text(html.replace('<html lang="zh-CN">', '<html lang="zh-CN" data-hosting="github-pages">'), encoding='utf-8')
        print('Updated root Pages entry')
    else:
        build(args.output, args.api_base)
