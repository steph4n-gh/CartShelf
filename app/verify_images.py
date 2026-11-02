#!/usr/bin/env python3
"""Verify explicitly selected pack images through read-only virtual mounts."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media


def verify_image(root: Path, image: Path) -> dict:
    image = media.managed(root, image.resolve(strict=True))
    expected_path = image.with_suffix('.img.json')
    expected = json.loads(expected_path.read_text()) if expected_path.exists() else None
    info = media.raw_image_info(image)
    if expected is not None and info['sha256'] != expected['sha256']:
        raise ValueError('The selected image differs from its saved checksum.')
    with media.mounted_image(image, readonly=True) as mount:
        profile, files = media.compilation_files(mount)
        manifest = json.loads((mount / 'MANIFEST.json').read_text())
        media.verify_files(mount, files)
        actual = {p.relative_to(mount).as_posix() for p in mount.rglob('*') if p.is_file()}
        wanted = {row['path'] for row in files}
        if actual != wanted:
            raise ValueError('The image contains files outside its verified pack inventory.')
        result = {'image_name': image.name, 'sha256': info['sha256'], 'size_bytes': info['size_bytes'],
                  'profile': profile, 'builds': len(manifest['release_ids']), 'files': len(files),
                  'manifest_sha256': media.sha256(mount / 'MANIFEST.json'), 'verified': True,
                  'exact_file_set': True, 'physical_media_touched': False}
    if media.sha256(image) != info['sha256']:
        raise ValueError('The read-only virtual mount changed its backing image.')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='A separate selected library folder.')
    parser.add_argument('--image', type=Path, action='append', required=True,
                        help='An image inside that library. Repeat to verify several images.')
    parser.add_argument('--output', type=Path, help='An optional explicit report destination.')
    args = parser.parse_args()
    root = media.workspace(args.root)
    results = [verify_image(root, image) for image in args.image]
    text = json.dumps(results, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    print(text, end='')


if __name__ == '__main__':
    main()
