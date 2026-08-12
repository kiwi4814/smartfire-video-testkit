"""GB28181 Keepalive 的 MESSAGE body（XML）编解码。

Keepalive 通过 SIP ``MESSAGE`` 请求携带，body 为 MANSCDP XML：

.. code-block:: xml

    <Notify>
      <CmdType>Keepalive</CmdType>
      <SN>1</SN>
      <DeviceID>34020000001320000001</DeviceID>
      <Status>OK</Status>
    </Notify>
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

CONTENT_TYPE = "Application/MANSCDP+xml"


@dataclass(frozen=True)
class KeepaliveData:
    device_id: str
    sn: int
    status: str


def build_keepalive_xml(device_id: str, sn: int, status: str = "OK") -> str:
    """构造 Keepalive XML body。"""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Notify>\n"
        "<CmdType>Keepalive</CmdType>\n"
        f"<SN>{sn}</SN>\n"
        f"<DeviceID>{device_id}</DeviceID>\n"
        f"<Status>{status}</Status>\n"
        "</Notify>"
    )


def parse_keepalive_xml(body: str) -> KeepaliveData:
    """解析 Keepalive XML body；畸形或缺字段时抛 ``ValueError``。"""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"Keepalive XML 解析失败: {exc}") from exc

    def _field(name: str) -> str:
        elem = root.find(name)
        if elem is None or not elem.text or not elem.text.strip():
            raise ValueError(f"Keepalive XML 缺少字段: {name}")
        return elem.text.strip()

    device_id = _field("DeviceID")
    sn_raw = _field("SN")
    try:
        sn = int(sn_raw)
    except ValueError as exc:
        raise ValueError(f"Keepalive SN 非数字: {sn_raw!r}") from exc
    status = _field("Status")

    cmd_type = root.findtext("CmdType")
    if cmd_type is not None and cmd_type.strip() != "Keepalive":
        raise ValueError(f"未知 CmdType: {cmd_type!r}")

    return KeepaliveData(device_id=device_id, sn=sn, status=status)
