//! PyO3 bindings: `doctrail._ingest_native`.
//!
//! Exposes the stripped extraction core to Python. Rust owns all multicore work
//! (rayon, GIL released via `Python::allow_threads`); Python only pushes paths in
//! and writes rows out. Each document is returned as a JSON string so the FFI
//! surface stays version-robust; `doctrail.ingest.native_extractor` parses them.

use crate::{classify_extraction_failure, extract_file, ExtractOptions, HtmlKind};
use pyo3::prelude::*;
use rayon::prelude::*;
use serde::Serialize;
use serde_json::Value;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;
use std::time::Instant;

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
    Ok(())
}
