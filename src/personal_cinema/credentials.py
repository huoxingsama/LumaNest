"""Per-user Windows DPAPI credentials. No plaintext or portable fallback."""
import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path

from .configuration import NAME, _atomic_write, _file_lock, _json_read


class CredentialError(RuntimeError):
    pass


class DPAPICipher:
    def __init__(self):
        if os.name != 'nt':
            raise CredentialError('此版本凭据保护需要 Windows DPAPI；不会退回明文存储。')
        self.crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)

    def _convert(self, value, decrypt=False):
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        source = DATA_BLOB(len(value), buffer)
        result = DATA_BLOB()
        fn = self.crypt.CryptUnprotectData if decrypt else self.crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        fn.restype = wintypes.BOOL
        # CRYPTPROTECT_UI_FORBIDDEN, current-user scope (not LOCAL_MACHINE).
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise CredentialError('凭据保护操作失败，请使用保存凭据的 Windows 账户重新授权。')
        try: return ctypes.string_at(result.pbData, result.cbData)
        finally:
            self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
            self.kernel.LocalFree.restype = ctypes.c_void_p
            self.kernel.LocalFree(result.pbData)

    def encrypt(self, value): return self._convert(value)
    def decrypt(self, value): return self._convert(value, decrypt=True)


class SecretStore:
    def __init__(self, root, cipher=None):
        self.root = Path(root).expanduser().absolute()
        self.path = self.root / 'secrets' / 'credentials.json'
        self.lock_path = self.root / 'secrets' / '.credentials.lock'
        self.cipher = cipher if cipher is not None else DPAPICipher()

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not NAME.fullmatch(name) or name == '__proto__':
            raise ValueError('凭据名称无效。')

    def _read(self):
        raw = _json_read(self.path, {'version': 1, 'protection': 'windows-dpapi-user', 'values': {}})
        if not isinstance(raw, dict) or raw.get('version') != 1 or raw.get('protection') != 'windows-dpapi-user' or not isinstance(raw.get('values'), dict):
            raise CredentialError('凭据库格式不兼容，请重新授权。')
        for name, value in raw['values'].items():
            self._name(name)
            if not isinstance(value, str) or len(value) > 200000: raise CredentialError('凭据记录损坏。')
        return raw

    def set(self, name, value):
        self._name(name)
        if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 65536 or '\x00' in value:
            raise ValueError('请输入有效凭据。')
        try: encrypted = self.cipher.encrypt(value.encode('utf-8'))
        except Exception as exc: raise CredentialError('无法保护凭据，未保存。') from exc
        with _file_lock(self.lock_path):
            data = self._read(); data['values'][name] = base64.b64encode(encrypted).decode('ascii')
            _atomic_write(self.path, data)

    def get(self, name):
        self._name(name)
        with _file_lock(self.lock_path): encoded = self._read()['values'].get(name)
        if encoded is None: return None
        try:
            value = self.cipher.decrypt(base64.b64decode(encoded, validate=True)).decode('utf-8')
            if not value: raise ValueError('empty')
            return value
        except Exception as exc:
            raise CredentialError('无法读取此凭据，请使用原 Windows 账户或重新授权。') from exc

    def delete(self, name):
        self._name(name)
        with _file_lock(self.lock_path):
            data = self._read(); data['values'].pop(name, None)
            _atomic_write(self.path, data)

    def status(self):
        with _file_lock(self.lock_path):
            return {name: bool(value) for name, value in self._read()['values'].items()}
