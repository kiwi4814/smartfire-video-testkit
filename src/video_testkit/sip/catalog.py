"""GB28181 Catalog（目录发现）的 MESSAGE body（XML）编解码与查询客户端。

Catalog 查询由 Provider 侧通过 SIP ``MESSAGE`` 发送，body 为 MANSCDP XML：

.. code-block:: xml

    <Query>
      <CmdType>Catalog</CmdType>
      <SN>1</SN>
      <DeviceID>34020000001320000001</DeviceID>
    </Query>

设备对一次查询可响应多条 ``MESSAGE``，每条 body 为 Catalog Response XML，
``SumNum`` 为目录总条数，``DeviceList Num`` 为本条消息条数；Provider 侧按
稳定 DeviceID 去重聚合，直到收满 ``SumNum`` 或聚合窗口到期。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from video_testkit.sip.keepalive import CONTENT_TYPE  # noqa: F401  # 复用 MANSCDP 类型

DEFAULT_CHARSET = "GB2312"
PROVIDER_SIP_ID = "34020000002000000001"


@dataclass(frozen=True)
class CatalogQueryData:
    device_id: str
    sn: int


@dataclass(frozen=True)
class CatalogItemData:
    device_id: str
    name: str
    manufacturer: str
    model: str
    status: str  # ON / OFF
    ptz_type: int
    parental: int
    resolution: str
    codec: str
    has_audio: bool
    supports_ptz: bool
    supports_device_record: bool


@dataclass(frozen=True)
class CatalogResponseData:
    sn: int
    device_id: str
    sum_num: int
    items: list[CatalogItemData]


@dataclass
class CatalogQueryResult:
    """一次 Catalog 查询的聚合结果（Provider 侧）。"""

    device_id: str
    query_sn: int
    items: list[CatalogItemData] = field(default_factory=list)
    sum_num: int = 0
    complete: bool = False


class CatalogQueryError(Exception):
    """Catalog 查询不可用（无监听地址、Registrar 未启动等）。"""


def _int_field(root: ET.Element, name: str) -> int:
    elem = root.find(name)
    if elem is None or elem.text is None or not elem.text.strip():
        raise ValueError(f"Catalog XML 缺少字段: {name}")
    try:
        return int(elem.text.strip())
    except ValueError as exc:
        raise ValueError(f"Catalog XML 字段非数字: {name}={elem.text.strip()!r}") from exc


def _text_field(root: ET.Element, name: str, required: bool = True) -> str:
    elem = root.find(name)
    if elem is None or elem.text is None or not elem.text.strip():
        if required:
            raise ValueError(f"Catalog XML 缺少字段: {name}")
        return ""
    return elem.text.strip()


def _bool_field(root: ET.Element, name: str, default: bool = False) -> bool:
    raw = _text_field(root, name, required=False)
    if not raw:
        return default
    return raw not in ("0", "false", "OFF", "FALSE")


def _decode_xml_body(body: bytes) -> str:
    """按 XML 声明中的 encoding 解码 body（ElementTree 对 bytes 仅支持 UTF-8/16）。"""
    match = re.search(rb"<\?xml[^>]*encoding=[\"']([A-Za-z0-9._-]+)[\"']", body[:256])
    encoding = match.group(1).decode("ascii") if match else "utf-8"
    try:
        return body.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")


def parse_catalog_query(body: bytes) -> CatalogQueryData:
    """解析 Catalog 查询 XML；畸形或缺字段时抛 ``ValueError``。"""
    try:
        root = ET.fromstring(_decode_xml_body(body))
    except ET.ParseError as exc:
        raise ValueError(f"Catalog 查询 XML 解析失败: {exc}") from exc

    cmd_type = _text_field(root, "CmdType")
    if cmd_type != "Catalog":
        raise ValueError(f"未知 CmdType: {cmd_type!r}")
    return CatalogQueryData(device_id=_text_field(root, "DeviceID"), sn=_int_field(root, "SN"))


def parse_catalog_response(body: bytes) -> CatalogResponseData:
    """解析 Catalog 响应 XML（按 XML 声明自动解码，支持 GB2312/UTF-8）。"""
    try:
        root = ET.fromstring(_decode_xml_body(body))
    except ET.ParseError as exc:
        raise ValueError(f"Catalog 响应 XML 解析失败: {exc}") from exc

    cmd_type = _text_field(root, "CmdType")
    if cmd_type != "Catalog":
        raise ValueError(f"未知 CmdType: {cmd_type!r}")
    sn = _int_field(root, "SN")
    device_id = _text_field(root, "DeviceID")
    sum_num = _int_field(root, "SumNum")

    device_list = root.find("DeviceList")
    items: list[CatalogItemData] = []
    if device_list is not None:
        for item in device_list.findall("Item"):
            info = item.find("Info")
            ptz_type = 0
            if info is not None:
                ptz_type = _int_field(info, "PTZType")
            items.append(
                CatalogItemData(
                    device_id=_text_field(item, "DeviceID"),
                    name=_text_field(item, "Name"),
                    manufacturer=_text_field(item, "Manufacturer"),
                    model=_text_field(item, "Model"),
                    status=_text_field(item, "Status"),
                    ptz_type=ptz_type,
                    parental=_int_field(item, "Parental"),
                    resolution=_text_field(item, "Resolution", required=False),
                    codec=_text_field(item, "Codec", required=False),
                    has_audio=_bool_field(item, "HasAudio"),
                    supports_ptz=ptz_type > 0,
                    supports_device_record=True,
                )
            )
    return CatalogResponseData(sn=sn, device_id=device_id, sum_num=sum_num, items=items)


def build_catalog_query_xml(device_id: str, sn: int) -> str:
    """构造 Catalog 查询 XML body（UTF-8）。"""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Query>\n"
        "<CmdType>Catalog</CmdType>\n"
        f"<SN>{sn}</SN>\n"
        f"<DeviceID>{device_id}</DeviceID>\n"
        "</Query>"
    )


def build_catalog_response_xml(
    sn: int,
    device_id: str,
    items: list[CatalogItemData],
    sum_num: int,
    charset: str = DEFAULT_CHARSET,
) -> str:
    """构造 Catalog 响应 XML body；``charset`` 同时写入 XML 声明并用于编码。"""
    num = len(items)
    item_xml = "\n".join(_item_xml(item) for item in items)
    return (
        f'<?xml version="1.0" encoding="{charset}"?>\n'
        "<Response>\n"
        "<CmdType>Catalog</CmdType>\n"
        f"<SN>{sn}</SN>\n"
        f"<DeviceID>{device_id}</DeviceID>\n"
        f"<SumNum>{sum_num}</SumNum>\n"
        f'<DeviceList Num="{num}">\n'
        f"{item_xml}\n"
        "</DeviceList>\n"
        "</Response>"
    )


def _item_xml(item: CatalogItemData) -> str:
    return (
        "<Item>\n"
        f"<DeviceID>{item.device_id}</DeviceID>\n"
        f"<Name>{item.name}</Name>\n"
        f"<Manufacturer>{item.manufacturer}</Manufacturer>\n"
        f"<Model>{item.model}</Model>\n"
        "<Owner></Owner>\n"
        "<CivilCode></CivilCode>\n"
        "<Address></Address>\n"
        f"<Parental>{item.parental}</Parental>\n"
        "<SafetyWay>0</SafetyWay>\n"
        "<RegisterWay>1</RegisterWay>\n"
        "<Secrecy>0</Secrecy>\n"
        f"<Status>{item.status}</Status>\n"
        f"<Resolution>{item.resolution}</Resolution>\n"
        f"<Codec>{item.codec}</Codec>\n"
        f"<HasAudio>{1 if item.has_audio else 0}</HasAudio>\n"
        "<Info>\n"
        f"<PTZType>{item.ptz_type}</PTZType>\n"
        "</Info>\n"
        "</Item>"
    )
