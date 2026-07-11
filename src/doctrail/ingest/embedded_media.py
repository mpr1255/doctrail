"""Extract, OCR, and store images embedded inside Office documents.

Office files (``.doc/.docx/.xls/.xlsx/.ppt/.pptx``) frequently paste screenshots
inline. A text-only extractor drops every one, yet those images are often the
primary evidence (a captured web page, a scanned table). This module pulls the
embedded images out, OCRs each one, and stores it as its own row in the same
``documents`` table, linked back to the parent document so a claim in the parent
text can be tied to the screenshot that proves it.

Extraction paths
----------------
- OOXML (``.docx/.xlsx/.pptx``) is a zip: read entries under ``word/media/`` ·
  ``xl/media/`` · ``ppt/media/`` directly, no conversion.
- Legacy binary (``.doc/.xls/.ppt``) hides pictures in OLE/Escher streams that
  python-docx/openpyxl ignore. A fast byte-scan for JPEG/PNG magic decides
  whether the file has any images at all; if so, LibreOffice headless converts
  it to OOXML and we read the media from the converted zip.

Row model
---------
Each embedded-image occurrence becomes a ``documents`` row keyed by its parent,
position, media name, and image hash. ``parent_sha1`` / ``parent_path`` /
``image_index`` record where it was found, while ``image_sha1`` preserves the
content identity shared by duplicate screenshots. ``raw_content`` is the OCR
text (so FTS indexes it); EXIF and the original media name land in ``metadata``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Zip paths that hold embedded pictures across the OOXML formats.
_OOXML_MEDIA_PREFIXES = ("word/media/", "xl/media/", "ppt/media/", "word/embeddings/")
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".emf", ".wmf"}

# What LibreOffice should convert each legacy binary to before we read its media.
_LEGACY_CONVERT_TARGET = {".doc": "docx", ".xls": "xlsx", ".ppt": "pptx"}
_OOXML_SUFFIXES = {".docx", ".xlsx", ".pptx"}

# OCR-able raster formats (the farm / textra path). EMF/WMF are vector and skipped.
_OCRABLE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff"}


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def embedded_occurrence_sha1(
    parent_sha1: str, image_index: int, media_name: str, image_sha1: str
) -> str:
    """Stable row key for one image occurrence inside one parent document."""
    identity = f"embedded-media-v1\0{parent_sha1}\0{image_index}\0{media_name}\0{image_sha1}"
    return sha1_bytes(identity.encode("utf-8", "surrogatepass"))


def scan_has_embedded_image(path: Path) -> bool:
    """Cheap yes/no: do the raw bytes contain a JPEG or PNG signature?

    Used to skip LibreOffice conversion for legacy files with no JPEG or PNG.
    Other image formats are not safely recognizable from the compound file's
    raw bytes, so callers must still treat this as a performance hint rather
    than proof that a document contains no media.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return (b"\xff\xd8\xff" in data) or (b"\x89PNG\r\n\x1a\n" in data)


def _zip_media(data: bytes) -> List[Tuple[str, bytes]]:
    """Return (media_name, image_bytes) for every embedded image in an OOXML zip."""
    out: List[Tuple[str, bytes]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                lower = name.lower()
                if not lower.startswith(_OOXML_MEDIA_PREFIXES):
                    continue
                if Path(lower).suffix not in _IMAGE_SUFFIXES:
                    continue
                try:
                    out.append((name, zf.read(name)))
                except Exception as exc:  # corrupt entry: skip, keep the rest
                    logger.warning(f"Could not read zip media {name}: {exc}")
    except zipfile.BadZipFile:
        return []
    return out


def _libreoffice_to_ooxml(path: Path, target: str, workdir: Path,
                          soffice: str = "soffice", timeout: int = 180) -> Optional[Path]:
    """Convert a legacy binary Office file to OOXML with LibreOffice headless.

    Uses a private per-call UserInstallation profile so concurrent conversions
    don't fight over one shared profile lock. Returns the converted file path or
    None on failure.
    """
    profile = workdir / "lo_profile"
    cmd = [
        soffice, "--headless", "--norestore", "--nolockcheck",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to", target, "--outdir", str(workdir), str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning(f"LibreOffice timed out converting {path.name}")
        return None
    except FileNotFoundError:
        raise RuntimeError(
            "LibreOffice (soffice) not found; install it to extract images from "
            "legacy .doc/.xls/.ppt files, or restrict to OOXML."
        )
    if proc.returncode != 0:
        logger.warning(f"LibreOffice failed on {path.name}: {proc.stderr.decode('utf-8', 'replace')[:200]}")
        return None
    converted = workdir / f"{path.stem}.{target}"
    return converted if converted.exists() else None


def office_images(path: Path, *, soffice: str = "soffice") -> List[Tuple[str, bytes]]:
    """Return every embedded image (name, bytes) in an Office file, [] if none.

    OOXML is read directly; legacy binaries are byte-scanned first and only
    converted via LibreOffice when they actually contain image data.
    """
    suffix = path.suffix.lower()
    if suffix in _OOXML_SUFFIXES:
        try:
            return _zip_media(path.read_bytes())
        except OSError:
            return []
    if suffix in _LEGACY_CONVERT_TARGET:
        if not scan_has_embedded_image(path):
            return []
        with tempfile.TemporaryDirectory(prefix="doctrail_media_") as tmp:
            workdir = Path(tmp)
            converted = _libreoffice_to_ooxml(path, _LEGACY_CONVERT_TARGET[suffix], workdir, soffice=soffice)
            if converted is None:
                return []
            return _zip_media(converted.read_bytes())
    return []


def image_exif(data: bytes) -> Dict[str, object]:
    """Pull basic image dimensions + EXIF tags. Returns {} if unreadable.

    Kept small and JSON-safe: format, size, mode, and human-named EXIF tags.
    """
    try:
        from PIL import Image, ExifTags
    except Exception:
        return {}
    meta: Dict[str, object] = {}
    try:
        with Image.open(io.BytesIO(data)) as im:
            meta["image_format"] = im.format
            meta["image_width"], meta["image_height"] = im.size
            meta["image_mode"] = im.mode
            raw = getattr(im, "getexif", lambda: None)()
            if raw:
                for tag_id, value in raw.items():
                    tag = ExifTags.TAGS.get(tag_id, str(tag_id))
                    if isinstance(value, bytes):
                        continue  # skip binary blobs (thumbnails, maker notes)
                    meta[f"exif_{tag}"] = str(value)
    except Exception as exc:
        logger.debug(f"EXIF read failed: {exc}")
        return meta
    return meta


def is_ocrable(media_name: str) -> bool:
    return Path(media_name.lower()).suffix in _OCRABLE_SUFFIXES


# Office file types we scan for embedded images.
OFFICE_SUFFIXES = set(_OOXML_SUFFIXES) | set(_LEGACY_CONVERT_TARGET)


def _office_files(input_dirs: List[str]) -> List[Path]:
    """Collect Office files under the input dirs, pruning hidden directories
    (.unison/.stversions/.git) the same way the main ingest walker does."""
    found: List[Path] = []
    for d in input_dirs:
        root = Path(d)
        if root.is_file():
            if root.suffix.lower() in OFFICE_SUFFIXES:
                found.append(root)
            continue
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix.lower() not in OFFICE_SUFFIXES:
                continue
            if any(part.startswith(".") for part in f.relative_to(root).parts[:-1]):
                continue
            found.append(f)
    return found


# Below this min-dimension, Office-embedded images are thumbnails that OCR
# cannot read (verified: 120-180px inline pictures return no text at any
# upscale). We still store the row (linked to its parent, with EXIF) but skip
# the wasted OCR call.
DEFAULT_MIN_OCR_DIM = 200


def _extract_rows_for_file(path: Path, *, soffice: str, ocr_fn: Callable[[Path], str],
                           ocr_engine: str, min_ocr_dim: int = DEFAULT_MIN_OCR_DIM) -> List[Dict[str, object]]:
    """Extract, EXIF, and OCR every embedded image in one Office file.

    Returns child-row payloads (one per image) ready for insert_document. The
    parent link is the sha1 of the parent file's bytes, which equals the parent
    row's own sha1 from the main text ingest.
    """
    try:
        parent_bytes = path.read_bytes()
    except OSError as exc:
        logger.warning(f"Could not read {path}: {exc}")
        return []
    parent_sha1 = sha1_bytes(parent_bytes)
    images = office_images(path, soffice=soffice)
    rows: List[Dict[str, object]] = []
    for index, (media_name, data) in enumerate(images):
        img_sha1 = sha1_bytes(data)
        row_sha1 = embedded_occurrence_sha1(parent_sha1, index, media_name, img_sha1)
        exif = image_exif(data)
        w = int(exif.get("image_width") or 0)
        h = int(exif.get("image_height") or 0)
        min_dim = min(w, h) if (w and h) else 0
        ocr_text = ""
        ocr_skipped = None
        if is_ocrable(media_name) and min_dim >= min_ocr_dim:
            try:
                ocr_text = ocr_fn_for_bytes(data, media_name, ocr_fn)
            except Exception as exc:
                logger.warning(f"OCR failed for {path.name} {media_name}: {exc}")
        elif is_ocrable(media_name):
            ocr_skipped = f"below_min_dim:{min_dim}<{min_ocr_dim}"
        metadata = {
            "source_format": "embedded-image",
            "extraction_method": "libreoffice-media" if path.suffix.lower() in _LEGACY_CONVERT_TARGET else "ooxml-media",
            "processing_method": "embedded-media-ocr",
            "embedded_media_name": media_name,
            "Content-Type": f"image/{Path(media_name).suffix.lower().lstrip('.')}",
        }
        if ocr_skipped:
            metadata["ocr_skipped"] = ocr_skipped
        metadata.update(exif)
        rows.append({
            "sha1": row_sha1,
            "synthetic_path": f"{path}#{media_name}",
            "content": ocr_text,
            "metadata": metadata,
            "parent_path": str(path),
            "extra_fields": {
                "parent_sha1": parent_sha1,
                "parent_path": str(path),
                "image_index": str(index),
                "image_sha1": img_sha1,
                "source_format": "embedded-image",
                "ocr_engine": ocr_engine if ocr_text else "",
            },
        })
    return rows


def ocr_fn_for_bytes(data: bytes, media_name: str, ocr_fn: Callable[[Path], str]) -> str:
    """Write image bytes to a temp file (the OCR clients take a path) and OCR."""
    suffix = Path(media_name).suffix.lower() or ".png"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tf:
        tf.write(data)
        tf.flush()
        return ocr_fn(Path(tf.name)) or ""


def _mac_ocr_callable():
    """Return an ocr(path)->text callable backed by the local mac-ocr farm."""
    from .document_processor import _load_mac_ocr_module
    import asyncio

    module = _load_mac_ocr_module()

    def _ocr(path: Path) -> str:
        return asyncio.run(module.ocr_async(str(path))) or ""

    return _ocr, "mac-ocr/apple-vision"


def run_media_ingest(
    input_dirs: List[str],
    db_path: str,
    table_name: str = "documents",
    *,
    ocr_engine: str = "mac-ocr",
    workers: int = 6,
    overwrite: bool = False,
    limit: Optional[int] = None,
    soffice: str = "soffice",
    min_ocr_dim: int = DEFAULT_MIN_OCR_DIM,
) -> Dict[str, int]:
    """Extract + OCR embedded Office images into ``table_name`` as child rows.

    Each image occurrence becomes a stable row linked to its parent via
    ``parent_sha1`` / ``image_index``. The separate ``image_sha1`` field records
    duplicate image content across parents. FTS on ``raw_content`` makes the OCR
    text searchable next to the source text.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import sqlite_utils
    from .database import insert_document, setup_fts

    if ocr_engine == "mac-ocr":
        ocr_fn, ocr_label = _mac_ocr_callable()
    elif ocr_engine == "none":
        ocr_fn, ocr_label = (lambda p: ""), "none"
    else:
        raise ValueError(f"Unsupported ocr_engine for media ingest: {ocr_engine}")

    files = _office_files(input_dirs)
    if limit:
        files = files[:limit]
    logger.info(f"Scanning {len(files)} Office file(s) for embedded images")

    db = sqlite_utils.Database(db_path)
    seen_row_sha1: set = set()
    if table_name in db.table_names() and not overwrite:
        try:
            seen_row_sha1 = {
                r["sha1"] for r in db[table_name].rows_where("source_format = ?", ["embedded-image"])
            }
        except Exception:
            seen_row_sha1 = set()

    stats = {"files_with_images": 0, "images": 0, "ocr_chars": 0, "inserted": 0, "skipped_dupe": 0}

    def worker(path: Path):
        return _extract_rows_for_file(path, soffice=soffice, ocr_fn=ocr_fn,
                                      ocr_engine=ocr_label, min_ocr_dim=min_ocr_dim)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, f): f for f in files}
        done = 0
        for fut in as_completed(futures):
            done += 1
            src = futures[fut]
            try:
                rows = fut.result()
            except Exception as exc:
                logger.warning(f"Embedded-media worker failed for {src.name}: {exc}")
                continue
            if rows:
                stats["files_with_images"] += 1
            for row in rows:
                stats["images"] += 1
                row_sha1 = row["sha1"]
                if row_sha1 in seen_row_sha1 and not overwrite:
                    stats["skipped_dupe"] += 1
                    continue
                seen_row_sha1.add(row_sha1)
                stats["ocr_chars"] += len(row["content"])
                insert_document(
                    db,
                    table_name,
                    row_sha1,
                    row["synthetic_path"],
                    row["content"],
                    row["metadata"],
                    extra_fields=row["extra_fields"],
                    overwrite=overwrite,
                    file_stat_path=row["parent_path"],
                )
                stats["inserted"] += 1
            if done % 50 == 0:
                logger.info(f"media ingest {done}/{len(files)} files, {stats['inserted']} images stored")

    # Refresh FTS so the new OCR text is searchable.
    try:
        setup_fts(db_path, table_name)
        db[f"{table_name}_fts"].populate()  # type: ignore[attr-defined]
    except Exception as exc:
        logger.debug(f"FTS refresh after media ingest: {exc}")
    return stats
