//! PyO3 bindings: `doctrail._ingest_native`.
//!
//! Exposes the stripped extraction core to Python. Rust owns all multicore work
//! (rayon, GIL released via `Python::allow_threads`); Python only pushes paths in
//! and writes rows out. Each document is returned as a JSON string so the FFI
//! surface stays version-robust; `doctrail.ingest.native_extractor` parses them.

#![allow(clippy::useless_conversion)] // PyO3 wrapper expansion around PyResult.

use crate::{classify_extraction_failure, extract_file, ExtractOptions, HtmlKind};
use anyhow::{bail, Context};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use rayon::prelude::*;
use serde::Serialize;
use serde_json::Value;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;
use std::time::Instant;
use zip::ZipArchive;

const ZIP_RATIO_CHECK_MIN_BYTES: u64 = 1024 * 1024;

/// One extraction result. Field names are the contract with
/// `doctrail.ingest.native_extractor` (see its module docstring).
#[derive(Serialize)]
struct DocOut {
    path: String,
    status: String,
    source_format: Option<String>,
    title: Option<String>,
    content: String,
    content_chars: usize,
    language: Option<String>,
    language_confidence: Option<f64>,
    mime_type: Option<String>,
    extraction_method: Option<String>,
    ocr_needed: bool,
    ocr_reason: Option<String>,
    fallback_kind: Option<String>,
    error: Option<String>,
    extraction_ms: u64,
}

#[derive(Debug, Serialize)]
struct ArchiveMember {
    path: String,
    member_path: String,
    uncompressed_bytes: u64,
    compressed_bytes: u64,
}

fn meta_str(meta: &Value, section: &str, key: &str) -> Option<String> {
    meta.get(section)?.get(key)?.as_str().map(str::to_string)
}

fn meta_bool(meta: &Value, section: &str, key: &str) -> bool {
    meta.get(section)
        .and_then(|s| s.get(key))
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

fn meta_f64(meta: &Value, section: &str, key: &str) -> Option<f64> {
    meta.get(section)?.get(key)?.as_f64()
}

fn extract_one(path: &str) -> DocOut {
    let started = Instant::now();
    let opts = ExtractOptions {
        mime_type: None,
        source_path: Some(path),
        kind: HtmlKind::Auto,
    };
    match extract_file(Path::new(path), opts) {
        Ok(doc) => {
            let meta = &doc.extraction_metadata;
            let ocr_needed = meta_bool(meta, "content_extraction", "ocr_needed")
                || meta_bool(meta, "content_extraction", "requires_full_pdf_ocr");
            let language = doc
                .language
                .clone()
                .or_else(|| meta_str(meta, "language_detection", "final_language"))
                .or_else(|| meta_str(meta, "language_detection", "detected_language"));
            let content_chars = doc.content.chars().count();
            DocOut {
                path: path.to_string(),
                status: "extracted".to_string(),
                source_format: Some(doc.source_format),
                title: Some(doc.title),
                content: doc.content,
                content_chars,
                language,
                language_confidence: meta_f64(meta, "language_detection", "confidence"),
                mime_type: meta_str(meta, "content_extraction", "detected_mime_type"),
                extraction_method: meta_str(meta, "content_extraction", "extraction_method"),
                ocr_needed,
                ocr_reason: meta_str(meta, "content_extraction", "ocr_reason"),
                fallback_kind: None,
                error: None,
                extraction_ms: started.elapsed().as_millis() as u64,
            }
        }
        Err(e) => {
            let failure = classify_extraction_failure(&e);
            let ocr_needed = failure
                .fallback_kind
                .as_deref()
                .map(|k| k.contains("ocr"))
                .unwrap_or(false);
            DocOut {
                path: path.to_string(),
                status: failure.extraction_status().to_string(),
                source_format: None,
                title: None,
                content: String::new(),
                content_chars: 0,
                language: None,
                language_confidence: None,
                mime_type: None,
                extraction_method: None,
                ocr_needed,
                ocr_reason: None,
                fallback_kind: failure.fallback_kind,
                error: Some(failure.message),
                extraction_ms: started.elapsed().as_millis() as u64,
            }
        }
    }
}

/// Build a `status=failed` result for a path (used when extraction panics).
fn failed_doc(path: &str, error: String) -> DocOut {
    DocOut {
        path: path.to_string(),
        status: "failed".to_string(),
        source_format: None,
        title: None,
        content: String::new(),
        content_chars: 0,
        language: None,
        language_confidence: None,
        mime_type: None,
        extraction_method: None,
        ocr_needed: false,
        ocr_reason: None,
        fallback_kind: None,
        error: Some(error),
        extraction_ms: 0,
    }
}

fn panic_to_string(panic: Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = panic.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = panic.downcast_ref::<String>() {
        s.clone()
    } else {
        "unknown panic".to_string()
    }
}

fn extract_one_json(path: &str) -> String {
    // Contain per-file Rust panics: a panic in one document must become a
    // `status=failed` result for that file, never propagate out of the rayon
    // closure (which would surface as a PyO3 PanicException and kill the whole
    // batch/CLI). A C-level abort/segfault in a native lib still cannot be
    // caught in-process; that is a known limitation.
    let doc = match catch_unwind(AssertUnwindSafe(|| extract_one(path))) {
        Ok(doc) => doc,
        Err(panic) => failed_doc(path, format!("panic: {}", panic_to_string(panic))),
    };
    serde_json::to_string(&doc).unwrap_or_else(|_| {
        format!(
            "{{\"path\":{path:?},\"status\":\"failed\",\"content\":\"\",\"content_chars\":0,\"ocr_needed\":false,\"error\":\"serialize_failed\"}}"
        )
    })
}

fn expand_zip_archive(
    archive_path: &Path,
    destination: &Path,
    max_entries: usize,
    max_member_bytes: u64,
    max_total_bytes: u64,
    max_compression_ratio: u64,
) -> anyhow::Result<Vec<ArchiveMember>> {
    if max_entries == 0
        || max_member_bytes == 0
        || max_total_bytes == 0
        || max_compression_ratio == 0
    {
        bail!("ZIP safety limits must all be positive");
    }

    let source = File::open(archive_path)
        .with_context(|| format!("opening ZIP archive {}", archive_path.display()))?;
    let mut archive = ZipArchive::new(source)
        .with_context(|| format!("parsing ZIP archive {}", archive_path.display()))?;
    if archive.len() > max_entries {
        bail!(
            "ZIP archive has {} entries, exceeding limit {}",
            archive.len(),
            max_entries
        );
    }

    fs::create_dir_all(destination)
        .with_context(|| format!("creating ZIP staging directory {}", destination.display()))?;
    let mut total_bytes = 0u64;
    let mut members = Vec::new();

    for index in 0..archive.len() {
        let mut entry = archive
            .by_index(index)
            .with_context(|| format!("reading ZIP entry {index}"))?;
        if entry.is_dir() {
            continue;
        }
        if entry.encrypted() {
            bail!("ZIP entry {:?} is encrypted", entry.name());
        }
        if entry.is_symlink() {
            bail!("ZIP entry {:?} is a symbolic link", entry.name());
        }

        let enclosed = entry
            .enclosed_name()
            .ok_or_else(|| anyhow::anyhow!("ZIP entry {:?} has an unsafe path", entry.name()))?;
        let member_path = enclosed
            .to_str()
            .ok_or_else(|| anyhow::anyhow!("ZIP entry path is not valid UTF-8"))?
            .to_string();
        let uncompressed_bytes = entry.size();
        let compressed_bytes = entry.compressed_size();
        if uncompressed_bytes > max_member_bytes {
            bail!(
                "ZIP entry {:?} declares {} bytes, exceeding per-entry limit {}",
                member_path,
                uncompressed_bytes,
                max_member_bytes
            );
        }
        total_bytes = total_bytes
            .checked_add(uncompressed_bytes)
            .ok_or_else(|| anyhow::anyhow!("ZIP expanded-size total overflowed"))?;
        if total_bytes > max_total_bytes {
            bail!(
                "ZIP archive declares {} expanded bytes, exceeding total limit {}",
                total_bytes,
                max_total_bytes
            );
        }
        if uncompressed_bytes >= ZIP_RATIO_CHECK_MIN_BYTES
            && (compressed_bytes == 0
                || uncompressed_bytes > compressed_bytes.saturating_mul(max_compression_ratio))
        {
            bail!(
                "ZIP entry {:?} exceeds compression-ratio limit {} ({} -> {} bytes)",
                member_path,
                max_compression_ratio,
                compressed_bytes,
                uncompressed_bytes
            );
        }

        let output_path = destination.join(format!("{index:06}")).join(&enclosed);
        if let Some(parent) = output_path.parent() {
            fs::create_dir_all(parent)
                .with_context(|| format!("creating ZIP member directory {}", parent.display()))?;
        }
        let partial_path = output_path.with_extension(format!(
            "{}partial",
            output_path
                .extension()
                .and_then(|value| value.to_str())
                .map(|value| format!("{value}."))
                .unwrap_or_default()
        ));
        let mut output = File::create(&partial_path)
            .with_context(|| format!("creating ZIP member {}", partial_path.display()))?;
        let copied = std::io::copy(
            &mut entry.by_ref().take(max_member_bytes.saturating_add(1)),
            &mut output,
        )
        .with_context(|| format!("expanding ZIP entry {member_path:?}"))?;
        output
            .flush()
            .with_context(|| format!("flushing ZIP member {}", partial_path.display()))?;
        if copied != uncompressed_bytes || copied > max_member_bytes {
            let _ = fs::remove_file(&partial_path);
            bail!(
                "ZIP entry {:?} expanded to {} bytes but declared {}",
                member_path,
                copied,
                uncompressed_bytes
            );
        }
        fs::rename(&partial_path, &output_path).with_context(|| {
            format!(
                "committing ZIP member {} -> {}",
                partial_path.display(),
                output_path.display()
            )
        })?;
        members.push(ArchiveMember {
            path: output_path.display().to_string(),
            member_path,
            uncompressed_bytes,
            compressed_bytes,
        });
    }

    Ok(members)
}

/// Expand a ZIP archive into a caller-owned staging directory after enforcing
/// path, entry-count, size, encryption, symlink, and compression-ratio limits.
#[pyfunction]
#[pyo3(signature = (
    archive_path,
    destination,
    max_entries=10_000,
    max_member_bytes=536_870_912,
    max_total_bytes=2_147_483_648,
    max_compression_ratio=200
))]
fn expand_zip(
    py: Python<'_>,
    archive_path: String,
    destination: String,
    max_entries: usize,
    max_member_bytes: u64,
    max_total_bytes: u64,
    max_compression_ratio: u64,
) -> PyResult<Vec<String>> {
    let result = py.allow_threads(|| {
        expand_zip_archive(
            Path::new(&archive_path),
            Path::new(&destination),
            max_entries,
            max_member_bytes,
            max_total_bytes,
            max_compression_ratio,
        )
    });
    result
        .map(|members| {
            members
                .into_iter()
                .map(|member| serde_json::to_string(&member).expect("archive member serializes"))
                .collect()
        })
        .map_err(|error| PyRuntimeError::new_err(error.to_string()))
}

/// Extract a batch of paths in parallel (rayon, GIL released). Returns one JSON
/// string per input path, order-preserving. `threads` sizes the rayon pool
/// (default: rayon's global pool = num_cpus).
#[pyfunction]
#[pyo3(signature = (paths, threads=None))]
fn extract_batch(py: Python<'_>, paths: Vec<String>, threads: Option<usize>) -> Vec<String> {
    py.allow_threads(|| {
        let run = || {
            paths
                .par_iter()
                .map(|p| extract_one_json(p))
                .collect::<Vec<String>>()
        };
        match threads {
            Some(n) if n > 0 => rayon::ThreadPoolBuilder::new()
                .num_threads(n)
                .build()
                .map(|pool| pool.install(run))
                .unwrap_or_else(|_| run()),
            _ => run(),
        }
    })
}

/// Extract a single path (GIL released). Returns one JSON string.
#[pyfunction]
fn extract_path(py: Python<'_>, path: String) -> String {
    py.allow_threads(|| extract_one_json(&path))
}

#[pymodule]
fn _ingest_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(extract_batch, m)?)?;
    m.add_function(wrap_pyfunction!(extract_path, m)?)?;
    m.add_function(wrap_pyfunction!(expand_zip, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use tempfile::tempdir;
    use zip::write::SimpleFileOptions;
    use zip::{CompressionMethod, ZipWriter};

    fn write_zip(path: &Path, members: &[(&str, &[u8])]) {
        let mut bytes = Cursor::new(Vec::new());
        {
            let mut writer = ZipWriter::new(&mut bytes);
            let options =
                SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
            for (name, content) in members {
                writer.start_file(*name, options).unwrap();
                writer.write_all(content).unwrap();
            }
            writer.finish().unwrap();
        }
        fs::write(path, bytes.into_inner()).unwrap();
    }

    #[test]
    fn expands_safe_zip_members_with_paths_and_sizes() {
        let root = tempdir().unwrap();
        let archive = root.path().join("sample.zip");
        let destination = root.path().join("out");
        write_zip(
            &archive,
            &[("docs/one.txt", b"first"), ("two.html", b"<p>second</p>")],
        );

        let members = expand_zip_archive(&archive, &destination, 10, 1024, 2048, 200).unwrap();

        assert_eq!(members.len(), 2);
        assert_eq!(members[0].member_path, "docs/one.txt");
        assert_eq!(fs::read(&members[0].path).unwrap(), b"first");
        assert_eq!(members[1].uncompressed_bytes, 13);
    }

    #[test]
    fn rejects_zip_traversal_without_writing_outside_staging() {
        let root = tempdir().unwrap();
        let archive = root.path().join("traversal.zip");
        let destination = root.path().join("out");
        write_zip(&archive, &[("../escape.txt", b"no")]);

        let error = expand_zip_archive(&archive, &destination, 10, 1024, 2048, 200)
            .unwrap_err()
            .to_string();

        assert!(error.contains("unsafe path"));
        assert!(!root.path().join("escape.txt").exists());
    }

    #[test]
    fn rejects_zip_entry_and_total_size_limits() {
        let root = tempdir().unwrap();
        let archive = root.path().join("large.zip");
        write_zip(&archive, &[("large.txt", &[b'x'; 128])]);

        let member_error =
            expand_zip_archive(&archive, &root.path().join("member"), 10, 64, 1024, 200)
                .unwrap_err()
                .to_string();
        assert!(member_error.contains("per-entry limit"));

        let total_error =
            expand_zip_archive(&archive, &root.path().join("total"), 10, 1024, 64, 200)
                .unwrap_err()
                .to_string();
        assert!(total_error.contains("total limit"));
    }
}
