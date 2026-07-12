//! PyO3 bindings: `doctrail._ingest_native`.
//!
//! Exposes the stripped extraction core to Python. Rust owns all multicore work
//! (rayon, GIL released via `Python::allow_threads`); Python only pushes paths in
//! and writes rows out. Each document is returned as a JSON string so the FFI
//! surface stays version-robust; `doctrail.ingest.native_extractor` parses them.

#![allow(clippy::useless_conversion)] // PyO3 wrapper expansion around PyResult.

use crate::{
    classify_extraction_failure, extract_bytes, extract_file, low_value_content_rejection,
    ExtractOptions, ExtractedDocument, HtmlKind,
};
use anyhow::{bail, Context, Result};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use rayon::prelude::*;
use serde::Serialize;
use serde_json::Value;
use sha1::{Digest, Sha1};
use std::ffi::OsString;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};
use tempfile::{tempdir, NamedTempFile};
use wait_timeout::ChildExt;
use zip::ZipArchive;

const ZIP_RATIO_CHECK_MIN_BYTES: u64 = 1024 * 1024;
const MAX_EXTERNAL_OUTPUT_BYTES: u64 = 64 * 1024 * 1024;
const LARGE_HTML_EXTERNAL_BYTES: u64 = 8 * 1024 * 1024;

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
    extraction_metadata: Option<Value>,
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

#[derive(Debug, Serialize)]
struct HashResult {
    path: String,
    sha1: Option<String>,
    error: Option<String>,
}

fn hash_one(path: &str) -> HashResult {
    let result = (|| -> Result<String> {
        let mut file = File::open(path).with_context(|| format!("opening {path} for hashing"))?;
        let mut hasher = Sha1::new();
        let mut buffer = vec![0_u8; 1024 * 1024];
        loop {
            let read = file
                .read(&mut buffer)
                .with_context(|| format!("reading {path} for hashing"))?;
            if read == 0 {
                break;
            }
            hasher.update(&buffer[..read]);
        }
        Ok(format!("{:x}", hasher.finalize()))
    })();
    match result {
        Ok(sha1) => HashResult {
            path: path.to_string(),
            sha1: Some(sha1),
            error: None,
        },
        Err(error) => HashResult {
            path: path.to_string(),
            sha1: None,
            error: Some(format!("{error:#}")),
        },
    }
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

fn doc_out_from_extracted(path: &str, doc: ExtractedDocument, started: Instant) -> DocOut {
    let meta = &doc.extraction_metadata;
    let ocr_needed = meta_bool(meta, "content_extraction", "ocr_needed")
        || meta_bool(meta, "content_extraction", "requires_full_pdf_ocr");
    let language = doc
        .language
        .clone()
        .or_else(|| meta_str(meta, "language_detection", "final_language"))
        .or_else(|| meta_str(meta, "language_detection", "detected_language"));
    let quality_rejection = matches!(doc.source_format.as_str(), "html" | "mhtml")
        .then(|| low_value_content_rejection(&doc.content, &doc.title))
        .flatten();
    let content = if quality_rejection.is_some() {
        String::new()
    } else {
        doc.content
    };
    let content_chars = content.chars().count();
    let extraction_metadata = doc.extraction_metadata.clone();
    DocOut {
        path: path.to_string(),
        status: "extracted".to_string(),
        source_format: Some(doc.source_format),
        title: Some(doc.title),
        content,
        content_chars,
        language,
        language_confidence: meta_f64(meta, "language_detection", "confidence"),
        mime_type: meta_str(meta, "content_extraction", "detected_mime_type"),
        extraction_method: meta_str(meta, "content_extraction", "extraction_method"),
        extraction_metadata: Some(extraction_metadata),
        ocr_needed,
        ocr_reason: meta_str(meta, "content_extraction", "ocr_reason"),
        fallback_kind: None,
        error: quality_rejection,
        extraction_ms: started.elapsed().as_millis() as u64,
    }
}

fn extract_one(path: &str) -> DocOut {
    let started = Instant::now();
    let extension = Path::new(path)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if matches!(extension.as_str(), "html" | "htm")
        && fs::metadata(path)
            .is_ok_and(|metadata| metadata.len() >= LARGE_HTML_EXTERNAL_BYTES)
    {
        if let Ok(document) = external_html(Path::new(path)) {
            return doc_out_from_extracted(path, document, started);
        }
    }
    if matches!(
        extension.as_str(),
        "png" | "jpg" | "jpeg" | "gif" | "bmp" | "tif" | "tiff"
    ) {
        return DocOut {
            path: path.to_string(),
            status: "fallback_required".to_string(),
            source_format: Some(extension),
            title: None,
            content: String::new(),
            content_chars: 0,
            language: None,
            language_confidence: None,
            mime_type: None,
            extraction_method: None,
            extraction_metadata: None,
            ocr_needed: true,
            ocr_reason: Some("image_requires_ocr".to_string()),
            fallback_kind: Some("configured_ocr_backend".to_string()),
            error: None,
            extraction_ms: started.elapsed().as_millis() as u64,
        };
    }
    let opts = ExtractOptions {
        mime_type: None,
        source_path: Some(path),
        kind: HtmlKind::Auto,
    };
    match extract_file(Path::new(path), opts) {
        Ok(doc) => doc_out_from_extracted(path, doc, started),
        Err(e) => {
            if let Ok(doc) = external_extract(Path::new(path)) {
                return doc_out_from_extracted(path, doc, started);
            }
            let failure = classify_extraction_failure(&e);
            let ocr_needed = failure.fallback_kind.as_deref() == Some("configured_ocr_backend");
            let ocr_reason = ocr_needed.then(|| "image_requires_ocr".to_string());
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
                extraction_metadata: None,
                ocr_needed,
                ocr_reason,
                fallback_kind: failure.fallback_kind,
                error: Some(failure.message),
                extraction_ms: started.elapsed().as_millis() as u64,
            }
        }
    }
}

fn external_extract(path: &Path) -> Result<ExtractedDocument> {
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    match extension.as_str() {
        "html" | "htm" => external_html(path),
        "mobi" | "azw" | "azw3" => external_ebook_convert(path, &extension),
        "djvu" | "djv" => external_djvu(path),
        "doc" => external_doc(path),
        "rtf" => external_rtf(path),
        "ppt" => external_ppt(path),
        "xlsx" => external_xlsx(path),
        _ => bail!("no bounded native external lane for extension {extension:?}"),
    }
}

fn external_text_document(
    path: &Path,
    content: String,
    source_format: &str,
    extraction_method: &str,
) -> Result<ExtractedDocument> {
    if content.trim().is_empty() {
        bail!(
            "{extraction_method} produced no text for {}",
            path.display()
        );
    }
    let mut document = extract_bytes(
        content.as_bytes(),
        ExtractOptions {
            mime_type: Some("text/plain"),
            source_path: Some("external.txt"),
            kind: HtmlKind::Auto,
        },
    )?;
    document.source_format = source_format.to_string();
    document.title = path
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_string();
    document.extraction_metadata["content_extraction"]["source_format"] =
        Value::String(source_format.to_string());
    document.extraction_metadata["content_extraction"]["extraction_method"] =
        Value::String(extraction_method.to_string());
    document.extraction_metadata["content_extraction"]["external_command_bounded"] =
        Value::Bool(true);
    document.extraction_metadata["content_extraction"]["original_bucket_path"] =
        Value::String(path.display().to_string());
    Ok(document)
}

fn external_ebook_convert(path: &Path, source_format: &str) -> Result<ExtractedDocument> {
    let temp = tempdir().context("creating ebook conversion directory")?;
    let output = temp.path().join("output.txt");
    run_program(
        "ebook-convert",
        vec![
            path.as_os_str().to_owned(),
            output.as_os_str().to_owned(),
            OsString::from("--txt-output-encoding=utf-8"),
        ],
        Duration::from_secs(180),
    )?;
    external_text_document(
        path,
        read_file_limited(&output)?,
        source_format,
        "ebook-convert",
    )
}

fn external_djvu(path: &Path) -> Result<ExtractedDocument> {
    match run_program(
        "djvutxt",
        vec![path.as_os_str().to_owned()],
        Duration::from_secs(180),
    ) {
        Ok(output) if !output.trim().is_empty() => {
            external_text_document(path, output, "djvu", "djvutxt")
        }
        _ => external_ebook_convert(path, "djvu"),
    }
}

fn external_rtf(path: &Path) -> Result<ExtractedDocument> {
    let textutil = run_program(
        "textutil",
        vec![
            OsString::from("-convert"),
            OsString::from("txt"),
            OsString::from("-stdout"),
            path.as_os_str().to_owned(),
        ],
        Duration::from_secs(120),
    );
    let (content, method) = match textutil {
        Ok(content) if !content.trim().is_empty() => (content, "textutil"),
        _ => (
            run_program(
                "unrtf",
                vec![OsString::from("--text"), path.as_os_str().to_owned()],
                Duration::from_secs(120),
            )?,
            "unrtf",
        ),
    };
    external_text_document(path, content, "rtf", method)
}

fn external_doc(path: &Path) -> Result<ExtractedDocument> {
    let antiword = run_program(
        "antiword",
        vec![path.as_os_str().to_owned()],
        Duration::from_secs(120),
    );
    if let Ok(content) = antiword {
        if external_text_is_usable(&content) {
            return external_text_document(path, content, "doc", "antiword");
        }
    }

    let textutil = run_program(
        "textutil",
        vec![
            OsString::from("-convert"),
            OsString::from("txt"),
            OsString::from("-stdout"),
            path.as_os_str().to_owned(),
        ],
        Duration::from_secs(120),
    );
    if let Ok(content) = textutil {
        if external_text_is_usable(&content) {
            return external_text_document(path, content, "doc", "textutil");
        }
    }

    let mut header = [0_u8; 8];
    File::open(path)
        .with_context(|| format!("opening legacy DOC {}", path.display()))?
        .read_exact(&mut header)
        .with_context(|| format!("reading legacy DOC header {}", path.display()))?;
    if header != [0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1] {
        bail!("legacy DOC fallbacks produced no usable text; file is not a CFB document");
    }

    let temp = tempdir().context("creating LibreOffice DOC conversion directory")?;
    let profile = temp.path().join("profile");
    fs::create_dir_all(&profile).context("creating LibreOffice DOC profile")?;
    run_program(
        "soffice",
        vec![
            OsString::from("--headless"),
            OsString::from("--nologo"),
            OsString::from("--nodefault"),
            OsString::from("--nolockcheck"),
            OsString::from(format!(
                "-env:UserInstallation=file://{}",
                profile.display()
            )),
            OsString::from("--convert-to"),
            OsString::from("txt:Text"),
            OsString::from("--outdir"),
            temp.path().as_os_str().to_owned(),
            path.as_os_str().to_owned(),
        ],
        Duration::from_secs(180),
    )?;
    let output = temp
        .path()
        .join(
            path.file_stem()
                .ok_or_else(|| anyhow::anyhow!("legacy DOC path has no stem"))?,
        )
        .with_extension("txt");
    let content = read_file_limited(&output)?;
    if !external_text_is_usable(&content) {
        bail!("LibreOffice produced no usable text from legacy DOC");
    }
    external_text_document(path, content, "doc", "libreoffice_text")
}

fn external_html(path: &Path) -> Result<ExtractedDocument> {
    let content = run_program(
        "w3m",
        vec![
            OsString::from("-dump"),
            OsString::from("-cols"),
            OsString::from("120"),
            path.as_os_str().to_owned(),
        ],
        Duration::from_secs(120),
    )?;
    external_text_document(path, content, "html", "w3m_bounded_fallback")
}

fn external_text_is_usable(content: &str) -> bool {
    if content.trim().is_empty() || !content.chars().any(char::is_alphanumeric) {
        return false;
    }
    let total = content.chars().count().max(1);
    let replacement = content.chars().filter(|character| *character == '\u{fffd}').count();
    let controls = content
        .chars()
        .filter(|character| {
            character.is_control() && !matches!(*character, '\n' | '\r' | '\t')
        })
        .count();
    replacement.saturating_mul(100) < total && controls.saturating_mul(100) < total
}

fn external_ppt(path: &Path) -> Result<ExtractedDocument> {
    let content = run_program(
        "strings",
        vec![
            OsString::from("-a"),
            OsString::from("-n"),
            OsString::from("4"),
            path.as_os_str().to_owned(),
        ],
        Duration::from_secs(120),
    )?;
    external_text_document(path, content, "ppt", "strings")
}

fn external_xlsx(path: &Path) -> Result<ExtractedDocument> {
    let temp = tempdir().context("creating LibreOffice spreadsheet repair directory")?;
    let profile = temp.path().join("profile");
    fs::create_dir_all(&profile).context("creating LibreOffice repair profile")?;
    run_program(
        "soffice",
        vec![
            OsString::from("--headless"),
            OsString::from("--nologo"),
            OsString::from("--nodefault"),
            OsString::from("--nolockcheck"),
            OsString::from(format!(
                "-env:UserInstallation=file://{}",
                profile.display()
            )),
            OsString::from("--convert-to"),
            OsString::from("xlsx"),
            OsString::from("--outdir"),
            temp.path().as_os_str().to_owned(),
            path.as_os_str().to_owned(),
        ],
        Duration::from_secs(180),
    )?;
    let output = temp.path().join(
        path.file_name()
            .ok_or_else(|| anyhow::anyhow!("spreadsheet path has no filename"))?,
    );
    let mut document = extract_file(
        &output,
        ExtractOptions {
            mime_type: None,
            source_path: path.to_str(),
            kind: HtmlKind::Auto,
        },
    )?;
    document.extraction_metadata["content_extraction"]["extraction_method"] =
        Value::String("libreoffice_repair_calamine".to_string());
    document.extraction_metadata["content_extraction"]["external_command_bounded"] =
        Value::Bool(true);
    Ok(document)
}

fn run_program(program: &str, args: Vec<OsString>, timeout: Duration) -> Result<String> {
    let stdout = NamedTempFile::new().context("creating external stdout file")?;
    let stderr = NamedTempFile::new().context("creating external stderr file")?;
    let mut child = Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout.reopen()?))
        .stderr(Stdio::from(stderr.reopen()?))
        .spawn()
        .with_context(|| format!("starting external extractor {program}"))?;
    let status = match child.wait_timeout(timeout)? {
        Some(status) => status,
        None => {
            let _ = child.kill();
            let _ = child.wait();
            bail!("external extractor {program} timed out after {timeout:?}");
        }
    };
    let stderr_text = read_file_limited(stderr.path()).unwrap_or_default();
    if !status.success() {
        bail!(
            "external extractor {program} exited with {status}: {}",
            stderr_text.trim()
        );
    }
    read_file_limited(stdout.path())
}

fn read_file_limited(path: &Path) -> Result<String> {
    let size = fs::metadata(path)
        .with_context(|| format!("reading external output metadata {}", path.display()))?
        .len();
    if size > MAX_EXTERNAL_OUTPUT_BYTES {
        bail!(
            "external output {} is {} bytes; limit is {}",
            path.display(),
            size,
            MAX_EXTERNAL_OUTPUT_BYTES
        );
    }
    let bytes =
        fs::read(path).with_context(|| format!("reading external output {}", path.display()))?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
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
        extraction_metadata: None,
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
        .map_err(|error| PyRuntimeError::new_err(format!("{error:#}")))
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

/// Hash paths in parallel with streaming SHA-1 reads. Results are ordered and
/// per-file failures never abort the batch.
#[pyfunction]
#[pyo3(signature = (paths, threads=None))]
fn hash_batch(py: Python<'_>, paths: Vec<String>, threads: Option<usize>) -> Vec<String> {
    py.allow_threads(|| {
        let run = || {
            paths
                .par_iter()
                .map(|path| serde_json::to_string(&hash_one(path)).expect("hash result serializes"))
                .collect::<Vec<String>>()
        };
        match threads {
            Some(count) if count > 0 => rayon::ThreadPoolBuilder::new()
                .num_threads(count)
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
    m.add_function(wrap_pyfunction!(hash_batch, m)?)?;
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

    #[test]
    fn external_text_gate_keeps_short_real_text() {
        assert!(external_text_is_usable("PRISMA flow diagram"));
    }

    #[test]
    fn external_text_gate_rejects_binary_control_dump() {
        let garbage = "word\0\u{0001}\u{0002}\u{0003}".repeat(50);
        assert!(!external_text_is_usable(&garbage));
    }

    #[test]
    fn streaming_sha1_matches_known_vector() {
        let root = tempdir().unwrap();
        let path = root.path().join("known.txt");
        fs::write(&path, b"abc").unwrap();

        let result = hash_one(path.to_str().unwrap());

        assert_eq!(
            result.sha1.as_deref(),
            Some("a9993e364706816aba3e25717850c26c9cd0d89d")
        );
        assert!(result.error.is_none());
    }
}
