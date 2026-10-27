"""Install one hash-pinned Linux x86_64 browser during the Docker build only."""

import hashlib
from pathlib import Path
import stat
from urllib.request import urlopen
import zipfile

VERSION = '156.0.1-beta.36'
SHA256 = '72637885ee67c93466ad377831c647192f8f08fb5c6fd9279de1320b49dd2b53'
URL = f'https://github.com/daijro/camoufox/releases/download/v{VERSION}/camoufox-{VERSION}-lin.x86_64.zip'
ARCHIVE = Path(f'/opt/browser-asset-cache/camoufox-{VERSION}.zip')
DESTINATION = Path('/opt/camoufox')
LIMIT = 1_500_000_000


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    if not ARCHIVE.is_file() or ARCHIVE.stat().st_size > LIMIT or digest(ARCHIVE) != SHA256:
        partial = ARCHIVE.with_suffix('.part')
        try:
            print(f'Downloading pinned Camoufox {VERSION} (about 1.3 GB)', flush=True)
            with urlopen(URL, timeout=45) as source, partial.open('wb') as target:
                size = 0
                reported = 0
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > LIMIT:
                        raise ValueError('Browser archive exceeds size limit')
                    target.write(chunk)
                    if size - reported >= 64 * 1024 * 1024:
                        print(f'Browser archive: {size // (1024 * 1024)} MiB downloaded', flush=True)
                        reported = size
            if digest(partial) != SHA256:
                raise ValueError('Browser archive SHA256 mismatch')
            partial.replace(ARCHIVE)
        finally:
            partial.unlink(missing_ok=True)
    DESTINATION.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ARCHIVE) as archive:
        for entry in archive.infolist():
            target = (DESTINATION / entry.filename).resolve()
            if not target.is_relative_to(DESTINATION) or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('Invalid browser archive member')
            archive.extract(entry, DESTINATION)
            if target.is_file():
                target.chmod(0o755 if entry.external_attr >> 16 & 0o111 else 0o644)
    if not (DESTINATION / 'camoufox-bin').is_file():
        raise ValueError('Browser executable is missing')
    print(f'Installed Camoufox {VERSION}; SHA256 verified')


if __name__ == '__main__':
    main()
