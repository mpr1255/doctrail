use anyhow::{anyhow, Context, Result};
use base64::{engine::general_purpose, Engine as _};
use chardetng::EncodingDetector;
use encoding_rs::{Encoding, GBK, UTF_8};
use kuchikikiki::traits::*;
use mail_parser::{Encoding as MimeEncoding, Message, MessageParser, MessagePart, MimeHeaders};
use mimetype_detector::detect as detect_mime_type;
use once_cell::sync::Lazy;
use readabilityrs::{Readability, ReadabilityOptions};
use regex::{Captures, Regex};
use scraper::{ElementRef, Html, Selector};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::borrow::Cow;
use std::collections::BTreeSet;
use std::fs;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;
use std::time::{Duration, Instant};

mod extractors;
mod py;

use extractors::language::{detect_language, language_detection_metadata};
use extractors::types::GeneralExtractedDocument;

const MIN_CONTENT_LENGTH_TO_INDEX: usize = 100;
const HTML2TEXT_WIDTH: usize = 120;
const MIN_DOM_OVER_HTML2TEXT_CHARS: usize = 800;
const METADATA_TEXT_PREVIEW_CHARS: usize = 2_000;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ServerErrorTemplateRule {
    name: String,
    max_chars: usize,
    max_nonempty_lines: usize,
    #[serde(default)]
    titles: Vec<String>,
    #[serde(default)]
    error_phrases_any: Vec<String>,
    #[serde(default)]
    content_markers_all: Vec<String>,
    #[serde(default)]
    content_markers_any: Vec<String>,
    #[serde(default)]
    apply_during_fallback_selection: bool,
}

static UNICODE_ESCAPE_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"\\u([0-9a-fA-F]{4})").expect("unicode regex"));
static ARCHIVE_SNAPSHOT_TITLE_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)^snapshot\s+(?:of|for)\s+https?://\S+\s*$").expect("snapshot title regex")
});
static MARKDOWN_LINK_ONLY_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^\[[^\]]{1,80}\]\([^)]+\)$").expect("markdown link regex"));
static META_CHARSET_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r#"(?is)<meta\s+[^>]*(?:charset=["']?\s*([A-Za-z0-9._:-]+)|content=["'][^"']*charset=([A-Za-z0-9._:-]+))"#)
        .expect("meta charset regex")
});
static SERVER_ERROR_TEMPLATE_RULES: Lazy<Vec<ServerErrorTemplateRule>> =
    Lazy::new(load_server_error_template_rules);

pub fn validate_server_error_template_rules_at_startup() {
    Lazy::force(&SERVER_ERROR_TEMPLATE_RULES);
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HtmlKind {
    Auto,
    Html,
    Mhtml,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ExtractKind {
    Html,
    Mhtml,
    Image,
    Pdf,
    Document,
    Spreadsheet,
    Ebook,
    Text,
}

impl ExtractKind {
    fn as_str(self) -> &'static str {
        match self {
            ExtractKind::Html => "html",
            ExtractKind::Mhtml => "mhtml",
            ExtractKind::Image => "image",
            ExtractKind::Pdf => "pdf",
            ExtractKind::Document => "document",
            ExtractKind::Spreadsheet => "spreadsheet",
            ExtractKind::Ebook => "ebook",
            ExtractKind::Text => "text",
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct ExtractOptions<'a> {
    pub mime_type: Option<&'a str>,
    pub source_path: Option<&'a str>,
    pub kind: HtmlKind,
}

#[derive(Debug, Serialize)]
pub struct FastLaneDecision {
    pub is_html_fast_lane: bool,
    pub kind: Option<String>,
    pub reason: String,
    pub normalized_mime_type: Option<String>,
    pub normalized_extension: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ContentTypeDetection {
    pub mime_type: String,
    pub name: String,
    pub extension: String,
    pub kind: String,
    pub extract_kind: Option<String>,
    pub is_generic: bool,
}

#[derive(Debug, Serialize)]
pub struct ExtractedDocument {
    pub source_format: String,
    pub title: String,
    pub content: String,
    pub language: Option<String>,
    pub extraction_metadata: Value,
    pub timing_ms: Value,
    pub content_length: usize,
    pub html2text_length: usize,
    pub used_html2text_fallback: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExtractionFailureCategory {
    SkippedUnsupported,
    FallbackRequired,
    Failed,
}

impl ExtractionFailureCategory {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::SkippedUnsupported => "skipped_unsupported",
            Self::FallbackRequired => "fallback_required",
            Self::Failed => "failed",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ExtractionFailure {
    pub category: ExtractionFailureCategory,
    pub error_type: &'static str,
    pub message: String,
    pub fallback_required: bool,
    pub fallback_kind: Option<String>,
}

impl ExtractionFailure {
    pub fn extraction_status(&self) -> &'static str {
        self.category.as_str()
    }
}

#[derive(Debug)]
struct DomTextCandidate {
    text: String,
    score: f64,
    char_count: usize,
    link_density: f64,
    selector_name: String,
}

#[derive(Debug)]
struct HtmlDecodeResult<'a> {
    encoding_name: &'static str,
    decoded: Cow<'a, str>,
    source: &'static str,
    declared_encoding: Option<String>,
    had_errors: bool,
}

pub fn classify_html_candidate(
    mime_type: Option<&str>,
    source_path: Option<&str>,
) -> FastLaneDecision {
    let normalized_mime_type = mime_type
        .map(normalize_mime_type)
        .filter(|value| !value.is_empty());
    let normalized_extension = source_path.and_then(normalized_extension);

    if let Some(mime) = normalized_mime_type.as_deref() {
        if is_mhtml_mime(mime) {
            return FastLaneDecision {
                is_html_fast_lane: true,
                kind: Some("mhtml".to_string()),
                reason: "mime_type".to_string(),
                normalized_mime_type,
                normalized_extension,
            };
        }
        if is_html_mime(mime) {
            return FastLaneDecision {
                is_html_fast_lane: true,
                kind: Some("html".to_string()),
                reason: "mime_type".to_string(),
                normalized_mime_type,
                normalized_extension,
            };
        }
    }

    if let Some(ext) = normalized_extension.as_deref() {
        if matches!(ext, "mht" | "mhtml") {
            return FastLaneDecision {
                is_html_fast_lane: true,
                kind: Some("mhtml".to_string()),
                reason: "extension".to_string(),
                normalized_mime_type,
                normalized_extension,
            };
        }
        if matches!(ext, "html" | "htm" | "shtml" | "jhtml") {
            return FastLaneDecision {
                is_html_fast_lane: true,
                kind: Some("html".to_string()),
                reason: "extension".to_string(),
                normalized_mime_type,
                normalized_extension,
            };
        }
    }

    FastLaneDecision {
        is_html_fast_lane: false,
        kind: None,
        reason: "no_html_mime_or_extension".to_string(),
        normalized_mime_type,
        normalized_extension,
    }
}

pub fn extract_file(path: &Path, options: ExtractOptions<'_>) -> Result<ExtractedDocument> {
    let total_started_at = Instant::now();
    let read_started_at = Instant::now();
    let bytes = fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    let read_duration = read_started_at.elapsed();

    let resolve_started_at = Instant::now();
    let detection = detect_content_type(&bytes);
    let kind = resolve_extract_kind(options, &detection).ok_or_else(|| {
        anyhow!(
            "file is not a supported Rust extraction candidate: mime_type={:?} source_path={:?}",
            options.mime_type,
            options.source_path
        )
    })?;
    let resolve_duration = resolve_started_at.elapsed();

    let mut extracted = extract_by_kind(&bytes, options, kind)?;
    insert_content_type_detection(&mut extracted, options, &detection, kind);
    insert_timing(&mut extracted, "file_read", read_duration);
    insert_timing(&mut extracted, "kind_resolve", resolve_duration);
    insert_timing(
        &mut extracted,
        "extract_file_total",
        total_started_at.elapsed(),
    );
    Ok(extracted)
}

pub fn extract_bytes(bytes: &[u8], options: ExtractOptions<'_>) -> Result<ExtractedDocument> {
    let total_started_at = Instant::now();
    let resolve_started_at = Instant::now();
    let detection = detect_content_type(bytes);
    let kind = resolve_extract_kind(options, &detection).ok_or_else(|| {
        anyhow!(
            "file is not a supported Rust extraction candidate: mime_type={:?} source_path={:?}",
            options.mime_type,
            options.source_path
        )
    })?;
    let resolve_duration = resolve_started_at.elapsed();

    let mut extracted = extract_by_kind(bytes, options, kind)?;
    insert_content_type_detection(&mut extracted, options, &detection, kind);
    insert_timing(&mut extracted, "kind_resolve", resolve_duration);
    insert_timing(
        &mut extracted,
        "extract_bytes_total",
        total_started_at.elapsed(),
    );
    Ok(extracted)
}

pub fn decode_to_html(bytes: &[u8], options: ExtractOptions<'_>) -> Result<String> {
    let detection = detect_content_type(bytes);
    let kind = resolve_extract_kind(options, &detection).ok_or_else(|| {
        anyhow!(
            "file is not a supported Rust extraction candidate: mime_type={:?} source_path={:?}",
            options.mime_type,
            options.source_path
        )
    })?;

    match kind {
        ExtractKind::Html => {
            let stripped = strip_httrack_header(bytes);
            Ok(decode_html_bytes(stripped).decoded.into_owned())
        }
        ExtractKind::Mhtml => {
            let repaired_bytes = repair_mhtml_missing_part_separator(bytes);
            let message = MessageParser::default()
                .parse(repaired_bytes.as_ref())
                .ok_or_else(|| anyhow!("failed to parse MHTML/MIME message"))?;
            let (part, selected_body_type) = select_mhtml_body_part(&message).ok_or_else(|| {
                anyhow!("MHTML message has no HTML, XML, or plain-text body part")
            })?;
            let raw_message = message.raw_message.as_ref();
            let raw_body = raw_message
                .get(part.offset_body as usize..part.offset_end as usize)
                .ok_or_else(|| anyhow!("MHTML body offsets are invalid"))?;
            let part_bytes = decode_mime_transfer(raw_body, part.encoding)
                .context("decoding MHTML body transfer encoding")?;
            let part_charset = part
                .content_type()
                .and_then(|content_type| content_type.attribute("charset"));
            let decoded = decode_mhtml_body_bytes(
                &part_bytes,
                part_charset,
                selected_body_type,
            );
            Ok(mhtml_body_as_html(decoded.decoded.as_ref(), selected_body_type).into_owned())
        }
        other => anyhow::bail!(
            "file resolves to {}, not HTML/MHTML: mime_type={:?} source_path={:?}",
            other.as_str(),
            options.mime_type,
            options.source_path
        ),
    }
}

pub fn html2text_full_dump(html: &str) -> String {
    html2text_cleaned_from_html(html).0
}

pub fn classify_extraction_failure(error: &anyhow::Error) -> ExtractionFailure {
    let message = error_chain_message(error);
    let lower = message.to_ascii_lowercase();
    if let Some(fallback_kind) = fallback_kind_from_message(&message) {
        return ExtractionFailure {
            category: ExtractionFailureCategory::FallbackRequired,
            error_type: "content_extraction_failed",
            message,
            fallback_required: true,
            fallback_kind: Some(fallback_kind),
        };
    }

    if lower.contains("not a supported rust extraction candidate")
        || lower.contains("unsupported document format")
        || lower.contains("unsupported ebook format")
    {
        return ExtractionFailure {
            category: ExtractionFailureCategory::SkippedUnsupported,
            error_type: "unsupported_file_type",
            message,
            fallback_required: false,
            fallback_kind: None,
        };
    }

    ExtractionFailure {
        category: ExtractionFailureCategory::Failed,
        error_type: "content_extraction_failed",
        message,
        fallback_required: false,
        fallback_kind: None,
    }
}

fn error_chain_message(error: &anyhow::Error) -> String {
    error
        .chain()
        .map(ToString::to_string)
        .collect::<Vec<_>>()
        .join(": ")
}

fn fallback_kind_from_message(message: &str) -> Option<String> {
    let lower = message.to_ascii_lowercase();
    if let Some(index) = lower.find("fallback_required=") {
        let start = index + "fallback_required=".len();
        let value = message[start..]
            .split(|ch: char| ch.is_whitespace() || matches!(ch, ';' | ',' | ')' | '}'))
            .next()
            .unwrap_or_default()
            .trim_matches(|ch| matches!(ch, '"' | '\'' | '`' | ':' | '.'));
        if !value.is_empty() {
            return Some(value.to_string());
        }
    }

    if lower.contains("external_command_fallback_not_enabled") {
        for source_format in ["mobi", "azw3", "azw", "djvu", "rtf"] {
            if lower.contains(&format!("{source_format} extraction is not supported")) {
                return Some(format!("{source_format}_external_command"));
            }
        }
        return Some("external_command".to_string());
    }

    if lower.contains("legacy ppt extraction") && lower.contains("legacy fallback") {
        return Some("legacy_ppt_libreoffice_pdf_pdftotext".to_string());
    }

    None
}

fn extract_mhtml_bytes(bytes: &[u8], source_path: Option<&str>) -> Result<ExtractedDocument> {
    let parse_started_at = Instant::now();
    let repaired_bytes = repair_mhtml_missing_part_separator(bytes);
    let missing_part_separator_repaired = matches!(&repaired_bytes, Cow::Owned(_));
    let message = MessageParser::default()
        .parse(repaired_bytes.as_ref())
        .ok_or_else(|| anyhow!("failed to parse MHTML/MIME message"))?;
    let parse_duration = parse_started_at.elapsed();

    let body_started_at = Instant::now();
    let (part, selected_body_type) = select_mhtml_body_part(&message)
        .ok_or_else(|| anyhow!("MHTML message has no HTML, XML, or plain-text body part"))?;
    let raw_message = message.raw_message.as_ref();
    let raw_body = raw_message
        .get(part.offset_body as usize..part.offset_end as usize)
        .ok_or_else(|| anyhow!("MHTML body offsets are invalid"))?;
    let part_bytes = decode_mime_transfer(raw_body, part.encoding)
        .context("decoding MHTML body transfer encoding")?;
    let part_charset = part
        .content_type()
        .and_then(|content_type| content_type.attribute("charset"));
    let decoded = decode_mhtml_body_bytes(&part_bytes, part_charset, selected_body_type);
    let body_duration = body_started_at.elapsed();

    let body_html = mhtml_body_as_html(decoded.decoded.as_ref(), selected_body_type);
    let mut result = extract_html_string(&body_html, source_path, "mhtml")?;
    insert_timing(&mut result, "mhtml_parse", parse_duration);
    insert_timing(&mut result, "mhtml_body_select", body_duration);
    set_decode_metadata(&mut result, &decoded);
    result.extraction_metadata["content_extraction"]["extractor_type"] = json!("mhtml");
    result.extraction_metadata["content_extraction"]["source_format"] = json!("mhtml");
    result.extraction_metadata["content_extraction"]["mhtml_html_body_count"] =
        json!(message.html_body_count());
    result.extraction_metadata["content_extraction"]["mhtml_selected_body_type"] =
        json!(selected_body_type);
    result.extraction_metadata["content_extraction"]["mhtml_transfer_encoding"] =
        json!(format!("{:?}", part.encoding));
    result.extraction_metadata["content_extraction"]["mhtml_missing_part_separator_repaired"] =
        json!(missing_part_separator_repaired);
    if let Some(subject) = message.subject() {
        result.extraction_metadata["content_extraction"]["mhtml_subject"] = json!(subject);
        if result.title.is_empty() && !is_archive_snapshot_title(subject) {
            result.title = repair_title(subject);
        }
    }
    result.source_format = "mhtml".to_string();
    Ok(result)
}

fn repair_mhtml_missing_part_separator(bytes: &[u8]) -> Cow<'_, [u8]> {
    const CONTENT_LOCATION: &[u8] = b"\r\nContent-Location:";
    let mut insertions = Vec::new();
    let mut cursor = 0;

    while let Some(relative_start) = find_bytes(&bytes[cursor..], CONTENT_LOCATION) {
        let header_start = cursor + relative_start + CONTENT_LOCATION.len();
        let Some(relative_end) = find_bytes(&bytes[header_start..], b"\r\n") else {
            break;
        };
        let body_start = header_start + relative_end + 2;
        if bytes.get(body_start) == Some(&b'<') {
            insertions.push(body_start);
        }
        cursor = body_start;
    }

    if insertions.is_empty() {
        return Cow::Borrowed(bytes);
    }

    let mut repaired = Vec::with_capacity(bytes.len() + insertions.len() * 2);
    let mut copied_until = 0;
    for insertion in insertions {
        repaired.extend_from_slice(&bytes[copied_until..insertion]);
        repaired.extend_from_slice(b"\r\n");
        copied_until = insertion;
    }
    repaired.extend_from_slice(&bytes[copied_until..]);
    Cow::Owned(repaired)
}

fn select_mhtml_body_part<'a>(
    message: &'a Message<'a>,
) -> Option<(&'a MessagePart<'a>, &'static str)> {
    if let Some(part) = message.html_part(0) {
        if let Some(body_type) = mhtml_html_body_type(part).or_else(|| mhtml_plain_body_type(part))
        {
            return Some((part, body_type));
        }
    }

    if let Some(part) = message
        .parts
        .iter()
        .find_map(|part| mhtml_html_body_type(part).map(|body_type| (part, body_type)))
    {
        return Some(part);
    }

    let normal_body = message
        .parts
        .iter()
        .find_map(|part| mhtml_xml_body_type(part).map(|body_type| (part, body_type)))
        .or_else(|| {
            message
                .parts
                .iter()
                .find_map(|part| mhtml_plain_body_type(part).map(|body_type| (part, body_type)))
        });
    if normal_body.is_some() {
        return normal_body;
    }

    if message.parts.len() == 1 {
        let part = &message.parts[0];
        if mhtml_octet_stream_body_type(part).is_some() {
            return Some((part, "application/octet-stream-text"));
        }
    }
    None
}

fn mhtml_octet_stream_body_type(part: &MessagePart<'_>) -> Option<&'static str> {
    let content_type = part.content_type()?;
    (content_type
        .c_type
        .as_ref()
        .eq_ignore_ascii_case("application")
        && content_type
            .c_subtype
            .as_deref()
            .is_some_and(|subtype| subtype.eq_ignore_ascii_case("octet-stream")))
    .then_some("application/octet-stream-text")
}

fn mhtml_html_body_type(part: &MessagePart<'_>) -> Option<&'static str> {
    let content_type = part.content_type()?;
    let mime_type = content_type.c_type.as_ref();
    let subtype = content_type.c_subtype.as_deref().unwrap_or_default();

    if mime_type.eq_ignore_ascii_case("text") && subtype.eq_ignore_ascii_case("html") {
        return Some("text/html");
    }
    if mime_type.eq_ignore_ascii_case("application") && subtype.eq_ignore_ascii_case("xhtml+xml") {
        return Some("application/xhtml+xml");
    }
    None
}

fn mhtml_xml_body_type(part: &MessagePart<'_>) -> Option<&'static str> {
    if part
        .content_disposition()
        .is_some_and(|disposition| disposition.is_attachment())
    {
        return None;
    }

    let content_type = part.content_type()?;
    let mime_type = content_type.c_type.as_ref();
    let subtype = content_type.c_subtype.as_deref().unwrap_or_default();
    if mime_type.eq_ignore_ascii_case("text") && subtype.eq_ignore_ascii_case("xml") {
        return Some("text/xml");
    }
    if mime_type.eq_ignore_ascii_case("application") {
        if subtype.eq_ignore_ascii_case("xml") {
            return Some("application/xml");
        }
        if subtype.eq_ignore_ascii_case("xhtml+xml") {
            return Some("application/xhtml+xml");
        }
        if subtype.to_ascii_lowercase().ends_with("+xml") {
            return Some("application/*+xml");
        }
    }
    None
}

fn mhtml_plain_body_type(part: &MessagePart<'_>) -> Option<&'static str> {
    let content_type = part.content_type()?;
    let subtype = content_type.c_subtype.as_deref().unwrap_or_default();
    (content_type.c_type.as_ref().eq_ignore_ascii_case("text")
        && subtype.eq_ignore_ascii_case("plain"))
    .then_some("text/plain")
}

fn mhtml_body_as_html<'a>(decoded: &'a str, selected_body_type: &str) -> Cow<'a, str> {
    if !matches!(
        selected_body_type,
        "text/plain" | "application/octet-stream-text"
    ) {
        return Cow::Borrowed(decoded);
    }

    let mut html = String::with_capacity(decoded.len() + 37);
    html.push_str("<html><body><pre>");
    for character in decoded.chars() {
        match character {
            '&' => html.push_str("&amp;"),
            '<' => html.push_str("&lt;"),
            '>' => html.push_str("&gt;"),
            '"' => html.push_str("&quot;"),
            '\'' => html.push_str("&#39;"),
            _ => html.push(character),
        }
    }
    html.push_str("</pre></body></html>");
    Cow::Owned(html)
}

fn decode_mhtml_body_bytes<'a>(
    data: &'a [u8],
    hinted_encoding: Option<&str>,
    selected_body_type: &str,
) -> HtmlDecodeResult<'a> {
    let detected = decode_html_bytes_with_hint(data, hinted_encoding);
    if selected_body_type != "application/octet-stream-text" || hinted_encoding.is_some() {
        return detected;
    }

    let western_extended = detected
        .decoded
        .chars()
        .filter(|character| ('\u{00a0}'..='\u{00ff}').contains(character))
        .count();
    let (gbk_text, _, gbk_had_errors) = GBK.decode(data);
    let gbk_cjk = gbk_text
        .chars()
        .filter(|character| is_cjk_or_fullwidth_codepoint(*character as u32))
        .count();
    if !gbk_had_errors && western_extended >= 20 && gbk_cjk >= 20 {
        return HtmlDecodeResult {
            encoding_name: GBK.name(),
            decoded: gbk_text,
            source: "octet_stream_gbk_heuristic",
            declared_encoding: None,
            had_errors: false,
        };
    }
    detected
}

fn extract_html_bytes(bytes: &[u8], source_path: Option<&str>) -> Result<ExtractedDocument> {
    let strip_started_at = Instant::now();
    let stripped = strip_httrack_header(bytes);
    let strip_duration = strip_started_at.elapsed();
    let decode_started_at = Instant::now();
    let decoded = decode_html_bytes(stripped);
    let decode_duration = decode_started_at.elapsed();
    let mut result = extract_html_string(&decoded.decoded, source_path, "html")?;
    set_decode_metadata(&mut result, &decoded);
    insert_timing(&mut result, "httrack_strip", strip_duration);
    insert_timing(&mut result, "html_decode", decode_duration);
    Ok(result)
}

/// Structural limits that keep the HTML parsers (kuchikikiki, scraper/html5ever,
/// readabilityrs) out of their super-linear / stack-overflow regimes. Fuzzing
/// found that a few-thousand-deep nest already overflows the parser recursion on
/// a 2 MB thread stack and SIGABRTs the whole process (which `catch_unwind`
/// cannot intercept), and ~200k attributes on one tag hangs html5ever's O(n²)
/// attribute de-duplication. These are DEPTH/attribute pathologies and are the
/// only ones separable by a cheap pre-parse scan.
///
/// BREADTH pathologies (e.g. ~20k unclosed table rows) are NOT gated here: real
/// pages legitimately reach ~33k tags / ~33k table cells in the same sample, so
/// any element/cell cap would false-reject real content. Breadth hangs are
/// bounded instead by the wall-clock deadline in `extract_html_string`.
/// Nesting-depth ceiling for the structural gate. Chosen to sit far above real
/// pages — including malformed-but-parseable ones (broken rich-text editors emit
/// hundreds of unclosed `<span>`; the deepest real page seen at 100k scale nests
/// a few hundred) — while staying far below the depth that overflows the 64 MB
/// extraction-thread stack (empirically many tens of thousands). Override with
/// DOCTRAIL_INGEST_MAX_NESTING_DEPTH.
const DEFAULT_MAX_NESTING_DEPTH: i64 = 8_192;
const MAX_ATTRS_PER_TAG: usize = 2_000;

fn positive_i64(value: Option<&str>, default: i64) -> i64 {
    value
        .and_then(|value| value.trim().parse::<i64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(default)
}

fn max_nesting_depth() -> i64 {
    let value = std::env::var("DOCTRAIL_INGEST_MAX_NESTING_DEPTH").ok();
    positive_i64(value.as_deref(), DEFAULT_MAX_NESTING_DEPTH)
}

/// Ceiling on extracted-content length before insert. Real articles are tiny
/// (p99.9 ≈ 34k chars, p99.99 ≈ 510k across a 100k-doc corpus); anything past a
/// couple million chars is readability failing to isolate the article and keeping
/// the whole page (observed 14–18 MB whole-page ad/nav dumps). Storing those
/// pollutes the dedup graph, so they are rejected as terminal errors rather than
/// inserted. Two orders of magnitude above real content, so it cannot false-reject
/// an article. Override with DOCTRAIL_INGEST_MAX_CONTENT_CHARS.
const DEFAULT_MAX_CONTENT_CHARS: usize = 2_000_000;

fn positive_usize(value: Option<&str>, default: usize) -> usize {
    value
        .and_then(|value| value.trim().parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(default)
}

pub fn max_content_chars() -> usize {
    let value = std::env::var("DOCTRAIL_INGEST_MAX_CONTENT_CHARS").ok();
    positive_usize(value.as_deref(), DEFAULT_MAX_CONTENT_CHARS)
}

pub fn content_quality_rejection(content: &str, title: &str) -> Option<String> {
    let content_chars = content.chars().count();
    if content_chars < MIN_CONTENT_LENGTH_TO_INDEX {
        return Some(format!(
            "content too short after extraction: {content_chars}"
        ));
    }

    let max_chars = max_content_chars();
    if content_chars > max_chars {
        return Some(format!(
            "content too long after extraction: {content_chars} chars exceeds {max_chars} \
             (whole-page dump, not an article)"
        ));
    }

    if let Some(reason) = server_error_template_rejection(content, title, false) {
        return Some(reason);
    }

    if let Some(reason) = low_value_content_rejection(content, title) {
        return Some(reason);
    }

    mojibake_rejection(content, title).map(|reason| format!("mojibake detected: {reason}"))
}

/// Stack for the extraction thread. Well above the 2 MB default so that any
/// depth that slips under `MAX_NESTING_DEPTH` still cannot overflow, and paired
/// with the wall-clock deadline that bounds runtime.
const EXTRACT_THREAD_STACK: usize = 64 * 1024 * 1024;

/// Default wall-clock deadline for a single document's parsing stages. Legitimate
/// pages finish in well under this (worst observed ~6 s on a 12 MB page);
/// super-linear hangs are cut here and logged as a terminal failure. Override
/// with DOCTRAIL_INGEST_TIMEOUT_MS.
const DEFAULT_EXTRACT_TIMEOUT_MS: u64 = 20_000;

fn positive_u64(value: Option<&str>, default: u64) -> u64 {
    value
        .and_then(|value| value.trim().parse::<u64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(default)
}

fn extract_timeout() -> Duration {
    let value = std::env::var("DOCTRAIL_INGEST_TIMEOUT_MS").ok();
    Duration::from_millis(positive_u64(value.as_deref(), DEFAULT_EXTRACT_TIMEOUT_MS))
}

/// Optional-close / structural-container elements (per the HTML spec's optional
/// end-tag rules) plus the document containers. In their normal container context
/// html5ever auto-closes these, so real pages keep them flat — thousands of
/// `<li>`, `<option>`, or `<tr><td>` — and counting them would false-reject long
/// lists, tables, and `<select>` menus. A few (rt, rp, optgroup) do nest in
/// html5ever when repeated BARE outside their container, so skipping them is not
/// DOM-depth-faithful there; but real pages close them or keep them shallow, and
/// the alternative (counting) would false-reject the real flat case that a cheap
/// byte scanner cannot cheaply distinguish. We take the false-positive-safe side
/// and let the wall-clock deadline bound the rare bare-nested flood.
fn is_optional_close_or_container(name: &str) -> bool {
    matches!(
        name,
        "html"
            | "head"
            | "body"
            | "li"
            | "dd"
            | "dt"
            | "p"
            | "rt"
            | "rp"
            | "optgroup"
            | "option"
            | "colgroup"
            | "caption"
            | "thead"
            | "tbody"
            | "tfoot"
            | "tr"
            | "td"
            | "th"
    )
}

fn is_void_element(name: &str) -> bool {
    matches!(
        name,
        "area"
            | "base"
            | "br"
            | "col"
            | "embed"
            | "hr"
            | "img"
            | "input"
            | "link"
            | "meta"
            | "param"
            | "source"
            | "track"
            | "wbr"
    )
}

/// The parsing namespace an element lives in. html5ever acknowledges the
/// self-closing slash (making `<x/>` an empty leaf) only in foreign content; in
/// the HTML namespace the slash is ignored on every non-void element, so `<x/>`
/// still opens and nests. Tracking this is the only way to know whether a
/// self-closing tag is a leaf, because the same name behaves differently by
/// context: `<path/>` self-closes inside `<svg>` but opens in an HTML body.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Ns {
    Html,
    Svg,
    Math,
}

struct OpenEl {
    name: String,
    ns: Ns,
    /// Whether this element's children parse as HTML — true for all HTML elements
    /// and for the SVG/MathML integration points. Computed once at push time
    /// (annotation-xml needs its encoding attribute), so `current_ns` is O(1).
    hosts_html: bool,
}

/// Whether an element opened in namespace `ns` with name `name` hosts HTML
/// children (an integration point). SVG foreignObject/desc/title and the MathML
/// text integration points always do; MathML annotation-xml does only with an
/// `encoding` of text/html or application/xhtml+xml — `attr_region` is the tag's
/// attribute text, used only for that case.
fn element_hosts_html(ns: Ns, name: &str, attr_region: &str) -> bool {
    match ns {
        Ns::Html => true,
        Ns::Svg => matches!(name, "foreignobject" | "desc" | "title"),
        Ns::Math => {
            matches!(name, "mi" | "mo" | "mn" | "ms" | "mtext")
                || (name == "annotation-xml"
                    && tag_attributes(attr_region).iter().any(|(k, v)| {
                        k == "encoding"
                            && matches!(
                                v.trim().to_ascii_lowercase().as_str(),
                                "text/html" | "application/xhtml+xml"
                            )
                    }))
        }
    }
}

/// HTML element names that trigger a foreign-content breakout: inside an svg/math
/// subtree html5ever pops back to HTML for these, so they nest as HTML (the
/// HTML5 tree-construction "in foreign content" list). Names NOT here — genuine
/// SVG/MathML elements and custom elements — stay foreign and self-close as
/// leaves, exactly as the parser treats them. `font` is excluded because it only
/// breaks out with a color/face/size attribute (handled separately).
fn breaks_out_of_foreign_content(name: &str) -> bool {
    matches!(
        name,
        "b" | "big"
            | "blockquote"
            | "body"
            | "br"
            | "center"
            | "code"
            | "dd"
            | "div"
            | "dl"
            | "dt"
            | "em"
            | "embed"
            | "h1"
            | "h2"
            | "h3"
            | "h4"
            | "h5"
            | "h6"
            | "head"
            | "hr"
            | "i"
            | "img"
            | "li"
            | "listing"
            | "menu"
            | "meta"
            | "nobr"
            | "ol"
            | "p"
            | "pre"
            | "ruby"
            | "s"
            | "small"
            | "span"
            | "strong"
            | "strike"
            | "sub"
            | "sup"
            | "table"
            | "tt"
            | "u"
            | "ul"
            | "var"
    )
}

/// A `<font>` start tag breaks out of foreign content only when it carries a
/// color, face, or size attribute (matching html5ever); a bare `<font>` inside
/// SVG/MathML stays foreign. `attr_region` is the tag's attribute text.
fn font_breaks_out_of_foreign_content(attr_region: &str) -> bool {
    tag_attributes(attr_region)
        .iter()
        .any(|(k, _)| matches!(k.as_str(), "color" | "face" | "size"))
}

/// Minimal attribute name/value scan over a start tag's attribute region (the
/// text between the tag name and `>`). Names are lowercased; values are returned
/// raw. Not a full HTML attribute parser — just enough for the two spec checks
/// that need attributes (font breakout, annotation-xml encoding), and only ever
/// called for those rare element names.
fn tag_attributes(region: &str) -> Vec<(String, String)> {
    let b = region.as_bytes();
    let n = b.len();
    let mut i = 0usize;
    let mut out = Vec::new();
    while i < n {
        while i < n && (b[i].is_ascii_whitespace() || b[i] == b'/') {
            i += 1;
        }
        if i >= n || b[i] == b'>' {
            break;
        }
        let name_start = i;
        while i < n && b[i] != b'=' && b[i] != b'/' && b[i] != b'>' && !b[i].is_ascii_whitespace() {
            i += 1;
        }
        let name = region[name_start..i].to_ascii_lowercase();
        while i < n && b[i].is_ascii_whitespace() {
            i += 1;
        }
        let mut value = String::new();
        if i < n && b[i] == b'=' {
            i += 1;
            while i < n && b[i].is_ascii_whitespace() {
                i += 1;
            }
            if i < n && (b[i] == b'"' || b[i] == b'\'') {
                let quote = b[i];
                i += 1;
                let value_start = i;
                while i < n && b[i] != quote {
                    i += 1;
                }
                value = region[value_start..i.min(n)].to_string();
                if i < n {
                    i += 1;
                }
            } else {
                let value_start = i;
                while i < n && !b[i].is_ascii_whitespace() && b[i] != b'>' {
                    i += 1;
                }
                value = region[value_start..i].to_string();
            }
        }
        if !name.is_empty() {
            out.push((name, value));
        }
    }
    out
}

/// Case-insensitive byte search for `needle` in `haystack[start..]`.
fn find_ci(haystack: &[u8], start: usize, needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || start >= haystack.len() {
        return None;
    }
    let last = haystack.len().saturating_sub(needle.len());
    (start..=last).find(|&i| {
        haystack[i..i + needle.len()]
            .iter()
            .zip(needle)
            .all(|(a, b)| a.eq_ignore_ascii_case(b))
    })
}

/// Reject markup pathological enough to overflow the parser recursion, in one
/// single byte scan run before any parser is invoked. Tracks actual DOM nesting
/// depth via a stack of currently-open elements that require an explicit close
/// tag; void and optional-close/container elements (li/tr/td/p/…) never count.
/// An end tag pops the nearest matching open element (and its unclosed
/// descendants); an UNMATCHED end tag is ignored, exactly as the HTML parser
/// treats it — so `<div></b><div></b>…` cannot fake a shallow depth. Thus
/// `<div>a<div>a…` (genuinely 100k deep even with text between opens) is caught,
/// while a flat list/table of thousands of `<li>`/`<tr><td>` stays at depth ~1.
/// Also caps attributes on a single tag. The stack is bounded by
/// MAX_NESTING_DEPTH, so it never grows large and per-close scans stay cheap.
/// Breadth pathologies are handled by the deadline, not here.
fn pathological_markup_reason(html: &str) -> Option<String> {
    let bytes = html.as_bytes();
    let n = bytes.len();
    let mut i = 0usize;
    let mut open: Vec<OpenEl> = Vec::new();
    let max_depth = max_nesting_depth();

    while i < n {
        if bytes[i] != b'<' {
            i += 1;
            continue;
        }
        if i + 1 >= n {
            break;
        }
        let c = bytes[i + 1];
        if c == b'!' || c == b'?' {
            // Comment / doctype / CDATA / processing instruction: skip to '>'.
            i += 2;
            while i < n && bytes[i] != b'>' {
                i += 1;
            }
            i += 1;
            continue;
        }
        let closing = c == b'/';
        let name_start = if closing { i + 2 } else { i + 1 };
        if name_start >= n {
            break;
        }
        if !bytes[name_start].is_ascii_alphabetic() {
            // A stray '<' or '</' that does not begin a real tag; treat as text.
            i += 1;
            continue;
        }
        let mut j = name_start;
        while j < n && (bytes[j].is_ascii_alphanumeric() || bytes[j] == b'-' || bytes[j] == b':') {
            j += 1;
        }
        let name = html[name_start..j].to_ascii_lowercase();
        // Scan to '>', counting attributes (meaningful only for opening tags) and
        // detecting self-closing. Quotes are honored so '>' inside a value does
        // not end the tag early. `region_start` marks the attribute text so the
        // rare font/annotation-xml checks can re-scan it.
        let region_start = j;
        let mut attrs = 0usize;
        let mut in_ws = true; // the tag name was just consumed
        let mut in_quote = 0u8;
        let mut last_non_ws = 0u8;
        while j < n {
            let b = bytes[j];
            if in_quote != 0 {
                if b == in_quote {
                    in_quote = 0;
                }
                last_non_ws = b;
                j += 1;
                continue;
            }
            if b == b'>' {
                break;
            }
            if (b == b'"' || b == b'\'') && last_non_ws == b'=' {
                // A quote only starts a quoted attribute value in value position
                // (right after '='). Elsewhere it is an ordinary character in an
                // unquoted value; treating every quote as a delimiter lets a stray
                // quote like `content=#FF0000"` desync the scan and swallow the
                // rest of the document.
                in_quote = b;
                last_non_ws = b;
            } else if b.is_ascii_whitespace() {
                in_ws = true;
            } else {
                if in_ws {
                    attrs += 1;
                    in_ws = false;
                }
                last_non_ws = b;
            }
            if !closing && attrs > MAX_ATTRS_PER_TAG {
                return Some(format!(
                    "tag <{name}> carries more than {MAX_ATTRS_PER_TAG} attributes"
                ));
            }
            j += 1;
        }
        let self_closing = last_non_ws == b'/';
        let attr_region = &html[region_start..j.min(n)];
        i = if j < n { j + 1 } else { j };

        if closing {
            // Pop the nearest matching open element and any unclosed descendants.
            // An unmatched end tag matches nothing and is ignored, so it cannot
            // decrement real nesting depth.
            if let Some(pos) = open.iter().rposition(|o| o.name == name) {
                open.truncate(pos);
            }
            continue;
        }

        // The namespace this start tag parses in. <svg>/<math> enter their foreign
        // namespace; an HTML breakout name inside foreign content pops back to
        // HTML; otherwise the tag inherits the current insertion context (which is
        // HTML at the top level, inside HTML elements, and at foreign integration
        // points).
        let context = open.last().map_or(
            Ns::Html,
            |top| if top.hosts_html { Ns::Html } else { top.ns },
        );
        let breaks_out = context != Ns::Html
            && (breaks_out_of_foreign_content(&name)
                || (name == "font" && font_breaks_out_of_foreign_content(attr_region)));
        let ns = if name == "svg" {
            Ns::Svg
        } else if name == "math" {
            Ns::Math
        } else if breaks_out {
            // Foreign-content breakout: html5ever pops the foreign (non-integration)
            // ancestors off the stack and reparses this tag as HTML, so everything
            // after it is HTML too. Mirror that pop, otherwise a later foreign-named
            // self-closing tag (`<svg><div></div><path/>...`) would still be scored
            // as a foreign leaf and hide its nesting. The pop is amortised O(1):
            // each element is pushed and popped at most once overall.
            while open
                .last()
                .is_some_and(|top| top.ns != Ns::Html && !top.hosts_html)
            {
                open.pop();
            }
            Ns::Html
        } else {
            context
        };

        // Raw-text elements skip their content so stray '<' cannot be miscounted —
        // but only in the HTML namespace, where they truly are raw text. In foreign
        // content <script>/<style> are ordinary elements, so skipping there would
        // let `<svg><style/>...` hide following markup.
        if ns == Ns::Html && (name == "script" || name == "style" || name == "textarea") {
            let close = format!("</{name}");
            i = find_ci(bytes, i, close.as_bytes()).unwrap_or(n);
            continue;
        }

        // Void and optional-close elements never change tracked nesting depth —
        // but only in the HTML namespace. In foreign content those same names
        // (`<svg><input>`, `<svg><li>`) are ordinary elements that nest, so the
        // skip must not apply there.
        if ns == Ns::Html && (is_void_element(&name) || is_optional_close_or_container(&name)) {
            continue;
        }

        // A trailing '/' makes a start tag a non-nesting leaf only where html5ever
        // acknowledges it: in foreign content. In the HTML namespace the slash is
        // ignored on non-void elements, so `<div/>`, `<path/>`, `<g/>`, custom
        // elements, and any element at an integration point or after a breakout
        // still OPEN and nest.
        if self_closing && ns != Ns::Html {
            continue;
        }
        let hosts_html = element_hosts_html(ns, &name, attr_region);
        open.push(OpenEl {
            name,
            ns,
            hosts_html,
        });
        if open.len() as i64 > max_depth {
            return Some(format!(
                "DOM nesting depth exceeds {max_depth} (pathological markup)"
            ));
        }
    }

    None
}

/// Extract from an HTML string with two safety layers the panic guard cannot
/// provide: a pre-parse structural gate that rejects depth/attribute pathologies
/// (which would otherwise SIGABRT the whole process on stack overflow), and a
/// wall-clock deadline enforced by running the parsers on a dedicated large-stack
/// thread (which bounds super-linear breadth hangs like unclosed table soup). A
/// panic, timeout, or thread failure becomes a terminal extraction error rather
/// than a wedged worker or an aborted process.
fn extract_html_string(
    html: &str,
    source_path: Option<&str>,
    source_format: &str,
) -> Result<ExtractedDocument> {
    let html_without_nuls = html.replace('\0', "");
    if let Some(reason) = pathological_markup_reason(&html_without_nuls) {
        anyhow::bail!("pathological markup: {reason}");
    }

    let timeout = extract_timeout();
    let bucket_owned = source_path.map(str::to_string);
    let format_owned = source_format.to_string();
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    std::thread::Builder::new()
        .name("html-extract".to_string())
        .stack_size(EXTRACT_THREAD_STACK)
        .spawn(move || {
            let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                extract_html_string_inner(
                    &html_without_nuls,
                    bucket_owned.as_deref(),
                    &format_owned,
                )
            }));
            let _ = tx.send(outcome);
        })
        .context("spawning extraction thread")?;
    match rx.recv_timeout(timeout) {
        Ok(Ok(result)) => result,
        Ok(Err(_panic)) => anyhow::bail!("extraction panicked"),
        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
            anyhow::bail!("extraction timed out after {}ms", timeout.as_millis())
        }
        Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
            anyhow::bail!("extraction thread disconnected before returning")
        }
    }
}

fn extract_html_string_inner(
    html: &str,
    source_path: Option<&str>,
    source_format: &str,
) -> Result<ExtractedDocument> {
    let total_started_at = Instant::now();
    let nuls_started_at = Instant::now();
    let html_without_nuls = html.replace('\0', "");
    let nuls_duration = nuls_started_at.elapsed();

    let boilerplate_started_at = Instant::now();
    let (mut readability_input_html, boilerplate_nodes_removed) =
        strip_known_boilerplate_html(&html_without_nuls);
    let boilerplate_duration = boilerplate_started_at.elapsed();

    let metadata_started_at = Instant::now();
    let basic = extract_basic_metadata(&html_without_nuls);
    let metadata_duration = metadata_started_at.elapsed();

    let mut html2text_cleaned = String::new();
    let mut html2text_error = None;
    let mut html2text_duration = Duration::ZERO;
    let mut html2text_evaluated = false;
    let mut boilerplate_strip_reverted = false;

    let dom_density_started_at = Instant::now();
    let mut dom_density_candidate = dom_density_fallback_text(&readability_input_html);
    if boilerplate_nodes_removed > 0 && dom_density_candidate.is_none() {
        let original_candidate = dom_density_fallback_text(&html_without_nuls);
        if original_candidate.is_some() {
            readability_input_html = html_without_nuls.clone();
            dom_density_candidate = original_candidate;
            boilerplate_strip_reverted = true;
        }
    }
    let dom_density_duration = dom_density_started_at.elapsed();

    let readability_started_at = Instant::now();
    let options = ReadabilityOptions::builder()
        .char_threshold(500)
        .nb_top_candidates(5)
        .keep_classes(false)
        .build();
    let article = Readability::new(&readability_input_html, None, Some(options))
        .ok()
        .and_then(|readability| readability.parse());
    let readability_duration = readability_started_at.elapsed();

    let content_started_at = Instant::now();
    let mut content = article
        .as_ref()
        .and_then(|article| article.text_content.as_deref())
        .map(clean_text)
        .unwrap_or_default();
    let mut title = first_meaningful_title(&basic)
        .or_else(|| {
            article
                .as_ref()
                .and_then(|article| article.title.as_deref())
                .map(repair_title)
                .filter(|title| !title.is_empty() && !is_archive_snapshot_title(title))
        })
        .unwrap_or_default();
    title = repair_title(&title);
    let mut used_html2text_fallback = false;
    let mut used_dom_density_fallback = false;
    let mut used_title_window_crop = false;
    let content_chars = content.chars().count();
    let mut html2text_chars = 0;
    let dom_density_chars = dom_density_candidate
        .as_ref()
        .map(|candidate| candidate.char_count)
        .unwrap_or_default();
    let readability_text_chars = content_chars;
    let mut fallback_reason = "readability".to_string();
    let allow_aggressive_fallback = allows_aggressive_html_fallback(source_format);
    let dom_density_fallback_candidate = dom_density_candidate
        .as_ref()
        .filter(|candidate| !is_definitely_cruft_fallback_candidate(&candidate.text, &title));
    let readability_cruft = allow_aggressive_fallback
        && content_chars >= MIN_CONTENT_LENGTH_TO_INDEX
        && is_definitely_cruft_fallback_candidate(&content, &title);
    let should_use_dom_density = should_use_dom_density_fallback(
        content_chars,
        dom_density_fallback_candidate,
        allow_aggressive_fallback,
    );
    let should_probe_html2text = should_probe_html2text_fallback(
        content_chars,
        allow_aggressive_fallback,
        readability_cruft,
    );
    let defer_small_dom_for_html2text = should_use_dom_density
        && should_probe_html2text
        && dom_density_fallback_candidate
            .map(should_defer_small_dom_candidate_for_html2text)
            .unwrap_or(false);
    if should_use_dom_density && !defer_small_dom_for_html2text {
        if let Some(candidate) = dom_density_fallback_candidate {
            content = candidate.text.clone();
            used_dom_density_fallback = true;
            fallback_reason = "dom_density_short_readability".to_string();
        }
    } else if should_probe_html2text {
        let html2text_started_at = Instant::now();
        let (cleaned, error) = html2text_cleaned_from_html(&readability_input_html);
        html2text_duration = html2text_started_at.elapsed();
        html2text_chars = cleaned.chars().count();
        html2text_error = error;
        html2text_evaluated = true;
        html2text_cleaned = cleaned;
        let mut used_richer_fallback = false;
        if !is_definitely_cruft_fallback_candidate(&html2text_cleaned, &title)
            && should_use_html2text_fallback(
                content_chars,
                html2text_chars,
                allow_aggressive_fallback,
                readability_cruft,
            )
        {
            if let Some(candidate) = dom_density_fallback_candidate.filter(|candidate| {
                should_use_dom_density_over_html2text_fallback(
                    html2text_chars,
                    candidate,
                    allow_aggressive_fallback,
                )
            }) {
                content = candidate.text.clone();
                used_dom_density_fallback = true;
                fallback_reason = "dom_density_over_html2text".to_string();
                used_richer_fallback = true;
            } else {
                content = html2text_cleaned.clone();
                used_html2text_fallback = true;
                fallback_reason = if readability_cruft {
                    "html2text_cruft_readability".to_string()
                } else {
                    "html2text_short_readability".to_string()
                };
                used_richer_fallback = true;
            }
        }
        if !used_richer_fallback && should_use_dom_density {
            if let Some(candidate) = dom_density_fallback_candidate {
                content = candidate.text.clone();
                used_dom_density_fallback = true;
                fallback_reason = "dom_density_short_readability".to_string();
            }
        }
    }

    if title.is_empty() {
        title = title_from_content_text(&content).unwrap_or_default();
        title = repair_title(&title);
    }
    if (used_dom_density_fallback || used_html2text_fallback)
        && !used_title_window_crop
        && !title.is_empty()
    {
        if let Some(cropped) = crop_content_to_title_window(&content, &title) {
            content = cropped;
            used_title_window_crop = true;
        }
    }
    let content_duration = content_started_at.elapsed();

    let repair_started_at = Instant::now();
    let (content, unicode_escape_repaired) = repair_unicode_escapes(&content);
    let repair_duration = repair_started_at.elapsed();

    let language_started_at = Instant::now();
    let language_detection = detect_language(&content);
    let language = language_detection.language.clone();
    let language_duration = language_started_at.elapsed();

    let mut timing_ms = Map::new();
    timing_ms.insert(
        "nul_strip".to_string(),
        json!(duration_millis(nuls_duration)),
    );
    timing_ms.insert(
        "known_boilerplate_strip".to_string(),
        json!(duration_millis(boilerplate_duration)),
    );
    timing_ms.insert(
        "basic_metadata".to_string(),
        json!(duration_millis(metadata_duration)),
    );
    timing_ms.insert(
        "html2text".to_string(),
        json!(duration_millis(html2text_duration)),
    );
    timing_ms.insert(
        "dom_density_fallback".to_string(),
        json!(duration_millis(dom_density_duration)),
    );
    timing_ms.insert(
        "readability".to_string(),
        json!(duration_millis(readability_duration)),
    );
    timing_ms.insert(
        "content_title_select".to_string(),
        json!(duration_millis(content_duration)),
    );
    timing_ms.insert(
        "unicode_escape_repair".to_string(),
        json!(duration_millis(repair_duration)),
    );
    timing_ms.insert(
        "language_detect".to_string(),
        json!(duration_millis(language_duration)),
    );
    timing_ms.insert(
        "extract_html_string_total".to_string(),
        json!(duration_millis(total_started_at.elapsed())),
    );

    let mut content_extraction = Map::new();
    content_extraction.insert("extractor_type".to_string(), json!("readabilityrs"));
    content_extraction.insert("source_format".to_string(), json!(source_format));
    content_extraction.insert("content_length".to_string(), json!(content.chars().count()));
    content_extraction.insert(
        "readability_library".to_string(),
        json!("readabilityrs Rust Mozilla Readability port"),
    );
    let (html2text_preview, html2text_truncated) =
        text_preview(&html2text_cleaned, METADATA_TEXT_PREVIEW_CHARS);
    content_extraction.insert("html2text_preview".to_string(), json!(html2text_preview));
    content_extraction.insert(
        "html2text_truncated".to_string(),
        json!(html2text_truncated),
    );
    content_extraction.insert(
        "html2text_evaluated".to_string(),
        json!(html2text_evaluated),
    );
    content_extraction.insert(
        "readability_text_length".to_string(),
        json!(readability_text_chars),
    );
    content_extraction.insert("html2text_length".to_string(), json!(html2text_chars));
    if let Some(error) = html2text_error {
        content_extraction.insert("html2text_error".to_string(), json!(error));
    }
    content_extraction.insert(
        "used_html2text_fallback".to_string(),
        json!(used_html2text_fallback),
    );
    content_extraction.insert(
        "used_dom_density_fallback".to_string(),
        json!(used_dom_density_fallback),
    );
    content_extraction.insert(
        "used_title_window_crop".to_string(),
        json!(used_title_window_crop),
    );
    content_extraction.insert("fallback_reason".to_string(), json!(fallback_reason));
    content_extraction.insert("dom_density_length".to_string(), json!(dom_density_chars));
    if let Some(candidate) = dom_density_candidate.as_ref() {
        content_extraction.insert("dom_density_score".to_string(), json!(candidate.score));
        content_extraction.insert(
            "dom_density_link_density".to_string(),
            json!(candidate.link_density),
        );
        content_extraction.insert(
            "dom_density_selector".to_string(),
            json!(candidate.selector_name),
        );
    }
    content_extraction.insert(
        "unicode_escape_repaired".to_string(),
        json!(unicode_escape_repaired),
    );
    content_extraction.insert(
        "boilerplate_nodes_removed".to_string(),
        json!(boilerplate_nodes_removed),
    );
    content_extraction.insert(
        "boilerplate_strip_reverted".to_string(),
        json!(boilerplate_strip_reverted),
    );
    content_extraction.insert("timing_ms".to_string(), Value::Object(timing_ms.clone()));
    if let Some(source_path) = source_path {
        content_extraction.insert("original_bucket_path".to_string(), json!(source_path));
    }
    if let Some(article) = article {
        content_extraction.insert("title".to_string(), json!(article.title));
        content_extraction.insert("byline".to_string(), json!(article.byline));
        content_extraction.insert("excerpt".to_string(), json!(article.excerpt));
        content_extraction.insert("lang".to_string(), json!(article.lang));
        content_extraction.insert("site_name".to_string(), json!(article.site_name));
    }
    for (key, value) in basic {
        content_extraction.insert(key, value);
    }

    let extraction_metadata = json!({
        "file": {
            "size": null,
            "encoding": null,
        },
        "content_extraction": content_extraction,
        "title_extraction": {
            "method": if title.is_empty() { "none" } else { "metadata_or_content" },
        },
        "language_detection": language_detection_metadata(&language_detection),
    });

    Ok(ExtractedDocument {
        source_format: source_format.to_string(),
        title,
        content_length: content.chars().count(),
        html2text_length: html2text_chars,
        content,
        language,
        extraction_metadata,
        timing_ms: Value::Object(timing_ms),
        used_html2text_fallback,
    })
}

fn strip_known_boilerplate_html(html: &str) -> (String, usize) {
    let document = kuchikikiki::parse_html().one(html);
    let selectors = [
        ".navbox",
        ".navbox-styles",
        ".vertical-navbox",
        ".metadata",
        ".printfooter",
        ".catlinks",
        ".mw-hidden-catlinks",
        ".mw-footer",
        ".noprint",
        ".nomobile",
        ".sidebar",
        "script",
        "style",
        "noscript",
        ".portal",
        "#catlinks",
        "#footer",
        "#mw-navigation",
    ];
    let mut removed = 0;
    for selector in selectors {
        let Ok(matches) = document.select(selector) else {
            continue;
        };
        let nodes: Vec<_> = matches.map(|matched| matched.as_node().clone()).collect();
        removed += nodes.len();
        for node in nodes {
            node.detach();
        }
    }
    if removed == 0 {
        return (html.to_string(), 0);
    }
    let mut bytes = Vec::new();
    if document.serialize(&mut bytes).is_err() {
        return (html.to_string(), 0);
    }
    match String::from_utf8(bytes) {
        Ok(cleaned) => (cleaned, removed),
        Err(_) => (html.to_string(), 0),
    }
}

pub fn detect_content_type(bytes: &[u8]) -> ContentTypeDetection {
    let detected = detect_mime_type(bytes);
    let mhtml_signature = looks_like_mhtml_signature(bytes);
    let null_padded_text = looks_like_null_padded_legacy_text(bytes);
    let mime_type = if mhtml_signature {
        "multipart/related".to_string()
    } else if null_padded_text {
        "text/plain".to_string()
    } else {
        detected.mime().to_string()
    };
    let normalized_mime_type = normalize_mime_type(&mime_type);
    let extract_kind = detected_extract_kind(&normalized_mime_type)
        .or_else(|| detected_text_extract_kind(&normalized_mime_type))
        .map(ExtractKind::as_str)
        .map(str::to_string);

    ContentTypeDetection {
        mime_type,
        name: if mhtml_signature {
            "MHTML".to_string()
        } else if null_padded_text {
            "Null-padded legacy text".to_string()
        } else {
            detected.name().to_string()
        },
        extension: if mhtml_signature {
            "mhtml".to_string()
        } else if null_padded_text {
            "txt".to_string()
        } else {
            detected.extension().to_string()
        },
        kind: if mhtml_signature {
            "MHTML".to_string()
        } else if null_padded_text {
            "Text".to_string()
        } else {
            detected.kind().to_string()
        },
        extract_kind,
        is_generic: is_generic_detected_mime(&normalized_mime_type),
    }
}

fn resolve_declared_extract_kind(options: ExtractOptions<'_>) -> Option<ExtractKind> {
    match options.kind {
        HtmlKind::Html => Some(ExtractKind::Html),
        HtmlKind::Mhtml => Some(ExtractKind::Mhtml),
        HtmlKind::Auto => classify_extract_kind(options.mime_type, options.source_path),
    }
}

fn resolve_extract_kind(
    options: ExtractOptions<'_>,
    detection: &ContentTypeDetection,
) -> Option<ExtractKind> {
    let normalized_detected_mime = normalize_mime_type(&detection.mime_type);
    let declared_kind = resolve_declared_extract_kind(options);

    if !detection.is_generic {
        if let Some(kind) = detected_extract_kind(&normalized_detected_mime) {
            return Some(kind);
        }

        if let Some(kind) = detected_text_extract_kind(&normalized_detected_mime) {
            if declared_kind.is_none() || detection.name == "Null-padded legacy text" {
                return Some(kind);
            }
        }
    }

    declared_kind
}

fn classify_extract_kind(
    mime_type: Option<&str>,
    source_path: Option<&str>,
) -> Option<ExtractKind> {
    let normalized_mime_type = mime_type
        .map(normalize_mime_type)
        .filter(|value| !value.is_empty());
    let normalized_extension = source_path.and_then(normalized_extension);

    if let Some(mime) = normalized_mime_type.as_deref() {
        if is_mhtml_mime(mime) {
            return Some(ExtractKind::Mhtml);
        }
        if is_html_mime(mime) {
            return Some(ExtractKind::Html);
        }
        if is_pdf_mime(mime) {
            return Some(ExtractKind::Pdf);
        }
        if is_document_mime(mime) {
            return Some(ExtractKind::Document);
        }
        if is_spreadsheet_mime(mime) {
            return Some(ExtractKind::Spreadsheet);
        }
        if is_ebook_mime(mime) {
            return Some(ExtractKind::Ebook);
        }
    }

    let extension_kind = match normalized_extension.as_deref() {
        Some("mht" | "mhtml") => Some(ExtractKind::Mhtml),
        Some("html" | "htm" | "shtml" | "jhtml") => Some(ExtractKind::Html),
        Some("pdf") => Some(ExtractKind::Pdf),
        Some("doc" | "docx" | "docm" | "ppt" | "pptx" | "pptm") => Some(ExtractKind::Document),
        Some("csv" | "tsv" | "xls" | "xlsx" | "xlsm" | "xlsb" | "ods") => {
            Some(ExtractKind::Spreadsheet)
        }
        Some("epub" | "mobi" | "azw" | "azw3" | "djvu" | "djv" | "rtf") => Some(ExtractKind::Ebook),
        Some(
            "txt" | "text" | "md" | "markdown" | "json" | "jsonl" | "ndjson" | "xml" | "yaml"
            | "yml" | "log",
        ) => Some(ExtractKind::Text),
        _ => None,
    };
    if extension_kind.is_some() {
        return extension_kind;
    }

    if normalized_mime_type.as_deref().is_some_and(is_text_mime) {
        Some(ExtractKind::Text)
    } else {
        None
    }
}

fn extract_by_kind(
    bytes: &[u8],
    options: ExtractOptions<'_>,
    kind: ExtractKind,
) -> Result<ExtractedDocument> {
    match kind {
        ExtractKind::Html => extract_html_bytes(bytes, options.source_path),
        ExtractKind::Mhtml => extract_mhtml_bytes(bytes, options.source_path),
        ExtractKind::Image => anyhow::bail!(
            "detected image content requires OCR; fallback_required=configured_ocr_backend"
        ),
        ExtractKind::Pdf => {
            extractors::pdf::extract_pdf_bytes(bytes, options.source_path, options.mime_type)
                .map(general_to_extracted)
        }
        ExtractKind::Document => extractors::document::extract_document_bytes(
            bytes,
            options.source_path,
            options.mime_type,
        )
        .map(general_to_extracted),
        ExtractKind::Spreadsheet => extractors::spreadsheet::extract_spreadsheet_bytes(
            bytes,
            options.source_path,
            options.mime_type,
        )
        .map(general_to_extracted),
        ExtractKind::Ebook => {
            extractors::ebook::extract_ebook_bytes(bytes, options.source_path, options.mime_type)
                .map(general_to_extracted)
        }
        ExtractKind::Text => {
            let compact;
            let null_padded_legacy = looks_like_null_padded_legacy_text(bytes);
            let text_bytes = if null_padded_legacy {
                compact = bytes
                    .iter()
                    .copied()
                    .filter(|byte| *byte != 0)
                    .collect::<Vec<_>>();
                compact.as_slice()
            } else {
                bytes
            };
            let text_mime = if null_padded_legacy {
                Some("text/plain; charset=gb18030")
            } else {
                options.mime_type
            };
            let document = extractors::text::extract_text_bytes(
                text_bytes,
                options.source_path,
                text_mime,
            )?;
            if null_padded_legacy {
                let total = document.content.chars().count().max(1);
                let suspicious = document
                    .content
                    .chars()
                    .filter(|character| {
                        let codepoint = *character as u32;
                        *character == '\u{fffd}'
                            || (0xe000..=0xf8ff).contains(&codepoint)
                            || (character.is_control()
                                && !matches!(*character, '\n' | '\r' | '\t'))
                    })
                    .count();
                if suspicious * 100 >= total {
                    anyhow::bail!(
                        "corrupt null-padded legacy text: suspicious character ratio {:.2}%",
                        suspicious as f64 * 100.0 / total as f64
                    );
                }
            }
            Ok(general_to_extracted(document))
        }
    }
}

fn detected_extract_kind(normalized_mime_type: &str) -> Option<ExtractKind> {
    if is_mhtml_mime(normalized_mime_type) {
        return Some(ExtractKind::Mhtml);
    }
    if is_html_mime(normalized_mime_type) {
        return Some(ExtractKind::Html);
    }
    if normalized_mime_type.starts_with("image/") {
        return Some(ExtractKind::Image);
    }
    if is_pdf_mime(normalized_mime_type) {
        return Some(ExtractKind::Pdf);
    }
    if is_spreadsheet_mime(normalized_mime_type) {
        return Some(ExtractKind::Spreadsheet);
    }
    if is_document_mime(normalized_mime_type) {
        return Some(ExtractKind::Document);
    }
    if is_ebook_mime(normalized_mime_type) {
        return Some(ExtractKind::Ebook);
    }
    None
}

fn looks_like_mhtml_signature(bytes: &[u8]) -> bool {
    let prefix = &bytes[..bytes.len().min(65_536)];
    let lower = String::from_utf8_lossy(prefix).to_ascii_lowercase();
    lower.contains("mime-version:")
        && lower.contains("content-type: multipart/related")
        && (lower.contains("snapshot-content-location:") || lower.contains("content-location:"))
}

fn looks_like_null_padded_legacy_text(bytes: &[u8]) -> bool {
    if bytes.starts_with(&[0xff, 0xfe]) || bytes.starts_with(&[0xfe, 0xff]) {
        return false;
    }
    let sample = &bytes[..bytes.len().min(65_536)];
    if sample.len() < 256 || sample.iter().filter(|byte| **byte == 0).count() * 10 < sample.len() {
        return false;
    }
    let compact = sample
        .iter()
        .copied()
        .filter(|byte| *byte != 0)
        .collect::<Vec<_>>();
    let (decoded, _, _) = GBK.decode(&compact);
    let total = decoded.chars().count().max(1);
    let printable = decoded
        .chars()
        .filter(|character| {
            character.is_alphanumeric()
                || character.is_whitespace()
                || character.is_ascii_punctuation()
                || is_cjk_or_fullwidth_codepoint(*character as u32)
        })
        .count();
    let alphanumeric = decoded
        .chars()
        .filter(|character| character.is_alphanumeric())
        .count();
    let replacements = decoded
        .chars()
        .filter(|character| *character == '\u{fffd}')
        .count();
    let cjk = decoded
        .chars()
        .filter(|character| is_cjk_or_fullwidth_codepoint(*character as u32))
        .count();
    printable * 100 >= total * 85
        && alphanumeric * 100 >= total * 40
        && replacements * 100 < total
        && cjk * 10 >= total
}

fn detected_text_extract_kind(normalized_mime_type: &str) -> Option<ExtractKind> {
    if is_text_mime(normalized_mime_type) {
        Some(ExtractKind::Text)
    } else {
        None
    }
}

fn is_generic_detected_mime(normalized_mime_type: &str) -> bool {
    matches!(
        normalized_mime_type,
        "application/octet-stream"
            | "application/x-empty"
            | "application/zip"
            | "application/x-zip"
            | "application/x-zip-compressed"
            | "application/x-ole-storage"
            | "application/x-cfb"
    )
}

fn general_to_extracted(document: GeneralExtractedDocument) -> ExtractedDocument {
    ExtractedDocument {
        source_format: document.source_format,
        title: document.title,
        content: document.content,
        language: document.language,
        extraction_metadata: document.extraction_metadata,
        timing_ms: document.timing_ms,
        content_length: document.content_length,
        html2text_length: 0,
        used_html2text_fallback: false,
    }
}

fn normalize_mime_type(mime_type: &str) -> String {
    mime_type
        .split(';')
        .next()
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase()
}

fn is_html_mime(mime_type: &str) -> bool {
    matches!(mime_type, "text/html" | "application/xhtml+xml")
}

fn is_mhtml_mime(mime_type: &str) -> bool {
    matches!(
        mime_type,
        "application/x-mimearchive" | "message/rfc822" | "multipart/related"
    )
}

fn is_pdf_mime(mime_type: &str) -> bool {
    matches!(mime_type, "application/pdf" | "application/x-pdf")
}

fn is_document_mime(mime_type: &str) -> bool {
    matches!(
        mime_type,
        "application/msword"
            | "application/vnd.ms-word"
            | "application/vnd.ms-powerpoint"
            | "application/mspowerpoint"
            | "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            | "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            | "application/vnd.ms-word.document.macroenabled.12"
            | "application/vnd.ms-powerpoint.presentation.macroenabled.12"
    )
}

fn is_spreadsheet_mime(mime_type: &str) -> bool {
    matches!(
        mime_type,
        "text/csv"
            | "text/tab-separated-values"
            | "application/csv"
            | "application/vnd.ms-excel"
            | "application/msexcel"
            | "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            | "application/vnd.ms-excel.sheet.macroenabled.12"
            | "application/vnd.ms-excel.sheet.binary.macroenabled.12"
            | "application/vnd.oasis.opendocument.spreadsheet"
    )
}

fn is_ebook_mime(mime_type: &str) -> bool {
    matches!(
        mime_type,
        "application/epub+zip"
            | "application/x-mobipocket-ebook"
            | "application/vnd.amazon.ebook"
            | "image/vnd.djvu"
            | "image/x-djvu"
            | "application/rtf"
            | "text/rtf"
    )
}

fn is_text_mime(mime_type: &str) -> bool {
    mime_type.starts_with("text/")
        || matches!(
            mime_type,
            "application/json"
                | "application/jsonl"
                | "application/ld+json"
                | "application/xml"
                | "application/x-ndjson"
                | "application/x-yaml"
                | "application/yaml"
        )
}

fn normalized_extension(path: &str) -> Option<String> {
    let path = path.split(['?', '#']).next().unwrap_or(path);
    let filename = path.rsplit('/').next().unwrap_or(path).to_ascii_lowercase();
    let mut parts: Vec<&str> = filename.split('.').collect();
    if parts.len() < 2 {
        return None;
    }
    let mut ext = parts.pop().unwrap_or_default();
    if ext == "tmp" && parts.len() >= 2 {
        ext = parts.pop().unwrap_or_default();
    }
    if ext.is_empty() {
        None
    } else {
        Some(ext.to_string())
    }
}

fn strip_httrack_header(data: &[u8]) -> &[u8] {
    if matches!(data.first(), Some(b'<' | 0xef)) || data.starts_with(&[0xff, 0xfe]) {
        return data;
    }
    let markers: [&[u8]; 6] = [
        b"<!DOCTYPE",
        b"<!doctype",
        b"<html",
        b"<HTML",
        b"<head",
        b"<HEAD",
    ];
    let html_start = markers
        .iter()
        .filter_map(|marker| find_bytes(data, marker))
        .min();
    if let Some(pos) = html_start {
        if pos > 100 {
            return &data[pos..];
        }
    }
    data
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn decode_html_bytes(data: &[u8]) -> HtmlDecodeResult<'_> {
    decode_html_bytes_with_hint(data, None)
}

fn decode_html_bytes_with_hint<'a>(
    data: &'a [u8],
    hinted_encoding: Option<&str>,
) -> HtmlDecodeResult<'a> {
    if data.starts_with(&[0xef, 0xbb, 0xbf]) {
        let (text, _, had_errors) = UTF_8.decode(&data[3..]);
        return HtmlDecodeResult {
            encoding_name: UTF_8.name(),
            decoded: text,
            source: "bom",
            declared_encoding: Some("utf-8".to_string()),
            had_errors,
        };
    }
    if data.starts_with(&[0xff, 0xfe]) {
        let encoding = encoding_rs::UTF_16LE;
        let (text, _, had_errors) = encoding.decode(&data[2..]);
        return HtmlDecodeResult {
            encoding_name: encoding.name(),
            decoded: text,
            source: "bom",
            declared_encoding: Some("utf-16le".to_string()),
            had_errors,
        };
    }
    if data.starts_with(&[0xfe, 0xff]) {
        let encoding = encoding_rs::UTF_16BE;
        let (text, _, had_errors) = encoding.decode(&data[2..]);
        return HtmlDecodeResult {
            encoding_name: encoding.name(),
            decoded: text,
            source: "bom",
            declared_encoding: Some("utf-16be".to_string()),
            had_errors,
        };
    }

    let sniff = String::from_utf8_lossy(&data[..data.len().min(10_000)]);
    let meta_declared_encoding = META_CHARSET_RE
        .captures(&sniff)
        .and_then(|captures| captures.get(1).or_else(|| captures.get(2)))
        .map(|matched| matched.as_str().trim().to_string());
    let declared_source = if meta_declared_encoding.is_some() {
        "html_meta"
    } else {
        "mime_charset"
    };
    let declared_encoding = meta_declared_encoding.or_else(|| {
        hinted_encoding
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string)
    });
    if let Some(label) = declared_encoding.as_deref() {
        if let Some(encoding) = Encoding::for_label(label.as_bytes()) {
            let (text, _, had_errors) = encoding.decode(data);
            if let Some(utf8_text) =
                should_override_declared_with_utf8(data, &text, had_errors, encoding)
            {
                return HtmlDecodeResult {
                    encoding_name: UTF_8.name(),
                    decoded: utf8_text,
                    source: "utf8_override_declared",
                    declared_encoding,
                    had_errors: false,
                };
            }
            return HtmlDecodeResult {
                encoding_name: encoding.name(),
                decoded: text,
                source: declared_source,
                declared_encoding,
                had_errors,
            };
        }
    }

    if declared_encoding.is_none() {
        let (utf8_text, _, utf8_had_errors) = UTF_8.decode(data);
        if !utf8_had_errors {
            return HtmlDecodeResult {
                encoding_name: UTF_8.name(),
                decoded: utf8_text,
                source: "valid_utf8",
                declared_encoding,
                had_errors: false,
            };
        }
    }

    let mut detector = EncodingDetector::new();
    detector.feed(&data[..data.len().min(10_000)], true);
    let encoding = detector.guess(None, true);
    let (text, _, had_errors) = encoding.decode(data);
    HtmlDecodeResult {
        encoding_name: encoding.name(),
        decoded: text,
        source: "chardetng",
        declared_encoding,
        had_errors,
    }
}

fn set_decode_metadata(result: &mut ExtractedDocument, decoded: &HtmlDecodeResult<'_>) {
    result.extraction_metadata["file"]["encoding"] = json!(decoded.encoding_name);
    result.extraction_metadata["file"]["encoding_source"] = json!(decoded.source);
    result.extraction_metadata["file"]["declared_encoding"] = json!(decoded.declared_encoding);
    result.extraction_metadata["file"]["encoding_had_errors"] = json!(decoded.had_errors);
    result.extraction_metadata["content_extraction"]["encoding"] = json!(decoded.encoding_name);
    result.extraction_metadata["content_extraction"]["encoding_source"] = json!(decoded.source);
    result.extraction_metadata["content_extraction"]["declared_encoding"] =
        json!(decoded.declared_encoding);
    result.extraction_metadata["content_extraction"]["encoding_had_errors"] =
        json!(decoded.had_errors);
}

fn should_override_declared_with_utf8<'a>(
    data: &'a [u8],
    declared_text: &str,
    declared_had_errors: bool,
    declared_encoding: &'static Encoding,
) -> Option<Cow<'a, str>> {
    if declared_encoding == UTF_8 {
        return None;
    }

    let (utf8_text, _, utf8_had_errors) = UTF_8.decode(data);
    if utf8_had_errors {
        return None;
    }
    if declared_had_errors {
        return Some(utf8_text);
    }

    let mut detector = EncodingDetector::new();
    detector.feed(&data[..data.len().min(10_000)], true);
    if detector.guess(None, true) == UTF_8 {
        return Some(utf8_text);
    }

    let declared_score = decoded_text_badness(declared_text);
    let utf8_score = decoded_text_badness(&utf8_text);
    if declared_score >= utf8_score.saturating_add(20) {
        Some(utf8_text)
    } else {
        None
    }
}

fn decoded_text_badness(text: &str) -> usize {
    let replacement_chars = text.chars().filter(|char| *char == '\u{fffd}').count();
    let mojibake_markers = [
        "Ã", "Â", "â€", "â€™", "å", "æ", "ç", "è", "é", "娓", "鍙", "璇", "绔", "鏍", "濡", "缃",
        "寰", "妧",
    ]
    .iter()
    .map(|marker| text.matches(marker).count())
    .sum::<usize>();
    replacement_chars * 50 + mojibake_markers * 4
}

/// Unambiguous Latin-1/Windows-1252 misdecode signatures: what UTF-8 multibyte
/// sequences look like when the bytes are wrongly shown as Latin-1 (e.g. "é"
/// becomes "Ã©", curly quotes become "â€™"). These do not occur in correctly
/// decoded text, so a run of them is strong evidence of mojibake.
///
/// Every entry is at least two chars and is a genuine misdecode SHAPE, never a
/// standalone legitimate letter. In particular the bare "Ã" and the bare "â€"
/// prefix are deliberately excluded: "Ã" is a legitimate uppercase letter in
/// Portuguese/Vietnamese all-caps headings (false-positive rejections), and both
/// bare forms are substrings of the specific sequences below, which would double
/// count every real marker. The specific two-char forms retain full detection.
const STRONG_MOJIBAKE_MARKERS: [&str; 20] = [
    "Ã©", "Ã¨", "Ã¤", "Ã¶", "Ã¼", "Ã±", "Ã ", "Ã¡", "Ã³", "Ãº", "Ã\u{ad}", "Ã§", "â€™", "â€œ",
    "â€“", "â€”", "Â«", "Â»", "Â·", "Â ",
];

/// Decide whether extracted text is genuine mojibake that should be rejected
/// rather than indexed. Mirrors the intent of the Python ingestor's
/// `MOJIBAKE_DETECTED` gate (ftfy is_bad + significance + CJK/Cyrillic carve-out)
/// with conservative, corpus-calibrated thresholds so legitimate CJK content is
/// never rejected: on a 1000-doc real sample, 999 docs have zero markers.
///
/// Two families are handled:
/// 1. Latin-1/1252 misdecode ("Ã©…"): high precision, and guarded by a CJK
///    carve-out so a stray marker inside real Chinese text is ignored. Chinese
///    that was itself Latin-misdecoded turns INTO this Latin garbage (its CJK
///    ratio collapses), so the carve-out still lets that be rejected.
/// 2. U+FFFD replacement runs: language-agnostic decode failure.
///
/// GBK/Big5-misdecode-to-CJK garbage is deliberately NOT flagged here — it is
/// valid-CJK-looking and only reliably separable from real Chinese with n-gram
/// models (ftfy). The decode path (`should_override_declared_with_utf8`) is the
/// layer that prevents it upstream.
pub fn mojibake_rejection(content: &str, title: &str) -> Option<String> {
    let combined = if title.is_empty() {
        content.to_string()
    } else {
        format!("{content}\n{title}")
    };
    let non_space = combined.chars().filter(|c| !c.is_whitespace()).count();
    if non_space < 40 {
        return None;
    }
    let per_10k = |n: usize| (n as f64) / (non_space as f64 / 10_000.0);

    let strong: usize = STRONG_MOJIBAKE_MARKERS
        .iter()
        .map(|marker| combined.matches(marker).count())
        .sum();
    let replacement = combined.chars().filter(|c| *c == '\u{fffd}').count();

    let cjk = combined
        .chars()
        .filter(|c| is_cjk_or_fullwidth_codepoint(*c as u32))
        .count();
    let cjk_ratio = cjk as f64 / non_space as f64;

    if cjk_ratio < 0.30 && strong >= 3 && per_10k(strong) >= 2.0 {
        return Some(format!(
            "latin1 mojibake markers={strong} density_per_10k={:.1}",
            per_10k(strong)
        ));
    }
    if replacement >= 8 && per_10k(replacement) >= 5.0 {
        return Some(format!(
            "replacement-char corruption count={replacement} density_per_10k={:.1}",
            per_10k(replacement)
        ));
    }

    // Binary content (PDFs/images mislabeled as HTML) or a wrong charset applied
    // to real text decodes into a high fraction of C0/C1 control bytes, which
    // never occur in genuine extracted text. On the 100k corpus, real content
    // sits at ~0 control fraction while this garbage sits at ~0.8, with nothing
    // in between, and no CJK page (cjk_frac>0.3) ever exceeds it — so 0.10 is a
    // safe floor that rejects the garbage Python already declines without
    // touching real content (including the CJK that Python wrongly rejects).
    let control = combined
        .chars()
        .filter(|c| {
            let o = *c as u32;
            (o < 0x20 && *c != '\t' && *c != '\n' && *c != '\r') || (0x7f..=0x9f).contains(&o)
        })
        .count();
    if control as f64 / non_space as f64 >= 0.10 {
        return Some(format!(
            "binary/control-character content ratio={:.2}",
            control as f64 / non_space as f64
        ));
    }
    None
}

fn decode_mime_transfer(data: &[u8], encoding: MimeEncoding) -> Result<Vec<u8>> {
    match encoding {
        MimeEncoding::None => Ok(trim_mime_body(data).to_vec()),
        MimeEncoding::QuotedPrintable => Ok(decode_quoted_printable(trim_mime_body(data))),
        MimeEncoding::Base64 => {
            let cleaned = trim_mime_body(data)
                .iter()
                .copied()
                .filter(|byte| !byte.is_ascii_whitespace())
                .collect::<Vec<_>>();
            Ok(general_purpose::STANDARD.decode(cleaned)?)
        }
    }
}

fn trim_mime_body(data: &[u8]) -> &[u8] {
    data.strip_suffix(b"\r\n")
        .or_else(|| data.strip_suffix(b"\n"))
        .unwrap_or(data)
}

fn decode_quoted_printable(data: &[u8]) -> Vec<u8> {
    let mut output = Vec::with_capacity(data.len());
    let mut index = 0;
    while index < data.len() {
        if data[index] != b'=' {
            output.push(data[index]);
            index += 1;
            continue;
        }
        if data.get(index + 1..index + 3) == Some(b"\r\n") {
            index += 3;
            continue;
        }
        if data.get(index + 1) == Some(&b'\n') {
            index += 2;
            continue;
        }
        if let (Some(high), Some(low)) = (data.get(index + 1), data.get(index + 2)) {
            if let (Some(high), Some(low)) = (hex_value(*high), hex_value(*low)) {
                output.push(high * 16 + low);
                index += 3;
                continue;
            }
        }
        output.push(data[index]);
        index += 1;
    }
    output
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn clean_text(text: &str) -> String {
    text.lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn html2text_cleaned_from_html(html: &str) -> (String, Option<String>) {
    match catch_unwind(AssertUnwindSafe(|| {
        html2text::from_read(html.as_bytes(), HTML2TEXT_WIDTH)
    })) {
        Ok(Ok(text)) => (clean_html2text_rendered_text(&text), None),
        Ok(Err(error)) => (String::new(), Some(error.to_string())),
        Err(_) => (
            String::new(),
            Some("html2text panicked while rendering HTML".to_string()),
        ),
    }
}

fn clean_html2text_rendered_text(text: &str) -> String {
    text.lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !is_html2text_grid_noise_line(line))
        .collect::<Vec<_>>()
        .join("\n")
}

fn is_html2text_grid_noise_line(line: &str) -> bool {
    let mut non_space = 0usize;
    let mut graphic = 0usize;
    let mut letters_or_numbers = 0usize;
    for ch in line.chars() {
        if ch.is_whitespace() {
            continue;
        }
        non_space += 1;
        if ch.is_alphanumeric() || ('\u{3400}'..='\u{9fff}').contains(&ch) {
            letters_or_numbers += 1;
        }
        if matches!(
            ch,
            '─' | '━'
                | '│'
                | '┃'
                | '┌'
                | '┐'
                | '└'
                | '┘'
                | '├'
                | '┤'
                | '┬'
                | '┴'
                | '┼'
                | '╭'
                | '╮'
                | '╯'
                | '╰'
                | '═'
                | '║'
                | '╔'
                | '╗'
                | '╚'
                | '╝'
                | '╠'
                | '╣'
                | '╦'
                | '╩'
                | '╬'
                | '/'
                | '\\'
        ) {
            graphic += 1;
        }
    }
    non_space >= 20 && letters_or_numbers == 0 && graphic * 100 >= non_space * 80
}

fn text_preview(text: &str, max_chars: usize) -> (String, bool) {
    let mut preview = String::new();
    let mut truncated = false;
    for (index, ch) in text.chars().enumerate() {
        if index >= max_chars {
            truncated = true;
            break;
        }
        preview.push(ch);
    }
    (preview, truncated)
}

fn dom_density_fallback_text(html: &str) -> Option<DomTextCandidate> {
    let document = Html::parse_document(html);
    let candidate_selector =
        Selector::parse("article, main, section, div, td, table, body").ok()?;
    let link_selector = Selector::parse("a").ok()?;
    let mut best: Option<DomTextCandidate> = None;

    for element in document.select(&candidate_selector) {
        if is_low_value_container(&element) {
            continue;
        }
        let raw_text = element.text().collect::<Vec<_>>().join("\n");
        let text = clean_text(&raw_text);
        let char_count = text.chars().count();
        if char_count < MIN_CONTENT_LENGTH_TO_INDEX {
            continue;
        }

        let link_text = element
            .select(&link_selector)
            .flat_map(|link| link.text())
            .collect::<Vec<_>>()
            .join("\n");
        let link_chars = clean_text(&link_text).chars().count();
        let link_density = ratio(link_chars, char_count);
        if link_density > 0.45 && char_count < 3000 {
            continue;
        }

        let punctuation = text
            .chars()
            .filter(|ch| "。！？；，,.!?;:".contains(*ch))
            .count();
        let long_lines = text
            .lines()
            .filter(|line| line.chars().count() >= 20)
            .count();
        let tag_bonus = match element.value().name() {
            "article" => 600.0,
            "main" => 400.0,
            "section" => 150.0,
            "td" => 80.0,
            _ => 0.0,
        };
        let non_link_chars = char_count.saturating_sub(link_chars);
        let score = non_link_chars as f64
            + (punctuation as f64 * 18.0)
            + (long_lines as f64 * 35.0)
            + tag_bonus
            - (link_density * char_count as f64 * 1.4);
        let candidate = DomTextCandidate {
            text,
            score,
            char_count,
            link_density,
            selector_name: element.value().name().to_string(),
        };
        if best
            .as_ref()
            .map(|current| candidate.score > current.score)
            .unwrap_or(true)
        {
            best = Some(candidate);
        }
    }

    best
}

fn should_use_dom_density_fallback(
    readability_chars: usize,
    candidate: Option<&DomTextCandidate>,
    allow_aggressive_fallback: bool,
) -> bool {
    let Some(candidate) = candidate else {
        return false;
    };
    if candidate.char_count < MIN_CONTENT_LENGTH_TO_INDEX {
        return false;
    }
    if readability_chars < MIN_CONTENT_LENGTH_TO_INDEX {
        return true;
    }
    if !allow_aggressive_fallback {
        return false;
    }
    if readability_chars < 500 && candidate.char_count >= readability_chars.saturating_mul(2) {
        return true;
    }
    readability_chars < 1000
        && candidate.link_density <= 0.35
        && candidate.char_count >= readability_chars.saturating_mul(3)
}

fn allows_aggressive_html_fallback(source_format: &str) -> bool {
    matches!(source_format, "html" | "mhtml")
}

fn should_use_html2text_fallback(
    readability_chars: usize,
    html2text_chars: usize,
    allow_aggressive_fallback: bool,
    readability_cruft: bool,
) -> bool {
    if readability_chars < MIN_CONTENT_LENGTH_TO_INDEX {
        return html2text_chars >= MIN_CONTENT_LENGTH_TO_INDEX
            || html2text_chars > readability_chars.saturating_add(10);
    }
    if !allow_aggressive_fallback {
        return false;
    }
    if html2text_chars > 50_000 {
        return false;
    }
    if readability_cruft {
        return html2text_chars >= MIN_CONTENT_LENGTH_TO_INDEX;
    }
    if readability_chars < 300 {
        return html2text_chars >= readability_chars.saturating_mul(3);
    }
    readability_chars < 1000 && html2text_chars >= readability_chars.saturating_mul(4)
}

fn should_use_dom_density_over_html2text_fallback(
    html2text_chars: usize,
    candidate: &DomTextCandidate,
    allow_aggressive_fallback: bool,
) -> bool {
    if !allow_aggressive_fallback {
        return false;
    }
    if candidate.char_count < MIN_CONTENT_LENGTH_TO_INDEX || html2text_chars < 400 {
        return false;
    }
    if candidate.char_count < MIN_DOM_OVER_HTML2TEXT_CHARS {
        return false;
    }
    candidate.link_density <= 0.45
        && candidate.char_count <= 5_000
        && candidate.char_count.saturating_mul(3) <= html2text_chars
}

fn should_defer_small_dom_candidate_for_html2text(candidate: &DomTextCandidate) -> bool {
    candidate.char_count < MIN_DOM_OVER_HTML2TEXT_CHARS
}

fn should_probe_html2text_fallback(
    readability_chars: usize,
    allow_aggressive_fallback: bool,
    readability_cruft: bool,
) -> bool {
    if readability_cruft {
        return true;
    }
    readability_chars < MIN_CONTENT_LENGTH_TO_INDEX
        || (allow_aggressive_fallback && readability_chars < 1000)
}

fn is_definitely_cruft_fallback_candidate(content: &str, title: &str) -> bool {
    server_error_template_rejection(content, title, true).is_some()
        || low_value_content_rejection(content, title).is_some()
        || mojibake_rejection(content, title).is_some()
        || looks_like_script_or_form_dump(content)
}

fn is_low_value_container(element: &ElementRef<'_>) -> bool {
    let value = element.value();
    let attrs = [value.attr("id"), value.attr("class"), value.attr("role")]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>()
        .join(" ")
        .to_ascii_lowercase();
    [
        "nav",
        "menu",
        "footer",
        "header",
        "sidebar",
        "breadcrumb",
        "toolbar",
        "pagination",
        "comment",
        "copyright",
        "friendlink",
        "linklist",
    ]
    .iter()
    .any(|needle| attrs.contains(needle))
}

pub(crate) fn low_value_content_rejection(content: &str, title: &str) -> Option<String> {
    if looks_like_login_placeholder(content) {
        return Some("low-value extracted content: login placeholder".to_string());
    }
    if looks_like_http_error_template(content) {
        return Some("low-value extracted content: http error template".to_string());
    }
    if looks_like_navigation_shell(content, title) {
        return Some("low-value extracted content: navigation/list shell".to_string());
    }
    if looks_like_script_or_form_dump(content) {
        return Some("low-value extracted content: script/form dump".to_string());
    }
    None
}

fn looks_like_login_placeholder(content: &str) -> bool {
    let lower = content.to_ascii_lowercase();
    lower.contains("this is login.htm from the docs subdirectory")
        || (content.contains("会员登录")
            && content.contains("立即注册")
            && content.contains("400-810-9888"))
}

fn looks_like_script_or_form_dump(content: &str) -> bool {
    let mut script_lines = 0usize;
    let mut script_chars = 0usize;
    let mut meaningful_lines = 0usize;
    let mut first_script_line: Option<usize> = None;

    for (index, line) in content.lines().enumerate() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        meaningful_lines += 1;
        if is_script_like_text_line(trimmed) {
            script_lines += 1;
            script_chars += trimmed.chars().count();
            first_script_line.get_or_insert(index);
        }
    }

    if script_lines >= 5 && first_script_line.unwrap_or(usize::MAX) <= 8 {
        return true;
    }

    let content_chars = content.chars().filter(|ch| !ch.is_whitespace()).count();
    content_chars >= 500
        && script_lines >= 4
        && script_chars.saturating_mul(100) >= content_chars.saturating_mul(12)
        && meaningful_lines >= 8
}

fn is_script_like_text_line(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    lower.contains("function ")
        || lower.contains("document.")
        || lower.contains("document.write")
        || lower.contains("window.")
        || lower.contains("getelementbyid")
        || lower.contains(".ready(function")
        || lower.contains("eval(")
        || lower.contains("new regexp")
        || lower.contains("alert(")
        || lower.contains("return;")
        || lower.starts_with("var ")
        || lower.starts_with("if (")
        || lower.starts_with("if(")
        || lower.starts_with("for (")
        || lower.starts_with("for(")
        || lower.starts_with("$(")
        || lower.starts_with("});")
        || lower.starts_with("}")
}

fn looks_like_http_error_template(content: &str) -> bool {
    let lower = content.to_ascii_lowercase();
    (content.contains("HTTP 错误") && content.contains("Not Found"))
        || (content.contains("您要找的资源已被删除") && content.contains("详细错误信息"))
        || (lower.contains("http error") && lower.contains("not found"))
}

fn load_server_error_template_rules() -> Vec<ServerErrorTemplateRule> {
    let mut rules: Vec<ServerErrorTemplateRule> =
        serde_json::from_str(include_str!("../rules/server-error-templates.json"))
            .expect("valid server-error template rules JSON");
    validate_server_error_template_rules(&mut rules)
        .expect("valid server-error template rule definitions");
    rules
}

fn validate_server_error_template_rules(
    rules: &mut [ServerErrorTemplateRule],
) -> std::result::Result<(), String> {
    if rules.is_empty() {
        return Err("server-error template rules must not be empty".to_string());
    }

    let mut names = BTreeSet::new();
    for rule in rules {
        rule.name = rule.name.trim().to_string();
        normalize_server_error_rule_values(&mut rule.titles);
        normalize_server_error_rule_values(&mut rule.error_phrases_any);
        normalize_server_error_rule_values(&mut rule.content_markers_all);
        normalize_server_error_rule_values(&mut rule.content_markers_any);

        if rule.name.is_empty()
            || rule.max_chars == 0
            || rule.max_nonempty_lines == 0
            || !has_server_error_rule_values(&rule.error_phrases_any)
            || !server_error_rule_values_are_valid(&rule.titles)
            || !server_error_rule_values_are_valid(&rule.content_markers_all)
            || !server_error_rule_values_are_valid(&rule.content_markers_any)
            || (rule.content_markers_all.is_empty() && rule.content_markers_any.is_empty())
            || !names.insert(rule.name.clone())
        {
            return Err(format!("invalid server-error template rule: {}", rule.name));
        }
    }

    Ok(())
}

fn normalize_server_error_rule_values(values: &mut [String]) {
    for value in values {
        *value = value.trim().to_ascii_lowercase();
    }
}

fn has_server_error_rule_values(values: &[String]) -> bool {
    !values.is_empty() && server_error_rule_values_are_valid(values)
}

fn server_error_rule_values_are_valid(values: &[String]) -> bool {
    let mut seen = BTreeSet::new();
    values
        .iter()
        .all(|value| !value.is_empty() && seen.insert(value))
}

fn server_error_template_rejection(
    content: &str,
    title: &str,
    during_fallback_selection: bool,
) -> Option<String> {
    server_error_template_rejection_with_rules(
        &SERVER_ERROR_TEMPLATE_RULES,
        content,
        title,
        during_fallback_selection,
    )
}

fn server_error_template_rejection_with_rules(
    rules: &[ServerErrorTemplateRule],
    content: &str,
    title: &str,
    during_fallback_selection: bool,
) -> Option<String> {
    let content_chars = content.chars().count();
    let nonempty_lines = content
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .count();
    let title = title.trim().to_ascii_lowercase();

    if !rules.iter().any(|rule| {
        server_error_template_rule_metadata_matches(
            rule,
            content_chars,
            nonempty_lines,
            &title,
            during_fallback_selection,
        )
    }) {
        return None;
    }

    let content = content.to_ascii_lowercase();
    rules
        .iter()
        .find(|rule| {
            server_error_template_rule_metadata_matches(
                rule,
                content_chars,
                nonempty_lines,
                &title,
                during_fallback_selection,
            ) && rule
                .error_phrases_any
                .iter()
                .any(|phrase| content.contains(phrase))
                && rule
                    .content_markers_all
                    .iter()
                    .all(|marker| content.contains(marker))
                && (rule.content_markers_any.is_empty()
                    || rule
                        .content_markers_any
                        .iter()
                        .any(|marker| content.contains(marker)))
        })
        .map(|rule| {
            format!(
                "low-value extracted content: server error template ({})",
                rule.name
            )
        })
}

fn server_error_template_rule_metadata_matches(
    rule: &ServerErrorTemplateRule,
    content_chars: usize,
    nonempty_lines: usize,
    title: &str,
    during_fallback_selection: bool,
) -> bool {
    (!during_fallback_selection || rule.apply_during_fallback_selection)
        && content_chars <= rule.max_chars
        && nonempty_lines <= rule.max_nonempty_lines
        && (rule.titles.is_empty() || rule.titles.iter().any(|candidate| candidate == title))
}

fn looks_like_navigation_shell(content: &str, title: &str) -> bool {
    let lines: Vec<&str> = content
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect();
    if lines.len() < 8 {
        return false;
    }

    let title_key = compact_text_key(title);
    let mut low_value_lines = 0usize;
    let mut marker_hits = 0usize;
    let mut substantive_lines = 0usize;

    for line in &lines {
        if is_navigation_shell_line(line) {
            low_value_lines += 1;
            marker_hits += navigation_marker_hits(line);
            continue;
        }
        if is_substantive_content_line(line, &title_key) {
            substantive_lines += 1;
        }
    }

    marker_hits >= 8
        && substantive_lines <= 1
        && low_value_lines * 100 >= lines.len() * 60
        && content.chars().count() <= 12_000
}

fn is_navigation_shell_line(line: &str) -> bool {
    let trimmed = line.trim();
    MARKDOWN_LINK_ONLY_RE.is_match(trimmed)
        || navigation_marker_hits(trimmed) > 0
        || trimmed.starts_with("http://")
        || trimmed.starts_with("https://")
        || trimmed.starts_with("Copyright")
        || trimmed.starts_with("版权所有")
}

fn navigation_marker_hits(line: &str) -> usize {
    const MARKERS: [&str; 31] = [
        "首页",
        "政务公开",
        "政务服务",
        "政民互动",
        "走进",
        "联系我们",
        "网站地图",
        "友情链接",
        "登录",
        "注册",
        "无障碍",
        "长者模式",
        "上一条",
        "下一条",
        "上一篇",
        "下一篇",
        "更多",
        "打印",
        "关闭",
        "扫码",
        "公众号",
        "官方微博",
        "版权所有",
        "ICP备",
        "公网安备",
        "Copyright",
        "User login",
        "Register",
        "Home",
        "Sitemap",
        "Contact",
    ];
    MARKERS
        .iter()
        .filter(|marker| line.contains(**marker))
        .count()
}

fn is_substantive_content_line(line: &str, title_key: &str) -> bool {
    let key = compact_text_key(line);
    if key.is_empty() || (!title_key.is_empty() && key == title_key) {
        return false;
    }
    let chars = line.chars().count();
    if chars < 35 {
        return false;
    }
    let punctuation = line
        .chars()
        .filter(|ch| "。！？；，,.!?;:".contains(*ch))
        .count();
    punctuation >= 2 || chars >= 90
}

fn extract_basic_metadata(html: &str) -> Map<String, Value> {
    let document = Html::parse_document(html);
    let mut metadata = Map::new();

    if let Ok(selector) = Selector::parse("title") {
        if let Some(title) = document
            .select(&selector)
            .next()
            .map(|node| clean_text(&node.text().collect::<Vec<_>>().join(" ")))
            .filter(|title| !title.is_empty())
        {
            metadata.insert(
                "title".to_string(),
                json!(repair_title(&title.replace('\0', ""))),
            );
        }
    }

    let mut meta_tags = Map::new();
    if let Ok(selector) = Selector::parse("meta") {
        for meta in document.select(&selector) {
            let value = meta.value();
            let name = value
                .attr("name")
                .or_else(|| value.attr("property"))
                .or_else(|| value.attr("http-equiv"));
            if let (Some(name), Some(content)) = (name, value.attr("content")) {
                meta_tags.insert(
                    name.replace('\0', ""),
                    json!(repair_title(&content.replace('\0', ""))),
                );
            }
        }
    }
    if !meta_tags.is_empty() {
        metadata.insert("meta_tags".to_string(), Value::Object(meta_tags));
    }

    if let Ok(selector) = Selector::parse("a[href]") {
        let links = document.select(&selector).count();
        if links > 0 {
            metadata.insert("links_count".to_string(), json!(links));
        }
    }

    metadata
}

fn first_meaningful_title(metadata: &Map<String, Value>) -> Option<String> {
    let meta_tags = metadata.get("meta_tags").and_then(Value::as_object);
    let mut candidates = Vec::new();
    if let Some(meta_tags) = meta_tags {
        candidates.extend([
            meta_tags.get("ArticleTitle"),
            meta_tags.get("archiver-title"),
            meta_tags.get("og:title"),
            meta_tags.get("title"),
        ]);
    }
    candidates.extend([
        metadata.get("ArticleTitle"),
        metadata.get("HTML:Title"),
        metadata.get("Title"),
        metadata.get("title"),
    ]);

    candidates
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(repair_title)
        .find(|title| !title.is_empty() && !is_archive_snapshot_title(title))
}

fn is_archive_snapshot_title(value: &str) -> bool {
    ARCHIVE_SNAPSHOT_TITLE_RE.is_match(value.trim())
}

fn repair_title(value: &str) -> String {
    let (title, _) = repair_unicode_escapes_for_title(value);
    title.trim().to_string()
}

fn title_from_content_text(text: &str) -> Option<String> {
    for raw_line in text.lines() {
        let mut line = raw_line.split_whitespace().collect::<Vec<_>>().join(" ");
        line = line.trim_matches(|c| "#*->:： ".contains(c)).to_string();
        if line.is_empty()
            || is_archive_snapshot_title(&line)
            || line.to_ascii_lowercase().contains("http://")
            || line.to_ascii_lowercase().contains("https://")
            || MARKDOWN_LINK_ONLY_RE.is_match(&line)
        {
            continue;
        }
        for marker in [
            "发布时间",
            "发布日期",
            "发布者",
            "浏览次数",
            "来源：",
            "来源:",
        ] {
            if let Some(index) = line.find(marker) {
                if index >= 8 {
                    line.truncate(index);
                    line = line.trim().to_string();
                    break;
                }
            }
        }
        line = line.trim_matches(|c| "#*->:： ".contains(c)).to_string();
        if line.chars().count() < 4 {
            continue;
        }
        if !line
            .chars()
            .any(|ch| ch.is_ascii_alphanumeric() || ('\u{3400}'..='\u{9fff}').contains(&ch))
        {
            continue;
        }
        if line.chars().count() <= 120 {
            return Some(line);
        }
        for separator in ["。", "！", "？", "!", "?", "；", ";"] {
            if let Some(index) = line.find(separator) {
                let char_index = line[..index].chars().count();
                if (8..120).contains(&char_index) {
                    let end = index + separator.len();
                    return Some(line[..end].trim().to_string());
                }
            }
        }
        return Some(format!(
            "{}...",
            line.chars().take(120).collect::<String>().trim_end()
        ));
    }
    None
}

fn crop_content_to_title_window(content: &str, title: &str) -> Option<String> {
    let title_key = compact_text_key(title);
    if title_key.chars().count() < 4 {
        return None;
    }

    let lines: Vec<&str> = content.lines().collect();
    let start = lines.iter().position(|line| {
        let key = compact_text_key(line);
        key == title_key
            || (title_key.chars().count() >= 8 && key.contains(&title_key))
            || (key.chars().count() >= 8 && title_key.contains(&key))
    })?;

    let mut kept: Vec<String> = Vec::new();
    let mut kept_chars = 0;
    let mut previous_key = String::new();
    for line in lines.iter().skip(start) {
        let cleaned = trim_content_line(line);
        if cleaned.is_empty() {
            continue;
        }
        if kept_chars >= MIN_CONTENT_LENGTH_TO_INDEX && is_article_tail_marker(&cleaned) {
            break;
        }
        let key = compact_text_key(&cleaned);
        if !key.is_empty() && key == previous_key {
            continue;
        }
        kept_chars += cleaned.chars().count();
        previous_key = key;
        kept.push(cleaned);
    }

    let cropped = kept.join("\n");
    let cropped_chars = cropped.chars().count();
    let original_chars = content.chars().count();
    if original_chars >= 1_000
        && cropped_chars < 500
        && cropped_chars.saturating_mul(3) < original_chars
    {
        return None;
    }
    if cropped_chars >= MIN_CONTENT_LENGTH_TO_INDEX && cropped_chars + 20 < original_chars {
        Some(cropped)
    } else {
        None
    }
}

fn trim_content_line(line: &str) -> String {
    let trimmed = line.trim().trim_matches(|ch| "#*->:：[]【】 ".contains(ch));
    trimmed.to_string()
}

fn compact_text_key(text: &str) -> String {
    text.chars()
        .filter(|ch| ch.is_ascii_alphanumeric() || ('\u{3400}'..='\u{9fff}').contains(ch))
        .flat_map(char::to_lowercase)
        .collect()
}

fn is_article_tail_marker(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    let compact = compact_text_key(line);
    [
        "上一条",
        "下一条",
        "关闭窗口",
        "打印本页",
        "责任编辑",
        "相关链接",
        "相关稿件",
        "相关文章",
        "附件下载",
        "网站地图",
        "主办单位",
        "承办单位",
        "版权所有",
        "公网安备",
        "ICP备",
    ]
    .iter()
    .any(|marker| line.contains(marker) || compact.contains(&compact_text_key(marker)))
        || lower.contains("copyright")
        || lower.contains("all rights reserved")
}

fn repair_unicode_escapes(text: &str) -> (String, bool) {
    repair_unicode_escapes_with_thresholds(text, 3, 10)
}

fn repair_unicode_escapes_for_title(text: &str) -> (String, bool) {
    repair_unicode_escapes_with_thresholds(text, 1, 10)
}

fn repair_unicode_escapes_with_thresholds(
    text: &str,
    min_cjkish_matches: usize,
    min_total_matches: usize,
) -> (String, bool) {
    let matches: Vec<_> = UNICODE_ESCAPE_RE.captures_iter(text).collect();
    if matches.is_empty() {
        return (text.to_string(), false);
    }
    let cjkish_matches = matches
        .iter()
        .filter_map(|captures| captures.get(1))
        .filter_map(|matched| u32::from_str_radix(matched.as_str(), 16).ok())
        .filter(|codepoint| is_cjk_or_fullwidth_codepoint(*codepoint))
        .count();
    if cjkish_matches < min_cjkish_matches && matches.len() < min_total_matches {
        return (text.to_string(), false);
    }

    let repaired = UNICODE_ESCAPE_RE
        .replace_all(text, |captures: &Captures<'_>| {
            let Some(hex) = captures.get(1) else {
                return captures[0].to_string();
            };
            let Ok(codepoint) = u32::from_str_radix(hex.as_str(), 16) else {
                return captures[0].to_string();
            };
            if (0xd800..=0xdfff).contains(&codepoint) {
                return captures[0].to_string();
            }
            char::from_u32(codepoint)
                .map(|ch| ch.to_string())
                .unwrap_or_else(|| captures[0].to_string())
        })
        .into_owned();
    let changed = repaired != text;
    (repaired, changed)
}

fn is_cjk_or_fullwidth_codepoint(codepoint: u32) -> bool {
    (0x3000..=0x303f).contains(&codepoint)
        || (0x3400..=0x4dbf).contains(&codepoint)
        || (0x4e00..=0x9fff).contains(&codepoint)
        || (0xf900..=0xfaff).contains(&codepoint)
        || (0xff00..=0xffef).contains(&codepoint)
}

fn ratio(numerator: usize, denominator: usize) -> f64 {
    if denominator == 0 {
        0.0
    } else {
        numerator as f64 / denominator as f64
    }
}

fn duration_millis(duration: Duration) -> u128 {
    let millis = duration.as_millis();
    if millis == 0 && !duration.is_zero() {
        1
    } else {
        millis
    }
}

fn insert_timing(extracted: &mut ExtractedDocument, key: &str, duration: Duration) {
    let millis = json!(duration_millis(duration));

    if let Some(timing) = extracted.timing_ms.as_object_mut() {
        timing.insert(key.to_string(), millis.clone());
    }

    if let Some(content_extraction) = extracted
        .extraction_metadata
        .get_mut("content_extraction")
        .and_then(Value::as_object_mut)
    {
        let timing = content_extraction
            .entry("timing_ms")
            .or_insert_with(|| json!({}));
        if let Some(timing) = timing.as_object_mut() {
            timing.insert(key.to_string(), millis);
        }
    }
}

fn insert_content_type_detection(
    extracted: &mut ExtractedDocument,
    options: ExtractOptions<'_>,
    detection: &ContentTypeDetection,
    resolved_kind: ExtractKind,
) {
    if let Some(content_extraction) = extracted
        .extraction_metadata
        .get_mut("content_extraction")
        .and_then(Value::as_object_mut)
    {
        content_extraction.insert(
            "detected_mime_type".to_string(),
            json!(&detection.mime_type),
        );
        content_extraction.insert("detected_mime_name".to_string(), json!(&detection.name));
        content_extraction.insert(
            "detected_mime_extension".to_string(),
            json!(&detection.extension),
        );
        content_extraction.insert("detected_mime_kind".to_string(), json!(&detection.kind));
        content_extraction.insert(
            "detected_mime_is_generic".to_string(),
            json!(detection.is_generic),
        );
        content_extraction.insert(
            "detected_extract_kind".to_string(),
            json!(&detection.extract_kind),
        );
        content_extraction.insert(
            "resolved_extract_kind".to_string(),
            json!(resolved_kind.as_str()),
        );
        content_extraction.insert("declared_mime_type".to_string(), json!(options.mime_type));
        content_extraction.insert(
            "declared_bucket_path".to_string(),
            json!(options.source_path),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Nesting depth guaranteed to exceed the structural gate's default ceiling,
    /// so depth-rejection tests stay correct if `DEFAULT_MAX_NESTING_DEPTH` moves.
    const OVER_MAX_DEPTH: usize = DEFAULT_MAX_NESTING_DEPTH as usize + 200;

    #[test]
    fn positive_runtime_limits_reject_blank_zero_and_garbage() {
        assert_eq!(
            positive_usize(None, DEFAULT_MAX_CONTENT_CHARS),
            DEFAULT_MAX_CONTENT_CHARS
        );
        assert_eq!(
            positive_usize(Some("5000"), DEFAULT_MAX_CONTENT_CHARS),
            5000
        );
        assert_eq!(
            positive_usize(Some("0"), DEFAULT_MAX_CONTENT_CHARS),
            DEFAULT_MAX_CONTENT_CHARS
        );
        assert_eq!(
            positive_usize(Some("bad"), DEFAULT_MAX_CONTENT_CHARS),
            DEFAULT_MAX_CONTENT_CHARS
        );
        assert_eq!(positive_i64(Some("64"), DEFAULT_MAX_NESTING_DEPTH), 64);
        assert_eq!(positive_u64(Some("250"), DEFAULT_EXTRACT_TIMEOUT_MS), 250);
    }

    #[test]
    fn server_error_rule_rejects_short_apache_php_404_error_template() {
        let content = "The requested URL /index.php was not found on this server.\nApache/2.4.39 (Win64) OpenSSL/1.1.1b PHP/7.2.18 mod_fcgid/2.3.10-dev Server at yunchuangyinyue.com Port 443";

        assert_eq!(
            content_quality_rejection(content, "404 Not Found").as_deref(),
            Some(
                "low-value extracted content: server error template (apache_php_short_http_error)"
            )
        );
    }

    #[test]
    fn content_quality_rejects_cnki_login_placeholder() {
        let content = "会员登录 立即注册 用户名 密码 服务热线：400-810-9888".repeat(20);
        assert_eq!(
            content_quality_rejection(&content, "会员登录").as_deref(),
            Some("low-value extracted content: login placeholder")
        );
    }

    #[test]
    fn content_quality_rejects_ezproxy_login_placeholder() {
        let content = "This is login.htm from the docs subdirectory. ".repeat(20);
        assert_eq!(
            content_quality_rejection(&content, "Login").as_deref(),
            Some("low-value extracted content: login placeholder")
        );
    }

    #[test]
    fn server_error_rule_rejects_short_apache_not_found_template_without_footer() {
        let content = "Not Found\nThe requested URL /V20/gw/nybk/202007/t20200708_7449277.htm was not found on this server.\nAdditionally, a 404 Not Found error was encountered while trying to use an ErrorDocument to handle the request.";

        assert_eq!(
            content_quality_rejection(content, "Unexpected response").as_deref(),
            Some("low-value extracted content: server error template (apache_short_not_found_template)")
        );
    }

    #[test]
    fn server_error_rule_rejects_short_chinese_not_found_template() {
        let content = "404 Not Found\n抱歉，您访问的页面不存在！\n[<-返回首页](http://ggzyjy.nmg.gov.cn/nmgggfwpt-home-web \"Back on track trooper!\")";

        assert_eq!(
            content_quality_rejection(content, "404 Not Found").as_deref(),
            Some("low-value extracted content: server error template (chinese_short_not_found_template)")
        );
    }

    #[test]
    fn server_error_rule_rejects_short_english_not_found_template() {
        let content = "404\nSorry, the page you visited does not exist. It may be that the access link is wrong or the file does not exist.";

        assert_eq!(
            content_quality_rejection(content, "404 Not Found").as_deref(),
            Some("low-value extracted content: server error template (english_short_not_found_template)")
        );
    }

    #[test]
    fn server_error_rule_rejects_short_litespeed_error_template() {
        let content = "Proudly powered by LiteSpeed Web Server. Please be advised that LiteSpeed Technologies Inc. is not a web hosting company and has no control over content found on this site.";

        assert_eq!(
            content_quality_rejection(content, "404 Not Found").as_deref(),
            Some("low-value extracted content: server error template (litespeed_short_error_template)")
        );
    }

    #[test]
    fn server_error_rule_rejects_short_nginx_404_template() {
        let content = "此网页已被删除\nRequest URL: /\nStatus Code: 404 Not Found\nServer: nginx\nRedirecting mobile users to baidu.com. Go to Homepage or Go Back.";

        assert_eq!(
            content_quality_rejection(content, "404 Not Found").as_deref(),
            Some("low-value extracted content: server error template (nginx_short_404_template)")
        );
    }

    #[test]
    fn server_error_rule_rejects_short_apache_php_template_without_a_status_title() {
        let content = "The requested URL /index.php was not found on this server.\nApache/2.4.39 (Win64) OpenSSL/1.1.1b PHP/7.2.18 mod_fcgid/2.3.10-dev Server at yunchuangyinyue.com Port 443";

        assert_eq!(
            content_quality_rejection(content, "Unexpected response").as_deref(),
            Some(
                "low-value extracted content: server error template (apache_php_short_http_error)"
            )
        );
    }

    #[test]
    fn server_error_rule_rejects_short_apache_php_500_error_template() {
        let content = "The server encountered an internal error and was unable to complete your request.\nApache/2.4.58 (Ubuntu) OpenSSL/3.0.2 PHP/8.2.0 Server at example.org Port 443";

        assert_eq!(
            content_quality_rejection(content, "500 Internal Server Error").as_deref(),
            Some(
                "low-value extracted content: server error template (apache_php_short_http_error)"
            )
        );
    }

    #[test]
    fn server_error_rule_keeps_a_long_report_that_quotes_an_error() {
        let template = "The requested URL /index.php was not found on this server.\nApache/2.4.39 (Win64) OpenSSL/1.1.1b PHP/7.2.18 mod_fcgid/2.3.10-dev Server at yunchuangyinyue.com Port 443";
        let content = format!(
            "{template}\n{}",
            "This incident report documents the migration, the reproduction steps, the remediation, and the subsequent validation in detail. ".repeat(24)
        );

        assert!(content.chars().count() > SERVER_ERROR_TEMPLATE_RULES[0].max_chars);
        assert!(server_error_template_rejection(&content, "404 Not Found", false).is_none());
    }

    #[test]
    fn server_error_rule_rejects_final_content_but_not_fallback_candidates_by_default() {
        let content = "The requested URL /index.php was not found on this server.\nApache/2.4.39 (Win64) OpenSSL/1.1.1b PHP/7.2.18 mod_fcgid/2.3.10-dev Server at yunchuangyinyue.com Port 443";

        assert!(server_error_template_rejection(content, "404 Not Found", false).is_some());
        assert!(!is_definitely_cruft_fallback_candidate(
            content,
            "404 Not Found"
        ));
    }

    #[test]
    fn server_error_rule_can_opt_into_fallback_selection() {
        let rule = ServerErrorTemplateRule {
            name: "test".to_string(),
            max_chars: 200,
            max_nonempty_lines: 2,
            titles: vec![],
            error_phrases_any: vec!["not found".to_string()],
            content_markers_all: vec!["server at".to_string()],
            content_markers_any: vec!["apache/".to_string()],
            apply_during_fallback_selection: true,
        };
        let content = "The requested URL /index.php was not found on this server.\nApache/2.4.39 Server at example.org Port 443";

        assert!(server_error_template_rejection_with_rules(
            &[rule],
            content,
            "Unexpected response",
            true
        )
        .is_some());
    }

    #[test]
    fn server_error_rule_rejects_short_apache_php_unauthorized_template() {
        let content = "You are not authorized to access this resource.\nApache/2.4.58 (Ubuntu) PHP/8.2.0 Server at example.org Port 443";

        assert_eq!(
            content_quality_rejection(content, "Unexpected response").as_deref(),
            Some(
                "low-value extracted content: server error template (apache_php_short_http_error)"
            )
        );
    }

    #[test]
    fn server_error_rule_keeps_a_short_technical_note_without_a_server_footer() {
        let content = "This troubleshooting note explains why an Apache/2.4 deployment can return not found for a legacy route after a reverse-proxy migration. It documents the configuration check, the expected rewrite behavior, and the validation command for administrators.";

        assert!(content_quality_rejection(content, "Apache troubleshooting note").is_none());
    }

    #[test]
    fn server_error_template_rules_allow_optional_fields_but_reject_unknown_ones() {
        let valid = r#"[
            {
                "name": "test",
                "max_chars": 200,
                "max_nonempty_lines": 2,
                "error_phrases_any": ["not found"],
                "content_markers_any": ["apache/"]
            }
        ]"#;
        let parsed: Vec<ServerErrorTemplateRule> =
            serde_json::from_str(valid).expect("valid optional rule fields");
        assert!(parsed[0].titles.is_empty());
        assert!(!parsed[0].apply_during_fallback_selection);

        let unknown = r#"[
            {
                "name": "test",
                "max_chars": 200,
                "max_nonempty_lines": 2,
                "error_phrases_any": ["not found"],
                "content_markers_any": ["apache/"],
                "mistyped_field": true
            }
        ]"#;
        assert!(serde_json::from_str::<Vec<ServerErrorTemplateRule>>(unknown).is_err());
    }

    #[test]
    fn server_error_template_rules_require_independent_body_evidence() {
        let mut rules = vec![ServerErrorTemplateRule {
            name: "title_only".to_string(),
            max_chars: 200,
            max_nonempty_lines: 2,
            titles: vec!["404 Not Found".to_string()],
            error_phrases_any: vec!["not found".to_string()],
            content_markers_all: vec![],
            content_markers_any: vec![],
            apply_during_fallback_selection: false,
        }];

        assert!(validate_server_error_template_rules(&mut rules).is_err());
    }

    #[test]
    fn classifies_mhtml_by_mime_or_extension() {
        let by_mime = classify_html_candidate(Some("message/rfc822"), Some("x.bin"));
        assert!(by_mime.is_html_fast_lane);
        assert_eq!(by_mime.kind.as_deref(), Some("mhtml"));

        let by_extension =
            classify_html_candidate(Some("application/octet-stream"), Some("x/y/page.mhtml"));
        assert!(by_extension.is_html_fast_lane);
        assert_eq!(by_extension.kind.as_deref(), Some("mhtml"));
    }

    #[test]
    fn classifies_html_tmp_by_inner_extension() {
        let decision = classify_html_candidate(
            Some("application/octet-stream"),
            Some("cache/page.html.tmp"),
        );
        assert!(decision.is_html_fast_lane);
        assert_eq!(decision.kind.as_deref(), Some("html"));
    }

    #[test]
    fn detected_pdf_bytes_override_html_or_mhtml_label() {
        let pdf_bytes = b"%PDF-1.7\n";
        let detection = detect_content_type(pdf_bytes);
        assert_eq!(normalize_mime_type(&detection.mime_type), "application/pdf");
        assert_eq!(detection.extract_kind.as_deref(), Some("pdf"));

        assert_eq!(
            resolve_extract_kind(
                ExtractOptions {
                    mime_type: Some("message/rfc822"),
                    source_path: Some("objects/mislabeled.mhtml"),
                    kind: HtmlKind::Auto,
                },
                &detection
            ),
            Some(ExtractKind::Pdf)
        );
        assert_eq!(
            resolve_extract_kind(
                ExtractOptions {
                    mime_type: Some("text/html"),
                    source_path: Some("objects/mislabeled.html"),
                    kind: HtmlKind::Auto,
                },
                &detection
            ),
            Some(ExtractKind::Pdf)
        );
    }

    #[test]
    fn detected_image_bytes_override_html_extension() {
        let png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00";
        let detection = detect_content_type(png);
        assert_eq!(normalize_mime_type(&detection.mime_type), "image/png");
        assert_eq!(detection.extract_kind.as_deref(), Some("image"));
        assert_eq!(
            resolve_extract_kind(
                ExtractOptions {
                    mime_type: None,
                    source_path: Some("thumbnail.html"),
                    kind: HtmlKind::Auto,
                },
                &detection,
            ),
            Some(ExtractKind::Image)
        );
    }

    #[test]
    fn null_padded_legacy_chinese_text_overrides_pdf_extension() {
        let source = "这是一份恢复出来的中文资料，文件名虽然以PDF结尾，实际内容是旧式编码的纯文本。".repeat(20);
        let (encoded, _, _) = GBK.encode(&source);
        let mut padded = Vec::with_capacity(encoded.len() * 5 / 4);
        for chunk in encoded.chunks(4) {
            padded.extend_from_slice(chunk);
            padded.push(0);
        }

        let detection = detect_content_type(&padded);
        assert_eq!(detection.mime_type, "text/plain");
        let extracted = extract_bytes(
            &padded,
            ExtractOptions {
                mime_type: None,
                source_path: Some("mislabeled.pdf"),
                kind: HtmlKind::Auto,
            },
        )
        .unwrap();

        assert_eq!(extracted.source_format, "text");
        assert!(extracted.content.contains("恢复出来的中文资料"));
    }

    #[test]
    fn mhtml_signature_overrides_html_extension() {
        let bytes = b"From: saved\r\nSnapshot-Content-Location: https://example.test/\r\nMIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=x\r\n\r\n--x--\r\n";
        let detection = detect_content_type(bytes);
        assert_eq!(
            normalize_mime_type(&detection.mime_type),
            "multipart/related"
        );
        assert_eq!(detection.extract_kind.as_deref(), Some("mhtml"));
        assert_eq!(
            resolve_extract_kind(
                ExtractOptions {
                    mime_type: None,
                    source_path: Some("snapshot.html"),
                    kind: HtmlKind::Auto,
                },
                &detection,
            ),
            Some(ExtractKind::Mhtml)
        );
    }

    #[test]
    fn generic_zip_detection_does_not_override_office_extension() {
        let detection = detect_content_type(b"PK\x03\x04\x14\x00\x00\x00");
        assert!(detection.is_generic);
        assert_eq!(
            resolve_extract_kind(
                ExtractOptions {
                    mime_type: Some("application/octet-stream"),
                    source_path: Some("objects/workbook.xlsx"),
                    kind: HtmlKind::Auto,
                },
                &detection
            ),
            Some(ExtractKind::Spreadsheet)
        );
    }

    #[test]
    fn broad_text_detection_does_not_override_declared_html_fragment() {
        let detection = detect_content_type(b"hello world");
        assert_eq!(detection.extract_kind.as_deref(), Some("text"));
        assert_eq!(
            resolve_extract_kind(
                ExtractOptions {
                    mime_type: Some("text/html"),
                    source_path: Some("objects/page.html"),
                    kind: HtmlKind::Auto,
                },
                &detection
            ),
            Some(ExtractKind::Html)
        );
    }

    #[test]
    fn routes_macro_and_extended_office_formats_to_extractors() {
        assert!(matches!(
            classify_extract_kind(None, Some("content/sample.docm")),
            Some(ExtractKind::Document)
        ));
        assert!(matches!(
            classify_extract_kind(
                Some("application/vnd.ms-powerpoint.presentation.macroenabled.12"),
                Some("content/sample.bin"),
            ),
            Some(ExtractKind::Document)
        ));
        assert!(matches!(
            classify_extract_kind(None, Some("content/sample.xlsb")),
            Some(ExtractKind::Spreadsheet)
        ));
        assert!(matches!(
            classify_extract_kind(
                Some("application/vnd.oasis.opendocument.spreadsheet"),
                Some("content/sample.bin"),
            ),
            Some(ExtractKind::Spreadsheet)
        ));
    }

    #[test]
    fn auto_extracts_plain_text() {
        let extracted = extract_bytes(
            b"First line\nSecond line\nThird line",
            ExtractOptions {
                mime_type: Some("text/plain; charset=utf-8"),
                source_path: Some("notes/sample.txt"),
                kind: HtmlKind::Auto,
            },
        )
        .unwrap();

        assert_eq!(extracted.source_format, "text");
        assert_eq!(extracted.content, "First line\nSecond line\nThird line");
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["extractor_type"],
            "text"
        );
    }

    #[test]
    fn auto_extracts_csv_by_extension_before_broad_text_mime() {
        let extracted = extract_bytes(
            b"name,value\nalpha,1\nbeta,2\n",
            ExtractOptions {
                mime_type: Some("text/plain"),
                source_path: Some("exports/sample.csv"),
                kind: HtmlKind::Auto,
            },
        )
        .unwrap();

        assert_eq!(extracted.source_format, "csv");
        assert!(extracted.content.contains("alpha"));
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["extractor_type"],
            "csv"
        );
    }

    #[test]
    fn classifies_unrouted_binary_as_skipped_unsupported() {
        let error = extract_bytes(
            b"\x00\x01not useful document text",
            ExtractOptions {
                mime_type: Some("application/octet-stream"),
                source_path: Some("objects/sample.bin"),
                kind: HtmlKind::Auto,
            },
        )
        .unwrap_err();

        let failure = classify_extraction_failure(&error);
        assert_eq!(
            failure.category,
            ExtractionFailureCategory::SkippedUnsupported
        );
        assert_eq!(failure.extraction_status(), "skipped_unsupported");
        assert_eq!(failure.error_type, "unsupported_file_type");
        assert!(!failure.fallback_required);
        assert_eq!(failure.fallback_kind, None);
    }

    #[test]
    fn classifies_legacy_ppt_as_fallback_required() {
        let error = extract_bytes(
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
            ExtractOptions {
                mime_type: Some("application/vnd.ms-powerpoint"),
                source_path: Some("slides/legacy.ppt"),
                kind: HtmlKind::Auto,
            },
        )
        .unwrap_err();

        let failure = classify_extraction_failure(&error);
        assert_eq!(
            failure.category,
            ExtractionFailureCategory::FallbackRequired
        );
        assert_eq!(failure.extraction_status(), "fallback_required");
        assert_eq!(failure.error_type, "content_extraction_failed");
        assert!(failure.fallback_required);
        assert_eq!(
            failure.fallback_kind.as_deref(),
            Some("legacy_ppt_libreoffice_pdf_pdftotext")
        );
    }

    #[test]
    fn extracts_simple_html() {
        let html = br#"
        <html>
          <head><title>Example title</title><meta name="author" content="Tester"></head>
          <body><article><h1>Example title</h1><p>This is the main article content with enough text to pass the minimum extraction threshold. It has another sentence for readability scoring.</p></article></body>
        </html>
        "#;
        let extracted = extract_html_bytes(html, Some("sample.html")).unwrap();
        assert_eq!(extracted.title, "Example title");
        assert!(extracted.content.contains("main article content"));
        assert_eq!(extracted.source_format, "html");
    }

    #[test]
    fn html2text_metadata_is_bounded_preview() {
        let repeated = (0..70)
            .map(|index| {
                format!("<a href=\"/{index}\">Diagnostic link {index:02} with long text.</a>")
            })
            .collect::<String>();
        let html = format!(
            "<html><head><title>Long link page</title></head><body>{repeated}</body></html>"
        );

        let extracted = extract_html_bytes(html.as_bytes(), Some("long.html")).unwrap();
        let content_extraction = extracted
            .extraction_metadata
            .get("content_extraction")
            .and_then(Value::as_object)
            .expect("content extraction metadata");
        let preview = content_extraction
            .get("html2text_preview")
            .and_then(Value::as_str)
            .expect("html2text preview");

        assert!(content_extraction.get("html2text").is_none());
        assert!(extracted.html2text_length > METADATA_TEXT_PREVIEW_CHARS);
        assert!(preview.chars().count() <= METADATA_TEXT_PREVIEW_CHARS);
        assert_eq!(
            content_extraction
                .get("html2text_truncated")
                .and_then(Value::as_bool),
            Some(true)
        );
    }

    #[test]
    fn dom_density_fallback_prefers_low_link_article_text() {
        let html = r#"
        <html>
          <body>
            <div class="nav">
              <a href="/1">首页</a><a href="/2">政务公开</a><a href="/3">在线办事</a>
              <a href="/4">新闻中心</a><a href="/5">网站地图</a>
            </div>
            <div id="content">
              <h1>环境整治大检查</h1>
              <p>检查组对各乡镇场环境卫生整治工作进行了现场检查，重点查看保洁人员投入、绿化维护、临街楼房立面改造和长效机制建设情况。</p>
              <p>会议要求各单位继续完善整改台账，压实责任，确保环境整治成果稳定保持，并及时向社会公开相关工作进展。</p>
            </div>
          </body>
        </html>
        "#;

        let candidate = dom_density_fallback_text(html).unwrap();
        assert!(candidate.text.contains("检查组对各乡镇场环境卫生整治工作"));
        assert!(!candidate.text.contains("网站地图"));
        assert!(candidate.link_density < 0.2);
    }

    #[test]
    fn dom_density_fallback_replaces_short_readability_stub() {
        let candidate = DomTextCandidate {
            text: "x".repeat(900),
            score: 900.0,
            char_count: 900,
            link_density: 0.2,
            selector_name: "div".to_string(),
        };

        assert!(should_use_dom_density_fallback(180, Some(&candidate), true));
        assert!(!should_use_dom_density_fallback(
            180,
            Some(&candidate),
            false
        ));
        assert!(!should_use_dom_density_fallback(
            700,
            Some(&candidate),
            true
        ));
    }

    #[test]
    fn mhtml_allows_aggressive_dom_density_fallback() {
        let candidate = DomTextCandidate {
            text: "x".repeat(900),
            score: 900.0,
            char_count: 900,
            link_density: 0.2,
            selector_name: "div".to_string(),
        };

        assert!(allows_aggressive_html_fallback("html"));
        assert!(allows_aggressive_html_fallback("mhtml"));
        assert!(!allows_aggressive_html_fallback("pdf"));
        assert!(should_use_dom_density_fallback(
            180,
            Some(&candidate),
            allows_aggressive_html_fallback("mhtml")
        ));
    }

    #[test]
    fn html_extraction_reverts_overbroad_boilerplate_strip() {
        let article = "为了认真贯彻落实学生工作会议精神，进一步规范和管理学生社团活动行为，\
             充分发挥学生社团在校园文化建设中的平台作用，会议要求各社团完善活动档案、\
             提高宣传报道效率，并在每学期开展经验交流和总结表彰。"
            .repeat(3);
        let html = format!(
            r#"
            <html>
              <head><title>学生社团工作安排大会</title></head>
              <body>
                <div class="metadata">
                  <h1>学生社团工作安排大会</h1>
                  <p>{article}</p>
                </div>
              </body>
            </html>
            "#
        );

        let extracted = extract_html_bytes(html.as_bytes(), Some("student-clubs.html")).unwrap();
        let content_extraction = extracted
            .extraction_metadata
            .get("content_extraction")
            .and_then(Value::as_object)
            .expect("content extraction metadata");

        assert!(extracted
            .content
            .contains("进一步规范和管理学生社团活动行为"));
        assert_eq!(
            content_extraction
                .get("boilerplate_strip_reverted")
                .and_then(Value::as_bool),
            Some(true)
        );
    }

    #[test]
    fn html2text_fallback_rejects_massive_boilerplate_page() {
        assert!(should_use_html2text_fallback(120, 800, true, false));
        assert!(!should_use_html2text_fallback(120, 800, false, false));
        assert!(!should_use_html2text_fallback(120, 426_942, true, false));
        assert!(should_use_html2text_fallback(700, 3_000, true, false));
        assert!(!should_use_html2text_fallback(700, 2_000, true, false));
    }

    #[test]
    fn html2text_fallback_probes_cruft_readability_output() {
        assert!(should_probe_html2text_fallback(2_000, true, true));
        assert!(should_use_html2text_fallback(2_000, 800, true, true));
        assert!(!should_use_html2text_fallback(2_000, 90, true, true));
        assert!(!should_use_html2text_fallback(2_000, 426_942, true, true));
        assert!(!should_probe_html2text_fallback(2_000, false, false));
    }

    #[test]
    fn html2text_fallback_candidates_are_screened_for_definite_cruft() {
        let nav_shell = r#"
        首页
        政务公开
        政务服务
        政民互动
        网站地图
        友情链接
        上一条
        下一条
        版权所有
        Copyright
        "#;
        let article = "检查组对各乡镇场环境卫生整治工作进行了现场检查，重点查看保洁人员投入、\
             绿化维护、临街楼房立面改造和长效机制建设情况。会议要求各单位继续完善整改台账，\
             压实责任，确保环境整治成果稳定保持，并及时向社会公开相关工作进展。";
        let mojibake = "Ã©".repeat(20);

        assert!(is_definitely_cruft_fallback_candidate(nav_shell, ""));
        assert!(is_definitely_cruft_fallback_candidate(&mojibake, ""));
        assert!(!is_definitely_cruft_fallback_candidate(article, ""));
    }

    #[test]
    fn dom_density_fallback_rejects_script_dump_candidate() {
        let article = "检查组对各乡镇场环境卫生整治工作进行了现场检查，重点查看保洁人员投入、\
             绿化维护、临街楼房立面改造和长效机制建设情况。会议要求各单位继续完善整改台账，\
             压实责任，确保环境整治成果稳定保持，并及时向社会公开相关工作进展。"
            .repeat(4);
        let html = format!(
            r#"
            <html>
              <head><title>环境整治大检查</title></head>
              <body>
                <script>
                function addToBookmark(){{
                  var href="/news/1.html";
                  var title=document.title;
                  document.location.href = href + title;
                }}
                function postMsg(){{
                  if (document.form1.content.value == "") {{
                    alert("请填写留言。");
                    return;
                  }}
                  document.form1.submit();
                }}
                </script>
                <div class="metadata">
                  <h1>环境整治大检查</h1>
                  <p>{article}</p>
                </div>
              </body>
            </html>
            "#
        );
        let script_dump = r#"
        function addToBookmark(){
          var href="/news/1.html";
          var title=document.title;
          document.location.href = href + title;
        }
        function postMsg(){
          if (document.form1.content.value == "") {
            alert("请填写留言。");
            return;
          }
          document.form1.submit();
        }
        "#;

        assert!(looks_like_script_or_form_dump(script_dump));
        let extracted = extract_html_bytes(html.as_bytes(), Some("script-dump.html")).unwrap();
        let content_extraction = extracted
            .extraction_metadata
            .get("content_extraction")
            .and_then(Value::as_object)
            .expect("content extraction metadata");

        assert!(extracted
            .content
            .contains("检查组对各乡镇场环境卫生整治工作"));
        assert!(!extracted.content.contains("function addToBookmark"));
        assert_eq!(
            content_extraction
                .get("used_dom_density_fallback")
                .and_then(Value::as_bool),
            Some(false)
        );
        assert_eq!(
            content_extraction
                .get("boilerplate_strip_reverted")
                .and_then(Value::as_bool),
            Some(true)
        );
    }

    #[test]
    fn html2text_cleanup_removes_grid_rule_noise() {
        let rendered = r#"
        ───────────────────────────────────────────────────────────────────────
        │[简体中文][1]│|│[设为首页][2]  |  [加入收藏][3]  |
        ////////////////////////////////////////////////////////////////////////
        当前位置：首页>>信息公开>>重点信息公开
        沙湾县高中城建设项目公示
        发表时间:2018年12月16日
        "#;

        let cleaned = clean_html2text_rendered_text(rendered);

        assert!(!cleaned.contains("────"));
        assert!(!cleaned.contains("////"));
        assert!(cleaned.contains("沙湾县高中城建设项目公示"));
        assert!(cleaned.contains("发表时间:2018年12月16日"));
    }

    #[test]
    fn title_from_content_keeps_multibyte_sentence_boundary() {
        let title = title_from_content_text(
            "9月1日至15日，昆明机场将进行西跑道盲降设备升级改造工程不停航施工。为确保升级改造期间施工安全、机场运行保障正常，云南监管局根据不停航工程施工进度，制定专题工作计划。8月30日，召开专题会议，研究运行保障、施工组织、风险防控、应急值守、信息报送和现场协调等工作安排，要求各单位细化责任。",
        )
        .unwrap();

        assert_eq!(
            title,
            "9月1日至15日，昆明机场将进行西跑道盲降设备升级改造工程不停航施工。"
        );
    }

    #[test]
    fn compact_dom_candidate_can_replace_pagewide_html2text() {
        let compact_candidate = DomTextCandidate {
            text: "x".repeat(800),
            score: 800.0,
            char_count: 800,
            link_density: 0.1,
            selector_name: "div".to_string(),
        };
        let tiny_candidate = DomTextCandidate {
            text: "x".repeat(180),
            score: 180.0,
            char_count: 180,
            link_density: 0.1,
            selector_name: "td".to_string(),
        };
        let too_small_candidate = DomTextCandidate {
            text: "x".repeat(389),
            score: 389.0,
            char_count: 389,
            link_density: 0.1,
            selector_name: "div".to_string(),
        };
        let link_heavy_candidate = DomTextCandidate {
            text: "x".repeat(800),
            score: 800.0,
            char_count: 800,
            link_density: 0.8,
            selector_name: "div".to_string(),
        };

        assert!(should_use_dom_density_over_html2text_fallback(
            6_000,
            &compact_candidate,
            true
        ));
        assert!(!should_defer_small_dom_candidate_for_html2text(
            &compact_candidate
        ));
        assert!(!should_use_dom_density_over_html2text_fallback(
            6_000,
            &compact_candidate,
            false
        ));
        assert!(!should_use_dom_density_over_html2text_fallback(
            6_000,
            &tiny_candidate,
            true
        ));
        assert!(should_defer_small_dom_candidate_for_html2text(
            &tiny_candidate
        ));
        assert!(!should_use_dom_density_over_html2text_fallback(
            19_632,
            &too_small_candidate,
            true
        ));
        assert!(should_defer_small_dom_candidate_for_html2text(
            &too_small_candidate
        ));
        assert!(!should_use_dom_density_over_html2text_fallback(
            6_000,
            &link_heavy_candidate,
            true
        ));
    }

    #[test]
    fn title_window_crop_removes_header_and_footer_navigation() {
        let content = r#"
        [网站首页][1]
        [政务公开][2]
        环境整治大检查
        环境整治大检查
        检查组对各乡镇场环境卫生整治工作进行了现场检查，重点查看保洁人员投入、绿化维护、临街楼房立面改造和长效机制建设情况。
        会议要求各单位继续完善整改台账，压实责任，确保环境整治成果稳定保持，并及时向社会公开相关工作进展。
        下一条：其他新闻
        版权所有：测试政府网站
        "#;

        let cropped = crop_content_to_title_window(content, "环境整治大检查").unwrap();
        assert!(cropped.starts_with("环境整治大检查"));
        assert!(cropped.contains("检查组对各乡镇场环境卫生整治工作"));
        assert!(!cropped.contains("网站首页"));
        assert!(!cropped.contains("下一条"));
        assert!(!cropped.contains("版权所有"));
    }

    #[test]
    fn title_window_crop_rejects_tiny_slice_of_rich_fallback() {
        let rich_tail = (0..50)
            .map(|index| {
                format!(
                    "相关列表第{index}项包含足够长的正文线索和页面文本，不能因为标题附近出现尾部标记就全部丢掉。"
                )
            })
            .collect::<Vec<_>>()
            .join("\n");
        let content = format!(
            "栏目导航\n环境整治大检查\n检查组对各乡镇场环境卫生整治工作进行了现场检查，重点查看保洁人员投入和长效机制建设情况。\n下一条：其他新闻\n{rich_tail}"
        );

        assert!(crop_content_to_title_window(&content, "环境整治大检查").is_none());
    }

    #[test]
    fn extracts_basic_mhtml() {
        let mhtml = br#"From: <Saved by Blink>
Snapshot-Content-Location: http://test
Subject: =?UTF-8?Q?MHTML_title?=
MIME-Version: 1.0
Content-Type: multipart/related;
   type="text/html";
   boundary="boundary"

--boundary
Content-Type: text/html; charset="utf-8"
Content-ID: <frame-0@mhtml.blink>
Content-Transfer-Encoding: quoted-printable
Content-Location: http://test

<html><head><title>MHTML title</title></head><body><article><p>This is a saved page with enough body text to pass the extraction threshold and become useful content for indexing.</p></article></body></html>
--boundary--
"#;
        let extracted = extract_mhtml_bytes(mhtml, Some("sample.mhtml")).unwrap();
        assert_eq!(extracted.source_format, "mhtml");
        assert!(extracted.content.contains("saved page"));
    }

    #[test]
    fn mhtml_repairs_missing_separator_after_content_location() {
        let mhtml = concat!(
            "From: <Saved by Web Archiver>\r\n",
            "Subject: Snapshot of https://example.test/malformed\r\n",
            "MIME-Version: 1.0\r\n",
            "Content-Type: multipart/related; type=\"text/html\"; boundary=\"----=_CrawlerBoundary_test\"\r\n",
            "\r\n",
            "------=_CrawlerBoundary_test\r\n",
            "Content-Type: text/html;charset=utf-8\r\n",
            "Content-Transfer-Encoding: 8bit\r\n",
            "Content-Location: https://example.test/malformed\r\n",
            "<!DOCTYPE html><html><head><title>Malformed crawler snapshot</title></head>",
            "<body><article><p>This archived page has enough substantive body text to verify recovery when the crawler omits the required blank line between MIME part headers and its HTML body.</p></article></body></html>\r\n",
            "------=_CrawlerBoundary_test--\r\n",
        );

        let extracted = extract_mhtml_bytes(mhtml.as_bytes(), Some("malformed.mhtml")).unwrap();
        assert_eq!(extracted.source_format, "mhtml");
        assert!(extracted.content.contains("archived page"));
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]
                ["mhtml_missing_part_separator_repaired"],
            true
        );
    }

    #[test]
    fn mhtml_uses_html_part_when_parser_classifies_it_as_an_attachment() {
        let mhtml = r#"From: <Saved by Web Archiver>
Subject: Snapshot of http://example.test/attachment
MIME-Version: 1.0
Content-Type: multipart/related; boundary="boundary"

--boundary
Content-Type: text/html; charset=utf-8
Content-Disposition: attachment; filename="snapshot.html"
Content-Transfer-Encoding: 8bit
Content-Location: http://example.test/attachment

<html><head><title>Attachment-classified snapshot</title></head><body><article><p>This archived page has enough substantive body text to confirm that an HTML root part remains extractable when the MIME parser classifies it as an attachment.</p></article></body></html>
--boundary--
"#;

        let extracted = extract_mhtml_bytes(mhtml.as_bytes(), Some("attachment.mhtml")).unwrap();
        assert_eq!(extracted.source_format, "mhtml");
        assert!(extracted.content.contains("archived page"));
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["mhtml_selected_body_type"],
            "text/html"
        );
    }

    #[test]
    fn mhtml_falls_back_to_plain_text_when_markup_is_missing() {
        let mhtml = r#"From: <Saved by Web Archiver>
Subject: Plain-text snapshot
MIME-Version: 1.0
Content-Type: multipart/related; boundary="boundary"

--boundary
Content-Type: text/plain; charset=utf-8
Content-Transfer-Encoding: 8bit
Content-Location: http://example.test/plain

This plain-text archived page contains enough substantive material to remain useful for indexing even though the snapshot has no HTML or XML MIME part.
--boundary--
"#;

        let extracted = extract_mhtml_bytes(mhtml.as_bytes(), Some("plain.mhtml")).unwrap();
        assert_eq!(extracted.source_format, "mhtml");
        assert!(extracted.content.contains("plain-text archived page"));
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["mhtml_selected_body_type"],
            "text/plain"
        );
    }

    #[test]
    fn mhtml_recovers_single_base64_octet_stream_text() {
        let mhtml = r#"From: <Saved by Mozilla>
Subject: Unknown
MIME-Version: 1.0
Content-Type: application/octet-stream
Content-Transfer-Encoding: base64
Content-Location: http://example.test/archive.txt

VGhpcyBpcyBhIHJlY292ZXJlZCBzaW5nbGUtcGFydCB0ZXh0IHBheWxvYWQgZnJvbSBhbiBNSE1MIGFyY2hpdmUu
"#;

        let extracted = extract_mhtml_bytes(mhtml.as_bytes(), Some("single-part.mht")).unwrap();
        assert_eq!(extracted.source_format, "mhtml");
        assert!(extracted.content.contains("recovered single-part text payload"));
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["mhtml_selected_body_type"],
            "application/octet-stream-text"
        );
    }

    #[test]
    fn mhtml_octet_stream_detects_legacy_gbk_text() {
        let source = "新语丝网站保存的中文文章正文包含足够多的汉字，用来验证没有字符集声明的旧式单文件网页归档能够正确识别简体中文编码，而不是存成西文乱码。";
        let (bytes, _, _) = GBK.encode(source);

        let decoded = decode_mhtml_body_bytes(
            bytes.as_ref(),
            None,
            "application/octet-stream-text",
        );

        assert_eq!(decoded.encoding_name, GBK.name());
        assert!(decoded.decoded.contains("新语丝网站保存的中文文章"));
    }

    #[test]
    fn mhtml_overrides_false_gbk_when_body_is_utf8() {
        let mhtml = r#"From: <Saved by Web Archiver>
Subject: Snapshot of http://example.test/
MIME-Version: 1.0
Content-Type: multipart/related; type="text/html"; boundary="boundary"

--boundary
Content-Type: text/html; charset=GBK
Content-Transfer-Encoding: 8bit
Content-Location: http://example.test/

<html><head>
<meta http-equiv="Content-Type" content="text/html; charset=GBK">
<title>清华大学大型仪器共享服务平台</title>
</head><body><article><p>清华大学大型仪器共享服务平台提供实验室动态、开放仪器、收费标准和联系方式等信息，这段正文足够长，可以进入索引并验证编码没有变成乱码。</p></article></body></html>
--boundary--
"#;

        let extracted = extract_mhtml_bytes(mhtml.as_bytes(), Some("sample.mhtml")).unwrap();
        assert_eq!(extracted.title, "清华大学大型仪器共享服务平台");
        assert!(extracted.content.contains("清华大学大型仪器共享服务平台"));
        assert!(!extracted.content.contains("娓呭崕"));
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["encoding_source"],
            "utf8_override_declared"
        );
    }

    #[test]
    fn mhtml_prefers_valid_utf8_when_charset_is_absent() {
        let mhtml = r#"From: <Saved by Web Archiver>
Subject: Snapshot of http://example.test/
MIME-Version: 1.0
Content-Type: multipart/related; type="text/html"; boundary="boundary"

--boundary
Content-Type: text/html
Content-Transfer-Encoding: 8bit
Content-Location: http://example.test/

<html><head><title>中国邮政采购公告</title></head><body><article><p>石家庄邮电职业技术学院采购公告正文足够长，包含采购内容、供应商资格、响应文件递交方式和联系方式，用来验证没有声明字符集时仍然优先按有效 UTF-8 解码。</p></article></body></html>
--boundary--
"#;

        let extracted = extract_mhtml_bytes(mhtml.as_bytes(), Some("sample.mhtml")).unwrap();
        assert_eq!(extracted.title, "中国邮政采购公告");
        assert!(extracted.content.contains("石家庄邮电职业技术学院采购公告"));
        assert!(!extracted.content.contains("çŸ³"));
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["encoding_source"],
            "valid_utf8"
        );
    }

    #[test]
    fn mhtml_uses_inline_xml_body_when_html_part_is_missing() {
        let mhtml = r#"From: <Saved by Web Archiver>
Subject: Snapshot of http://example.test/xml
MIME-Version: 1.0
Content-Type: multipart/related; type="text/xml"; boundary="boundary"

--boundary
Content-Type: text/xml; charset=utf-8
Content-Transfer-Encoding: 8bit
Content-Location: http://example.test/xml

<?xml version="1.0" encoding="UTF-8"?>
<document><head><title>盐田区政务公开信息</title></head><body><article><p>盐田区政务公开信息正文包含办事指南、政策依据、申请材料、办理时限、受理地点和联系电话，这段正文足够长，可以验证只有 XML MIME part 的 MHTML 快照仍然能够被纳入索引。</p></article></body></document>
--boundary--
"#;

        let extracted = extract_mhtml_bytes(mhtml.as_bytes(), Some("xml-body.mhtml")).unwrap();
        assert_eq!(extracted.source_format, "mhtml");
        assert!(extracted.content.contains("盐田区政务公开信息正文"));
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["mhtml_selected_body_type"],
            "text/xml"
        );
    }

    #[test]
    fn repairs_literal_cjk_unicode_escapes() {
        let (repaired, changed) = repair_unicode_escapes(r"\u65b0\u7586\u7ef4\u543e");
        assert!(changed);
        assert!(repaired.contains("新疆"));
    }

    #[test]
    fn repairs_literal_cjk_unicode_escapes_in_basic_metadata() {
        let html = r#"
        <html>
          <head>
            <title>\u65b0\u7586\u7ef4\u543e</title>
            <meta name="archiver-title" content="\u65b0\u7586\u7ef4\u543e">
          </head>
          <body>
            <article>
              <p>新疆维吾尔自治区发布基层治理工作动态，围绕公共服务、就业保障、教育培训和社区建设介绍了连续推进的重点安排，正文足够长，可以进入索引。</p>
            </article>
          </body>
        </html>
        "#;

        let extracted = extract_bytes(
            html.as_bytes(),
            ExtractOptions {
                mime_type: Some("text/html"),
                source_path: Some("sample.html"),
                kind: HtmlKind::Html,
            },
        )
        .unwrap();

        assert_eq!(extracted.title, "新疆维吾");
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["meta_tags"]["archiver-title"],
            "新疆维吾"
        );
    }

    #[test]
    fn mojibake_gate_rejects_latin1_misdecode() {
        // A paragraph of UTF-8 French wrongly shown as Latin-1.
        let garbage = "Ã‰tude rÃ©alisÃ©e Ã  lâ€™Ã©cole. La sÃ©curitÃ© des donnÃ©es Ã©tait \
             gÃ©rÃ©e par lâ€™Ã©quipe. Les rÃ©sultats Ã©taient trÃ¨s clairs et prÃ©cis \
             pour lâ€™Ã©valuation finale des Ã©lÃ¨ves concernÃ©s cette annÃ©e."
            .repeat(2);
        assert!(mojibake_rejection(&garbage, "").is_some());
    }

    #[test]
    fn mojibake_gate_keeps_clean_chinese() {
        let clean = "疫情发生以来，作为新冠肺炎医疗救治定点医院分管护理工作的业务副院长，\
             宋玉霞一直没有停下忙碌的脚步。此次定点医院参与医疗救治工作的护理队伍有1260人，\
             来自全疆24家医疗机构、33支医疗队。为高效开展护理工作，定点医院设立了联合护理部。";
        assert!(mojibake_rejection(clean, "").is_none());
    }

    #[test]
    fn mojibake_gate_keeps_clean_english_with_accents() {
        // Correctly decoded accents (café, naïve, résumé) must not be flagged.
        let clean = "The café served a naïve soufflé to the résumé committee. \
             This is a perfectly ordinary English paragraph with a few accented \
             loanwords and no encoding corruption whatsoever in the text body here."
            .repeat(2);
        assert!(mojibake_rejection(&clean, "").is_none());
    }

    #[test]
    fn mojibake_gate_keeps_uppercase_latin_with_a_tilde() {
        // All-caps Portuguese uses a legitimate uppercase Ã (SÃO, NÃO, PÃO,
        // NAÇÃO, CORAÇÃO, IRMÃOS). With a zero CJK ratio these once tripped the
        // gate via the bare "Ã" marker; the misdecode-shape-only marker set must
        // leave clean uppercase Latin alone.
        let clean = "SÃO PAULO É GRANDE. A NAÇÃO NÃO COME PÃO SECO. \
             OS IRMÃOS TÊM CORAÇÃO E CANTAM COM EMOÇÃO NA SÃO JOÃO."
            .repeat(3);
        assert!(mojibake_rejection(&clean, "").is_none());
    }

    #[test]
    fn mojibake_gate_rejects_control_char_binary() {
        // A binary file (PDF/JPEG mislabeled HTML) decoded as latin1 becomes a
        // stream of C0/C1 control bytes, which the U+FFFD branch would miss but
        // never appear in real text. Alternate a C0 control with a letter.
        let mut garbage = String::new();
        for _ in 0..400 {
            garbage.push('\u{0007}'); // BEL, a C0 control
            garbage.push('a');
        }
        assert!(mojibake_rejection(&garbage, "").is_some());
    }

    #[test]
    fn mojibake_gate_keeps_clean_chinese_against_control_check() {
        // Real CJK is printable ideographs at ~0 control fraction and must pass
        // the new control-character branch.
        let clean = "国务院办公厅关于印发全国政务服务效能提升行动方案的通知，\
             各省、自治区、直辖市人民政府，国务院各部委、各直属机构。"
            .repeat(20);
        assert!(mojibake_rejection(&clean, "").is_none());
    }

    #[test]
    fn pathological_markup_rejects_deep_block_nesting() {
        let html = format!(
            "{}text{}",
            "<div>".repeat(OVER_MAX_DEPTH),
            "</div>".repeat(OVER_MAX_DEPTH)
        );
        assert!(pathological_markup_reason(&html).is_some());
    }

    #[test]
    fn pathological_markup_rejects_deep_inline_nesting() {
        // The stack-overflow SIGABRT case: deeply nested inline elements.
        let html = "<span>".repeat(OVER_MAX_DEPTH);
        assert!(pathological_markup_reason(&html).is_some());
    }

    #[test]
    fn pathological_markup_rejects_deep_nesting_with_text_between_opens() {
        // Text between opens does NOT close a <div>, so this nests genuinely deep.
        // A scan that reset depth on text (the earlier bug) would miss it.
        let mut html = String::from("<html><body>");
        for _ in 0..OVER_MAX_DEPTH {
            html.push_str("<div>a");
        }
        assert!(pathological_markup_reason(&html).is_some());
    }

    #[test]
    fn pathological_markup_allows_flat_divs_with_text() {
        // Balanced flat <div>text</div> repeated is shallow and must be allowed.
        let html = "<div>paragraph of text</div>".repeat(6000);
        assert!(pathological_markup_reason(&html).is_none());
    }

    #[test]
    fn pathological_markup_rejects_deep_nesting_masked_by_unmatched_closes() {
        // An unmatched </b> is ignored by the parser, so it must not cancel the
        // open <div>; this is still genuinely deep.
        let mut html = String::from("<html><body>");
        for _ in 0..OVER_MAX_DEPTH {
            html.push_str("<div></b>");
        }
        assert!(pathological_markup_reason(&html).is_some());
    }

    #[test]
    fn pathological_markup_allows_shallow_overlapping_tag_soup() {
        // Sloppy overlapping inline tags (<b><i>x</b></i>) are real-world tag soup
        // and stay shallow; the guard must not reject them.
        let html = "<html><body><b><i>x</b></i></body></html>".repeat(2000);
        assert!(pathological_markup_reason(&html).is_none());
    }

    #[test]
    fn pathological_markup_rejects_self_closing_syntax_on_non_void_elements() {
        // HTML5 ignores the trailing slash on non-void HTML elements, so <div/>
        // still opens a div and nests. The guard must count these.
        for tag in ["<div/>", "<section/>", "<template/>", "<my-widget/>"] {
            let mut html = String::from("<html><body>");
            for _ in 0..OVER_MAX_DEPTH {
                html.push_str(tag);
            }
            assert!(
                pathological_markup_reason(&html).is_some(),
                "self-closing {tag} should count toward depth"
            );
        }
    }

    #[test]
    fn pathological_markup_allows_foreign_self_closing_leaves() {
        // Inside SVG the trailing slash really self-closes, so thousands of
        // sibling <circle/>/<rect/>/<path/> are flat leaves, not nesting.
        let mut html = String::from("<html><body><svg>");
        for _ in 0..6000 {
            html.push_str("<circle/><rect/><path/>");
        }
        html.push_str("</svg></body></html>");
        assert!(pathological_markup_reason(&html).is_none());
        // Repeated self-closed empty <svg/> roots are flat siblings too.
        assert!(pathological_markup_reason(&"<svg/>".repeat(6000)).is_none());
    }

    #[test]
    fn pathological_markup_rejects_self_closing_foreign_names_in_html_namespace() {
        // In the HTML namespace html5ever ignores the slash on these SVG/MathML
        // names, so <g/>, <path/>, <feGaussianBlur/>, <fencedframe/> etc. open and
        // nest. Only inside genuine foreign content do they self-close. Covers the
        // round-5 bypass.
        for tag in [
            "<g/>",
            "<path/>",
            "<foreignObject/>",
            "<desc/>",
            "<feGaussianBlur/>",
            "<fencedframe/>",
            "<mspace/>",
        ] {
            let mut html = String::from("<html><body>");
            for _ in 0..OVER_MAX_DEPTH {
                html.push_str(tag);
            }
            assert!(
                pathological_markup_reason(&html).is_some(),
                "self-closing {tag} in HTML namespace must count toward depth"
            );
        }
    }

    #[test]
    fn pathological_markup_rejects_self_closing_rawtext_then_foreign_breakout() {
        // A self-closed <style/>/<script/> inside SVG is an empty foreign leaf, not
        // a raw-text run to EOF; the following <div/> break out to HTML and nest.
        // Skipping to </style> here would hide them (round-5 bypass).
        for rawtext in ["<style/>", "<script/>", "<textarea/>"] {
            let mut html = format!("<html><body><svg>{rawtext}");
            for _ in 0..OVER_MAX_DEPTH {
                html.push_str("<div/>");
            }
            html.push_str("</svg></body></html>");
            assert!(
                pathological_markup_reason(&html).is_some(),
                "self-closing {rawtext} in SVG must not hide the following breakout nesting"
            );
        }
    }

    #[test]
    fn pathological_markup_rejects_foreign_void_name_nesting() {
        // HTML void/optional-close names are only void in the HTML namespace. In
        // SVG/MathML the non-breakout ones (<input>, <option>, <col>, <tr>, <base>,
        // <track> ...) are ordinary foreign elements that nest — html5ever parses
        // <svg><input> as SVG. (li/meta/etc. are breakout names, handled elsewhere.)
        // Round-6 bug.
        for name in ["input", "option", "col", "tr", "base", "track", "caption"] {
            let mut html = String::from("<html><body><svg>");
            for _ in 0..OVER_MAX_DEPTH {
                html.push_str(&format!("<{name}>"));
            }
            assert!(
                pathological_markup_reason(&html).is_some(),
                "foreign <{name}> should nest and be rejected"
            );
        }
    }

    #[test]
    fn pathological_markup_font_breakout_is_attribute_conditional() {
        // <font> breaks out of foreign content only with a color/face/size
        // attribute; a bare <font/> inside SVG is a foreign leaf. Round-6 bug.
        let mut bare = String::from("<html><body><svg>");
        for _ in 0..5000 {
            bare.push_str("<font/>");
        }
        bare.push_str("</svg></body></html>");
        assert!(
            pathological_markup_reason(&bare).is_none(),
            "bare self-closing <font/> in SVG is a foreign leaf"
        );
        let mut colored = String::from("<html><body><svg>");
        for _ in 0..OVER_MAX_DEPTH {
            colored.push_str("<font color=\"red\"/>");
        }
        assert!(
            pathological_markup_reason(&colored).is_some(),
            "<font color> breaks out to HTML and nests"
        );
    }

    #[test]
    fn pathological_markup_annotation_xml_hosts_html_only_with_encoding() {
        // annotation-xml hosts HTML children only with a text/html encoding; without
        // it, its children are MathML leaves that self-close. Round-6 false-reject.
        let mut plain = String::from("<html><body><math><annotation-xml>");
        for _ in 0..5000 {
            plain.push_str("<path/>");
        }
        plain.push_str("</annotation-xml></math></body></html>");
        assert!(
            pathological_markup_reason(&plain).is_none(),
            "annotation-xml without html encoding hosts MathML leaves"
        );
        let mut html_enc =
            String::from("<html><body><math><annotation-xml encoding=\"text/html\">");
        for _ in 0..OVER_MAX_DEPTH {
            html_enc.push_str("<div/>");
        }
        assert!(
            pathological_markup_reason(&html_enc).is_some(),
            "annotation-xml[encoding=text/html] hosts nesting HTML"
        );
    }

    #[test]
    fn pathological_markup_rejects_foreign_self_closing_after_breakout() {
        // Once a breakout (<div>) pops the svg ancestors, the parser is in HTML, so
        // subsequent <path/> open and nest even though the same name self-closes
        // inside genuine SVG. The guard must pop the foreign ancestors on breakout.
        let mut html = String::from("<html><body><svg><div></div>");
        for _ in 0..OVER_MAX_DEPTH {
            html.push_str("<path/>");
        }
        html.push_str("</body></html>");
        assert!(
            pathological_markup_reason(&html).is_some(),
            "foreign-named self-closing tags after a breakout must count toward depth"
        );
        // But a still-open svg keeps its self-closing children as leaves.
        let mut ok = String::from("<html><body><svg>");
        for _ in 0..8000 {
            ok.push_str("<path/>");
        }
        ok.push_str("</svg></body></html>");
        assert!(pathological_markup_reason(&ok).is_none());
    }

    #[test]
    fn pathological_markup_rejects_self_closing_html_at_foreign_integration_points() {
        // At an SVG/MathML integration point the parser is back in HTML mode, so a
        // self-closing <div/> opens and nests. Deciding by name (div is not a
        // foreign leaf) rejects these deterministically instead of leaking them to
        // the wall-clock deadline. Covers the round-4 bypass.
        let prefixes = [
            "<html><body><svg><foreignObject>",
            "<html><body><svg><desc>",
            "<html><body><svg><title>",
            "<html><body><math><mtext>",
            "<html><body><math><mi>",
            "<html><body><math><annotation-xml encoding=\"text/html\">",
            // A bare foreign subtree: <div> breaks out to HTML content.
            "<html><body><svg>",
        ];
        for prefix in prefixes {
            let mut html = String::from(prefix);
            for _ in 0..OVER_MAX_DEPTH {
                html.push_str("<div/>");
            }
            assert!(
                pathological_markup_reason(&html).is_some(),
                "deep self-closing <div/> under {prefix} must be rejected"
            );
        }
    }

    #[test]
    fn pathological_markup_rejects_attribute_flood() {
        let mut tag = String::from("<div");
        for k in 0..3000 {
            tag.push_str(&format!(" a{k}=\"1\""));
        }
        tag.push('>');
        assert!(pathological_markup_reason(&tag).is_some());
    }

    #[test]
    fn pathological_markup_allows_stray_quote_in_unquoted_value() {
        // A real WordPress page had `content=#FF0000"` — an unquoted value with a
        // stray quote. A quote outside value position must not open a quoted region
        // and desync the scan (which previously swallowed the rest of the document
        // and tripped the attribute-flood check on a normal page).
        let mut html = String::from(
            "<html><head>\
             <meta name=\"msapplication-navbutton-color\" content=#FF0000\">\
             <meta name=\"theme-color\" content=\"#FF0000\">",
        );
        for _ in 0..300 {
            html.push_str("<meta property=\"og:tag\" content=\"value here\" />");
        }
        html.push_str("</head><body><p>Real article text.</p></body></html>");
        assert!(
            pathological_markup_reason(&html).is_none(),
            "a stray quote in an unquoted value must not trip the flood guard"
        );
    }

    #[test]
    fn pathological_markup_allows_normal_article() {
        let html = "<html><body><article><h1>Title</h1>\
             <p>A perfectly ordinary paragraph of readable text goes here.</p>\
             <p>Another ordinary paragraph with a <a href=\"x\">link</a> inside it.</p>\
             </article></body></html>";
        assert!(pathological_markup_reason(html).is_none());
    }

    #[test]
    fn pathological_markup_allows_flat_optional_close_lists() {
        // Thousands of flat <li> with text and no explicit close must NOT be
        // rejected: the text between opens resets the pure-open run.
        let mut html = String::from("<ul>");
        for k in 0..6000 {
            html.push_str(&format!("<li>item {k}"));
        }
        html.push_str("</ul>");
        assert!(pathological_markup_reason(&html).is_none());
    }

    #[test]
    fn pathological_markup_allows_void_element_flood() {
        let html = "<br>".repeat(6000);
        assert!(pathological_markup_reason(&html).is_none());
    }

    #[test]
    fn pathological_markup_allows_large_well_formed_table() {
        let mut html = String::from("<table>");
        for r in 0..4000 {
            html.push_str(&format!("<tr><td>cell {r}</td></tr>"));
        }
        html.push_str("</table>");
        assert!(pathological_markup_reason(&html).is_none());
    }

    #[test]
    fn pathological_markup_ignores_angle_brackets_inside_script() {
        // '<' inside a <script> body must not be miscounted as nesting.
        let mut js = String::from("<script>");
        for _ in 0..5000 {
            js.push_str("if (a<b) { doThing(); } ");
        }
        js.push_str("</script><p>ok</p>");
        assert!(pathological_markup_reason(&js).is_none());
    }
}
