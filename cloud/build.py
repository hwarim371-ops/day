"""Build a code-only repository and self-contained Apps Script UI."""
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
APP_FILES = ['booking_manager.py', 'booking_store.py', 'booking_discovery.py',
             'naver_room_booking_upgraded.py', 'room_classifier.py', 'room_reconcile.py', 'booking_file_lock.py']
WEB_FILES = ['index.html', 'app.js', 'style.css', 'lucide.min.js', 'LUCIDE_LICENSE']


def web_html():
    web = ROOT / 'app/web'
    html = (web / 'index.html').read_text(encoding='utf-8')
    css = (web / 'style.css').read_text(encoding='utf-8')
    css += '\n#cloudPending{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:1000;max-width:calc(100% - 32px);padding:12px 18px;background:#163e2e;color:white;border-radius:6px;box-shadow:0 3px 18px #0002;font-size:14px;letter-spacing:0}#cloudPending[hidden]{display:none}'
    html = html.replace('<link rel="stylesheet" href="/style.css" />', '<style>' + css + '</style>')
    for name in ['lucide.min.js', 'app.js']:
        html = html.replace(f'<script defer src="/{name}"></script>', '')
    html = html.replace('__TOKEN__', 'cloud-session').replace('PC 연결 중', '클라우드 연결 중').replace('로컬 데이터</small>', 'Google Drive</small>')
    scripts = [(web / 'lucide.min.js'), ROOT / 'cloud/apps_script/transport.js', web / 'app.js']
    tail = '<div id="cloudPending" role="status" aria-live="polite" hidden></div>'
    for source in scripts:
        tail += '<script>' + source.read_text(encoding='utf-8').replace('</script', '<\\/script') + '</script>'
    return html.replace('</body>', tail + '</body>')


def build(output, refresh=False):
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()) and not refresh:
        raise ValueError('Use a new empty output directory; no existing files are removed')
    output.mkdir(parents=True, exist_ok=True)
    files = [*(('app/' + f) for f in APP_FILES), *(('app/web/' + f) for f in WEB_FILES),
        'cloud/__init__.py', 'cloud/runner.py', 'cloud/snapshot.py', 'cloud/requirements.txt',
        'cloud/apps_script/Code.gs', 'cloud/apps_script/appsscript.json', 'cloud/apps_script/transport.js',
        'cloud/README.md', 'cloud/build.py', 'cloud/seed.py', 'cloud/preview.py', 'cloud/repository.gitignore',
        'cloud/workflows/collect.yml', 'tests/test_cloud.py', 'tests/test_apps_script.cjs',
        'tests/test_manager.py', 'tests/test_room_classifier.py', 'tests/test_room_reconcile.py', 'tests/test_cloud_ui.py']
    for rel in files:
        target = output / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    target = output / '.github/workflows/collect.yml'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'cloud/workflows/collect.yml', target)
    (output / 'cloud/apps_script/Index.html').write_text(web_html(), encoding='utf-8')
    shutil.copy2(ROOT / 'cloud/repository.gitignore', output / '.gitignore')
    (output / 'README.md').write_text('# Camping Booking Cloud\n\nGoogle Apps Script + GitHub Actions + Google Drive.\n\n[Deployment guide](cloud/README.md)\n\nNo operational data or credentials belong in this repository.\n', encoding='utf-8')
    print(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--refresh', action='store_true', help='Refresh only the generated code file allowlist')
    args = parser.parse_args()
    build(args.output, args.refresh)
