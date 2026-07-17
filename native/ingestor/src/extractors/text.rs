use anyhow::Result;
use chardetng::EncodingDetector;
use encoding_rs::{Encoding, UTF_8};
use serde_json::{json, Map, Value};
use std::borrow::Cow;
use std::time::{Duration, Instant};

use super::language::{detect_language, language_detection_metadata};
use super::types::GeneralExtractedDocument;

#[derive(Debug)]
struct TextDecodeResult<'a> {
    encoding_name: &'static str,
    decoded: Cow<'a, str>,
    source: &'static str,
    declared_encoding: Option<String>,
    had_errors: bool,
}

pub(crate) fn extract_text_bytes(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
) -> Result<GeneralExtractedDocument> {
    let total_started_at = Instant::now();

    let decode_started_at = Instant::now();
    let decoded = decode_text_bytes(bytes, mime_type);
    let decode_duration = decode_started_at.elapsed();

    let normalize_started_at = Instant::now();
    let normalized = normalize_text(decoded.decoded.as_ref());
    let line_count = normalized.lines().count();
    let normalize_duration = normalize_started_at.elapsed();

    let clean_started_at = Instant::now();
    let content = clean_text(&normalized);
    let clean_duration = clean_started_at.elapsed();

    let language_started_at = Instant::now();
    let language_report = detect_language(&content);
    let language_duration = language_started_at.elapsed();
    let language = language_report.language.clone();
    let content_length = content.chars().count();

    let mut timing_ms = Map::new();
    timing_ms.insert(
        "decode".to_string(),
        json!(duration_millis(decode_duration)),
    );
    timing_ms.insert(
        "normalize".to_string(),
        json!(duration_millis(normalize_duration)),
    );
    timing_ms.insert(
        "clean_text".to_string(),
        json!(duration_millis(clean_duration)),
    );
    timing_ms.insert(
        "language_detect".to_string(),
        json!(duration_millis(language_duration)),
    );
    timing_ms.insert(
        "extract_text_bytes_total".to_string(),
        json!(duration_millis(total_started_at.elapsed())),
    );
    let timing_value = Value::Object(timing_ms.clone());

    let mut content_extraction = Map::new();
    content_extraction.insert("extractor_type".to_string(), json!("text"));
    content_extraction.insert("source_format".to_string(), json!("text"));
    content_extraction.insert("file_size".to_string(), json!(bytes.len()));
    content_extraction.insert("encoding".to_string(), json!(decoded.encoding_name));
    content_extraction.insert("encoding_source".to_string(), json!(decoded.source));
    content_extraction.insert(
        "declared_encoding".to_string(),
        json!(decoded.declared_encoding),
    );
    content_extraction.insert("encoding_had_errors".to_string(), json!(decoded.had_errors));
    content_extraction.insert("line_count".to_string(), json!(line_count));
    content_extraction.insert("content_length".to_string(), json!(content_length));
    content_extraction.insert("timing_ms".to_string(), timing_value.clone());
    if let Some(source_path) = source_path {
        content_extraction.insert("original_bucket_path".to_string(), json!(source_path));
    }
    if let Some(mime_type) = mime_type {
        content_extraction.insert("mime_type".to_string(), json!(mime_type));
    }

    let extraction_metadata = json!({
        "file": {
            "size": bytes.len(),
            "encoding": decoded.encoding_name,
        },
        "content_extraction": content_extraction,
        "title_extraction": {
            "method": "none",
        },
        "language_detection": language_detection_metadata(&language_report),
    });

    Ok(GeneralExtractedDocument {
        source_format: "text".to_string(),
        title: String::new(),
        content,
        language,
        extraction_metadata,
        timing_ms: timing_value,
        content_length,
    })
}

fn decode_text_bytes<'a>(data: &'a [u8], mime_type: Option<&str>) -> TextDecodeResult<'a> {
    if data.starts_with(&[0xef, 0xbb, 0xbf]) {
        let (text, _, had_errors) = UTF_8.decode(&data[3..]);
        return TextDecodeResult {
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
        return TextDecodeResult {
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
        return TextDecodeResult {
            encoding_name: encoding.name(),
            decoded: text,
            source: "bom",
            declared_encoding: Some("utf-16be".to_string()),
            had_errors,
        };
    }

    let declared_encoding = charset_from_mime(mime_type);
    if let Some(label) = declared_encoding.as_deref() {
        if let Some(encoding) = Encoding::for_label(label.as_bytes()) {
            let (text, _, had_errors) = encoding.decode(data);
            return TextDecodeResult {
                encoding_name: encoding.name(),
                decoded: text,
                source: "mime_charset",
                declared_encoding,
                had_errors,
            };
        }
    }

    let mut detector = EncodingDetector::new();
    detector.feed(&data[..data.len().min(10_000)], true);
    let encoding = detector.guess(None, true);
    let (text, _, had_errors) = encoding.decode(data);
    TextDecodeResult {
        encoding_name: encoding.name(),
        decoded: text,
        source: "chardetng",
        declared_encoding,
        had_errors,
    }
}

fn charset_from_mime(mime_type: Option<&str>) -> Option<String> {
    mime_type.and_then(|value| {
        value.split(';').skip(1).find_map(|part| {
            let (name, value) = part.trim().split_once('=')?;
            if !name.trim().eq_ignore_ascii_case("charset") {
                return None;
            }
            let charset = value.trim().trim_matches('"').trim_matches('\'');
            if charset.is_empty() {
                None
            } else {
                Some(charset.to_string())
            }
        })
    })
}

fn normalize_text(text: &str) -> String {
    text.replace('\0', "")
        .replace("\r\n", "\n")
        .replace('\r', "\n")
}

fn clean_text(text: &str) -> String {
    text.lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn duration_millis(duration: Duration) -> u128 {
    let millis = duration.as_millis();
    if millis == 0 && !duration.is_zero() {
        1
    } else {
        millis
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_utf8_text_with_python_style_cleanup() {
        let document = extract_text_bytes(
            b" First line\r\n\r\nSecond line\0 \rThird line\n",
            Some("notes/sample.txt"),
            Some("text/plain; charset=utf-8"),
        )
        .unwrap();

        assert_eq!(document.source_format, "text");
        assert_eq!(document.content, "First line\nSecond line\nThird line");
        assert_eq!(document.content_length, document.content.chars().count());
        assert_eq!(
            document.extraction_metadata["content_extraction"]["extractor_type"],
            "text"
        );
        assert_eq!(
            document.extraction_metadata["content_extraction"]["line_count"],
            4
        );
        assert_eq!(
            document.extraction_metadata["content_extraction"]["encoding"],
            "UTF-8"
        );
        assert_eq!(
            document.extraction_metadata["content_extraction"]["original_bucket_path"],
            "notes/sample.txt"
        );
    }

    #[test]
    fn replaces_invalid_bytes_in_declared_utf8() {
        let document =
            extract_text_bytes(b"valid \xff text", None, Some("text/plain; charset=utf-8"))
                .unwrap();

        assert_eq!(document.content, "valid \u{fffd} text");
        assert_eq!(
            document.extraction_metadata["content_extraction"]["encoding_had_errors"],
            true
        );
    }

    #[test]
    fn decodes_utf16le_bom() {
        let bytes = [
            0xff, 0xfe, b'H', 0x00, b'i', 0x00, b'\r', 0x00, b'\n', 0x00, b't', 0x00, b'h', 0x00,
            b'e', 0x00, b'r', 0x00, b'e', 0x00,
        ];

        let document = extract_text_bytes(&bytes, None, Some("text/plain")).unwrap();

        assert_eq!(document.content, "Hi\nthere");
        assert_eq!(
            document.extraction_metadata["content_extraction"]["encoding_source"],
            "bom"
        );
        assert_eq!(
            document.extraction_metadata["content_extraction"]["encoding"],
            "UTF-16LE"
        );
    }

    #[test]
    fn falls_back_to_chardetng_without_declared_charset() {
        let decoded = decode_text_bytes(b"plain ascii", Some("text/plain"));

        assert_eq!(decoded.source, "chardetng");
        assert_eq!(decoded.decoded.as_ref(), "plain ascii");
    }
}
