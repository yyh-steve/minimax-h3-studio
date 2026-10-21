"""Local single-image Ref2VA subset with mandatory persisted idempotency keys."""
from __future__ import annotations

import httpx

from ..config import is_official_h3_origin
from .base import CreateResult, ProviderError, classify_http_error
from .capabilities import validate_local_request
from .h3_official import OfficialH3


class LocalH3(OfficialH3):
    def _verify_local_contract(self) -> None:
        if is_official_h3_origin(self.base_url):
            raise ProviderError('bad_request', 'local 模式不能连接官方 H3 地址', retryable=False)
        try:
            with self._client() as client:
                response = client.get('/capabilities')
        except httpx.HTTPError as error:
            # No generation POST has been sent; retrying this preflight is safe.
            raise ProviderError('network', '本地服务能力检查失败，尚未提交生成请求', retryable=True) from error
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code // 100 != 2:
            raise classify_http_error(response.status_code, data)
        if (not isinstance(data, dict) or data.get('model') != 'MiniMax-H3'
                or data.get('workflow') != 'single_image_ref2va'
                or not isinstance(data.get('local_extensions'), list)
                or 'Idempotency-Key' not in data['local_extensions']):
            raise ProviderError('bad_request', '目标服务未声明本地 Ref2VA 与 Idempotency-Key 契约，拒绝提交',
                                code='local_contract_unverified', retryable=False)

    def create(self, request_json: dict, *, idempotency_key: str) -> CreateResult:
        if (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128
                or any(ord(c) < 33 or ord(c) > 126 for c in idempotency_key)):
            raise ValueError("本地 Idempotency-Key 必须是 1–128 位无空格的可打印 ASCII，且须持久化复用")
        validate_local_request(request_json)
        self._verify_local_contract()
        return self._create(request_json, headers={"Idempotency-Key": idempotency_key})
