"""Compile the production session functions against a deterministic kernel mock."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
source = (root / 'lsdriver/lsdriver.c').read_text(encoding='utf-8')
state = source.split('static struct request_obj *req;', 1)[1].split('static int DispatchThreadFunction', 1)[0]
connection = source.split('/* Take a reference under RCU,', 1)[1].split('//去掉 ARM64', 1)[0]
production = 'static struct request_obj *req;' + state + '\n/* Take a reference under RCU,' + connection
compiler = os.environ.get('CC') or shutil.which('cc') or shutil.which('gcc')
if not compiler:
    raise SystemExit('A host C compiler is required')
with tempfile.TemporaryDirectory(prefix='lsdriver-session-') as directory:
    work = Path(directory)
    (work / 'session_under_test.h').write_text(production, encoding='utf-8')
    for version in [330240, 394752]:  # Linux 5.10 and 6.6: both GUP signatures.
        binary = work / ('session-test.exe' if os.name == 'nt' else 'session-test')
        subprocess.run([compiler, '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-Wno-unused-variable',
                        f'-DLINUX_VERSION_CODE={version}', '-I', str(work),
                        str(root / 'lsdriver/arm64_tests/session/test_lifecycle.c'),
                        '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=15)
