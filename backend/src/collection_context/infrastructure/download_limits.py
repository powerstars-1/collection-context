"""Local original-media policy, independent from model upload/derived-input limits."""
from collection_context.application.contracts import ContextError

MB = 1_000_000
DEFAULT_DOWNLOAD_MB = 1024
MAX_DOWNLOAD_MB = 2048
MAX_SOURCE_BYTES = MAX_DOWNLOAD_MB * MB


def validate_download_mb(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_DOWNLOAD_MB:
        raise ContextError("invalid_argument", f"单个文件最大下载大小须为1至{MAX_DOWNLOAD_MB} MB的整数。")
    return value


def download_max_bytes(settings: dict) -> int:
    return validate_download_mb(settings.get("max_download_mb", DEFAULT_DOWNLOAD_MB)) * MB
