"""One source identity for sequential buffers and resumable offline blocks."""
import hashlib
import json


FIELDS = ('id', 'remote', 'size', 'source_fid', 'source_file_id', 'fid',
          'source_modified', 'source_checksum')


def identity(entry):
    # Keep the existing playback-buffer identity format so already verified
    # sequential blocks retain their key. Names and sizes alone are insufficient.
    raw = json.dumps([entry.get(key) for key in FIELDS], ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def partial_identity(entry, block_size):
    return dict(version=1, id=entry['id'], remote=entry['remote'], size=entry['size'],
                block_size=block_size, source_fingerprint=identity(entry))
