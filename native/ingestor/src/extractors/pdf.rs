use anyhow::Context;
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::io::Write;
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
static SOFT_HYPHEN_BREAK_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"\u{ad}[ \t]*\r?\n[ \t]*").expect("soft hyphen regex"));
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

pub(crate) fn extract_pdf_bytes(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
) -> anyhow::Result<super::types::GeneralExtractedDocument> {
    if !bytes[..bytes.len().min(1024)]
        .windows(5)
        .any(|window| window == b"%PDF-")
    {
        anyhow::bail!("damaged PDF: missing %PDF header in first 1024 bytes");
    }
    let total_started_at = Instant::now();

    let backend_request = requested_pdf_backend();
    let attempt = extract_pdf_text_attempt(bytes, backend_request)?;
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

/// pdftotext unless the caller set DOCTRAIL_PDF_BACKEND=mupdf.
fn requested_pdf_backend() -> PdfBackend {
    match std::env::var("DOCTRAIL_PDF_BACKEND").as_deref() {
        Ok("mupdf") => PdfBackend::Mupdf,
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
) -> anyhow::Result<PdfExtractionAttempt> {
    let started_at = Instant::now();
    if backend == PdfBackend::Mupdf {
        let result = extract_with_mupdf_guarded(bytes)?;
        return Ok(process_pdf_extraction_result(result, started_at.elapsed()));
    }
    // A missing binary is a setup error, not a damaged PDF: never mask it.
    let result = match extract_with_pdftotext(bytes) {
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

fn extract_with_pdftotext(bytes: &[u8]) -> anyhow::Result<PdfTextExtraction> {
    let program = std::env::var("DOCTRAIL_PDFTOTEXT").unwrap_or_else(|_| "pdftotext".to_string());
    let mut input = tempfile::NamedTempFile::new().context("creating pdftotext input file")?;
    input.write_all(bytes).context("writing pdftotext input file")?;
    let output = tempfile::NamedTempFile::new().context("creating pdftotext output file")?;
    let stderr = tempfile::NamedTempFile::new().context("creating pdftotext stderr file")?;
    let mut child = Command::new(&program)
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
    // A soft hyphen marks a word broken across lines: rejoin it, and drop the rest.
    let rejoined = SOFT_HYPHEN_BREAK_RE.replace_all(&raw_text, "");
    let mut cleaned_raw = String::with_capacity(rejoined.len());
    for ch in rejoined.chars() {
        // pdftotext keeps ligature glyphs, which hide a word from search.
        match ch {
            '\u{ad}' | PAGE_TAG_OPEN | PAGE_TAG_CLOSE => {}
            '\u{fb00}' => cleaned_raw.push_str("ff"),
            '\u{fb01}' => cleaned_raw.push_str("fi"),
            '\u{fb02}' => cleaned_raw.push_str("fl"),
            '\u{fb03}' => cleaned_raw.push_str("ffi"),
            '\u{fb04}' => cleaned_raw.push_str("ffl"),
            '\u{fb05}' | '\u{fb06}' => cleaned_raw.push_str("st"),
            other => cleaned_raw.push(other),
        }
    }
    let rejoined = cleaned_raw;
    let marked_raw_content = add_python_style_page_markers(&rejoined);
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

fn add_python_style_page_markers(raw_text: &str) -> String {
    raw_text
        .split('\x0c')
        .enumerate()
        .filter_map(|(index, page)| {
            let page = page.trim();
            (!page.is_empty()).then(|| format!("--- Page {} ---\n{}", index + 1, page))
        })
        .collect::<Vec<_>>()
        .join("\n\n")
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
    let mut paragraphs = Vec::new();
    let mut current_paragraph: Vec<String> = Vec::new();
    // Set at a page marker; the next line of text carries the page's tag.
    let mut page_start: Option<usize> = None;

    for line in lines {
        let line = line.trim();
        if line.is_empty() {
            if !current_paragraph.is_empty() {
                paragraphs.push(current_paragraph.join(""));
                current_paragraph.clear();
            }
            continue;
        }

        if let Some(rest) = line.strip_prefix("--- Page") {
            page_start = rest.trim_end_matches('-').trim().parse().ok();
            continue;
        }
        let tag = page_start
            .take()
            .map(|page| format!("{PAGE_TAG_OPEN}{page}{PAGE_TAG_CLOSE}"))
            .unwrap_or_default();

        if starts_new_paragraph(line, &current_paragraph) {
            if !current_paragraph.is_empty() {
                paragraphs.push(current_paragraph.join(""));
            }
            current_paragraph = vec![format!("{tag}{line}")];
            continue;
        }

        let previous = current_paragraph.last_mut().expect("paragraph has a line");
        let mut tail = previous.chars().rev();
        let last_char = tail.next();
        let before_last = tail.next();
        let first_char = line.chars().next();
        if matches!(last_char, Some('-' | '\u{2010}'))
            && before_last.is_some_and(char::is_alphabetic)
            && first_char.is_some_and(char::is_lowercase)
        {
            // A word hyphenated across the line break.
            previous.pop();
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
        paragraphs.push(current_paragraph.join(""));
    }

    paragraphs.join("\n\n")
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
fn long_word_share(text: &str) -> f64 {
    let mut words = 0usize;
    let mut long = 0usize;
    for word in LATIN_WORD_RE.find_iter(text) {
        words += 1;
        if word.as_str().chars().count() >= LONG_WORD_LETTERS {
            long += 1;
        }
    }
    if words < MIN_WORDS_FOR_LONG_WORD_SHARE {
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
    fn hyphenated_line_breaks_are_rejoined() {
        assert_eq!(smart_paragraph_cleanup("the gar-\ndener waters"), "the gardener waters");
        assert_eq!(smart_paragraph_cleanup("pages 10-\n12 and North-\nSouth"), "pages 10- 12 and North- South");
        let attempt = attempt_from("the gar\u{ad}\ndener waters the ro\u{ad}ses", 1);
        assert_eq!(attempt.content, "the gardener waters the roses");
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
            let attempt = extract_pdf_text_attempt(&bytes, backend).unwrap();
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
}
