"""RSA public-key encryption through Windows CNG; no private key is imported."""

import ctypes
from ctypes import wintypes
import os

from .client import CloudError


def encrypt_pkcs1(public_blob, plaintext):
    if os.name != 'nt':
        raise CloudError('monitor_platform_unsupported')
    if not isinstance(plaintext, bytes) or not 1 <= len(plaintext) <= 245:
        raise CloudError('monitor_credentials_invalid')
    dll = ctypes.WinDLL('bcrypt.dll')
    handle = ctypes.c_void_p
    pointer = ctypes.c_void_p
    size = wintypes.ULONG
    dll.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(handle), wintypes.LPCWSTR, wintypes.LPCWSTR, size]
    dll.BCryptImportKeyPair.argtypes = [handle, handle, wintypes.LPCWSTR, ctypes.POINTER(handle), pointer, size, size]
    dll.BCryptEncrypt.argtypes = [handle, pointer, size, pointer, pointer, size, pointer, size, ctypes.POINTER(size), size]
    dll.BCryptDestroyKey.argtypes = [handle]
    dll.BCryptCloseAlgorithmProvider.argtypes = [handle, size]
    for name in ('BCryptOpenAlgorithmProvider', 'BCryptImportKeyPair', 'BCryptEncrypt',
                 'BCryptDestroyKey', 'BCryptCloseAlgorithmProvider'):
        getattr(dll, name).restype = wintypes.LONG
    algorithm, key = handle(), handle()
    blob = ctypes.create_string_buffer(public_blob)
    message = ctypes.create_string_buffer(plaintext)
    try:
        if dll.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), 'RSA', None, 0) != 0:
            raise CloudError('monitor_credentials_invalid')
        if dll.BCryptImportKeyPair(algorithm, None, 'RSAPUBLICBLOB', ctypes.byref(key), blob, len(public_blob), 0) != 0:
            raise CloudError('monitor_credentials_invalid')
        length = size()
        if dll.BCryptEncrypt(key, message, len(plaintext), None, None, 0, None, 0, ctypes.byref(length), 2) != 0:
            raise CloudError('monitor_credentials_invalid')
        if length.value != 256:
            raise CloudError('monitor_credentials_invalid')
        output = ctypes.create_string_buffer(length.value)
        if dll.BCryptEncrypt(key, message, len(plaintext), None, None, 0, output, len(output), ctypes.byref(length), 2) != 0:
            raise CloudError('monitor_credentials_invalid')
        return output.raw[:length.value]
    finally:
        if key.value:
            dll.BCryptDestroyKey(key)
        if algorithm.value:
            dll.BCryptCloseAlgorithmProvider(algorithm, 0)
