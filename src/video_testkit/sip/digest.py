"""SIP Digest 鉴权（MD5 + qop=auth）。

仅用于测试套件内的 REGISTER 鉴权，不实现任何 RFC 完整性之外的扩展。
"""

from __future__ import annotations

import hashlib
import re
import secrets

_PARAM_RE = re.compile(r'([A-Za-z0-9_\-]+)\s*=\s*(?:"([^"]*)"|([^,\s]+))')


def md5_hex(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def generate_nonce() -> str:
    return secrets.token_hex(16)


def generate_cnonce() -> str:
    return secrets.token_hex(8)


def parse_params(header: str) -> dict[str, str]:
    """解析逗号分隔的键值参数（含引号值），例如 WWW-Authenticate 头。"""
    return {m.group(1).lower(): (m.group(2) or m.group(3)) for m in _PARAM_RE.finditer(header)}


def compute_response(
    username: str,
    realm: str,
    password: str,
    nonce: str,
    method: str,
    uri: str,
    nc: str = "00000001",
    cnonce: str | None = None,
    qop: str = "auth",
) -> str:
    ha1 = md5_hex(f"{username}:{realm}:{password}")
    ha2 = md5_hex(f"{method}:{uri}")
    if qop == "auth":
        if cnonce is None:
            raise ValueError("qop=auth 需要 cnonce")
        return md5_hex(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    return md5_hex(f"{ha1}:{nonce}:{ha2}")


def build_authorization_header(
    username: str,
    realm: str,
    nonce: str,
    uri: str,
    method: str,
    password: str,
    cnonce: str,
    nc: str = "00000001",
) -> str:
    response = compute_response(username, realm, password, nonce, method, uri, nc, cnonce, "auth")
    return (
        f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
        f'uri="{uri}", response="{response}", algorithm=MD5, cnonce="{cnonce}", '
        f'opaque="", qop=auth, nc={nc}'
    )
