use anyhow::Context;
use flate2::read::GzDecoder;
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use wait_timeout::ChildExt;

use super::language::{detect_language, language_detection_metadata};
use super::types::LanguageDetectionReport;

static HAN_SPACE_HAN_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(\p{Han})\s+(\p{Han})").expect("han spacing regex"));
static HAN_SPACE_ALNUM_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(\p{Han})\s+([A-Za-z0-9])").expect("han alnum spacing regex"));
static ALNUM_SPACE_HAN_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"([A-Za-z0-9])\s+(\p{Han})").expect("alnum han spacing regex"));
static EXCESSIVE_WHITESPACE_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"\s{2,}").expect("whitespace regex"));
static NUMBERED_LIST_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^[0-9]+[\.、]").expect("numbered list regex"));
static CHINESE_NUMBERED_LIST_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^[一二三四五六七八九十]+[、.]").expect("chinese list regex"));
static CHAPTER_MARKER_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^第[一二三四五六七八九十0-9]+[章节部分条]").expect("chapter marker regex")
});
static PDF_EXTRACTION_LOCK: Lazy<Mutex<()>> = Lazy::new(|| Mutex::new(()));
static PDFTOTEXT_HYPHEN_FLAG_CACHE: Lazy<Mutex<HashMap<String, bool>>> =
    Lazy::new(|| Mutex::new(HashMap::new()));
static PAGE_TAG_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"\u{fdd0}([0-9]+)\u{fdd1}").expect("page tag regex"));
static LATIN_WORD_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"[A-Za-z\u{c0}-\u{24f}]{3,}").expect("latin word regex"));
const MIN_TEXT_CHARS_PER_PAGE: usize = 80;
/// Lines seen this often are page furniture: running heads and download stamps.
const REPEATED_LINE_COUNT: usize = 3;
const MIN_PAGES_FOR_PAGE_SHARE: usize = 4;
/// A Latin word this long is almost always several words with the spaces lost.
const LONG_WORD_LETTERS: usize = 20;
const MIN_WORDS_FOR_LONG_WORD_SHARE: usize = 200;
/// Above this share of long words, try the other backend.
const RETRY_LONG_WORD_SHARE: f64 = 0.01;
/// Above this share after both backends, the text layer has no word spacing.
const OCR_LONG_WORD_SHARE: f64 = 0.03;
// Noncharacters that tag where each page starts while paragraphs are joined.
const PAGE_TAG_OPEN: char = '\u{fdd0}';
const PAGE_TAG_CLOSE: char = '\u{fdd1}';
const PDFTOTEXT_TIMEOUT: Duration = Duration::from_secs(120);
const PDFTOTEXT_INSTALL_HINT: &str = "pdftotext not found: install Poppler \
(macOS: brew install poppler; Debian/Ubuntu: apt install poppler-utils), set \
DOCTRAIL_PDFTOTEXT to the binary, or pass --pdf-engine pymupdf";

struct PdfTextExtraction {
    raw_text: String,
    page_count: Option<usize>,
    page_count_method: Option<&'static str>,
    backend: &'static str,
    fallback_from: Option<&'static str>,
    fallback_error: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PdfBackend {
    Pdftotext,
    Mupdf,
}

struct PdfExtractionAttempt {
    raw_text: String,
    smart_content: String,
    content: String,
    /// (page number, character offset in `content`) for each page with text.
    page_offsets: Vec<(usize, usize)>,
    long_word_share: f64,
    page_count: Option<usize>,
    page_count_method: Option<&'static str>,
    backend: Option<&'static str>,
    extraction_failed: bool,
    extraction_error: Option<String>,
    language_detection: LanguageDetectionReport,
    ocr_needed: bool,
    ocr_reason: &'static str,
    pdf_text_duration: Duration,
    cleanup_duration: Duration,
    language_duration: Duration,
    fallback_from: Option<&'static str>,
    fallback_error: Option<String>,
    fallback_attempts: Vec<Value>,
}

#[derive(Clone, Debug, Default)]
pub struct PdfConfig {
    pub backend: Option<String>,
    pub pdftotext: Option<String>,
}

#[cfg(test)]
pub(crate) fn extract_pdf_bytes(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
) -> anyhow::Result<super::types::GeneralExtractedDocument> {
    extract_pdf_bytes_with_config(bytes, source_path, mime_type, &PdfConfig::default())
}

pub(crate) fn extract_pdf_bytes_with_config(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
    pdf: &PdfConfig,
) -> anyhow::Result<super::types::GeneralExtractedDocument> {
    if !bytes[..bytes.len().min(1024)]
        .windows(5)
        .any(|window| window == b"%PDF-")
    {
        anyhow::bail!("damaged PDF: missing %PDF header in first 1024 bytes");
    }
    let total_started_at = Instant::now();

    let backend_request = requested_pdf_backend(pdf);
    let attempt = extract_pdf_text_attempt(bytes, backend_request, pdf.pdftotext.as_deref())?;
    let title = title_from_raw_pdf_text(&attempt.raw_text)
        .or_else(|| title_from_content_text(&attempt.content))
        .unwrap_or_default();

    let mut timing_ms = Map::new();
    timing_ms.insert(
        "pdf_text_extract".to_string(),
        json!(duration_millis(attempt.pdf_text_duration)),
    );
    timing_ms.insert(
        "smart_paragraph_cleanup".to_string(),
        json!(duration_millis(attempt.cleanup_duration)),
    );
    timing_ms.insert(
        "language_detect".to_string(),
        json!(duration_millis(attempt.language_duration)),
    );
    timing_ms.insert(
        "extract_pdf_bytes_total".to_string(),
        json!(duration_millis(total_started_at.elapsed())),
    );

    let mut content_extraction = Map::new();
    content_extraction.insert("extractor_type".to_string(), json!("pdf"));
    content_extraction.insert("source_format".to_string(), json!("pdf"));
    content_extraction.insert("file_size".to_string(), json!(bytes.len()));
    content_extraction.insert(
        "page_count".to_string(),
        attempt.page_count.map_or(Value::Null, |value| json!(value)),
    );
    if let Some(method) = attempt.page_count_method {
        content_extraction.insert("page_count_method".to_string(), json!(method));
    }
    let extraction_method = match attempt.backend {
        Some("pdftotext") => "pdftotext_smart_paragraphs",
        Some("mupdf") => "mupdf_smart_paragraphs",
        _ => "pdf_text_smart_paragraphs",
    };
    content_extraction.insert("extraction_method".to_string(), json!(extraction_method));
    content_extraction.insert(
        "pdf_text_backend_requested".to_string(),
        json!(backend_request.as_str()),
    );
    content_extraction.insert(
        "pdf_text_backend".to_string(),
        attempt.backend.map_or(Value::Null, |value| json!(value)),
    );
    if let Some(fallback_from) = attempt.fallback_from {
        content_extraction.insert("pdf_text_fallback_from".to_string(), json!(fallback_from));
    }
    if let Some(fallback_error) = attempt.fallback_error {
        content_extraction.insert("pdf_text_fallback_error".to_string(), json!(fallback_error));
    }
    if !attempt.fallback_attempts.is_empty() {
        content_extraction.insert(
            "pdf_text_fallback_attempts".to_string(),
            Value::Array(attempt.fallback_attempts.clone()),
        );
    }
    content_extraction.insert(
        "page_offsets".to_string(),
        json!(attempt
            .page_offsets
            .iter()
            .map(|(page, offset)| [*page, *offset])
            .collect::<Vec<_>>()),
    );
    content_extraction.insert(
        "long_word_share".to_string(),
        json!(attempt.long_word_share),
    );
    content_extraction.insert(
        "character_count".to_string(),
        json!(attempt.content.chars().count()),
    );
    content_extraction.insert(
        "raw_character_count".to_string(),
        json!(attempt.smart_content.chars().count()),
    );
    content_extraction.insert(
        "pdf_text_raw_character_count".to_string(),
        json!(attempt.raw_text.chars().count()),
    );
    content_extraction.insert(
        "extraction_failed".to_string(),
        json!(attempt.extraction_failed),
    );
    if let Some(error) = &attempt.extraction_error {
        content_extraction.insert("extraction_error".to_string(), json!(error));
    }
    content_extraction.insert("ocr_needed".to_string(), json!(attempt.ocr_needed));
    content_extraction.insert(
        "requires_full_pdf_ocr".to_string(),
        json!(attempt.ocr_needed),
    );
    content_extraction.insert("ocr_reason".to_string(), json!(attempt.ocr_reason));
    content_extraction.insert("timing_ms".to_string(), Value::Object(timing_ms.clone()));
    if let Some(source_path) = source_path {
        content_extraction.insert("original_bucket_path".to_string(), json!(source_path));
    }
    if let Some(mime_type) = mime_type {
        content_extraction.insert("mime_type".to_string(), json!(mime_type));
    }

    let extraction_metadata = json!({
        "file": {
            "size": bytes.len(),
            "encoding": null,
        },
        "content_extraction": content_extraction,
        "title_extraction": {
            "method": if title.is_empty() { "none" } else { "content_first_line" },
        },
        "language_detection": language_detection_metadata(&attempt.language_detection),
    });

    Ok(super::types::GeneralExtractedDocument {
        source_format: "pdf".to_string(),
        title,
        content_length: attempt.content.chars().count(),
        content: attempt.content,
        language: attempt.language_detection.language,
        extraction_metadata,
        timing_ms: Value::Object(timing_ms),
    })
}

/// pdftotext unless the caller or DOCTRAIL_PDF_BACKEND asked for mupdf.
fn requested_pdf_backend(pdf: &PdfConfig) -> PdfBackend {
    if let Some(backend) = pdf.backend.as_deref() {
        return match backend {
            "mupdf" | "pymupdf" => PdfBackend::Mupdf,
            _ => PdfBackend::Pdftotext,
        };
    }
    match std::env::var("DOCTRAIL_PDF_BACKEND").as_deref() {
        Ok("mupdf") | Ok("pymupdf") => PdfBackend::Mupdf,
        _ => PdfBackend::Pdftotext,
    }
}

impl PdfBackend {
    fn as_str(self) -> &'static str {
        match self {
            PdfBackend::Pdftotext => "pdftotext",
            PdfBackend::Mupdf => "mupdf",
        }
    }
}

fn extract_pdf_text_attempt(
    bytes: &[u8],
    backend: PdfBackend,
    pdftotext_override: Option<&str>,
) -> anyhow::Result<PdfExtractionAttempt> {
    let started_at = Instant::now();
    if backend == PdfBackend::Mupdf {
        let result = extract_with_mupdf_guarded(bytes)?;
        return Ok(process_pdf_extraction_result(result, started_at.elapsed()));
    }
    // A missing binary is a setup error, not a damaged PDF: never mask it.
    let result = match extract_with_pdftotext(bytes, pdftotext_override) {
        Err(error) if is_not_found(&error) => anyhow::bail!(PDFTOTEXT_INSTALL_HINT),
        Err(error) => extract_with_mupdf_guarded(bytes)?.map(|mut extraction| {
            extraction.fallback_from = Some("pdftotext");
            extraction.fallback_error = Some(error.to_string());
            extraction
        }),
        ok => ok,
    };
    let fell_back = matches!(&result, Ok(extraction) if extraction.backend == "mupdf");
    let attempt = process_pdf_extraction_result(result, started_at.elapsed());
    if fell_back || !(attempt.ocr_needed || attempt.long_word_share >= RETRY_LONG_WORD_SHARE) {
        return Ok(attempt);
    }
    // pdftotext loses the spaces in some files and finds little text in others
    // where MuPDF reads them well, so give MuPDF a try before settling for OCR.
    let reason = if attempt.ocr_needed {
        attempt.ocr_reason
    } else {
        "words_run_together"
    };
    let Ok(Ok(mut extraction)) = extract_with_mupdf_guarded(bytes) else {
        return Ok(attempt);
    };
    extraction.fallback_from = Some("pdftotext");
    extraction.fallback_error = Some(reason.to_string());
    let second = process_pdf_extraction_result(Ok(extraction), started_at.elapsed());
    let better = !second.ocr_needed
        && (attempt.ocr_needed || second.long_word_share < attempt.long_word_share);
    Ok(if better { second } else { attempt })
}

fn is_not_found(error: &anyhow::Error) -> bool {
    error
        .downcast_ref::<std::io::Error>()
        .is_some_and(|io| io.kind() == std::io::ErrorKind::NotFound)
}

/// Poppler 26+ strips end-of-line hyphens by default (`-remove-hyphens all`),
/// fusing hyphenated compounds like `state-\nowned` into `stateowned` before
/// doctrail sees the line break. Detect `-remove-hyphens` once per binary path
/// so we pass `-remove-hyphens none` on Poppler 26+ without breaking older
/// Poppler builds that do not know the flag.
fn pdftotext_supports_remove_hyphens(program: &str) -> bool {
    {
        let cache = PDFTOTEXT_HYPHEN_FLAG_CACHE
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some(&supported) = cache.get(program) {
            return supported;
        }
    }
    let supported = Command::new(program)
        .arg("-h")
        .stdin(Stdio::null())
        .output()
        .map(|output| {
            let stdout = String::from_utf8_lossy(&output.stdout);
            let stderr = String::from_utf8_lossy(&output.stderr);
            stdout.contains("-remove-hyphens") || stderr.contains("-remove-hyphens")
        })
        .unwrap_or(false);
    let mut cache = PDFTOTEXT_HYPHEN_FLAG_CACHE
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    cache.insert(program.to_string(), supported);
    supported
}

fn extract_with_pdftotext(
    bytes: &[u8],
    pdftotext_override: Option<&str>,
) -> anyhow::Result<PdfTextExtraction> {
    let program = pdftotext_override
        .map(str::to_string)
        .or_else(|| std::env::var("DOCTRAIL_PDFTOTEXT").ok())
        .unwrap_or_else(|| "pdftotext".to_string());
    let mut input = tempfile::NamedTempFile::new().context("creating pdftotext input file")?;
    input.write_all(bytes).context("writing pdftotext input file")?;
    let output = tempfile::NamedTempFile::new().context("creating pdftotext output file")?;
    let stderr = tempfile::NamedTempFile::new().context("creating pdftotext stderr file")?;
    let mut command = Command::new(&program);
    if pdftotext_supports_remove_hyphens(&program) {
        command.args(["-remove-hyphens", "none"]);
    }
    let mut child = command
        .args(["-enc", "UTF-8"])
        .arg(input.path())
        .arg(output.path())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr.reopen()?))
        .spawn()?;
    let Some(status) = child.wait_timeout(PDFTOTEXT_TIMEOUT)? else {
        let _ = child.kill();
        let _ = child.wait();
        anyhow::bail!("pdftotext timed out after {PDFTOTEXT_TIMEOUT:?}");
    };
    if !status.success() {
        let message = std::fs::read_to_string(stderr.path()).unwrap_or_default();
        anyhow::bail!("pdftotext exited with {status}: {}", message.trim());
    }
    let raw_text = String::from_utf8_lossy(&std::fs::read(output.path())?).into_owned();
    // pdftotext ends every page with a form feed.
    let page_count = raw_text.matches('\x0c').count();
    Ok(PdfTextExtraction {
        raw_text,
        page_count: Some(page_count),
        page_count_method: Some("pdftotext_form_feeds"),
        backend: "pdftotext",
        fallback_from: None,
        fallback_error: None,
    })
}

/// MuPDF is not thread-safe, so calls are serialized and panics become errors.
fn extract_with_mupdf_guarded(bytes: &[u8]) -> anyhow::Result<anyhow::Result<PdfTextExtraction>> {
    let _guard = PDF_EXTRACTION_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    catch_unwind(AssertUnwindSafe(|| extract_with_mupdf(bytes))).map_err(|payload| {
        let message = payload
            .downcast_ref::<&str>()
            .copied()
            .or_else(|| payload.downcast_ref::<String>().map(String::as_str))
            .unwrap_or("unknown panic");
        anyhow::anyhow!("mupdf panicked on malformed pdf: {message}")
    })
}

fn process_pdf_extraction_result(
    result: anyhow::Result<PdfTextExtraction>,
    pdf_text_duration: Duration,
) -> PdfExtractionAttempt {
    let (extraction, extraction_error) = match result {
        Ok(extraction) => (Some(extraction), None),
        Err(error) => (None, Some(error.to_string())),
    };

    let raw_text = extraction
        .as_ref()
        .map(|value| value.raw_text.clone())
        .unwrap_or_default();
    let cleanup_started_at = Instant::now();
    // Expand ligature glyphs (which hide words from search) and strip any
    // stray internal page sentinels from source text. Soft hyphens (`\u{ad}`)
    // stay until `smart_paragraph_cleanup` so line-end soft hyphens rejoin
    // cleanly even across a page break, and interior soft hyphens are dropped.
    let mut cleaned_raw = String::with_capacity(raw_text.len());
    for ch in raw_text.chars() {
        match ch {
            PAGE_TAG_OPEN | PAGE_TAG_CLOSE => {}
            '\u{fb00}' => cleaned_raw.push_str("ff"),
            '\u{fb01}' => cleaned_raw.push_str("fi"),
            '\u{fb02}' => cleaned_raw.push_str("fl"),
            '\u{fb03}' => cleaned_raw.push_str("ffi"),
            '\u{fb04}' => cleaned_raw.push_str("ffl"),
            '\u{fb05}' | '\u{fb06}' => cleaned_raw.push_str("st"),
            other => cleaned_raw.push(other),
        }
    }
    let marked_raw_content = tag_physical_pages(&cleaned_raw);
    let tagged = smart_paragraph_cleanup(&marked_raw_content);
    let smart_content = PAGE_TAG_RE.replace_all(&tagged, "").into_owned();
    let (content, page_offsets) = take_page_offsets(&clean_text(&tagged));
    let long_word_share = long_word_share(&content);
    let cleanup_duration = cleanup_started_at.elapsed();

    let language_started_at = Instant::now();
    let language_detection = detect_language(&content);
    let language_duration = language_started_at.elapsed();

    let extraction_failed = extraction.is_none();
    let page_count = extraction.as_ref().and_then(|value| value.page_count);
    let (mut ocr_needed, mut ocr_reason) =
        needs_ocr(&content, &raw_text, page_count, extraction_failed);
    if !ocr_needed && long_word_share >= OCR_LONG_WORD_SHARE {
        (ocr_needed, ocr_reason) = (true, "words_run_together");
    }

    PdfExtractionAttempt {
        raw_text,
        smart_content,
        content,
        page_offsets,
        long_word_share,
        page_count,
        page_count_method: extraction
            .as_ref()
            .and_then(|value| value.page_count_method),
        backend: extraction.as_ref().map(|value| value.backend),
        extraction_failed,
        extraction_error,
        language_detection,
        ocr_needed,
        ocr_reason,
        pdf_text_duration,
        cleanup_duration,
        language_duration,
        fallback_from: extraction.as_ref().and_then(|value| value.fallback_from),
        fallback_error: extraction.and_then(|value| value.fallback_error),
        fallback_attempts: Vec::new(),
    }
}

fn extract_with_mupdf(bytes: &[u8]) -> anyhow::Result<PdfTextExtraction> {
    let document =
        mupdf::Document::from_bytes(bytes, "pdf").context("open PDF with MuPDF from bytes")?;
    let page_count = document.page_count().context("read MuPDF page count")?;
    let page_count_usize =
        usize::try_from(page_count).context("MuPDF returned negative page count")?;
    let mut pages = Vec::with_capacity(page_count_usize);
    for page_index in 0..page_count {
        let page = document
            .load_page(page_index)
            .with_context(|| format!("load MuPDF page {}", page_index + 1))?;
        let text = page
            .text(mupdf::TextExtractOptions::default())
            .with_context(|| format!("extract MuPDF text from page {}", page_index + 1))?;
        pages.push(text);
    }

    Ok(PdfTextExtraction {
        raw_text: pages.join("\x0c"),
        page_count: Some(page_count_usize),
        page_count_method: Some("mupdf_page_count"),
        backend: "mupdf",
        fallback_from: None,
        fallback_error: None,
    })
}

fn tag_physical_pages(raw_text: &str) -> String {
    raw_text
        .split('\x0c')
        .enumerate()
        .filter_map(|(index, page)| {
            let page = page.trim();
            (!page.is_empty()).then(|| format!("{PAGE_TAG_OPEN}{}{PAGE_TAG_CLOSE}\n{page}", index + 1))
        })
        .collect::<Vec<_>>()
        .join("\n\n")
}

fn parse_page_marker_line(line: &str) -> Option<usize> {
    let rest = line.strip_prefix(PAGE_TAG_OPEN)?;
    let digits = rest.strip_suffix(PAGE_TAG_CLOSE)?;
    digits.parse().ok()
}

fn clean_text(text: &str) -> String {
    text.lines()
        .filter_map(|line| {
            let without_controls: String = line
                .chars()
                .filter(|character| !character.is_control() || *character == '\t')
                .collect();
            let cleaned = without_controls.trim();
            (!cleaned.is_empty()).then(|| cleaned.to_string())
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn aggressive_join(text: &str) -> String {
    text.lines()
        .filter_map(|line| {
            let mut cleaned = line.to_string();
            cleaned = HAN_SPACE_HAN_RE.replace_all(&cleaned, "$1$2").into_owned();
            cleaned = HAN_SPACE_HAN_RE.replace_all(&cleaned, "$1$2").into_owned();
            cleaned = HAN_SPACE_ALNUM_RE
                .replace_all(&cleaned, "$1 $2")
                .into_owned();
            cleaned = ALNUM_SPACE_HAN_RE
                .replace_all(&cleaned, "$1 $2")
                .into_owned();
            cleaned = EXCESSIVE_WHITESPACE_RE
                .replace_all(&cleaned, " ")
                .into_owned();
            let cleaned = cleaned.trim();
            (!cleaned.is_empty()).then(|| cleaned.to_string())
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn smart_paragraph_cleanup(text: &str) -> String {
    let text = aggressive_join(text);
    let lines = text.lines().collect::<Vec<_>>();
    let doc_words = DocWordCounts::from_lines(&lines);
    let mut paragraphs = Vec::new();
    let mut current_paragraph: Vec<String> = Vec::new();
    // Set at a page marker; the next line of text carries the page's tag.
    let mut page_start: Option<usize> = None;

    for raw_line in lines {
        let trimmed = raw_line.trim();
        if trimmed.is_empty() {
            if !current_paragraph.is_empty() {
                paragraphs.push(finalize_paragraph(&current_paragraph));
                current_paragraph.clear();
            }
            continue;
        }

        if let Some(page) = parse_page_marker_line(trimmed) {
            page_start = Some(page);
            continue;
        }
        // Keep a trailing soft hyphen only until the next line decides the join;
        // remove any interior soft hyphens from the line.
        let trailing_soft_hyphen = trimmed.ends_with('\u{ad}');
        let mut line: String = trimmed.chars().filter(|&ch| ch != '\u{ad}').collect();
        if line.is_empty() {
            continue;
        }
        if trailing_soft_hyphen {
            line.push('\u{ad}');
        }

        let tag = page_start
            .take()
            .map(|page| format!("{PAGE_TAG_OPEN}{page}{PAGE_TAG_CLOSE}"))
            .unwrap_or_default();

        if starts_new_paragraph(&line, &current_paragraph) {
            if !current_paragraph.is_empty() {
                paragraphs.push(finalize_paragraph(&current_paragraph));
            }
            current_paragraph = vec![format!("{tag}{line}")];
            continue;
        }

        let paragraph_so_far = current_paragraph.join("");
        let previous = current_paragraph.last_mut().expect("paragraph has a line");
        let mut tail = previous.chars().rev();
        let last_char = tail.next();
        let before_last = tail.next();
        let first_char = line.chars().next();
        if last_char == Some('\u{ad}') {
            previous.pop();
            current_paragraph.push(format!("{tag}{line}"));
        } else if matches!(last_char, Some('-' | '\u{2010}'))
            && before_last.is_some_and(char::is_alphanumeric)
            && first_char.is_some_and(char::is_alphanumeric)
        {
            let context = PAGE_TAG_RE.replace_all(&paragraph_so_far, "");
            if keep_line_break_hyphen(
                &context,
                &line,
                before_last.unwrap(),
                first_char.unwrap(),
                &doc_words,
            ) {
                current_paragraph.push(format!("{tag}{line}"));
            } else {
                previous.pop();
                current_paragraph.push(format!("{tag}{line}"));
            }
        } else if matches!(last_char, Some('\u{2011}'))
            && before_last.is_some_and(char::is_alphanumeric)
            && first_char.is_some_and(char::is_alphanumeric)
        {
            current_paragraph.push(format!("{tag}{line}"));
        } else if last_char.is_some_and(is_unspaced_script)
            || first_char.is_some_and(is_unspaced_script)
        {
            current_paragraph.push(format!("{tag}{line}"));
        } else {
            current_paragraph.push(format!(" {tag}{line}"));
        }
    }

    if !current_paragraph.is_empty() {
        paragraphs.push(finalize_paragraph(&current_paragraph));
    }

    paragraphs.join("\n\n")
}

fn finalize_paragraph(parts: &[String]) -> String {
    parts.join("").replace('\u{ad}', "")
}

/// English word list (derived from the MIT-licensed pyspellchecker dictionary)
/// used to tell wrapped words (`gar-` + `dener`) from hyphenated compounds
/// (`state-` + `owned`) when the document itself does not settle the question.
struct EnglishLexicon {
    all: HashSet<String>,
    common: HashSet<String>,
}

impl EnglishLexicon {
    fn load() -> Self {
        let compressed = include_bytes!("en_words.txt.gz");
        let mut decoder = GzDecoder::new(&compressed[..]);
        let mut raw = String::new();
        decoder
            .read_to_string(&mut raw)
            .expect("decompress bundled English word list");
        let mut all = HashSet::with_capacity(140_000);
        let mut common = HashSet::with_capacity(50_000);
        for line in raw.lines() {
            if let Some(word) = line.strip_prefix('*') {
                all.insert(word.to_string());
                common.insert(word.to_string());
            } else if !line.is_empty() {
                all.insert(line.to_string());
            }
        }
        Self { all, common }
    }

    fn is_word(&self, word: &str) -> bool {
        self.all.contains(word)
    }

    fn is_common(&self, word: &str) -> bool {
        self.common.contains(word)
    }
}

static ENGLISH_LEXICON: Lazy<EnglishLexicon> = Lazy::new(EnglishLexicon::load);

/// Prefixes that commonly take a hyphen in compounds when the closed form is
/// not a standard dictionary word (e.g. `anti-corruption`, `co-chair`).
const HYPHEN_PREFIXES: &[&str] = &[
    "anti", "auto", "bio", "co", "counter", "cross", "cyber", "de", "eco", "euro", "ex", "extra",
    "geo", "hyper", "infra", "inter", "intra", "macro", "mega", "meta", "micro", "mid", "mini",
    "multi", "neo", "non", "omni", "over", "pan", "para", "peri", "photo", "poly", "post", "pre",
    "pro", "proto", "pseudo", "quasi", "re", "retro", "self", "semi", "sino", "socio", "sub",
    "super", "supra", "tele", "trans", "tri", "ultra", "un", "under", "vice", "well",
];

fn is_hyphen_prefix(word: &str) -> bool {
    HYPHEN_PREFIXES.binary_search(&word).is_ok()
}

/// Counts intra-line words and hyphenated pairs across the same PDF so that a
/// compound or closed word seen elsewhere in the document decides a line break
/// in any Latin-script language.
struct DocWordCounts {
    plain: HashMap<String, usize>,
    hyphenated: HashMap<String, usize>,
}

impl DocWordCounts {
    fn from_lines(lines: &[&str]) -> Self {
        let mut plain: HashMap<String, usize> = HashMap::new();
        let mut hyphenated: HashMap<String, usize> = HashMap::new();
        for &raw_line in lines {
            let line = raw_line.trim();
            if line.is_empty() || parse_page_marker_line(line).is_some() {
                continue;
            }
            for token in line.split_whitespace() {
                // Skip tokens ending in a trailing line-break hyphen or soft hyphen.
                if token.ends_with(['-', '\u{2010}', '\u{ad}']) {
                    continue;
                }
                let clean_token = token.replace('\u{ad}', "");
                let trimmed = clean_token.trim_matches(|ch: char| !ch.is_alphabetic());
                if trimmed.is_empty() {
                    continue;
                }
                if trimmed.contains(['-', '\u{2010}', '\u{2011}']) {
                    // Keep every split segment in order so non-alphabetic gaps
                    // (like `gar-2-dener`) never create fake adjacent pairs.
                    let parts: Vec<&str> = trimmed.split(['-', '\u{2010}', '\u{2011}']).collect();
                    for pair in parts.windows(2) {
                        if !pair[0].is_empty()
                            && !pair[1].is_empty()
                            && pair[0].chars().all(char::is_alphabetic)
                            && pair[1].chars().all(char::is_alphabetic)
                        {
                            *hyphenated
                                .entry(format!("{}-{}", pair[0].to_lowercase(), pair[1].to_lowercase()))
                                .or_insert(0) += 1;
                        }
                    }
                } else if trimmed.chars().all(char::is_alphabetic) {
                    *plain.entry(trimmed.to_lowercase()).or_insert(0) += 1;
                }
            }
        }
        Self { plain, hyphenated }
    }

    fn plain_count(&self, word: &str) -> usize {
        self.plain.get(word).copied().unwrap_or(0)
    }

    fn hyphenated_count(&self, pair: &str) -> usize {
        self.hyphenated.get(pair).copied().unwrap_or(0)
    }
}

/// Extract the trailing alphabetic segment immediately before the line-end
/// hyphen, plus whether that segment is itself preceded by an earlier hyphen
/// (as in `day-to-` or `like-with-`).
fn trailing_hyphen_left(previous: &str) -> (String, bool) {
    let without_hyphen = &previous[..previous.len() - previous.chars().next_back().unwrap().len_utf8()];
    let mut start = without_hyphen.len();
    for (idx, ch) in without_hyphen.char_indices().rev() {
        if ch.is_alphabetic() {
            start = idx;
        } else {
            break;
        }
    }
    let left = without_hyphen[start..].to_lowercase();
    let had_prior_hyphen = without_hyphen[..start]
        .chars()
        .next_back()
        .is_some_and(|ch| matches!(ch, '-' | '\u{2010}' | '\u{2011}'));
    (left, had_prior_hyphen)
}

fn leading_alpha_word(line: &str) -> String {
    line.chars()
        .take_while(|ch| ch.is_alphabetic())
        .collect::<String>()
        .to_lowercase()
}

fn keep_line_break_hyphen(
    previous: &str,
    line: &str,
    before_last: char,
    first_char: char,
    doc_words: &DocWordCounts,
) -> bool {
    // Digit ranges (`10-12`, `COVID-19`) and capitalised compounds (`North-South`)
    // always keep their hyphen.
    if !before_last.is_alphabetic() || !first_char.is_lowercase() {
        return true;
    }
    let (left, had_prior_hyphen) = trailing_hyphen_left(previous);
    let right = leading_alpha_word(line);
    if left.is_empty() || right.is_empty() {
        return true;
    }
    // Standard typesetting never breaks after a single letter (`e-mail`, `x-ray`).
    if left.chars().count() == 1 {
        return true;
    }

    let joined = format!("{left}{right}");
    let hyph = format!("{left}-{right}");
    let lexicon = &*ENGLISH_LEXICON;

    // 1. Multi-part compounds like `day-to-day`, `up-to-date`, `state-of-the-art`
    // where the trailing connector + word (`to` + `day`) would otherwise collide
    // with an unrelated closed word (`today`) elsewhere in the same document.
    if had_prior_hyphen
        && matches!(
            left.as_str(),
            "to" | "of" | "the" | "in" | "on" | "by" | "and" | "or" | "at" | "for" | "as"
                | "with" | "out" | "up" | "per" | "de" | "la" | "et"
        )
        && lexicon.is_common(&right)
    {
        return true;
    }

    // 2. Same-document self-evidence (works in every Latin-script language).
    let h_count = doc_words.hyphenated_count(&hyph);
    let j_count = doc_words.plain_count(&joined);
    if h_count > j_count {
        return true;
    }
    if j_count > 0 {
        return false;
    }

    // 3. Dictionary closed word beats compound split (`gardener`, `newspaper`,
    // `recently`, `prevent`, `anti-inflammatory`).
    if lexicon.is_word(&joined) {
        return false;
    }

    // 4. Two standalone English words or productive hyphen prefix + word
    // (`state-owned`, `well-known`, `anti-corruption`, `like-with-like`).
    (lexicon.is_common(&left) || is_hyphen_prefix(&left)) && lexicon.is_common(&right)
}

fn starts_new_paragraph(line: &str, current_paragraph: &[String]) -> bool {
    current_paragraph.is_empty()
        || line.chars().next().is_some_and(is_bullet_start)
        || NUMBERED_LIST_RE.is_match(line)
        || CHINESE_NUMBERED_LIST_RE.is_match(line)
        || CHAPTER_MARKER_RE.is_match(line)
        || line.starts_with("* ")
        || line.starts_with("- ")
        || line.starts_with("+ ")
        || line.starts_with("# ")
        || current_paragraph
            .last()
            .is_some_and(|previous| ends_with_sentence_punctuation(previous))
}

/// Chinese and Japanese text and punctuation, which take no space at a line break.
fn is_unspaced_script(ch: char) -> bool {
    matches!(ch as u32,
        0x2e80..=0x9fff      // radicals, CJK punctuation, kana, Han
        | 0xf900..=0xfaff    // compatibility ideographs
        | 0xfe30..=0xfe4f    // vertical punctuation forms
        | 0xff00..=0xffef    // full-width forms
        | 0x20000..=0x3ffff  // Han extensions
    )
}

/// Removes the page tags and returns where each tagged page starts, in characters.
fn take_page_offsets(tagged: &str) -> (String, Vec<(usize, usize)>) {
    let mut content = String::with_capacity(tagged.len());
    let mut offsets = Vec::new();
    let mut chars = 0;
    let mut end = 0;
    for found in PAGE_TAG_RE.captures_iter(tagged) {
        let whole = found.get(0).expect("match");
        let before = &tagged[end..whole.start()];
        content.push_str(before);
        chars += before.chars().count();
        if let Ok(page) = found[1].parse() {
            offsets.push((page, chars));
        }
        end = whole.end();
    }
    content.push_str(&tagged[end..]);
    (content, offsets)
}

/// Share of Latin-script words too long to be one word; 0 for short texts.
/// Uppercase-heavy tokens (part codes, acronyms) and nucleotide sequences
/// (DNA/RNA primers in any case) are ignored so technical PDFs are not
/// mistaken for missing word spaces.
fn long_word_share(text: &str) -> f64 {
    let mut words = 0usize;
    let mut long = 0usize;
    let mut total_latin_chars = 0usize;
    let mut long_latin_chars = 0usize;
    for word in LATIN_WORD_RE.find_iter(text) {
        let token = word.as_str();
        words += 1;
        let len = token.chars().count();
        total_latin_chars += len;
        if len >= LONG_WORD_LETTERS {
            let lower = token.chars().filter(|ch| ch.is_lowercase()).count();
            let is_nucleotide = token
                .bytes()
                .all(|b| matches!(b.to_ascii_lowercase(), b'a' | b'c' | b'g' | b't' | b'u' | b'n'));
            if lower * 2 >= len && !is_nucleotide {
                long += 1;
                long_latin_chars += len;
            }
        }
    }
    if words < MIN_WORDS_FOR_LONG_WORD_SHARE {
        // A page whose spaces were completely stripped produces very few tokens
        // (sometimes just one), so catch long unbroken runs by character share.
        if long > 0 && long_latin_chars >= 500 && long_latin_chars * 2 >= total_latin_chars {
            return 1.0;
        }
        return 0.0;
    }
    long as f64 / words as f64
}

fn is_bullet_start(ch: char) -> bool {
    matches!(
        ch,
        '•' | '◦'
            | '▪'
            | '▫'
            | '◈'
            | '◉'
            | '※'
            | '→'
            | '►'
            | '▶'
            | '■'
            | '□'
            | '●'
            | '○'
            | '★'
            | '☆'
            | '①'
            | '②'
            | '③'
            | '④'
            | '⑤'
            | '⑥'
            | '⑦'
            | '⑧'
            | '⑨'
            | '⑩'
    )
}

fn ends_with_sentence_punctuation(text: &str) -> bool {
    text.trim_end()
        .chars()
        .next_back()
        .is_some_and(|ch| matches!(ch, '。' | '！' | '？' | '.' | '!' | '?' | ':' | '：'))
}

fn needs_ocr(
    content: &str,
    raw_text: &str,
    page_count: Option<usize>,
    extraction_failed: bool,
) -> (bool, &'static str) {
    if extraction_failed {
        return (true, "pdf_text_extraction_failed");
    }
    if page_count == Some(0) {
        return (true, "zero_pages");
    }
    if content.trim().is_empty() {
        return (true, "empty_extraction");
    }
    let non_space = content
        .chars()
        .filter(|character| !character.is_whitespace())
        .count();
    let replacement = content
        .chars()
        .filter(|character| *character == '\u{fffd}')
        .count();
    // Control characters are counted in the raw text, since cleanup removes
    // them. A font with a broken character map fills the page with them.
    let control = raw_text
        .chars()
        .filter(|character| {
            character.is_control() && !matches!(*character, '\t' | '\n' | '\r' | '\x0c')
        })
        .count();
    if replacement.saturating_mul(100) >= non_space || control.saturating_mul(100) >= non_space {
        return (true, "corrupt_text_layer");
    }
    if let Some(page_count) = page_count.filter(|count| *count > 0) {
        let minimum = page_count.saturating_mul(MIN_TEXT_CHARS_PER_PAGE);
        if content.chars().count() < minimum {
            return (true, "sparse_text_layer");
        }
        // A scan whose only text is a stamp on every page has plenty of
        // characters, all of them in a few repeated lines.
        if page_count >= REPEATED_LINE_COUNT && unrepeated_chars(raw_text) < minimum {
            return (true, "sparse_text_layer");
        }
        // A text cover page in front of scanned pages can carry the whole
        // file over the minimum, so most pages must have text of their own.
        let pages_with_text = raw_text
            .split('\x0c')
            .filter(|page| {
                page.chars().filter(|ch| !ch.is_whitespace()).count() >= MIN_TEXT_CHARS_PER_PAGE
            })
            .count();
        if page_count >= MIN_PAGES_FOR_PAGE_SHARE && pages_with_text * 2 < page_count {
            return (true, "sparse_text_layer");
        }
    }
    (false, "usable_text_layer")
}

/// Characters in lines that occur fewer than REPEATED_LINE_COUNT times.
fn unrepeated_chars(raw_text: &str) -> usize {
    let mut seen: std::collections::HashMap<&str, (usize, usize)> = std::collections::HashMap::new();
    for line in raw_text.split(['\n', '\x0c']).map(str::trim) {
        if !line.is_empty() {
            let entry = seen.entry(line).or_insert((0, line.chars().count()));
            entry.0 += 1;
        }
    }
    seen.values()
        .filter(|(count, _)| *count < REPEATED_LINE_COUNT)
        .map(|(count, chars)| count * chars)
        .sum()
}

fn title_from_content_text(content: &str) -> Option<String> {
    content
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .map(|line| line.chars().take(240).collect())
}

fn title_from_raw_pdf_text(raw_text: &str) -> Option<String> {
    raw_text
        .split('\x0c')
        .flat_map(str::lines)
        .map(str::trim)
        .find(|line| !line.is_empty())
        .map(|line| line.chars().take(240).collect())
}

fn duration_millis(duration: Duration) -> u128 {
    duration.as_millis()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn smart_cleanup_joins_chinese_pdf_line_breaks() {
        let text = "--- Page 1 ---\n这是 一 个\n中文 测试\n第二段结束。\n第三段开始";

        let cleaned = smart_paragraph_cleanup(text);

        assert!(cleaned.contains("这是一个中文测试"));
        assert!(cleaned.contains("第二段结束。\n\n第三段开始"));
    }

    fn attempt_from(raw_text: &str, pages: usize) -> PdfExtractionAttempt {
        let extraction = PdfTextExtraction {
            raw_text: raw_text.to_string(),
            page_count: Some(pages),
            page_count_method: Some("test_page_count"),
            backend: "test",
            fallback_from: None,
            fallback_error: None,
        };
        process_pdf_extraction_result(Ok(extraction), Duration::ZERO)
    }

    #[test]
    fn line_breaks_join_with_a_space_unless_the_script_has_none() {
        let cleaned = smart_paragraph_cleanup("the parks office (north\n(branch), open daily,\nexcept Mondays");
        assert_eq!(cleaned, "the parks office (north (branch), open daily, except Mondays");
        assert_eq!(smart_paragraph_cleanup("公园每天\n开放，\n周一除外"), "公园每天开放，周一除外");
        assert_eq!(smart_paragraph_cleanup("公园 2024\n年开放"), "公园 2024年开放");
    }

    #[test]
    fn hyphenated_line_breaks_are_rejoined_and_compounds_keep_their_hyphen() {
        assert_eq!(smart_paragraph_cleanup("the gar-\ndener waters"), "the gardener waters");
        assert_eq!(smart_paragraph_cleanup("the news-\npaper reported"), "the newspaper reported");
        assert_eq!(smart_paragraph_cleanup("a state-\nowned enterprise"), "a state-owned enterprise");
        assert_eq!(smart_paragraph_cleanup("a well-\nknown policy"), "a well-known policy");
        assert_eq!(smart_paragraph_cleanup("an anti-\ncorruption drive"), "an anti-corruption drive");
        assert_eq!(smart_paragraph_cleanup("day-to-\nday operations"), "day-to-day operations");
        assert_eq!(smart_paragraph_cleanup("anti-inflam-\nmatory drugs"), "anti-inflammatory drugs");
        assert_eq!(smart_paragraph_cleanup("send an e-\nmail today"), "send an e-mail today");
        assert_eq!(smart_paragraph_cleanup("pages 10-\n12 and North-\nSouth and COVID-\n19"), "pages 10-12 and North-South and COVID-19");
        // Same-document self-evidence preserves novel or non-English compounds.
        assert_eq!(
            smart_paragraph_cleanup("Cross-strait trade grew.\nThe cross-\nstrait link held"),
            "Cross-strait trade grew.\n\nThe cross-strait link held"
        );
        assert_eq!(
            smart_paragraph_cleanup("C'est peut-être vrai.\nIl peut-\nêtre ici"),
            "C'est peut-être vrai.\n\nIl peut-être ici"
        );
        // Soft hyphens are always line-wrap marks.
        let attempt = attempt_from("the gar\u{ad}\ndener waters the ro\u{ad}ses", 1);
        assert_eq!(attempt.content, "the gardener waters the roses");
    }

    #[test]
    fn uppercase_sequences_do_not_trigger_words_run_together() {
        let dna = "ACGTTGGATGGACCAACATTGAGTAACCAC cccATAGAAGAACTGCCCAA and primer sequence ".repeat(60);
        let attempt = attempt_from(&dna, 1);
        assert_eq!(attempt.long_word_share, 0.0);
        assert!(!attempt.ocr_needed);
    }

    #[test]
    fn page_offsets_point_at_each_page_in_content() {
        let attempt = attempt_from("The park opens at\x0cnine. Dogs are welcome.\x0c\x0cBring water.", 4);
        assert_eq!(attempt.content, "The park opens at nine. Dogs are welcome.\nBring water.");
        let starts: Vec<(usize, String)> = attempt
            .page_offsets
            .iter()
            .map(|(page, offset)| (*page, attempt.content.chars().skip(*offset).take(5).collect()))
            .collect();
        assert_eq!(
            starts,
            vec![(1, "The p".to_string()), (2, "nine.".to_string()), (4, "Bring".to_string())]
        );
    }

    #[test]
    fn stamp_only_scan_requires_ocr() {
        let stamp = "Downloaded from the town library on 3 May. Terms of use apply to this copy.";
        let attempt = attempt_from(&[stamp; 6].join("\x0c"), 6);
        assert!(attempt.ocr_needed);
        assert_eq!(attempt.ocr_reason, "sparse_text_layer");
    }

    #[test]
    fn text_cover_on_scanned_pages_requires_ocr() {
        let cover = "The town library lends books, maps and seeds. ".repeat(20);
        let attempt = attempt_from(&format!("{cover}\x0c\x0c\x0c\x0c\x0c"), 6);
        assert!(attempt.ocr_needed);
        assert_eq!(attempt.ocr_reason, "sparse_text_layer");
    }

    #[test]
    fn ligatures_become_letters() {
        assert_eq!(attempt_from("the o\u{fb03}ce \u{fb01}nds a \u{fb02}ag", 1).content, "the office finds a flag");
    }

    #[test]
    fn control_character_text_layer_requires_ocr() {
        let attempt = attempt_from(&"The\u{1}park\u{2}opens\u{3}at nine every day. ".repeat(10), 1);
        assert!(attempt.ocr_needed);
        assert_eq!(attempt.ocr_reason, "corrupt_text_layer");
    }

    #[test]
    fn text_without_word_spaces_requires_ocr() {
        let attempt = attempt_from(&"theparkopensatnineeveryday and dogs are welcome here. ".repeat(60), 1);
        assert!(attempt.long_word_share > 0.1);
        assert!(attempt.ocr_needed);
        assert_eq!(attempt.ocr_reason, "words_run_together");
    }

    #[test]
    fn clean_text_removes_unexpected_pdf_control_characters() {
        assert_eq!(clean_text("Useful\u{0085} text\n\u{0001}Second line"), "Useful text\nSecond line");
    }

    #[test]
    fn missing_pdf_header_is_damaged_not_ocr() {
        let error = extract_pdf_bytes(b"not a pdf", Some("bad.pdf"), Some("application/pdf"))
            .unwrap_err();
        assert!(error.to_string().contains("missing %PDF header"));
    }

    #[test]
    fn malformed_pdf_with_header_returns_ocr_signal_metadata() {
        let extracted = extract_pdf_bytes(
            b"%PDF-1.4\nbroken",
            Some("bad.pdf"),
            Some("application/pdf"),
        )
        .unwrap();
        let content_extraction = extracted
            .extraction_metadata
            .get("content_extraction")
            .unwrap();

        assert_eq!(extracted.source_format, "pdf");
        assert_eq!(content_extraction["extractor_type"], "pdf");
        assert_eq!(content_extraction["extraction_failed"], true);
        assert_eq!(content_extraction["ocr_needed"], true);
        assert_eq!(content_extraction["requires_full_pdf_ocr"], true);
    }

    #[test]
    fn extracts_existing_sample_pdf() {
        let sample_path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../legacy-ingestor/src/content_extraction/tests/samples/sample.pdf");
        if !sample_path.exists() {
            return;
        }

        let bytes = std::fs::read(&sample_path).unwrap();
        let extracted =
            extract_pdf_bytes(&bytes, Some("samples/sample.pdf"), Some("application/pdf")).unwrap();
        let content_extraction = extracted
            .extraction_metadata
            .get("content_extraction")
            .unwrap();

        assert_eq!(extracted.source_format, "pdf");
        assert_eq!(extracted.title, "Sample PDF Document");
        assert!(extracted.content.contains("sample PDF document"));
        assert!(extracted.content.contains("This is page 2"));
        assert_eq!(content_extraction["page_count"], 2);
        assert_eq!(
            content_extraction["extraction_method"],
            "pdftotext_smart_paragraphs"
        );
        assert_eq!(content_extraction["pdf_text_backend_requested"], "pdftotext");
        assert_eq!(content_extraction["pdf_text_backend"], "pdftotext");
        assert_eq!(
            content_extraction["ocr_needed"],
            false,
            "reason={} chars={} sample={:?}",
            content_extraction["ocr_reason"],
            extracted.content.chars().count(),
            extracted.content.chars().take(500).collect::<String>()
        );
    }

    #[test]
    fn pdftotext_and_mupdf_backends_agree_on_fixture() {
        let bytes = std::fs::read(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../tests/assets/files/federalist_fixture.pdf"),
        )
        .unwrap();
        for backend in [PdfBackend::Pdftotext, PdfBackend::Mupdf] {
            let attempt = extract_pdf_text_attempt(&bytes, backend, None).unwrap();
            assert_eq!(attempt.backend, Some(backend.as_str()));
            assert_eq!(attempt.page_count, Some(1));
            assert!(attempt.content.contains("Federalist fixture"));
            assert!(!attempt.ocr_needed, "{backend:?}: {}", attempt.ocr_reason);
        }
    }

    #[test]
    fn text_rich_real_pdf_from_env_never_requires_ocr() {
        let Ok(path) = std::env::var("DOCTRAIL_TEXT_RICH_PDF_TEST") else {
            return;
        };
        let bytes = std::fs::read(path).unwrap();
        let extracted = extract_pdf_bytes(&bytes, None, Some("application/pdf")).unwrap();
        let content_extraction = &extracted.extraction_metadata["content_extraction"];

        assert!(extracted.content.chars().count() > 1_000_000);
        assert_eq!(content_extraction["pdf_text_backend"], "pdftotext");
        assert_eq!(
            content_extraction["ocr_needed"],
            false,
            "reason={} chars={} sample={:?}",
            content_extraction["ocr_reason"],
            extracted.content.chars().count(),
            extracted.content.chars().take(500).collect::<String>()
        );
        assert_eq!(content_extraction["ocr_reason"], "usable_text_layer");
    }

    #[test]
    fn replacement_char_pdf_text_requires_ocr() {
        let extraction = PdfTextExtraction {
            raw_text: "���corrupted pdf text layer���".repeat(20),
            page_count: Some(1),
            page_count_method: Some("test_page_count"),
            backend: "test",
            fallback_from: None,
            fallback_error: None,
        };

        let attempt = process_pdf_extraction_result(Ok(extraction), Duration::from_millis(123));

        assert!(attempt.ocr_needed);
        assert_eq!(attempt.ocr_reason, "corrupt_text_layer");
    }

    #[test]
    fn doi_only_image_text_layer_requires_ocr() {
        let text = "https://doi.org/10.1016/j.example.2024.123456";
        let (ocr_needed, reason) = needs_ocr(text, text, Some(1), false);

        assert!(ocr_needed);
        assert_eq!(reason, "sparse_text_layer");
    }

    #[test]
    fn one_percent_replacement_chars_require_ocr() {
        let content = format!("{}�", "readable".repeat(12));
        let (ocr_needed, reason) = needs_ocr(&content, &content, Some(1), false);

        assert!(ocr_needed);
        assert_eq!(reason, "corrupt_text_layer");
    }

    #[test]
    fn dna_sequences_in_any_case_do_not_trigger_long_word_share() {
        let mut words = vec!["primer"; 180];
        for _ in 0..30 {
            words.push("ACGTTGGATGGTTTCATCTTTCAAAAATCAG");
            words.push("ctaaaaatacaaaaaattagctggaagtggt");
            words.push("CCTCCtccttcagttgacatatgg");
        }
        let text = words.join(" ");
        assert_eq!(long_word_share(&text), 0.0);
    }

    #[test]
    fn completely_glued_passage_triggers_long_word_share() {
        let glued = "accordancewithhisobligationstothestate".repeat(30);
        assert_eq!(long_word_share(&glued), 1.0);
    }

    #[test]
    fn literal_page_heading_text_is_preserved_and_not_parsed_as_page_break() {
        let raw = "--- Page layout ---\nFirst page body.\n--- Page 98 ---\nStill page one.\x0cSecond page body.";
        let attempt = process_pdf_extraction_result(
            Ok(PdfTextExtraction {
                raw_text: raw.to_string(),
                page_count: Some(2),
                page_count_method: Some("test"),
                backend: "test",
                fallback_from: None,
                fallback_error: None,
            }),
            Duration::from_millis(1),
        );
        assert!(attempt.content.contains("--- Page layout ---"));
        assert!(attempt.content.contains("--- Page 98 ---"));
        let page_numbers: Vec<usize> = attempt.page_offsets.iter().map(|(p, _)| *p).collect();
        assert_eq!(page_numbers, vec![1, 2]);
    }

    #[test]
    fn cross_page_soft_hyphen_rejoins_cleanly() {
        let raw = "The gar\u{ad}\n\x0cdener planted trees.";
        let attempt = process_pdf_extraction_result(
            Ok(PdfTextExtraction {
                raw_text: raw.to_string(),
                page_count: Some(2),
                page_count_method: Some("test"),
                backend: "test",
                fallback_from: None,
                fallback_error: None,
            }),
            Duration::from_millis(1),
        );
        assert_eq!(attempt.content, "The gardener planted trees.");
        assert_eq!(attempt.page_offsets, vec![(1, 0), (2, 7)]);
    }

    #[test]
    fn multi_hyphen_chains_and_nonbreaking_hyphen_evidence_work() {
        let raw = "We spoke today about day-\nto-\nday policy and day-to-\nday life.\n\
                   See gar-2-dener and gar-\ndener.\n\
                   Les \u{00e9}tats\u{2011}unis et les \u{00e9}tats-\nunis.";
        let attempt = process_pdf_extraction_result(
            Ok(PdfTextExtraction {
                raw_text: raw.to_string(),
                page_count: Some(1),
                page_count_method: Some("test"),
                backend: "test",
                fallback_from: None,
                fallback_error: None,
            }),
            Duration::from_millis(1),
        );
        assert!(attempt.content.contains("day-to-day policy"), "{}", attempt.content);
        assert!(attempt.content.contains("day-to-day life"), "{}", attempt.content);
        assert!(attempt.content.contains("and gardener."), "{}", attempt.content);
        assert!(attempt.content.contains("les \u{00e9}tats-unis."), "{}", attempt.content);
    }

    fn minimal_pdf_with_lines(lines: &[&str]) -> Vec<u8> {
        let mut stream = String::from("BT /F1 12 Tf 72 720 Td 16 TL\n");
        for (i, line) in lines.iter().enumerate() {
            let escaped = line.replace('\\', "\\\\").replace('(', "\\(").replace(')', "\\)");
            if i == 0 {
                stream.push_str(&format!("({escaped}) Tj\n"));
            } else {
                stream.push_str(&format!("T* ({escaped}) Tj\n"));
            }
        }
        stream.push_str("ET\n");

        let objects = vec![
            "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n".to_string(),
            "2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n".to_string(),
            "3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n".to_string(),
            format!("4 0 obj\n<< /Length {} >>\nstream\n{stream}endstream\nendobj\n", stream.len()),
            "5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n".to_string(),
        ];
        let mut pdf = b"%PDF-1.4\n".to_vec();
        let mut offsets = Vec::new();
        for obj in &objects {
            offsets.push(pdf.len());
            pdf.extend_from_slice(obj.as_bytes());
        }
        let xref_pos = pdf.len();
        pdf.extend_from_slice(format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1).as_bytes());
        for off in offsets {
            pdf.extend_from_slice(format!("{off:010} 00000 n \n").as_bytes());
        }
        pdf.extend_from_slice(
            format!("trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n", objects.len() + 1)
                .as_bytes(),
        );
        pdf
    }

    #[test]
    fn real_pdf_preserves_hyphenated_compounds_on_both_backends() {
        let bytes = minimal_pdf_with_lines(&[
            "The state-",
            "owned areas are well-",
            "known across North-",
            "South routes for day-to-",
            "day travel, while the gar-",
            "dener works quietly.",
        ]);
        for backend in [PdfBackend::Pdftotext, PdfBackend::Mupdf] {
            let attempt = extract_pdf_text_attempt(&bytes, backend, None).unwrap();
            assert!(attempt.content.contains("state-owned"), "{backend:?}: {}", attempt.content);
            assert!(attempt.content.contains("well-known"), "{backend:?}: {}", attempt.content);
            assert!(attempt.content.contains("North-South"), "{backend:?}: {}", attempt.content);
            assert!(attempt.content.contains("day-to-day"), "{backend:?}: {}", attempt.content);
            assert!(attempt.content.contains("gardener"), "{backend:?}: {}", attempt.content);
        }
    }
}
