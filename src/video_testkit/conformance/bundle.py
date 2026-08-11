"""Contract Bundle 的加载、Checksum 校验与 Schema 验证。"""

from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
from pathlib import Path
from typing import Any, cast

import jsonschema
import yaml


class ContractValidationError(Exception):
    """契约 Schema 校验失败异常。"""

    def __init__(
        self,
        operation_id: str,
        request_id: str,
        expected: str,
        actual: Any,
        message: str,
    ) -> None:
        self.operation_id = operation_id
        self.request_id = request_id
        self.expected = expected
        self.actual = actual
        self.message = message
        super().__init__(
            f"Contract mismatch in operation '{operation_id}' (requestId: {request_id}): {message}"
        )


class ContractBundle:
    """版本化 Contract Bundle 对象。"""

    def __init__(
        self,
        bundle_dir: Path,
        version: str,
        checksum: str,
        spec: dict[str, Any],
        manifest: dict[str, Any],
    ) -> None:
        self.bundle_dir = bundle_dir
        self.version = version
        self.checksum = checksum
        self.spec = spec
        self.manifest = manifest
        self._root_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "components": spec.get("components", {}),
        }
        self._operation_map = self._build_operation_map()

    @classmethod
    def load(
        self,
        bundle_path: str | Path | None = None,
        expected_version: str = "1.0.0-draft.1",
    ) -> ContractBundle:
        """从 tar.gz 或解压目录加载 Bundle，并执行完整 Checksum 校验。"""
        if bundle_path is None:
            # 查找默认的 vendored 路径
            root = Path(__file__).resolve().parents[3]
            default_archive = (
                root
                / "contracts"
                / "video-provider"
                / f"smartfire-video-provider-contract-{expected_version}.tar.gz"
            )
            if not default_archive.exists():
                # 尝试项目根目录下的相对路径
                default_archive = (
                    Path.cwd()
                    / "contracts"
                    / "video-provider"
                    / f"smartfire-video-provider-contract-{expected_version}.tar.gz"
                )
            bundle_path = default_archive

        bundle_path = Path(bundle_path)
        if not bundle_path.exists():
            raise FileNotFoundError(f"Contract Bundle file not found at: {bundle_path}")

        temp_dir: tempfile.TemporaryDirectory[str] | None = None
        if bundle_path.is_file() and bundle_path.name.endswith(".tar.gz"):
            archive_sha256 = self._compute_sha256(bundle_path)

            # 读取可选的 .sha256 文件
            sha_file = bundle_path.with_name(bundle_path.name + ".sha256")
            if sha_file.exists():
                expected_sha = sha_file.read_text(encoding="utf-8").strip().split()[0]
                if archive_sha256.lower() != expected_sha.lower():
                    msg = f"Bundle SHA-256 mismatch! Exp: {expected_sha}, Got: {archive_sha256}"
                    raise ValueError(msg)

            temp_dir = tempfile.TemporaryDirectory(prefix="contract_bundle_")
            with tarfile.open(bundle_path, "r:gz") as tar:
                tar.extractall(path=temp_dir.name)

            # 可能是直接包含文件，也可能是有一个同名子目录
            bundle_dir = Path(temp_dir.name)
            subdirs = list(bundle_dir.iterdir())
            if len(subdirs) == 1 and subdirs[0].is_dir():
                bundle_dir = subdirs[0]
            checksum = archive_sha256
        elif bundle_path.is_dir():
            bundle_dir = bundle_path
            checksum = "dir-unpacked"
        else:
            raise ValueError(f"Unsupported bundle path: {bundle_path}")

        manifest_file = bundle_dir / "bundle-manifest.json"
        if not manifest_file.exists():
            raise FileNotFoundError(f"bundle-manifest.json missing in {bundle_dir}")

        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        actual_version = manifest.get("contractVersion")
        if actual_version != expected_version:
            raise ValueError(
                f"Contract version mismatch! Expected {expected_version}, got {actual_version}"
            )

        # 验证 manifest 中记录的各个文件的 SHA-256
        for item in manifest.get("files", []):
            item_path = bundle_dir / item["path"]
            if item_path.exists() and item_path.is_file():
                computed_file_sha = self._compute_sha256(item_path)
                expected_file_sha = item["sha256"]
                if computed_file_sha.lower() != expected_file_sha.lower():
                    raise ValueError(
                        f"File corruption detected in bundle! {item['path']} sha256 mismatch."
                    )

        openapi_file = bundle_dir / "openapi.yaml"
        if not openapi_file.exists():
            raise FileNotFoundError(f"openapi.yaml missing in {bundle_dir}")

        spec = yaml.safe_load(openapi_file.read_text(encoding="utf-8"))

        return ContractBundle(
            bundle_dir=bundle_dir,
            version=actual_version,
            checksum=checksum,
            spec=spec,
            manifest=manifest,
        )

    @staticmethod
    def _compute_sha256(file_path: Path) -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()

    def _build_operation_map(self) -> dict[str, tuple[str, str, dict[str, Any]]]:
        """建立 operationId -> (path, method, operation_spec) 索引。"""
        op_map = {}
        for path, path_item in self.spec.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, op_spec in path_item.items():
                if not isinstance(op_spec, dict):
                    continue
                op_id = op_spec.get("operationId")
                if op_id:
                    op_map[op_id] = (path, method.upper(), op_spec)
        return op_map

    def get_operation_spec(self, operation_id: str) -> tuple[str, str, dict[str, Any]]:
        if operation_id not in self._operation_map:
            raise KeyError(f"OperationId '{operation_id}' not found in OpenAPI contract")
        return self._operation_map[operation_id]

    def get_response_schema(self, operation_id: str, status_code: int) -> dict[str, Any] | None:
        _, _, op_spec = self.get_operation_spec(operation_id)
        responses = op_spec.get("responses", {})
        code_str = str(status_code)
        resp_def = responses.get(code_str) or responses.get("default")
        if not resp_def:
            return None

        # 解析 $ref 对应的 response
        if "$ref" in resp_def:
            ref_path = resp_def["$ref"].lstrip("#/").split("/")
            curr = self.spec
            for part in ref_path:
                curr = curr.get(part, {})
            resp_def = curr

        content = resp_def.get("content", {})
        app_json = content.get("application/json", {})
        schema = app_json.get("schema")
        return cast(dict[str, Any], schema) if isinstance(schema, dict) else None

    def validate_response(
        self,
        operation_id: str,
        status_code: int,
        body: Any,
        request_id: str = "",
    ) -> None:
        """对响应 JSON Body 执行 JSON Schema 契约断言。"""
        if status_code == 204:
            if body:
                raise ContractValidationError(
                    operation_id=operation_id,
                    request_id=request_id,
                    expected="Empty response body for 204 No Content",
                    actual=body,
                    message="204 No Content response must have empty body",
                )
            return

        schema = self.get_response_schema(operation_id, status_code)
        if schema is None:
            # OpenAPI 中未声明该状态码的 content schema
            return

        full_schema = {**self._root_schema, **schema}
        validator = jsonschema.Draft202012Validator(full_schema)
        errors = list(validator.iter_errors(body))

        if errors:
            err = errors[0]
            path_str = ".".join(str(p) for p in err.path) if err.path else "root"
            expected_desc = f"Schema rule at '{path_str}': {err.message}"
            actual_val = err.instance
            raise ContractValidationError(
                operation_id=operation_id,
                request_id=request_id,
                expected=expected_desc,
                actual=actual_val,
                message=f"Field '{path_str}' contract violation: {err.message}",
            )
