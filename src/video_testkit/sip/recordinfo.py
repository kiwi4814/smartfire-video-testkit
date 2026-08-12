"""GB28181 RecordInfo（设备录像目录）的 MESSAGE body（XML）编解码。

RecordInfo 查询由 Provider 侧通过 SIP ``MESSAGE`` 发送，body 为 MANSCDP XML：

.. code-block:: xml

    <Query>
      <CmdType>RecordInfo</CmdType>
      <SN>1</SN>
      <DeviceID>34020000001310000001</DeviceID>
      <StartTime>2026-08-01T00:00:00</StartTime>
      <EndTime>2026-08-01T02:30:00</EndTime>
      <Type>ALL</Type>
    </Query>

设备对一次查询可响应多条 ``MESSAGE``，每条 body 为 RecordInfo Response XML，
``SumNum`` 为录像总条数，``RecordList Num`` 为本条消息条数；Provider 侧按
稳定时间区间（左闭右开）去重聚合，直到收满 ``SumNum`` 或聚合窗口到期。
时间字段为 GB28181 本地时间格式（``yyyy-MM-ddTHH:mm:ss``，无时区），本模块
统一按 UTC 解释（设备时间偏移由设备侧场景控制）。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime

DEFAULT_CHARSET = "GB2312"

GB_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


@dataclass(frozen=True)
class RecordInfoQueryData:
    device_id: str
    sn: int
    start_time: datetime
    end_time: datetime
    record_type: str


@dataclass(frozen=True)
class RecordInfoItemData:
    device_id: str
    name: str
    start_time: datetime
    end_time: datetime
    record_type: str
    file_size: int = 0
    file_path: str = ""


@dataclass(frozen=True)
class RecordInfoResponseData:
    sn: int
    device_id: str
    sum_num: int
    items: list[RecordInfoItemData]


@dataclass
class RecordInfoQueryResult:
    """一次 RecordInfo 查询的聚合结果（Provider 侧）。"""

    device_id: str
    query_sn: int
    items: list[RecordInfoItemData] = field(default_factory=list)
    sum_num: int = 0
    complete: bool = False


class RecordInfoQueryError(Exception):
    """RecordInfo 查询不可用（无监听地址、Registrar 未启动等）。"""


# ---------------------------------------------------------------- 时间辅助


def parse_gb_time(raw: str) -> datetime:
    """解析 GB28181 本地时间（``yyyy-MM-ddTHH:mm:ss``，可带 Z/offset），按 UTC 解释。"""
    text = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"RecordInfo 时间格式非法: {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_gb_time(dt: datetime) -> str:
    """将 aware datetime 格式化为 GB28181 本地时间文本（UTC 秒精度）。"""
    return dt.astimezone(UTC).strftime(GB_TIME_FORMAT)


def _gb_time_field(value: datetime | str) -> str:
    if isinstance(value, str):
        return format_gb_time(parse_gb_time(value))
    return format_gb_time(value)


# ---------------------------------------------------------------- 字段辅助


def _int_field(root: ET.Element, name: str) -> int:
    elem = root.find(name)
    if elem is None or elem.text is None or not elem.text.strip():
        raise ValueError(f"RecordInfo XML 缺少字段: {name}")
    try:
        return int(elem.text.strip())
    except ValueError as exc:
        raise ValueError(f"RecordInfo XML 字段非数字: {name}={elem.text.strip()!r}") from exc


def _text_field(root: ET.Element, name: str, required: bool = True) -> str:
    elem = root.find(name)
    if elem is None or elem.text is None or not elem.text.strip():
        if required:
            raise ValueError(f"RecordInfo XML 缺少字段: {name}")
        return ""
    return elem.text.strip()


def _decode_xml_body(body: bytes) -> str:
    """按 XML 声明中的 encoding 解码 body（ElementTree 对 bytes 仅支持 UTF-8/16）。"""
    match = re.search(rb"<\?xml[^>]*encoding=[\"']([A-Za-z0-9._-]+)[\"']", body[:256])
    encoding = match.group(1).decode("ascii") if match else "utf-8"
    try:
        return body.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 解析


def parse_recordinfo_query(body: bytes) -> RecordInfoQueryData:
    """解析 RecordInfo 查询 XML；畸形或缺字段时抛 ``ValueError``。"""
    try:
        root = ET.fromstring(_decode_xml_body(body))
    except ET.ParseError as exc:
        raise ValueError(f"RecordInfo 查询 XML 解析失败: {exc}") from exc

    cmd_type = _text_field(root, "CmdType")
    if cmd_type != "RecordInfo":
        raise ValueError(f"未知 CmdType: {cmd_type!r}")
    return RecordInfoQueryData(
        device_id=_text_field(root, "DeviceID"),
        sn=_int_field(root, "SN"),
        start_time=parse_gb_time(_text_field(root, "StartTime")),
        end_time=parse_gb_time(_text_field(root, "EndTime")),
        record_type=_text_field(root, "Type", required=False) or "ALL",
    )


def parse_recordinfo_response(body: bytes) -> RecordInfoResponseData:
    """解析 RecordInfo 响应 XML（按 XML 声明自动解码，支持 GB2312/UTF-8）。"""
    try:
        root = ET.fromstring(_decode_xml_body(body))
    except ET.ParseError as exc:
        raise ValueError(f"RecordInfo 响应 XML 解析失败: {exc}") from exc

    cmd_type = _text_field(root, "CmdType")
    if cmd_type != "RecordInfo":
        raise ValueError(f"未知 CmdType: {cmd_type!r}")
    sn = _int_field(root, "SN")
    device_id = _text_field(root, "DeviceID")
    sum_num = _int_field(root, "SumNum")

    record_list = root.find("RecordList")
    items: list[RecordInfoItemData] = []
    if record_list is not None:
        for item in record_list.findall("Item"):
            items.append(
                RecordInfoItemData(
                    device_id=_text_field(item, "DeviceID"),
                    name=_text_field(item, "Name"),
                    start_time=parse_gb_time(_text_field(item, "StartTime")),
                    end_time=parse_gb_time(_text_field(item, "EndTime")),
                    record_type=_text_field(item, "Type", required=False) or "time",
                    file_size=(
                        _int_field(item, "FileSize") if item.find("FileSize") is not None else 0
                    ),
                    file_path=_text_field(item, "FilePath", required=False),
                )
            )
    return RecordInfoResponseData(sn=sn, device_id=device_id, sum_num=sum_num, items=items)


# ---------------------------------------------------------------- 构造


def build_recordinfo_query_xml(
    device_id: str,
    sn: int,
    start_time: datetime | str,
    end_time: datetime | str,
    record_type: str = "ALL",
) -> str:
    """构造 RecordInfo 查询 XML body（UTF-8）。"""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Query>\n"
        "<CmdType>RecordInfo</CmdType>\n"
        f"<SN>{sn}</SN>\n"
        f"<DeviceID>{device_id}</DeviceID>\n"
        f"<StartTime>{_gb_time_field(start_time)}</StartTime>\n"
        f"<EndTime>{_gb_time_field(end_time)}</EndTime>\n"
        f"<Type>{record_type}</Type>\n"
        "</Query>"
    )


def build_recordinfo_response_xml(
    sn: int,
    device_id: str,
    items: list[RecordInfoItemData],
    sum_num: int,
    charset: str = DEFAULT_CHARSET,
) -> str:
    """构造 RecordInfo 响应 XML body；``charset`` 同时写入 XML 声明并用于编码。"""
    num = len(items)
    item_xml = "\n".join(_item_xml(item) for item in items)
    return (
        f'<?xml version="1.0" encoding="{charset}"?>\n'
        "<Response>\n"
        "<CmdType>RecordInfo</CmdType>\n"
        f"<SN>{sn}</SN>\n"
        f"<DeviceID>{device_id}</DeviceID>\n"
        f"<SumNum>{sum_num}</SumNum>\n"
        f'<RecordList Num="{num}">\n'
        f"{item_xml}\n"
        "</RecordList>\n"
        "</Response>"
    )


def _item_xml(item: RecordInfoItemData) -> str:
    return (
        "<Item>\n"
        f"<DeviceID>{item.device_id}</DeviceID>\n"
        f"<Name>{item.name}</Name>\n"
        f"<FilePath>{item.file_path}</FilePath>\n"
        "<Address></Address>\n"
        f"<StartTime>{format_gb_time(item.start_time)}</StartTime>\n"
        f"<EndTime>{format_gb_time(item.end_time)}</EndTime>\n"
        "<Secrecy>0</Secrecy>\n"
        f"<Type>{item.record_type}</Type>\n"
        f"<FileSize>{item.file_size}</FileSize>\n"
        "</Item>"
    )
