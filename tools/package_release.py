"""Create a shareable source archive, never copying local credentials or runtime data."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from datetime import datetime, timezone, timedelta
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ('README.md','requirements.txt','requirements.lock.txt','package.json',
              'package-lock.json','pytest.ini','run.ps1','.env.example','.gitignore',
              'Dockerfile','.dockerignore','compose.yaml','index.html','_config.yml')
SOURCE_DIRS = ('app','static','tests','docs','tools','.github')
EXCLUDED = {'.git','.venv','node_modules','data','test-results','log','logs',
            '__pycache__','.pytest_cache','.mypy_cache','.ruff_cache'}
PRIVATE_SUFFIXES = ('.pyc','.pyo','.sqlite3','.sqlite3-wal','.sqlite3-shm',
                    '.db','.db-wal','.db-shm','.log','.zip','.pem','.key')


def collect():
    paths = [ROOT/name for name in ROOT_FILES if (ROOT/name).is_file()]
    for directory in SOURCE_DIRS:
        source = ROOT/directory
        if not source.exists(): continue
        for path in source.rglob('*'):
            relative = path.relative_to(ROOT)
            if not path.is_file() or any(part in EXCLUDED for part in relative.parts): continue
            if path.name.startswith('.env') or path.name.endswith(PRIVATE_SUFFIXES): continue
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                raise ValueError(f'Unexpected external source link: {relative}')
            paths.append(path)
    return sorted(set(paths), key=lambda p:p.relative_to(ROOT).as_posix())


def audit(path, payload):
    relative = path.relative_to(ROOT).as_posix()
    if relative == '.env.example':
        text = payload.decode('utf-8-sig')
        for line in text.splitlines():
            if line.strip().startswith('LLM_API_KEY=') and line.split('=',1)[1].strip():
                raise ValueError('Environment example must have a blank model key')
    if path.suffix in ('.py','.js','.css','.html','.md','.txt','.json','.ps1') or relative=='.env.example':
        text = payload.decode('utf-8-sig')
        for match in re.finditer(r'sk-[A-Za-z0-9_-]{20,}',text):
            value = match.group().lower()
            if not any(tag in value for tag in ('test','fake','fixture','example','dummy','not-a-real')):
                line = text[:match.start()].count('\n')+1
                raise ValueError(f'Credential-like literal found in {relative}:{line}; not printed')


def build(output):
    paths = collect()
    if not (ROOT/'app/main.py') in paths or not (ROOT/'static/index.html') in paths:
        raise ValueError('Source tree incomplete')
    output = output.resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    entries = []
    total = 0
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for path in paths:
            relative = path.relative_to(ROOT).as_posix()
            payload = path.read_bytes()
            audit(path,payload)
            entries.append(dict(path=relative,bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest()))
            total += len(payload)
            archive.writestr('AlphaLab/'+relative,payload)
        manifest = dict(project='Alpha Lab',created_at=datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
            root='AlphaLab',file_count=len(entries),uncompressed_bytes=total,
            excluded=['.env','local API credentials','runtime databases and evidence','.venv',
                      'node_modules','caches','logs','old test-results','Git metadata'],
            files=entries)
        archive.writestr('AlphaLab/MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None: raise ValueError('ZIP CRC validation failed')
        for entry in manifest['files']:
            if hashlib.sha256(archive.read('AlphaLab/'+entry['path'])).hexdigest()!=entry['sha256']:
                raise ValueError('Manifest validation failed')
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+'.sha256').write_text(digest+'  '+output.name+'\n',encoding='utf-8')
    return dict(archive=str(output),sha256=digest,files=len(entries)+1,
                zip_bytes=output.stat().st_size,uncompressed_bytes=total,verified=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.output),ensure_ascii=False))
