//! PyO3 bindings: `doctrail._ingest_native`.
//!
//! Exposes the stripped extraction core to Python. Rust owns all multicore work
//! (rayon, GIL released via `Python::allow_threads`); Python only pushes paths in
//! and writes rows out. Each document is returned as a JSON string so the FFI
//! surface stays version-robust; `doctrail.ingest.native_extractor` parses them.

#![allow(clippy::useless_conversion)] // PyO3 wrapper expansion around PyResult.

use crate::{
    classify_extraction_failure, detect_content_type, extract_bytes, extract_bytes_with, extract_file,
    extract_file_with,
    ContentTypeDetection,
    low_value_content_rejection, ExtractOptions, ExtractedDocument, HtmlConfig, HtmlKind, HtmlMode,
};
use anyhow::{bail, Context, Result};
use chardetng::EncodingDetector;
use encoding_rs::{Encoding, BIG5, EUC_JP, EUC_KR, GBK, SHIFT_JIS};
use flate2::read::MultiGzDecoder;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
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
/// Largest decompressed size read from a gzip-compressed page.
const MAX_GZIP_PAGE_BYTES: u64 = 64 * 1024 * 1024;

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

fn doc_out_from_extracted(
    path: &str,
    mut doc: ExtractedDocument,
    started: Instant,
    html: &HtmlConfig,
) -> DocOut {
    let low_value_reason = matches!(doc.source_format.as_str(), "html" | "mhtml")
        .then(|| low_value_content_rejection(&doc.content, &doc.title))
        .flatten();
    if !html.rejects_low_value() {
        if let Some(reason) = &low_value_reason {
            doc.extraction_metadata["content_extraction"]["low_value_reason"] =
                Value::String(reason.clone());
        }
    }
    let quality_rejection = low_value_reason.filter(|_| html.rejects_low_value());
    let meta = &doc.extraction_metadata;
    let ocr_needed = meta_bool(meta, "content_extraction", "ocr_needed")
        || meta_bool(meta, "content_extraction", "requires_full_pdf_ocr");
    let language = doc
        .language
        .clone()
        .or_else(|| meta_str(meta, "language_detection", "final_language"))
        .or_else(|| meta_str(meta, "language_detection", "detected_language"));
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

fn extract_one(path: &str, html: &HtmlConfig) -> DocOut {
    let started = Instant::now();
    let extension = Path::new(path)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    // Crawls and recovered folders save images, videos, compressed pages, and
    // Word files under web-page or .txt names. Reading those as text stores
    // binary noise, so for these names trust the bytes instead.
    let sniffed = matches!(
        extension.as_str(),
        "html" | "htm" | "shtml" | "jhtml" | "mht" | "mhtml" | "txt"
    )
    .then(|| sniff_content(Path::new(path)))
    .flatten();
    let sniffed_mime = sniffed
        .as_ref()
        .map(|detection| detection.mime_type.to_ascii_lowercase())
        .unwrap_or_default();
    if matches!(sniffed_mime.as_str(), "application/gzip" | "application/x-gzip") {
        return extract_gzipped_page(path, html, started);
    }
    if matches!(sniffed_mime.as_str(), "application/msword" | "application/x-ole-storage") {
        let mut doc = match external_doc(Path::new(path)) {
            Ok(document) => return doc_out_from_extracted(path, document, started, html),
            Err(error) => failed_doc(path, format!("the file is a legacy Office document: {error:#}")),
        };
        doc.extraction_ms = started.elapsed().as_millis() as u64;
        return doc;
    }
    if sniffed_mime.starts_with("video/")
        || sniffed_mime.starts_with("audio/")
        || is_compressed_archive_mime(&sniffed_mime)
    {
        let mut doc = failed_doc(
            path,
            format!("the file holds {sniffed_mime} data, not text or a web page"),
        );
        doc.extraction_ms = started.elapsed().as_millis() as u64;
        return doc;
    }
    let sniffed_image = sniffed_mime.starts_with("image/")
        && !sniffed_mime.contains("svg")
        && !sniffed_mime.contains("djvu");
    if sniffed_image
        || matches!(
            extension.as_str(),
            "png" | "jpg" | "jpeg" | "gif" | "bmp" | "tif" | "tiff"
        )
    {
        let source_format = match sniffed.filter(|_| sniffed_image) {
            Some(detection) => detection.extension.trim_start_matches('.').to_string(),
            None => extension,
        };
        return DocOut {
            path: path.to_string(),
            status: "fallback_required".to_string(),
            source_format: Some(source_format),
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
    // Full mode renders large pages itself so its selectors and line filters apply.
    if html.mode == HtmlMode::Article
        && matches!(extension.as_str(), "html" | "htm")
        && fs::metadata(path).is_ok_and(|metadata| metadata.len() >= LARGE_HTML_EXTERNAL_BYTES)
    {
        if let Ok(document) = external_html(Path::new(path)) {
            return doc_out_from_extracted(path, document, started, html);
        }
    }
    let opts = ExtractOptions {
        mime_type: None,
        source_path: Some(path),
        kind: HtmlKind::Auto,
    };
    match extract_file_with(Path::new(path), opts, html) {
        Ok(doc) => doc_out_from_extracted(path, doc, started, html),
        Err(e) => {
            let mut profile_blocked = false;
            if let Ok(mut doc) = external_extract(Path::new(path)) {
                let is_html = matches!(doc.source_format.as_str(), "html" | "mhtml");
                // w3m dumps the whole page, so it would store text the profile excludes.
                profile_blocked = is_html && html.has_rules();
                if !profile_blocked {
                    if is_html && html.mode == HtmlMode::Full {
                        doc.extraction_metadata["content_extraction"]["html_mode"] =
                            Value::String("full".to_string());
                    }
                    return doc_out_from_extracted(path, doc, started, html);
                }
            }
            let mut failure = classify_extraction_failure(&e);
            if profile_blocked {
                failure.message.push_str(
                    "; the w3m fallback was skipped because it cannot apply the HTML profile",
                );
            }
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

/// Crawlers that store the raw HTTP body save pages served with gzip content
/// encoding still compressed. Decompress them and extract the page inside.
fn extract_gzipped_page(path: &str, html: &HtmlConfig, started: Instant) -> DocOut {
    let mut bytes = Vec::new();
    let read = File::open(path).and_then(|file| {
        MultiGzDecoder::new(file)
            .take(MAX_GZIP_PAGE_BYTES + 1)
            .read_to_end(&mut bytes)
    });
    let inner = detect_content_type(&bytes).mime_type.to_ascii_lowercase();
    let result = match read {
        Err(error) => Err(anyhow::anyhow!("the file is gzip data that does not decompress: {error}")),
        Ok(_) if bytes.len() as u64 > MAX_GZIP_PAGE_BYTES => Err(anyhow::anyhow!(
            "the file is gzip data that decompresses to more than {MAX_GZIP_PAGE_BYTES} bytes"
        )),
        Ok(_) if !(inner.starts_with("text/") || inner.contains("xml") || inner == "multipart/related") => {
            Err(anyhow::anyhow!("the file holds gzip-compressed {inner} data, not text or a web page"))
        }
        Ok(_) => {
            let opts = ExtractOptions {
                mime_type: None,
                source_path: Some(path),
                kind: HtmlKind::Auto,
            };
            extract_bytes_with(&bytes, opts, html)
        }
    };
    let mut doc = match result {
        Ok(mut document) => {
            document.extraction_metadata["content_extraction"]["content_encoding"] =
                Value::String("gzip".to_string());
            return doc_out_from_extracted(path, document, started, html);
        }
        Err(error) => failed_doc(path, format!("{error:#}")),
    };
    doc.extraction_ms = started.elapsed().as_millis() as u64;
    doc
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
    let replacement = content
        .chars()
        .filter(|character| *character == '\u{fffd}')
        .count();
    let controls = content
        .chars()
        .filter(|character| character.is_control() && !matches!(*character, '\n' | '\r' | '\t'))
        .count();
    let mut longest_repeated_run: usize = 0;
    let mut current_run: usize = 0;
    let mut previous = None;
    for character in content.chars() {
        if previous == Some(character) {
            current_run += 1;
        } else {
            previous = Some(character);
            current_run = 1;
        }
        longest_repeated_run = longest_repeated_run.max(current_run);
    }
    replacement.saturating_mul(100) < total
        && controls.saturating_mul(100) < total
        && !(longest_repeated_run >= 128 && longest_repeated_run.saturating_mul(4) >= total)
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

/// Bytes read to identify a file's real type before trusting its extension.
const SNIFF_BYTES: u64 = 64 * 1024;

/// Compressed archives found under web-page names: their bytes are not text,
/// and expanding them is the job of the archive path, which keys on the name.
/// A gzip file under a web-page name is a compressed page and is read as one.
fn is_compressed_archive_mime(mime: &str) -> bool {
    matches!(
        mime,
        "application/zip"
            | "application/gzip"
            | "application/x-gzip"
            | "application/x-bzip2"
            | "application/x-xz"
            | "application/x-7z-compressed"
            | "application/vnd.rar"
            | "application/x-rar-compressed"
            | "application/x-tar"
            | "application/zstd"
    )
}

/// The content type that the start of a file shows, or None if it cannot be read.
fn sniff_content(path: &Path) -> Option<ContentTypeDetection> {
    let mut head = Vec::new();
    File::open(path)
        .and_then(|file| file.take(SNIFF_BYTES).read_to_end(&mut head))
        .ok()?;
    Some(detect_content_type(&head))
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

fn extract_one_json(path: &str, html: &HtmlConfig) -> String {
    // Contain per-file Rust panics: a panic in one document must become a
    // `status=failed` result for that file, never propagate out of the rayon
    // closure (which would surface as a PyO3 PanicException and kill the whole
    // batch/CLI). A C-level abort/segfault in a native lib still cannot be
    // caught in-process; that is a known limitation.
    let doc = match catch_unwind(AssertUnwindSafe(|| extract_one(path, html))) {
        Ok(doc) => doc,
        Err(panic) => failed_doc(path, format!("panic: {}", panic_to_string(panic))),
    };
    serde_json::to_string(&doc).unwrap_or_else(|_| {
        format!(
            "{{\"path\":{path:?},\"status\":\"failed\",\"content\":\"\",\"content_chars\":0,\"ocr_needed\":false,\"error\":\"serialize_failed\"}}"
        )
    })
}

/// Names written without the ZIP UTF-8 flag are in the writer's code page,
/// which the zip crate reads as CP437. Chinese, Japanese and Korean Windows
/// archives use GBK, Big5, Shift_JIS or EUC-KR instead. One archive comes from
/// one machine, so guess a single code page from all its non-UTF-8 names and
/// return it only when the guess is one of those; otherwise CP437 stands.
/// Non-ASCII bytes in legacy member names needed before guessing a CJK encoding.
const MIN_LEGACY_NAME_BYTES: usize = 10;

fn legacy_name_encoding(archive: &mut ZipArchive<File>) -> Result<Option<&'static Encoding>> {
    let mut detector = EncodingDetector::new();
    let mut any_high_byte = false;
    let mut non_ascii_bytes = 0;
    for index in 0..archive.len() {
        let entry = archive
            .by_index_raw(index)
            .with_context(|| format!("reading ZIP entry {index}"))?;
        if std::str::from_utf8(entry.name_raw()).is_err() {
            detector.feed(entry.name_raw(), false);
            detector.feed(b"\n", false);
            any_high_byte |= entry.name_raw().iter().any(|&byte| byte >= 0xB0);
            non_ascii_bytes += entry.name_raw().iter().filter(|&&byte| byte >= 0x80).count();
        }
    }
    // CP437's accented letters and punctuation sit below 0xB0; above are box
    // drawing, Greek, and maths symbols, which Western names do not use but CJK
    // double-byte names nearly always do. The detector also guesses wrongly on
    // fewer than about five CJK characters (it reads 报告 as Korean), so keep
    // CP437 unless the names give it that much text.
    if !any_high_byte || non_ascii_bytes < MIN_LEGACY_NAME_BYTES {
        return Ok(None);
    }
    detector.feed(b"", true);
    let guess = detector.guess(None, false);
    Ok([GBK, BIG5, SHIFT_JIS, EUC_KR, EUC_JP]
        .contains(&guess)
        .then_some(guess))
}

/// The member name as text: raw bytes that are valid UTF-8 are UTF-8 (the
/// crate has already replaced them when the UTF-8 flag or the Info-ZIP
/// Unicode path field was present), and other names are decoded with the
/// archive's CJK code page. None keeps the crate's CP437 name. A decoded
/// name must pass the same containment check as the crate's own path.
fn decode_member_name(raw: &[u8], legacy: Option<&'static Encoding>) -> Option<String> {
    let name = match std::str::from_utf8(raw) {
        Ok(text) => text.to_string(),
        Err(_) => legacy?
            .decode_without_bom_handling_and_without_replacement(raw)?
            .into_owned(),
    };
    let mut depth = 0usize;
    if name.contains('\0') {
        return None;
    }
    for component in Path::new(&name).components() {
        match component {
            std::path::Component::Normal(_) => depth += 1,
            std::path::Component::ParentDir => depth = depth.checked_sub(1)?,
            std::path::Component::CurDir => {}
            _ => return None,
        }
    }
    Some(name)
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

    let legacy_encoding = legacy_name_encoding(&mut archive)?;
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
        let member_path = match decode_member_name(entry.name_raw(), legacy_encoding) {
            Some(name) => name,
            None => enclosed
                .to_str()
                .ok_or_else(|| anyhow::anyhow!("ZIP entry path is not valid UTF-8"))?
                .to_string(),
        };
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

fn parse_html_config(html_config: Option<&str>) -> PyResult<HtmlConfig> {
    html_config
        .map(HtmlConfig::from_json)
        .transpose()
        .map(Option::unwrap_or_default)
        .map_err(|error| PyValueError::new_err(format!("{error:#}")))
}

/// Validate an HTML config JSON string and return the settings in effect.
#[pyfunction]
fn normalize_html_config(html_config: &str) -> PyResult<String> {
    Ok(parse_html_config(Some(html_config))?
        .effective_json()
        .to_string())
}

/// Extract a batch of paths in parallel (rayon, GIL released). Returns one JSON
/// string per input path, order-preserving. `threads` sizes the rayon pool
/// (default: rayon's global pool = num_cpus). `html_config` is a JSON object
/// with the HTML settings (see `HtmlConfig`); it is validated before any file
/// is read.
#[pyfunction]
#[pyo3(signature = (paths, threads=None, html_config=None))]
fn extract_batch(
    py: Python<'_>,
    paths: Vec<String>,
    threads: Option<usize>,
    html_config: Option<&str>,
) -> PyResult<Vec<String>> {
    let html = parse_html_config(html_config)?;
    Ok(py.allow_threads(|| {
        let run = || {
            paths
                .par_iter()
                .map(|p| extract_one_json(p, &html))
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
    }))
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
#[pyo3(signature = (path, html_config=None))]
fn extract_path(py: Python<'_>, path: String, html_config: Option<&str>) -> PyResult<String> {
    let html = parse_html_config(html_config)?;
    Ok(py.allow_threads(|| extract_one_json(&path, &html)))
}

#[pymodule]
fn _ingest_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(extract_batch, m)?)?;
    m.add_function(wrap_pyfunction!(hash_batch, m)?)?;
    m.add_function(wrap_pyfunction!(extract_path, m)?)?;
    m.add_function(wrap_pyfunction!(normalize_html_config, m)?)?;
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

    /// A ZIP whose names are raw bytes without the UTF-8 flag, as old Windows
    /// tools wrote them: placeholder names of the same length are written and
    /// then swapped for the raw bytes in both headers.
    fn write_legacy_zip(path: &Path, names: &[&[u8]]) {
        let placeholders: Vec<String> = (0..names.len())
            .map(|index| format!("{index}").repeat(names[index].len()))
            .collect();
        let members: Vec<(&str, &[u8])> = placeholders
            .iter()
            .map(|name| (name.as_str(), &b"<p>legacy</p>"[..]))
            .collect();
        write_zip(path, &members);
        let mut bytes = fs::read(path).unwrap();
        for (placeholder, raw) in placeholders.iter().zip(names) {
            let needle = placeholder.as_bytes();
            let mut start = 0;
            while let Some(offset) = bytes[start..]
                .windows(needle.len())
                .position(|window| window == needle)
            {
                let at = start + offset;
                bytes[at..at + needle.len()].copy_from_slice(raw);
                start = at + needle.len();
            }
        }
        fs::write(path, bytes).unwrap();
    }

    #[test]
    fn decodes_gbk_member_names_from_legacy_chinese_archives() {
        let root = tempdir().unwrap();
        let archive = root.path().join("legacy.zip");
        let names = ["北京地区法轮功现象研究.htm", "新闻精选-北京专家学者.htm", "张玲莉.htm"];
        let raw: Vec<Vec<u8>> = names.iter().map(|name| GBK.encode(name).0.into_owned()).collect();
        write_legacy_zip(&archive, &raw.iter().map(Vec::as_slice).collect::<Vec<_>>());

        let members =
            expand_zip_archive(&archive, &root.path().join("out"), 10, 1024, 4096, 200).unwrap();

        let decoded: Vec<&str> = members.iter().map(|m| m.member_path.as_str()).collect();
        assert_eq!(decoded, names);
        assert_eq!(fs::read(&members[0].path).unwrap(), b"<p>legacy</p>");
    }

    #[test]
    fn keeps_cp437_for_western_legacy_names_and_reads_unflagged_utf8() {
        let root = tempdir().unwrap();
        let western = root.path().join("western.zip");
        // "Résumé.txt" in CP437, where é is 0x82.
        write_legacy_zip(&western, &[b"R\x82sum\x82.txt"]);
        let members =
            expand_zip_archive(&western, &root.path().join("w"), 10, 1024, 4096, 200).unwrap();
        assert_eq!(members[0].member_path, "Résumé.txt");

        let unflagged = root.path().join("unflagged.zip");
        write_legacy_zip(&unflagged, &["报告.htm".as_bytes()]);
        let members =
            expand_zip_archive(&unflagged, &root.path().join("u"), 10, 1024, 4096, 200).unwrap();
        assert_eq!(members[0].member_path, "报告.htm");
    }

    #[test]
    fn short_western_legacy_names_stay_cp437() {
        let root = tempdir().unwrap();
        for (raw, expected) in [
            (&b"B\x81ro.txt"[..], "Büro.txt"),
            (b"M\x81nchen.txt", "München.txt"),
            (b"fran\x87ais.txt", "français.txt"),
            (b"gar\x87on.txt", "garçon.txt"),
        ] {
            let archive = root.path().join(format!("{expected}.zip"));
            write_legacy_zip(&archive, &[raw]);
            let mut zip = ZipArchive::new(File::open(&archive).unwrap()).unwrap();
            assert_eq!(legacy_name_encoding(&mut zip).unwrap(), None, "{expected}");
            assert_eq!(decode_member_name(raw, None), None);
            assert_eq!(zip.by_index(0).unwrap().name(), expected);
        }
    }

    #[test]
    fn too_little_cjk_text_keeps_cp437_and_enough_is_decoded() {
        let root = tempdir().unwrap();
        // "报告.htm" in GBK: two characters, which the detector reads as Korean.
        let short = root.path().join("short.zip");
        write_legacy_zip(&short, &[b"\xb1\xa8\xb8\xe6.htm"]);
        let mut zip = ZipArchive::new(File::open(&short).unwrap()).unwrap();
        assert_eq!(legacy_name_encoding(&mut zip).unwrap(), None);

        // "会议纪要.doc" and "报告.htm": six characters.
        let enough = root.path().join("enough.zip");
        let names: [&[u8]; 2] = [b"\xbb\xe1\xd2\xe9\xbc\xcd\xd2\xaa.doc", b"\xb1\xa8\xb8\xe6.htm"];
        write_legacy_zip(&enough, &names);
        let mut zip = ZipArchive::new(File::open(&enough).unwrap()).unwrap();
        let encoding = legacy_name_encoding(&mut zip).unwrap();
        assert_eq!(decode_member_name(names[0], encoding).as_deref(), Some("会议纪要.doc"));
        assert_eq!(decode_member_name(names[1], encoding).as_deref(), Some("报告.htm"));
    }

    #[test]
    fn decoded_member_names_must_stay_inside_the_archive() {
        assert_eq!(decode_member_name(b"a/../b.txt", None).as_deref(), Some("a/../b.txt"));
        assert_eq!(decode_member_name(b"../b.txt", None), None);
        assert_eq!(decode_member_name(b"/etc/passwd", None), None);
        assert_eq!(decode_member_name(b"\xb1\xa8\xb8\xe6.htm", None), None);
        assert_eq!(
            decode_member_name(b"\xb1\xa8\xb8\xe6.htm", Some(GBK)).as_deref(),
            Some("报告.htm")
        );
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
    fn binary_files_named_html_are_judged_by_their_bytes() {
        let root = tempdir().unwrap();
        let jpeg = root.path().join("photo.html");
        let mut jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00H\x00H\x00\x00".to_vec();
        jpeg_bytes.extend(std::iter::repeat(0x5a).take(4096));
        fs::write(&jpeg, jpeg_bytes).unwrap();
        // The opening bytes of an M4V video found saved as .html in a crawl.
        let video = root.path().join("clip.m4v.html");
        let mut video_bytes = b"\x00\x00\x00\x1cftypM4V \x00\x00\x02\x00isomiso2avc1\x00\x00\x00\x08free".to_vec();
        video_bytes.extend(std::iter::repeat(0x3c).take(4096));
        fs::write(&video, video_bytes).unwrap();
        let page = root.path().join("page.html");
        fs::write(&page, "<html><body><article><p>A real page about the archive.</p></article></body></html>")
            .unwrap();
        let config = HtmlConfig::default();

        let image = extract_one(jpeg.to_str().unwrap(), &config);
        assert_eq!(image.status, "fallback_required");
        assert!(image.ocr_needed);
        assert_eq!(image.source_format.as_deref(), Some("jpg"));

        let clip = extract_one(video.to_str().unwrap(), &config);
        assert_eq!(clip.status, "failed");
        assert!(clip.error.unwrap().contains("video/"), "video detected");

        let html = extract_one(page.to_str().unwrap(), &config);
        assert_eq!(html.status, "extracted");
        assert!(html.content.contains("real page"));
    }

    #[test]
    fn gzip_compressed_pages_are_decompressed_and_read() {
        use flate2::{write::GzEncoder, Compression};
        let gzip = |data: &[u8]| {
            let mut encoder = GzEncoder::new(Vec::new(), Compression::fast());
            encoder.write_all(data).unwrap();
            encoder.finish().unwrap()
        };
        let root = tempdir().unwrap();
        let page = root.path().join("stock-information.aspx.html");
        fs::write(
            &page,
            gzip(b"<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Weather</title></head>\
                <body><article><p>Rain is expected across the region on Tuesday, with clearer skies \
                from Wednesday and light winds through the weekend.</p></article></body></html>"),
        )
        .unwrap();
        let binary = root.path().join("bundle.html");
        let mut jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00H\x00H\x00\x00".to_vec();
        jpeg.extend(std::iter::repeat(0x5a).take(4096));
        fs::write(&binary, gzip(&jpeg)).unwrap();
        let config = HtmlConfig::default();

        let doc = extract_one(page.to_str().unwrap(), &config);
        assert_eq!(doc.status, "extracted", "{:?}", doc.error);
        assert!(doc.content.contains("Rain is expected"), "{}", doc.content);
        let encoding = &doc.extraction_metadata.unwrap()["content_extraction"]["content_encoding"];
        assert_eq!(encoding, "gzip");

        let failed = extract_one(binary.to_str().unwrap(), &config);
        assert_eq!(failed.status, "failed");
        assert!(failed.error.unwrap().contains("gzip-compressed image/"));

        let text = root.path().join("forecast.txt");
        fs::write(&text, gzip(b"Rain is expected across the region on Tuesday.\n")).unwrap();
        let doc = extract_one(text.to_str().unwrap(), &config);
        assert_eq!(doc.status, "extracted", "{:?}", doc.error);
        assert_eq!(doc.content, "Rain is expected across the region on Tuesday.");
    }

    #[test]
    fn large_images_named_html_go_to_ocr_not_w3m() {
        let root = tempdir().unwrap();
        let jpeg = root.path().join("large.html");
        let mut bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00H\x00H\x00\x00".to_vec();
        bytes.resize(LARGE_HTML_EXTERNAL_BYTES as usize + 1024, 0x5a);
        fs::write(&jpeg, bytes).unwrap();

        let image = extract_one(jpeg.to_str().unwrap(), &HtmlConfig::default());
        assert_eq!(image.status, "fallback_required");
        assert!(image.ocr_needed);
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
    fn external_text_gate_rejects_recovered_repeated_character_garbage() {
        let garbage = format!(
            "{}\n{}",
            "1".repeat(600),
            "binary-looking fallback output with a few accidental words"
        );
        assert!(!external_text_is_usable(&garbage));
    }

    #[test]
    fn low_value_rejection_follows_the_html_mode() {
        let page = b"<html><body><p>This is login.htm from the docs subdirectory. Please sign in to continue to the archive of notices.</p></body></html>";
        let options = ExtractOptions {
            mime_type: Some("text/html"),
            source_path: Some("login.html"),
            kind: HtmlKind::Html,
        };
        let full = HtmlConfig::from_json(r#"{"mode": "full"}"#).unwrap();
        let doc = crate::extract_bytes_with(page, options, &full).unwrap();
        let out = doc_out_from_extracted("login.html", doc, Instant::now(), &full);
        assert!(out.content.contains("login.htm"));
        assert!(out.error.is_none());
        assert!(
            out.extraction_metadata.unwrap()["content_extraction"]["low_value_reason"]
                .as_str()
                .unwrap()
                .contains("login placeholder")
        );

        let article = HtmlConfig::default();
        let doc = crate::extract_bytes_with(page, options, &article).unwrap();
        let out = doc_out_from_extracted("login.html", doc, Instant::now(), &article);
        assert!(out.content.is_empty());
        assert!(out.error.unwrap().contains("login placeholder"));
    }

    #[test]
    fn invalid_html_config_is_rejected_before_extraction() {
        assert!(
            parse_html_config(Some(r#"{"mode": "full", "keep_selectors": ["div["]}"#)).is_err()
        );
        assert!(parse_html_config(Some("not json")).is_err());
        assert_eq!(parse_html_config(None).unwrap().mode, HtmlMode::Article);
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
