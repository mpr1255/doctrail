use anyhow::Context;
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::Mutex;
use std::time::{Duration, Instant};

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
    Mupdf,
}

struct PdfExtractionAttempt {
    raw_text: String,
    smart_content: String,
    content: String,
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
    let _guard = PDF_EXTRACTION_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
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

fn requested_pdf_backend() -> PdfBackend {
    PdfBackend::Mupdf
}

impl PdfBackend {
    fn as_str(self) -> &'static str {
        match self {
            PdfBackend::Mupdf => "mupdf",
        }
    }
}

fn extract_pdf_text_attempt(
    bytes: &[u8],
    _backend: PdfBackend,
) -> anyhow::Result<PdfExtractionAttempt> {
    let started_at = Instant::now();
    let result = match catch_unwind(AssertUnwindSafe(|| extract_with_mupdf(bytes))) {
        Ok(result) => result,
        Err(payload) => {
            let message = payload
                .downcast_ref::<&str>()
                .copied()
                .or_else(|| payload.downcast_ref::<String>().map(String::as_str))
                .unwrap_or("unknown panic");
            return Err(anyhow::anyhow!(
                "mupdf panicked on malformed pdf: {message}"
            ));
        }
    };
    Ok(process_pdf_extraction_result(result, started_at.elapsed()))
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
    let marked_raw_content = add_python_style_page_markers(&raw_text);
    let smart_content = smart_paragraph_cleanup(&marked_raw_content);
    let content = clean_text(&smart_content);
    let cleanup_duration = cleanup_started_at.elapsed();

    let language_started_at = Instant::now();
    let language_detection = detect_language(&content);
    let language_duration = language_started_at.elapsed();

    let extraction_failed = extraction.is_none();
    let page_count = extraction.as_ref().and_then(|value| value.page_count);
    let (mut ocr_needed, mut ocr_reason) =
        needs_ocr(&content, page_count, extraction_failed, &language_detection);
    if !ocr_needed && crate::mojibake_rejection(&content, "").is_some() {
        ocr_needed = true;
        ocr_reason = "mojibake_detected";
    }

    PdfExtractionAttempt {
        raw_text,
        smart_content,
        content,
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
        .map(str::trim)
        .filter(|line| !line.is_empty())
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

    for line in lines {
        let line = line.trim();
        if line.is_empty() {
            if !current_paragraph.is_empty() {
                paragraphs.push(current_paragraph.join(""));
                current_paragraph.clear();
            }
            continue;
        }

        if line.starts_with("--- Page") {
            continue;
        }

        let starts_new = starts_new_paragraph(line, &current_paragraph);
        if starts_new {
            if !current_paragraph.is_empty() {
                paragraphs.push(current_paragraph.join(""));
            }
            current_paragraph = vec![line.to_string()];
            continue;
        }

        if let Some(previous) = current_paragraph.last() {
            let last_char = previous.chars().next_back();
            let first_char = line.chars().next();
            if last_char.is_some_and(is_english_char_or_digit)
                && first_char.is_some_and(is_english_char_or_digit)
            {
                current_paragraph.push(format!(" {}", line));
            } else {
                current_paragraph.push(line.to_string());
            }
        } else {
            current_paragraph = vec![line.to_string()];
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

fn is_english_char_or_digit(ch: char) -> bool {
    (ch.is_ascii() && ch.is_alphabetic()) || ch.is_ascii_digit()
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
    page_count: Option<usize>,
    extraction_failed: bool,
    language_detection: &LanguageDetectionReport,
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
    if !language_detection.threshold_passed {
        return (true, "language_detection_threshold_failed");
    }
    (false, "language_detection_threshold_passed")
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

    #[test]
    fn invalid_pdf_returns_ocr_signal_metadata() {
        let extracted =
            extract_pdf_bytes(b"not a pdf", Some("bad.pdf"), Some("application/pdf")).unwrap();
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
            "mupdf_smart_paragraphs"
        );
        assert_eq!(content_extraction["pdf_text_backend_requested"], "mupdf");
        assert_eq!(content_extraction["pdf_text_backend"], "mupdf");
        assert_eq!(content_extraction["ocr_needed"], false);
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
        assert_eq!(attempt.ocr_reason, "mojibake_detected");
    }
}
