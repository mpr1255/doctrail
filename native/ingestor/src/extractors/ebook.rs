use anyhow::{anyhow, bail, Context, Result};
use once_cell::sync::Lazy;
use regex::Regex;
use scraper::{Html, Selector};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::io::{Cursor, Read};
use std::path::Path;
use std::time::{Duration, Instant};
use zip::ZipArchive;

use super::language::{detect_language, language_detection_metadata};
use super::types::GeneralExtractedDocument;

static ROOTFILE_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?is)<rootfile\b([^>]*)>").expect("rootfile regex"));
static ITEM_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?is)<item\b([^>]*)>").expect("opf item regex"));
static ITEMREF_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?is)<itemref\b([^>]*)>").expect("opf itemref regex"));
static ATTR_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r#"(?is)([A-Za-z_][A-Za-z0-9_.:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')"#)
        .expect("xml attr regex")
});
static TITLE_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?is)<(?:[A-Za-z0-9_]+:)?title\b[^>]*>(.*?)</(?:[A-Za-z0-9_]+:)?title>")
        .expect("opf title regex")
});
static ENTITY_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"&(#x[0-9A-Fa-f]+|#\d+|amp|lt|gt|quot|apos);").expect("entity regex"));

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum EbookFormat {
    Epub,
    Mobi,
    Azw,
    Azw3,
    Djvu,
    Rtf,
    Unknown,
}

#[derive(Debug)]
struct ShellFallbackSpec {
    source_format: &'static str,
    extractor_type: &'static str,
    command: &'static str,
    args_description: &'static str,
    timeout_seconds: u64,
}

#[derive(Debug)]
struct EpubManifestItem {
    href: String,
    media_type: Option<String>,
}

#[derive(Debug)]
struct EpubPackage {
    opf_path: String,
    title: Option<String>,
    manifest: BTreeMap<String, EpubManifestItem>,
    spine: Vec<String>,
}

pub(crate) fn extract_ebook_bytes(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
) -> Result<GeneralExtractedDocument> {
    match infer_ebook_format(bytes, source_path, mime_type) {
        EbookFormat::Epub => extract_epub_bytes(bytes, source_path, mime_type),
        EbookFormat::Mobi => unsupported_shell_fallback(bytes, source_path, mime_type, mobi_spec()),
        EbookFormat::Azw => unsupported_shell_fallback(bytes, source_path, mime_type, azw_spec()),
        EbookFormat::Azw3 => unsupported_shell_fallback(bytes, source_path, mime_type, azw3_spec()),
        EbookFormat::Djvu => unsupported_shell_fallback(bytes, source_path, mime_type, djvu_spec()),
        EbookFormat::Rtf => unsupported_shell_fallback(bytes, source_path, mime_type, rtf_spec()),
        EbookFormat::Unknown => bail!(
            "unsupported ebook format: could not infer EPUB, MOBI/AZW, DJVU, or RTF from mime_type={:?} source_path={:?}",
            mime_type,
            source_path
        ),
    }
}

fn extract_epub_bytes(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
) -> Result<GeneralExtractedDocument> {
    let total_started_at = Instant::now();

    let zip_started_at = Instant::now();
    let mut archive = ZipArchive::new(Cursor::new(bytes)).context("invalid EPUB ZIP container")?;
    let zip_open_duration = zip_started_at.elapsed();
    let zip_entry_count = archive.len();
    let names = sorted_zip_names(&mut archive)?;

    let opf_started_at = Instant::now();
    let package = read_epub_package(&mut archive, &names)?;
    let opf_duration = opf_started_at.elapsed();

    let order_started_at = Instant::now();
    let all_html_paths = sorted_html_paths(&names);
    let (ordered_paths, order_source, fallback_reason) =
        ordered_epub_html_paths(&package, &all_html_paths);
    let order_duration = order_started_at.elapsed();

    let html_started_at = Instant::now();
    let mut extracted_parts = Vec::new();
    let mut missing_items = Vec::new();
    for path in &ordered_paths {
        match read_zip_member_lossy(&mut archive, path) {
            Ok(html) => {
                let text = html_to_text(&html);
                if !text.is_empty() {
                    extracted_parts.push(text);
                }
            }
            Err(error) => {
                missing_items.push(json!({
                    "path": path,
                    "error": error.to_string(),
                }));
            }
        }
    }
    let html_duration = html_started_at.elapsed();

    let content = clean_text(&extracted_parts.join("\n\n"));
    if content.is_empty() {
        bail!(
            "EPUB extraction produced no readable text from {} selected HTML/XHTML/HTM item(s)",
            ordered_paths.len()
        );
    }

    let (title, title_source) = match package
        .title
        .as_deref()
        .map(str::trim)
        .filter(|title| !title.is_empty())
    {
        Some(title) => (title.to_string(), "opf_title"),
        None => match title_from_content(&content) {
            Some(title) => (title, "content_first_line"),
            None => (String::new(), "none"),
        },
    };

    let language_started_at = Instant::now();
    let language_detection = detect_language(&content);
    let language_duration = language_started_at.elapsed();
    let language = language_detection.language.clone();

    let mut timing_ms = Map::new();
    timing_ms.insert(
        "zip_open".to_string(),
        json!(duration_millis(zip_open_duration)),
    );
    timing_ms.insert(
        "opf_parse".to_string(),
        json!(duration_millis(opf_duration)),
    );
    timing_ms.insert(
        "item_order".to_string(),
        json!(duration_millis(order_duration)),
    );
    timing_ms.insert(
        "html_extract".to_string(),
        json!(duration_millis(html_duration)),
    );
    timing_ms.insert(
        "language_detect".to_string(),
        json!(duration_millis(language_duration)),
    );
    timing_ms.insert(
        "extract_ebook_total".to_string(),
        json!(duration_millis(total_started_at.elapsed())),
    );

    let mut content_extraction = Map::new();
    content_extraction.insert("extractor_type".to_string(), json!("epub"));
    content_extraction.insert("source_format".to_string(), json!("epub"));
    content_extraction.insert("file_size".to_string(), json!(bytes.len()));
    content_extraction.insert("title_source".to_string(), json!(title_source));
    content_extraction.insert("epub_title".to_string(), json!(package.title));
    content_extraction.insert("opf_path".to_string(), json!(package.opf_path));
    content_extraction.insert("zip_entry_count".to_string(), json!(zip_entry_count));
    content_extraction.insert(
        "opf_manifest_item_count".to_string(),
        json!(package.manifest.len()),
    );
    content_extraction.insert(
        "epub_html_item_count".to_string(),
        json!(all_html_paths.len()),
    );
    content_extraction.insert(
        "epub_spine_item_count".to_string(),
        json!(package.spine.len()),
    );
    content_extraction.insert(
        "selected_item_count".to_string(),
        json!(ordered_paths.len()),
    );
    content_extraction.insert(
        "extracted_item_count".to_string(),
        json!(extracted_parts.len()),
    );
    content_extraction.insert("order_source".to_string(), json!(order_source));
    content_extraction.insert(
        "fallback_reason".to_string(),
        fallback_reason.map(Value::String).unwrap_or(Value::Null),
    );
    content_extraction.insert("timing_ms".to_string(), Value::Object(timing_ms.clone()));
    if !missing_items.is_empty() {
        content_extraction.insert("missing_items".to_string(), Value::Array(missing_items));
    }
    if let Some(source_path) = source_path {
        content_extraction.insert("original_bucket_path".to_string(), json!(source_path));
    }
    if let Some(mime_type) = mime_type {
        content_extraction.insert("mime_type".to_string(), json!(mime_type));
    }

    let extraction_metadata = json!({
        "file": {
            "size": bytes.len(),
            "encoding": "utf-8-lossy",
        },
        "content_extraction": content_extraction,
        "title_extraction": {
            "method": title_source,
        },
        "language_detection": language_detection_metadata(&language_detection),
    });

    Ok(GeneralExtractedDocument {
        source_format: "epub".to_string(),
        title,
        content_length: content.chars().count(),
        content,
        language,
        extraction_metadata,
        timing_ms: Value::Object(timing_ms),
    })
}

fn read_epub_package(
    archive: &mut ZipArchive<Cursor<&[u8]>>,
    names: &[String],
) -> Result<EpubPackage> {
    let opf_path = find_opf_path(archive, names)?;
    let opf = read_zip_member_lossy(archive, &opf_path)?;
    let title = opf_title(&opf);
    let manifest = opf_manifest(&opf, &opf_path);
    let spine = opf_spine(&opf);

    Ok(EpubPackage {
        opf_path,
        title,
        manifest,
        spine,
    })
}

fn find_opf_path(archive: &mut ZipArchive<Cursor<&[u8]>>, names: &[String]) -> Result<String> {
    if names.iter().any(|name| name == "META-INF/container.xml") {
        let container = read_zip_member_lossy(archive, "META-INF/container.xml")?;
        for captures in ROOTFILE_RE.captures_iter(&container) {
            let attrs = xml_attrs(captures.get(1).map(|m| m.as_str()).unwrap_or_default());
            let full_path = attrs.get("full-path").or_else(|| attrs.get("full_path"));
            let media_type = attrs.get("media-type").or_else(|| attrs.get("media_type"));
            let Some(full_path) = full_path else {
                continue;
            };
            let is_package = media_type
                .map(|value| value.eq_ignore_ascii_case("application/oebps-package+xml"))
                .unwrap_or_else(|| full_path.to_ascii_lowercase().ends_with(".opf"));
            if is_package && names.iter().any(|name| name == full_path) {
                return Ok(full_path.to_string());
            }
        }
    }

    names
        .iter()
        .find(|name| name.to_ascii_lowercase().ends_with(".opf"))
        .cloned()
        .ok_or_else(|| anyhow!("EPUB ZIP does not contain an OPF package document"))
}

fn opf_title(opf: &str) -> Option<String> {
    TITLE_RE
        .captures(opf)
        .and_then(|captures| {
            captures.get(1).map(|matched| {
                let title = decode_xml_entities(matched.as_str());
                clean_text(&title)
            })
        })
        .filter(|title| !title.is_empty())
}

fn opf_manifest(opf: &str, opf_path: &str) -> BTreeMap<String, EpubManifestItem> {
    let base_dir = zip_parent(opf_path);
    let mut items = BTreeMap::new();
    for captures in ITEM_RE.captures_iter(opf) {
        let attrs = xml_attrs(captures.get(1).map(|m| m.as_str()).unwrap_or_default());
        let (Some(id), Some(href)) = (attrs.get("id"), attrs.get("href")) else {
            continue;
        };
        let path = resolve_epub_href(&base_dir, href);
        items.insert(
            id.to_string(),
            EpubManifestItem {
                href: path,
                media_type: attrs.get("media-type").cloned(),
            },
        );
    }
    items
}

fn opf_spine(opf: &str) -> Vec<String> {
    ITEMREF_RE
        .captures_iter(opf)
        .filter_map(|captures| {
            let attrs = xml_attrs(captures.get(1).map(|m| m.as_str()).unwrap_or_default());
            attrs.get("idref").cloned()
        })
        .collect()
}

fn ordered_epub_html_paths(
    package: &EpubPackage,
    all_html_paths: &[String],
) -> (Vec<String>, &'static str, Option<String>) {
    let all_html_set: BTreeSet<&str> = all_html_paths.iter().map(String::as_str).collect();
    let mut seen = BTreeSet::new();
    let mut ordered = Vec::new();

    for idref in &package.spine {
        let Some(item) = package.manifest.get(idref) else {
            continue;
        };
        if !is_epub_html_item(item) || !all_html_set.contains(item.href.as_str()) {
            continue;
        }
        if seen.insert(item.href.clone()) {
            ordered.push(item.href.clone());
        }
    }

    if ordered.is_empty() {
        return (
            all_html_paths.to_vec(),
            "sorted_html_entries",
            Some("opf_spine_unavailable_sorted_html_fallback".to_string()),
        );
    }

    for path in all_html_paths {
        if seen.insert(path.clone()) {
            ordered.push(path.clone());
        }
    }

    if ordered.len() > package.spine.len() {
        (
            ordered,
            "opf_spine_then_sorted_remainder",
            Some("non_spine_html_items_appended_in_sorted_order".to_string()),
        )
    } else {
        (ordered, "opf_spine", None)
    }
}

fn is_epub_html_item(item: &EpubManifestItem) -> bool {
    item.media_type
        .as_deref()
        .map(|media_type| {
            let media_type = media_type.to_ascii_lowercase();
            media_type == "application/xhtml+xml" || media_type == "text/html"
        })
        .unwrap_or_else(|| is_html_path(&item.href))
        || is_html_path(&item.href)
}

fn sorted_zip_names(archive: &mut ZipArchive<Cursor<&[u8]>>) -> Result<Vec<String>> {
    let mut names = Vec::with_capacity(archive.len());
    for index in 0..archive.len() {
        let file = archive
            .by_index(index)
            .with_context(|| format!("failed to read EPUB ZIP entry at index {index}"))?;
        if !file.is_dir() {
            names.push(file.name().to_string());
        }
    }
    names.sort();
    Ok(names)
}

fn sorted_html_paths(names: &[String]) -> Vec<String> {
    names
        .iter()
        .filter(|name| is_html_path(name))
        .cloned()
        .collect()
}

fn is_html_path(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    lower.ends_with(".html") || lower.ends_with(".xhtml") || lower.ends_with(".htm")
}

fn read_zip_member_lossy(archive: &mut ZipArchive<Cursor<&[u8]>>, name: &str) -> Result<String> {
    let mut file = archive
        .by_name(name)
        .with_context(|| format!("EPUB ZIP member not found: {name}"))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)
        .with_context(|| format!("failed reading EPUB ZIP member: {name}"))?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn html_to_text(html: &str) -> String {
    let document = Html::parse_document(html);
    let raw_text = Selector::parse("body")
        .ok()
        .and_then(|selector| document.select(&selector).next())
        .map(|body| body.text().collect::<Vec<_>>().join("\n"))
        .unwrap_or_else(|| {
            document
                .root_element()
                .text()
                .collect::<Vec<_>>()
                .join("\n")
        });
    clean_text(&raw_text)
}

fn clean_text(text: &str) -> String {
    text.lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn title_from_content(content: &str) -> Option<String> {
    content
        .lines()
        .map(str::trim)
        .find(|line| {
            !line.is_empty()
                && !line.starts_with("http://")
                && !line.starts_with("https://")
                && line.chars().count() <= 120
        })
        .map(str::to_string)
}

fn xml_attrs(raw: &str) -> BTreeMap<String, String> {
    ATTR_RE
        .captures_iter(raw)
        .filter_map(|captures| {
            let key = captures.get(1)?.as_str().to_ascii_lowercase();
            let value = captures
                .get(2)
                .or_else(|| captures.get(3))
                .map(|matched| decode_xml_entities(matched.as_str()))?;
            Some((key, value))
        })
        .collect()
}

fn decode_xml_entities(value: &str) -> String {
    ENTITY_RE
        .replace_all(value, |captures: &regex::Captures<'_>| {
            let entity = captures.get(1).map(|m| m.as_str()).unwrap_or_default();
            match entity {
                "amp" => "&".to_string(),
                "lt" => "<".to_string(),
                "gt" => ">".to_string(),
                "quot" => "\"".to_string(),
                "apos" => "'".to_string(),
                _ if entity.starts_with("#x") => u32::from_str_radix(&entity[2..], 16)
                    .ok()
                    .and_then(char::from_u32)
                    .map(|ch| ch.to_string())
                    .unwrap_or_else(|| captures[0].to_string()),
                _ if entity.starts_with('#') => entity[1..]
                    .parse::<u32>()
                    .ok()
                    .and_then(char::from_u32)
                    .map(|ch| ch.to_string())
                    .unwrap_or_else(|| captures[0].to_string()),
                _ => captures[0].to_string(),
            }
        })
        .into_owned()
}

fn zip_parent(path: &str) -> String {
    path.rsplit_once('/')
        .map(|(parent, _)| parent.to_string())
        .unwrap_or_default()
}

fn resolve_epub_href(base_dir: &str, href: &str) -> String {
    let href = href.split('#').next().unwrap_or(href);
    let decoded = urlencoding::decode(href)
        .map(|value| value.into_owned())
        .unwrap_or_else(|_| href.to_string());
    let joined = if decoded.starts_with('/') || base_dir.is_empty() {
        decoded.trim_start_matches('/').to_string()
    } else {
        format!("{base_dir}/{decoded}")
    };
    normalize_zip_path(&joined)
}

fn normalize_zip_path(path: &str) -> String {
    let mut parts = Vec::new();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            _ => parts.push(part),
        }
    }
    parts.join("/")
}

fn infer_ebook_format(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
) -> EbookFormat {
    if let Some(mime_type) = mime_type.map(normalize_mime_type) {
        match mime_type.as_str() {
            "application/epub+zip" => return EbookFormat::Epub,
            "application/x-mobipocket-ebook" | "application/vnd.amazon.ebook" => {
                return format_from_path(source_path).unwrap_or(EbookFormat::Mobi);
            }
            "image/vnd.djvu" | "image/x-djvu" => return EbookFormat::Djvu,
            "application/rtf" | "text/rtf" | "application/x-rtf" => return EbookFormat::Rtf,
            _ => {}
        }
    }

    if let Some(format) = format_from_path(source_path) {
        return format;
    }

    if bytes.starts_with(b"PK\x03\x04") || bytes.starts_with(b"PK\x05\x06") {
        return EbookFormat::Epub;
    }
    if bytes.starts_with(b"{\\rtf") {
        return EbookFormat::Rtf;
    }
    if bytes.starts_with(b"AT&TFORM") {
        return EbookFormat::Djvu;
    }
    if bytes
        .windows(b"BOOKMOBI".len())
        .take(4096)
        .any(|window| window == b"BOOKMOBI")
    {
        return EbookFormat::Mobi;
    }

    EbookFormat::Unknown
}

fn normalize_mime_type(mime_type: &str) -> String {
    mime_type
        .split(';')
        .next()
        .unwrap_or(mime_type)
        .trim()
        .to_ascii_lowercase()
}

fn format_from_path(source_path: Option<&str>) -> Option<EbookFormat> {
    let path = source_path?;
    let path = path.split(['?', '#']).next().unwrap_or(path);
    match Path::new(path)
        .extension()
        .and_then(|extension| extension.to_str())
        .map(|extension| extension.to_ascii_lowercase())
        .as_deref()
    {
        Some("epub") => Some(EbookFormat::Epub),
        Some("mobi") => Some(EbookFormat::Mobi),
        Some("azw") => Some(EbookFormat::Azw),
        Some("azw3") => Some(EbookFormat::Azw3),
        Some("djvu") | Some("djv") => Some(EbookFormat::Djvu),
        Some("rtf") => Some(EbookFormat::Rtf),
        _ => None,
    }
}

fn unsupported_shell_fallback(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
    spec: ShellFallbackSpec,
) -> Result<GeneralExtractedDocument> {
    let timing_ms = json!({
        "extract_ebook_total": 0,
    });
    let metadata = json!({
        "file": {
            "size": bytes.len(),
            "encoding": null,
        },
        "content_extraction": {
            "extractor_type": spec.extractor_type,
            "source_format": spec.source_format,
            "file_size": bytes.len(),
            "title_source": "none",
            "selected_item_count": 0,
            "extracted_item_count": 0,
            "fallback_reason": "external_command_fallback_not_enabled",
            "fallback_command": spec.command,
            "fallback_args": spec.args_description,
            "fallback_timeout_seconds": spec.timeout_seconds,
            "original_bucket_path": source_path,
            "mime_type": mime_type,
            "timing_ms": timing_ms,
        },
        "title_extraction": {
            "method": "none",
        },
        "language_detection": null,
    });

    bail!(
        "{} extraction is not supported by the native Rust ebook extractor yet; fallback_required={}_external_command; declared shell fallback `{}` ({}) is disabled for this byte API. metadata={}",
        spec.source_format,
        spec.source_format,
        spec.command,
        spec.args_description,
        metadata
    )
}

fn mobi_spec() -> ShellFallbackSpec {
    ShellFallbackSpec {
        source_format: "mobi",
        extractor_type: "ebook_convert",
        command: "ebook-convert",
        args_description: "<input> <output.txt> --txt-output-encoding=utf-8",
        timeout_seconds: 180,
    }
}

fn azw_spec() -> ShellFallbackSpec {
    ShellFallbackSpec {
        source_format: "azw",
        ..mobi_spec()
    }
}

fn azw3_spec() -> ShellFallbackSpec {
    ShellFallbackSpec {
        source_format: "azw3",
        ..mobi_spec()
    }
}

fn djvu_spec() -> ShellFallbackSpec {
    ShellFallbackSpec {
        source_format: "djvu",
        extractor_type: "djvu",
        command: "djvutxt",
        args_description: "<input>",
        timeout_seconds: 180,
    }
}

fn rtf_spec() -> ShellFallbackSpec {
    ShellFallbackSpec {
        source_format: "rtf",
        extractor_type: "rtf",
        command: "unrtf",
        args_description: "--text <input>",
        timeout_seconds: 120,
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use zip::write::SimpleFileOptions;
    use zip::ZipWriter;

    #[test]
    fn extracts_epub_in_spine_order_with_opf_title() {
        let epub = synthetic_epub(
            r#"<?xml version="1.0" encoding="UTF-8"?>
            <package xmlns:dc="http://purl.org/dc/elements/1.1/">
              <metadata><dc:title>Synthetic Book &amp; Tests</dc:title></metadata>
              <manifest>
                <item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
                <item id="chapter2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
              </manifest>
              <spine>
                <itemref idref="chapter2"/>
                <itemref idref="chapter1"/>
              </spine>
            </package>"#,
            &[
                (
                    "OPS/chapter1.xhtml",
                    "<html><body><h1>First chapter</h1><p>This appears second.</p></body></html>",
                ),
                (
                    "OPS/chapter2.xhtml",
                    "<html><body><h1>Second chapter</h1><p>This appears first.</p></body></html>",
                ),
            ],
        );

        let extracted = extract_ebook_bytes(
            &epub,
            Some("library/synthetic.epub"),
            Some("application/epub+zip"),
        )
        .expect("epub extraction should succeed");

        assert_eq!(extracted.source_format, "epub");
        assert_eq!(extracted.title, "Synthetic Book & Tests");
        assert!(extracted.content.contains("Second chapter"));
        assert!(extracted.content.contains("First chapter"));
        assert!(
            extracted.content.find("Second chapter") < extracted.content.find("First chapter"),
            "spine order should be preferred over sorted ZIP order"
        );

        let content_extraction = extracted
            .extraction_metadata
            .get("content_extraction")
            .and_then(Value::as_object)
            .expect("content extraction metadata");
        assert_eq!(content_extraction["extractor_type"], "epub");
        assert_eq!(content_extraction["source_format"], "epub");
        assert_eq!(content_extraction["title_source"], "opf_title");
        assert_eq!(content_extraction["opf_manifest_item_count"], 2);
        assert_eq!(content_extraction["epub_spine_item_count"], 2);
        assert_eq!(content_extraction["selected_item_count"], 2);
        assert_eq!(content_extraction["order_source"], "opf_spine");
    }

    #[test]
    fn falls_back_to_sorted_html_when_spine_has_no_html_items() {
        let epub = synthetic_epub(
            r#"<?xml version="1.0" encoding="UTF-8"?>
            <package xmlns:dc="http://purl.org/dc/elements/1.1/">
              <metadata><dc:title>No spine</dc:title></metadata>
              <manifest>
                <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"/>
              </manifest>
              <spine></spine>
            </package>"#,
            &[
                (
                    "OPS/b.xhtml",
                    "<html><body><p>Sorted second fallback text.</p></body></html>",
                ),
                (
                    "OPS/a.xhtml",
                    "<html><body><p>Sorted first fallback text.</p></body></html>",
                ),
            ],
        );

        let extracted = extract_ebook_bytes(&epub, Some("no-spine.epub"), None)
            .expect("sorted HTML fallback should extract text");

        assert!(
            extracted.content.find("Sorted first") < extracted.content.find("Sorted second"),
            "sorted fallback should be stable"
        );
        let content_extraction = extracted
            .extraction_metadata
            .get("content_extraction")
            .and_then(Value::as_object)
            .expect("content extraction metadata");
        assert_eq!(content_extraction["order_source"], "sorted_html_entries");
        assert_eq!(
            content_extraction["fallback_reason"],
            "opf_spine_unavailable_sorted_html_fallback"
        );
    }

    #[test]
    fn reports_shell_fallback_stub_for_rtf() {
        let error = extract_ebook_bytes(
            br"{\rtf1\ansi This is an RTF document.}",
            Some("sample.rtf"),
            Some("application/rtf"),
        )
        .expect_err("RTF should be an explicit shell fallback stub");

        let message = error.to_string();
        assert!(message.contains("rtf extraction is not supported"));
        assert!(message.contains("unrtf"));
        assert!(message.contains("external_command_fallback_not_enabled"));
    }

    fn synthetic_epub(opf: &str, html_items: &[(&str, &str)]) -> Vec<u8> {
        let cursor = Cursor::new(Vec::new());
        let mut writer = ZipWriter::new(cursor);
        let options = SimpleFileOptions::default();

        writer
            .start_file("mimetype", options)
            .expect("mimetype file");
        writer
            .write_all(b"application/epub+zip")
            .expect("write mimetype");
        writer
            .start_file("META-INF/container.xml", options)
            .expect("container file");
        writer
            .write_all(
                br#"<?xml version="1.0" encoding="UTF-8"?>
                <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
                  <rootfiles>
                    <rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/>
                  </rootfiles>
                </container>"#,
            )
            .expect("write container");
        writer
            .start_file("OPS/content.opf", options)
            .expect("opf file");
        writer.write_all(opf.as_bytes()).expect("write opf");

        for (path, html) in html_items {
            writer.start_file(path, options).expect("html file");
            writer.write_all(html.as_bytes()).expect("write html");
        }

        writer.finish().expect("finish zip").into_inner()
    }
}
