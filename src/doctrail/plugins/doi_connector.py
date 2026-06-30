"""DOI Resolver connector plugin for Doctrail."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table
import sqlite_utils

from doctrail.ingest import (
    clean_metadata as filter_metadata,
    process_document,
    setup_fts,
    SkippedFileException,
)


DEFAULT_CACHE_DB = os.path.expanduser("~/.config/doi_resolver/cache.sqlite")
DEFAULT_TABLE = "documents"
MIN_VALIDATION_SCORE = 3

console = Console()


@dataclass
class CachedArtifact:
    """Represents a cached file selected for ingestion."""

    doi: str
    collection_name: str
    file_path: Path
    file_type: str
    file_size: Optional[int]
    bibtex_key: Optional[str]
    bibtex_entry: Optional[str]
    enriched_bibtex: Optional[str]
    abstract: Optional[str]
    openalex_metadata: Optional[str]
    validation_score: Optional[int]
    best_format: Optional[str]
    created_at: Optional[str]
    added_at: Optional[str]


class Plugin:
    """Ingest collections from the DOI resolver cache."""

    @property
    def name(self) -> str:
        return "doi_connector"

    @property
    def description(self) -> str:
        return "Import PDFs and HTML captured by doi_resolver collections"

    @property
    def target_table(self) -> str:
        return getattr(self, "_table_name", DEFAULT_TABLE)

    async def ingest(
        self,
        db_path: str,
        config: Dict,
        verbose: bool = False,
        overwrite: bool = False,
        limit: Optional[int] = None,
        cache_db: Optional[str] = None,
        collection: Optional[str] = None,
        table: Optional[str] = None,
        fulltext: bool = False,
        base_path: Optional[str] = None,
        dry_run: bool = False,
        **_: Dict,
    ) -> Dict[str, int]:
        """Main ingest entrypoint used by the Doctrail CLI."""

        env_cache_db = os.environ.get("DOI_RESOLVER_CACHE_DB") or os.environ.get("DOCTRAIL_DOI_CACHE_DB")
        cache_path = Path(os.path.expanduser(cache_db or env_cache_db or DEFAULT_CACHE_DB))
        if not cache_path.exists():
            raise FileNotFoundError(
                f"DOI resolver cache not found at {cache_path}. Use --cache-db to point to cache.sqlite."
            )

        collection_name = (collection or "").strip()
        if not collection_name:
            raise ValueError(
                "--collection is required for doi_connector. Pass a specific collection name or 'ALL'."
            )

        self._table_name = table or DEFAULT_TABLE

        if verbose:
            logger.remove()
            logger.add(lambda msg: console.print(msg, end=""), level="DEBUG")

        artifacts = self._load_artifacts(cache_path, collection_name, limit, base_path)
        total = len(artifacts)

        if total == 0:
            console.print(
                Panel.fit(
                    f"No cached files matched collection '{collection_name}'.",
                    title="📭 Nothing to Ingest",
                    border_style="yellow",
                )
            )
            return {"total": 0, "success_count": 0, "error_count": 0, "skipped_count": 0}

        # Display summary table before processing
        self._render_summary(cache_path, db_path, collection_name, artifacts)

        if dry_run:
            self._render_preview(artifacts)
            console.print(
                Panel.fit(
                    "Dry run complete. No files were processed or written.",
                    title="Preview only",
                    border_style="magenta",
                )
            )
            return {
                "total": total,
                "success_count": 0,
                "error_count": 0,
                "skipped_count": 0,
            }

        db = sqlite_utils.Database(db_path)
        self._ensure_table_schema(db)
        existing_sha1s = set()
        if not overwrite and self.target_table in db.table_names():
            try:
                existing_sha1s = {
                    row["sha1"] for row in db.execute(f"SELECT sha1 FROM {self.target_table}")
                }
            except Exception as exc:
                logger.warning(f"Unable to read existing SHA1 hashes: {exc}")

        success_count = 0
        skipped_count = 0
        error_count = 0
        errors: List[str] = []

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
        )

        with progress:
            task_id = progress.add_task("Ingesting DOI cache", total=total)
            for artifact in artifacts:
                result = await self._process_artifact(
                    artifact,
                    db,
                    existing_sha1s,
                    overwrite,
                )
                status = result["status"]
                if status == "success":
                    success_count += 1
                elif status == "skipped":
                    skipped_count += 1
                else:
                    error_count += 1
                    errors.append(result["message"])

                progress.advance(task_id)

        if fulltext and success_count > 0:
            setup_fts(db_path, self.target_table)

        summary_text = (
            f"Total: {total}\n"
            f"Imported: {success_count}\n"
            f"Skipped: {skipped_count}\n"
            f"Errors: {error_count}"
        )

        console.print(
            Panel.fit(
                summary_text,
                title="DOI collection ingest",
                border_style="green" if error_count == 0 else "red",
            )
        )

        if errors:
            for line in errors[:10]:
                console.print(f"[red]•[/red] {line}")
            if len(errors) > 10:
                console.print(f"[dim]… {len(errors) - 10} additional errors omitted[/dim]")

        return {
            "total": total,
            "success_count": success_count,
            "error_count": error_count,
            "skipped_count": skipped_count,
        }

    def _render_preview(self, artifacts: List[CachedArtifact]) -> None:
        """Render a preview table showing the first few artifacts."""

        preview_rows = artifacts[: min(len(artifacts), 15)]
        if not preview_rows:
            return

        table = Table(title="Sample Artifacts", box=None)
        table.add_column("Collection")
        table.add_column("DOI")
        table.add_column("File")
        table.add_column("Type")
        table.add_column("Score", justify="center")
        table.add_column("Size")

        for artifact in preview_rows:
            table.add_row(
                artifact.collection_name,
                artifact.doi,
                artifact.file_path.name,
                artifact.file_type,
                "-" if artifact.validation_score is None else str(artifact.validation_score),
                self._format_size(artifact.file_size),
            )

        if len(artifacts) > len(preview_rows):
            table.caption = f"Showing {len(preview_rows)} of {len(artifacts)} artifacts"

        console.print(table)

    @staticmethod
    def _format_size(size: Optional[int]) -> str:
        if size is None:
            return "-"
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} B"

    def _load_artifacts(
        self,
        cache_path: Path,
        collection_name: str,
        limit: Optional[int],
        base_path: Optional[str],
    ) -> List[CachedArtifact]:
        """Load candidate artifacts from the doi_resolver cache."""

        conn = sqlite3.connect(cache_path)
        conn.row_factory = sqlite3.Row

        try:
            if collection_name.upper() != "ALL":
                if not self._collection_exists(conn, collection_name):
                    available = self._list_collections(conn)
                    choices = ", ".join(sorted(available)) or "<none>"
                    raise ValueError(
                        f"Collection '{collection_name}' not found in {cache_path}. Available: {choices}"
                    )

            params = [collection_name.upper(), collection_name, MIN_VALIDATION_SCORE]
            if collection_name.upper() == "ALL":
                params = ["ALL", "ALL", MIN_VALIDATION_SCORE]

            query = """
                WITH ranked AS (
                    SELECT
                        c.name AS collection_name,
                        cd.added_at,
                        dc.doi,
                        dc.file_path,
                        dc.file_type,
                        dc.file_size,
                        dc.bibtex_key,
                        dc.bibtex_entry,
                        dc.enriched_bibtex,
                        dc.abstract,
                        dc.openalex_metadata,
                        dc.created_at,
                        dv.validation_score,
                        dv.best_format,
                        ROW_NUMBER() OVER (
                            PARTITION BY c.name, dc.doi
                            ORDER BY
                                CASE
                                    WHEN dv.best_format IS NOT NULL AND dc.file_type = dv.best_format THEN 1
                                    WHEN dc.file_type = 'pdf' THEN 2
                                    WHEN dc.file_type = 'mhtml' THEN 3
                                    WHEN dc.file_type = 'html' THEN 4
                                    ELSE 5
                                END,
                                COALESCE(dv.validation_score, -1) DESC,
                                dc.created_at DESC
                        ) AS choice_rank
                    FROM collections c
                    JOIN collections_dois cd ON cd.collection_id = c.id
                    JOIN doi_cache dc ON dc.doi = cd.doi
                    LEFT JOIN doi_validation dv ON (dc.doi || '#' || dc.file_type) = dv.doi
                    WHERE (? = 'ALL' OR c.name = ?)
                      AND (dv.validation_score IS NULL OR dv.validation_score >= ?)
                      AND dc.file_path NOT LIKE '%--FAILED%'
                )
                SELECT *
                FROM ranked
                WHERE choice_rank = 1
                ORDER BY
                    CASE WHEN added_at IS NULL THEN 1 ELSE 0 END,
                    added_at DESC,
                    created_at DESC
            """

            rows = conn.execute(query, params).fetchall()
            if limit is not None:
                rows = rows[:limit]

            artifacts: List[CachedArtifact] = []
            base = Path(base_path).expanduser() if base_path else None

            for row in rows:
                file_path = Path(row["file_path"])
                if base and not file_path.is_absolute():
                    file_path = (base / file_path).resolve()
                artifacts.append(
                    CachedArtifact(
                        doi=row["doi"],
                        collection_name=row["collection_name"],
                        file_path=file_path,
                        file_type=row["file_type"],
                        file_size=row["file_size"],
                        bibtex_key=row["bibtex_key"],
                        bibtex_entry=row["bibtex_entry"],
                        enriched_bibtex=row["enriched_bibtex"],
                        abstract=row["abstract"],
                        openalex_metadata=row["openalex_metadata"],
                        validation_score=row["validation_score"],
                        best_format=row["best_format"],
                        created_at=row["created_at"],
                        added_at=row["added_at"],
                    )
                )

            return artifacts
        finally:
            conn.close()

    async def _process_artifact(
        self,
        artifact: CachedArtifact,
        db: sqlite_utils.Database,
        existing_sha1s: set[str],
        overwrite: bool,
    ) -> Dict[str, str]:
        """Process a cached artifact and insert it into the Doctrail database."""

        if not artifact.file_path.exists():
            return {
                "status": "error",
                "message": f"Missing file for {artifact.doi}: {artifact.file_path}",
            }

        try:
            file_sha1 = await asyncio.to_thread(self._compute_sha1, artifact.file_path)
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Unable to checksum {artifact.file_path}: {exc}",
            }

        if not overwrite and file_sha1 in existing_sha1s:
            return {
                "status": "skipped",
                "message": f"Already ingested {artifact.file_path.name}",
            }

        try:
            sha1, content, raw_metadata = await process_document(
                str(artifact.file_path),
                file_sha1,
            )
        except SkippedFileException as exc:
            return {"status": "skipped", "message": str(exc)}
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Failed to extract {artifact.file_path}: {exc}",
            }

        metadata = filter_metadata(raw_metadata)
        metadata.update(
            {
                "doi": artifact.doi,
                "doi_collection": artifact.collection_name,
                "doi_file_type": artifact.file_type,
                "doi_file_size": artifact.file_size,
                "doi_validation_score": artifact.validation_score,
                "doi_best_format": artifact.best_format,
                "doi_added_at": artifact.added_at,
                "doi_cached_at": artifact.created_at,
            }
        )

        if artifact.abstract and not metadata.get("abstract"):
            metadata["abstract"] = artifact.abstract
        if artifact.openalex_metadata and not metadata.get("openalex_metadata"):
            metadata["openalex_metadata"] = artifact.openalex_metadata

        record = self._build_record(artifact, metadata, content, sha1)

        try:
            db[self.target_table].insert(record, replace=overwrite)
            existing_sha1s.add(sha1)
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Failed to insert {artifact.doi}: {exc}",
            }

        return {"status": "success", "message": ""}

    def _ensure_table_schema(self, db: sqlite_utils.Database) -> None:
        """Ensure the ingestion target table matches the desired schema."""

        desired_columns: Dict[str, type] = {
            "sha1": str,
            "title": str,
            "abstract": str,
            "content": str,
            "openalex_metadata": str,
            "filename": str,
            "filepath": str,
            "file_created": str,
            "file_modified": str,
            "filetype": str,
            "extraction_method": str,
            "doi": str,
            "collection": str,
            "filesize": int,
            "added_on": str,
        }

        rename_map = {
            "metadata_title": "title",
            "metadata_abstract": "abstract",
            "metadata_openalex_metadata": "openalex_metadata",
            "metadata_original_file_type": "filetype",
            "metadata_extraction_method": "extraction_method",
            "metadata_doi": "doi",
            "metadata_doi_collection": "collection",
            "metadata_doi_file_size": "filesize",
            "metadata_doi_added_at": "added_on",
        }

        drop_columns = {
            "raw_content",
            "metadata_original_file_path",
            "metadata_Content-Type",
            "metadata_resourceName",
            "metadata_mhtml_from",
            "metadata_original_url",
            "metadata_source_url",
            "metadata_mhtml_subject",
            "metadata_save_date",
            "metadata_mhtml_date",
            "metadata_mime_version",
            "metadata_content_type",
            "metadata_file_type",
            "metadata_processing_method",
            "metadata_source",
            "metadata_doi_file_type",
            "metadata_doi_validation_score",
            "metadata_doi_best_format",
            "metadata_doi_cached_at",
            "metadata_bibtex_key",
            "metadata_bibtex_entry",
            "metadata_mhtml_=?utf_8?q?ns",
        }

        table = db[self.target_table]
        if not table.exists():
            table.create(desired_columns, pk="sha1")
            return

        existing_columns = {col.name for col in table.columns}
        rename = {
            old: new
            for old, new in rename_map.items()
            if old in existing_columns and new not in existing_columns
        }
        drops = [col for col in drop_columns if col in existing_columns]
        for old, new in rename_map.items():
            if old in existing_columns and new in existing_columns:
                drops.append(old)
        if rename or drops:
            desired_order = list(desired_columns.keys())
            column_order = [col for col in desired_order if col in (existing_columns | set(rename.values()))]
            table.transform(rename=rename, drop=drops, column_order=column_order or None)

        current_columns = {col.name for col in table.columns}
        for column, col_type in desired_columns.items():
            if column not in current_columns:
                table.create_column(column, col_type)

    def _build_record(
        self,
        artifact: CachedArtifact,
        metadata: Dict,
        content: str,
        sha1: str,
    ) -> Dict[str, Optional[str]]:
        """Construct a normalized row for insertion."""

        stats = artifact.file_path.stat()
        file_created = datetime.fromtimestamp(stats.st_ctime).isoformat()
        file_modified = datetime.fromtimestamp(stats.st_mtime).isoformat()

        title = metadata.get("title") or metadata.get("dc:title") or ""
        abstract = metadata.get("abstract") or artifact.abstract or ""
        openalex_metadata = metadata.get("openalex_metadata") or artifact.openalex_metadata or ""
        filetype = metadata.get("original_file_type") or artifact.file_path.suffix.lstrip(".") or artifact.file_type
        extraction_method = metadata.get("extraction_method") or metadata.get("processing_method") or ""
        doi = metadata.get("doi") or artifact.doi
        collection = metadata.get("doi_collection") or artifact.collection_name
        filesize = metadata.get("doi_file_size") or artifact.file_size
        added_on = metadata.get("doi_added_at") or artifact.added_at

        try:
            filesize_value = int(filesize) if filesize is not None else None
        except (TypeError, ValueError):
            filesize_value = None

        if isinstance(openalex_metadata, (dict, list)):
            openalex_metadata = json.dumps(openalex_metadata)
        elif openalex_metadata is None:
            openalex_metadata = ""

        def _to_str(value: Optional[str]) -> str:
            if value is None:
                return ""
            return value if isinstance(value, str) else str(value)

        title = _to_str(title)
        abstract = _to_str(abstract)
        filetype = _to_str(filetype)
        extraction_method = _to_str(extraction_method)
        doi = _to_str(doi)
        collection = _to_str(collection)
        added_on = _to_str(added_on)

        return {
            "sha1": sha1,
            "title": title,
            "abstract": abstract,
            "content": content,
            "openalex_metadata": openalex_metadata,
            "filename": artifact.file_path.name,
            "filepath": str(artifact.file_path),
            "file_created": file_created,
            "file_modified": file_modified,
            "filetype": filetype,
            "extraction_method": extraction_method,
            "doi": doi,
            "collection": collection,
            "filesize": filesize_value,
            "added_on": added_on,
        }


    @staticmethod
    def _compute_sha1(path: Path) -> str:
        """Compute a SHA1 digest for the provided file."""

        digest = hashlib.sha1()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _collection_exists(conn: sqlite3.Connection, name: str) -> bool:
        cursor = conn.execute("SELECT 1 FROM collections WHERE name = ? LIMIT 1", (name,))
        return cursor.fetchone() is not None

    @staticmethod
    def _list_collections(conn: sqlite3.Connection) -> List[str]:
        cursor = conn.execute("SELECT name FROM collections ORDER BY name")
        return [row[0] for row in cursor.fetchall()]

    def _render_summary(
        self,
        cache_path: Path,
        db_path: str,
        collection_name: str,
        artifacts: List[CachedArtifact],
    ) -> None:
        """Render a short summary describing the ingest operation."""

        unique_dois = {artifact.doi for artifact in artifacts}
        pdf_count = sum(1 for artifact in artifacts if artifact.file_type == "pdf")
        mhtml_count = sum(1 for artifact in artifacts if artifact.file_type == "mhtml")
        html_count = sum(1 for artifact in artifacts if artifact.file_type == "html")

        table = Table(show_header=False, box=None)
        table.add_row("Cache", str(cache_path))
        table.add_row("Database", db_path)
        table.add_row("Collection", collection_name)
        table.add_row("Documents", str(len(unique_dois)))
        table.add_row("Artifacts", str(len(artifacts)))
        table.add_row(
            "Formats",
            f"pdf: {pdf_count}, mhtml: {mhtml_count}, html: {html_count}",
        )

        console.print(
            Panel.fit(
                table,
                title="📚 DOI Resolver Import",
                border_style="cyan",
            )
        )
