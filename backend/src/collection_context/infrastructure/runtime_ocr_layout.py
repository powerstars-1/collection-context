"""Fixed private OCR weight candidate, not upstream-code license closure."""

from pathlib import Path

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.runtime_media_layout import bundled_component_archive

OCR_ID = "ocr-ppocrv6-small-rapidocr-3.9.2-development-1"
OCR_FILENAME = OCR_ID + ".zip"
OCR_BYTES = 31_845_897
OCR_SHA256 = "18f132093a969356c4f788a8166fbc3dd30271d153d4030fcd06845059004f93"
OCR_PAYLOAD_BYTES = 31_844_953


def bundled_ocr_archive() -> Path:
    return bundled_component_archive(OCR_FILENAME, OCR_BYTES)


def bundled_ocr_state() -> str:
    try:
        bundled_ocr_archive()
    except ContextError as error:
        return (
            "bundled_component_missing"
            if error.code == "runtime_component_not_bundled"
            else "bundled_component_invalid"
        )
    return "available"
