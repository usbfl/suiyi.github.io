"""Windows DPAPI or an explicit Fernet master key; never plaintext storage."""
import base64
import ctypes
import os
from ctypes import wintypes
from cryptography.fernet import Fernet, InvalidToken

def _fernet():
    key = os.getenv('ALPHALAB_SECRET_KEY', '')
    if not key:
        raise ValueError('Linux/Docker 密钥保险箱需要 ALPHALAB_SECRET_KEY；不会以明文保存密钥')
    try:
        return Fernet(key.encode('ascii'))
    except (ValueError, UnicodeError):
        raise ValueError('ALPHALAB_SECRET_KEY 必须是 Fernet.generate_key() 生成的密钥') from None

class Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]

def _crypt(value,protect):
    if os.name!='nt':raise ValueError('此版本密钥保险箱需要 Windows DPAPI；不会以明文保存密钥')
    buffer=ctypes.create_string_buffer(value)
    source=Blob(len(value),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)));target=Blob()
    crypt=ctypes.windll.crypt32
    if protect:
        ok=crypt.CryptProtectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target))
    else:
        ok=crypt.CryptUnprotectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target))
    if not ok:raise ValueError('无法访问当前 Windows 用户的密钥保险箱')
    try:return ctypes.string_at(target.data,target.size)
    finally:ctypes.windll.kernel32.LocalFree(target.data)

def seal(value):
    if not value:return ''
    if os.name == 'nt' and not os.getenv('ALPHALAB_SECRET_KEY'):
        return 'dpapi:'+base64.b64encode(_crypt(value.encode('utf-8'),True)).decode('ascii')
    return 'fernet:'+_fernet().encrypt(value.encode('utf-8')).decode('ascii')

def unseal(value):
    if not value:return ''
    if value.startswith('fernet:'):
        try:return _fernet().decrypt(value[7:].encode('ascii')).decode('utf-8')
        except (InvalidToken, UnicodeError):
            raise ValueError('密钥无法解密；请检查 ALPHALAB_SECRET_KEY 是否与保存时一致') from None
    if not value.startswith('dpapi:'):raise ValueError('不支持的密钥存储格式')
    return _crypt(base64.b64decode(value[6:]),False).decode('utf-8')
