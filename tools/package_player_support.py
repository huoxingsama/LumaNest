"""Package pinned x64 loader/font DLLs with notices and FriBiDi source.

The MPV core is downloaded directly from its pinned upstream release by the
installer; it is not redistributed in this archive. Build inputs come from the
unmodified Jellyfin MPV Shim v3.3.0 Windows distribution and named source tags.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import tarfile
import zipfile


DLL_HASHES = {
    'fribidi-0.dll': '7b5add237f08bad7c6075fa056892dcf4e0fb5093c9205814ceb3dde257e87ff',
    'vulkan-1.dll': 'e13eb45be42a7fd23d39368a8941e5f6b10766fe6cd7c7602336d239d51a9869',
}
SOURCE_HASH = '1b1cde5b235d40479e91be2f0e88a309e3214c8ab470ec8a2744d82a5a9ea05c'
VULKAN_LICENSE_HASH = '43c0a37e6a0fa7ff3c843b3ec5a4fac84b712558ddac103fbd4c1649662a9ece'
NOTICE = '''LumaNest Windows x64 player support

This archive contains the unmodified FriBiDi and Vulkan loader DLLs distributed
with Jellyfin MPV Shim v3.3.0. It contains no media, accounts or user settings.
MPV itself is fetched directly from the upstream pinned release at install time.

Upstream distribution and source/build instructions:
https://github.com/jellyfin/jellyfin-mpv-shim/releases/tag/v3.3.0
https://github.com/jellyfin/jellyfin-mpv-shim/tree/v3.3.0/tools

FriBiDi 1.0.16: LGPL-2.1-or-later. Copyright its respective contributors.
The complete unmodified source archive and COPYING are included here. Users may
replace the shared library with a compatible modified build. No restriction on
reverse engineering for debugging modifications to this library is imposed.
https://github.com/fribidi/fribidi/releases/tag/v1.0.16

Vulkan Loader: Apache-2.0 and the permissive notices in its included LICENSE.txt.
Source/build tag (also the Vulkan-Headers tag): vulkan-sdk-1.4.357.0
https://github.com/KhronosGroup/Vulkan-Loader/tree/vulkan-sdk-1.4.357.0
https://github.com/KhronosGroup/Vulkan-Headers/tree/vulkan-sdk-1.4.357.0

No Python, Microsoft runtime, OpenSSL or other system DLLs are included here.
This support archive requires a supported 64-bit Windows Python installation.
'''


def checked(path, expected):
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('Build input checksum mismatch: ' + Path(path).name)
    return data


def x64_dll(data):
    offset = struct.unpack_from('<I', data, 0x3c)[0]
    return data[:2] == b'MZ' and data[offset:offset + 4] == b'PE\0\0' and struct.unpack_from('<H', data, offset + 4)[0] == 0x8664


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dll-directory', type=Path, required=True)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('dist/lumanest-player-support-1-win-x64.zip'))
    args = parser.parse_args()
    entries = {}
    for name, digest in DLL_HASHES.items():
        payload = checked(args.dll_directory / name, digest)
        if not x64_dll(payload):
            raise ValueError('Support library is not x64: ' + name)
        entries[name] = payload
    source = args.sources / 'fribidi-1.0.16.tar.xz'
    entries['sources/fribidi-1.0.16.tar.xz'] = checked(source, SOURCE_HASH)
    with tarfile.open(source) as archive:
        entries['licenses/FriBiDi-COPYING.txt'] = archive.extractfile('fribidi-1.0.16/COPYING').read()
    entries['licenses/Vulkan-Loader-LICENSE.txt'] = checked(args.sources / 'Vulkan-Loader-LICENSE.txt', VULKAN_LICENSE_HASH)
    entries['NOTICE.txt'] = NOTICE.encode('utf-8')
    entries['support-manifest.json'] = json.dumps({'version': 1, 'architecture': 'x64', 'files': DLL_HASHES}, sort_keys=True, indent=2).encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, (2026, 11, 20, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    print(json.dumps({'file': args.output.name, 'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(), 'entries': len(entries)}))


if __name__ == '__main__':
    main()
