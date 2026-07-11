use anyhow::{anyhow, bail, Context, Result};
use quick_xml::escape::unescape;
use quick_xml::events::{BytesCData, BytesText, Event};
use quick_xml::Reader;
use serde_json::{json, Map, Value};
use std::io::{Cursor, Read, Seek};
use std::time::{Duration, Instant};
use zip::result::ZipError;
use zip::ZipArchive;

use super::language::{detect_language, language_detection_metadata};
use super::types::GeneralExtractedDocument;

const DOCX_MIME: &str = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const DOCM_MIME: &str = "application/vnd.ms-word.document.macroenabled.12";
const PPTX_MIME: &str = "application/vnd.openxmlformats-officedocument.presentationml.presentation";
const PPTM_MIME: &str = "application/vnd.ms-powerpoint.presentation.macroenabled.12";
const DOC_MIME: &str = "application/msword";
const PPT_MIME: &str = "application/vnd.ms-powerpoint";
const LEGACY_DOC_MIN_CHARS: usize = 10;
const LEGACY_DOC_SIZE_THRESHOLD_BYTES: usize = 1024;
const LEGACY_DOC_RATIO_THRESHOLD: f64 = 0.001;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DocumentFormat {
    Docx,
    Docm,
    Doc,
    Pptx,
    Pptm,
    Ppt,
    Unknown,
}

#[derive(Debug, Default)]
struct DocxBody {
    paragraphs: Vec<String>,
    tables: Vec<Vec<Vec<String>>>,
}

#[derive(Debug, Default)]
struct PptxSlide {
    lines: Vec<String>,
    table_count: usize,
}

#[derive(Debug)]
struct TextReasonableness {
    char_count: usize,
    ratio: f64,
    reasonable: bool,
    reason: &'static str,
}

pub(crate) fn extract_document_bytes(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
) -> Result<GeneralExtractedDocument> {
    let total_started_at = Instant::now();
    match classify_document_format(bytes, source_path, mime_type) {
        DocumentFormat::Docx => {
            extract_docx_bytes("docx", bytes, source_path, mime_type, total_started_at)
        }
        DocumentFormat::Docm => {
            extract_docx_bytes("docm", bytes, source_path, mime_type, total_started_at)
        }
        DocumentFormat::Pptx => {
            extract_pptx_bytes("pptx", bytes, source_path, mime_type, total_started_at)
        }
        DocumentFormat::Pptm => {
            extract_pptx_bytes("pptm", bytes, source_path, mime_type, total_started_at)
        }
        DocumentFormat::Doc => extract_doc_bytes(bytes, source_path, mime_type, total_started_at),
        DocumentFormat::Ppt => bail!(
            "legacy PPT extraction is not supported by the Rust document extractor; fallback_required=legacy_ppt_libreoffice_pdf_pdftotext; route this file to the legacy fallback that uses LibreOffice PDF conversion followed by pdftotext"
        ),
        DocumentFormat::Unknown => bail!(
            "unsupported document format: mime_type={mime_type:?} source_path={source_path:?}; Rust document extraction currently supports DOCX and PPTX OOXML packages"
        ),
    }
}

fn extract_doc_bytes(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
    total_started_at: Instant,
) -> Result<GeneralExtractedDocument> {
    let parse_started_at = Instant::now();
    let document = match office_oxide::doc::DocDocument::from_reader(Cursor::new(bytes)) {
        Ok(document) => document,
        Err(error) => bail!(
            "legacy DOC native Rust extraction failed with office_oxide; fallback_required=legacy_doc_antiword_libreoffice_or_ocr; error={error}"
        ),
    };
    let parse_duration = parse_started_at.elapsed();

    let content_started_at = Instant::now();
    let content = clean_text(&document.plain_text());
    let reasonableness = legacy_doc_text_reasonableness(bytes.len(), &content);
    let image_count = document.images().len();
    let content_duration = content_started_at.elapsed();

    if !reasonableness.reasonable {
        bail!(
            "legacy DOC native Rust extraction with office_oxide produced insufficient text; fallback_required=legacy_doc_antiword_libreoffice_or_ocr; chars={} file_size={} ratio={:.6} reason={} image_count={}",
            reasonableness.char_count,
            bytes.len(),
            reasonableness.ratio,
            reasonableness.reason,
            image_count
        );
    }

    let language_started_at = Instant::now();
    let language_report = detect_language(&content);
    let language_duration = language_started_at.elapsed();

    let mut timing_ms = Map::new();
    timing_ms.insert(
        "office_oxide_parse".to_string(),
        json!(duration_millis(parse_duration)),
    );
    timing_ms.insert(
        "content_build".to_string(),
        json!(duration_millis(content_duration)),
    );
    timing_ms.insert(
        "language_detect".to_string(),
        json!(duration_millis(language_duration)),
    );
    timing_ms.insert(
        "extract_document_total".to_string(),
        json!(duration_millis(total_started_at.elapsed())),
    );

    let properties = Map::new();
    let title = title_from_properties_or_content(&properties, &content);

    let mut content_extraction = Map::new();
    content_extraction.insert("extractor_type".to_string(), json!("doc"));
    content_extraction.insert("source_format".to_string(), json!("doc"));
    content_extraction.insert("parser".to_string(), json!("office_oxide"));
    content_extraction.insert("extraction_method".to_string(), json!("office_oxide"));
    content_extraction.insert("file_size".to_string(), json!(bytes.len()));
    content_extraction.insert("image_count".to_string(), json!(image_count));
    content_extraction.insert(
        "content_length".to_string(),
        json!(reasonableness.char_count),
    );
    content_extraction.insert(
        "text_file_size_ratio".to_string(),
        json!(reasonableness.ratio),
    );
    content_extraction.insert(
        "reasonableness_threshold".to_string(),
        json!(LEGACY_DOC_RATIO_THRESHOLD),
    );
    content_extraction.insert("fallback_required".to_string(), json!(false));
    content_extraction.insert("properties".to_string(), Value::Object(properties));
    content_extraction.insert("timing_ms".to_string(), Value::Object(timing_ms.clone()));
    if let Some(source_path) = source_path {
        content_extraction.insert("original_bucket_path".to_string(), json!(source_path));
    }
    if let Some(mime_type) = mime_type {
        content_extraction.insert("mime_type".to_string(), json!(mime_type));
    }

    finish_document(
        "doc",
        title,
        content,
        bytes.len(),
        language_report,
        Value::Object(content_extraction),
        Value::Object(timing_ms),
    )
}

fn extract_docx_bytes(
    source_format: &str,
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
    total_started_at: Instant,
) -> Result<GeneralExtractedDocument> {
    let zip_started_at = Instant::now();
    let mut archive =
        ZipArchive::new(Cursor::new(bytes)).context("opening DOCX OOXML zip package")?;
    let zip_duration = zip_started_at.elapsed();

    let document_xml_started_at = Instant::now();
    let document_xml = read_zip_string(&mut archive, "word/document.xml")?
        .ok_or_else(|| anyhow!("DOCX package is missing word/document.xml"))?;
    let document_xml_duration = document_xml_started_at.elapsed();

    let parse_started_at = Instant::now();
    let body = parse_docx_document_xml(&document_xml).context("parsing DOCX document.xml")?;
    let parse_duration = parse_started_at.elapsed();

    let metadata_started_at = Instant::now();
    let properties = read_core_properties(&mut archive)?;
    let metadata_duration = metadata_started_at.elapsed();

    let content_started_at = Instant::now();
    let content = build_docx_content(&body);
    let title = title_from_properties_or_content(&properties, &content);
    let content_duration = content_started_at.elapsed();

    let language_started_at = Instant::now();
    let language_report = detect_language(&content);
    let language_duration = language_started_at.elapsed();

    let mut timing_ms = Map::new();
    timing_ms.insert("zip_open".to_string(), json!(duration_millis(zip_duration)));
    timing_ms.insert(
        "document_xml_read".to_string(),
        json!(duration_millis(document_xml_duration)),
    );
    timing_ms.insert(
        "document_xml_parse".to_string(),
        json!(duration_millis(parse_duration)),
    );
    timing_ms.insert(
        "core_properties".to_string(),
        json!(duration_millis(metadata_duration)),
    );
    timing_ms.insert(
        "content_build".to_string(),
        json!(duration_millis(content_duration)),
    );
    timing_ms.insert(
        "language_detect".to_string(),
        json!(duration_millis(language_duration)),
    );
    timing_ms.insert(
        "extract_document_total".to_string(),
        json!(duration_millis(total_started_at.elapsed())),
    );

    let mut content_extraction = Map::new();
    content_extraction.insert("extractor_type".to_string(), json!("docx"));
    content_extraction.insert("source_format".to_string(), json!(source_format));
    content_extraction.insert("parser".to_string(), json!("ooxml_zip_quick_xml"));
    content_extraction.insert("file_size".to_string(), json!(bytes.len()));
    content_extraction.insert("paragraph_count".to_string(), json!(body.paragraphs.len()));
    content_extraction.insert("table_count".to_string(), json!(body.tables.len()));
    content_extraction.insert("content_length".to_string(), json!(content.chars().count()));
    content_extraction.insert("properties".to_string(), Value::Object(properties));
    content_extraction.insert("timing_ms".to_string(), Value::Object(timing_ms.clone()));
    if let Some(source_path) = source_path {
        content_extraction.insert("original_bucket_path".to_string(), json!(source_path));
    }
    if let Some(mime_type) = mime_type {
        content_extraction.insert("mime_type".to_string(), json!(mime_type));
    }

    finish_document(
        source_format,
        title,
        content,
        bytes.len(),
        language_report,
        Value::Object(content_extraction),
        Value::Object(timing_ms),
    )
}

fn extract_pptx_bytes(
    source_format: &str,
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
    total_started_at: Instant,
) -> Result<GeneralExtractedDocument> {
    let zip_started_at = Instant::now();
    let mut archive =
        ZipArchive::new(Cursor::new(bytes)).context("opening PPTX OOXML zip package")?;
    let zip_duration = zip_started_at.elapsed();

    let slide_names_started_at = Instant::now();
    let slide_names = sorted_pptx_slide_names(&mut archive)?;
    let slide_names_duration = slide_names_started_at.elapsed();

    let slides_started_at = Instant::now();
    let mut slide_sections = Vec::new();
    let mut table_count = 0usize;
    for (index, slide_name) in slide_names.iter().enumerate() {
        let slide_xml = read_zip_string(&mut archive, slide_name)?
            .ok_or_else(|| anyhow!("PPTX package is missing expected slide part {slide_name}"))?;
        let slide = parse_pptx_slide_xml(&slide_xml)
            .with_context(|| format!("parsing PPTX slide part {slide_name}"))?;
        table_count += slide.table_count;
        if !slide.lines.is_empty() {
            slide_sections.push(format!(
                "--- Slide {} ---\n{}",
                index + 1,
                slide.lines.join("\n")
            ));
        }
    }
    let slides_duration = slides_started_at.elapsed();

    let metadata_started_at = Instant::now();
    let properties = read_core_properties(&mut archive)?;
    let metadata_duration = metadata_started_at.elapsed();

    let content_started_at = Instant::now();
    let content = clean_text(&slide_sections.join("\n"));
    let title = title_from_properties_or_content(&properties, &content);
    let content_duration = content_started_at.elapsed();

    let language_started_at = Instant::now();
    let language_report = detect_language(&content);
    let language_duration = language_started_at.elapsed();

    let mut timing_ms = Map::new();
    timing_ms.insert("zip_open".to_string(), json!(duration_millis(zip_duration)));
    timing_ms.insert(
        "slide_names".to_string(),
        json!(duration_millis(slide_names_duration)),
    );
    timing_ms.insert(
        "slide_xml_parse".to_string(),
        json!(duration_millis(slides_duration)),
    );
    timing_ms.insert(
        "core_properties".to_string(),
        json!(duration_millis(metadata_duration)),
    );
    timing_ms.insert(
        "content_build".to_string(),
        json!(duration_millis(content_duration)),
    );
    timing_ms.insert(
        "language_detect".to_string(),
        json!(duration_millis(language_duration)),
    );
    timing_ms.insert(
        "extract_document_total".to_string(),
        json!(duration_millis(total_started_at.elapsed())),
    );

    let mut content_extraction = Map::new();
    content_extraction.insert("extractor_type".to_string(), json!("pptx"));
    content_extraction.insert("source_format".to_string(), json!(source_format));
    content_extraction.insert("parser".to_string(), json!("ooxml_zip_quick_xml"));
    content_extraction.insert("file_size".to_string(), json!(bytes.len()));
    content_extraction.insert("slide_count".to_string(), json!(slide_names.len()));
    content_extraction.insert("table_count".to_string(), json!(table_count));
    content_extraction.insert("content_length".to_string(), json!(content.chars().count()));
    content_extraction.insert("properties".to_string(), Value::Object(properties));
    content_extraction.insert("timing_ms".to_string(), Value::Object(timing_ms.clone()));
    if let Some(source_path) = source_path {
        content_extraction.insert("original_bucket_path".to_string(), json!(source_path));
    }
    if let Some(mime_type) = mime_type {
        content_extraction.insert("mime_type".to_string(), json!(mime_type));
    }

    finish_document(
        source_format,
        title,
        content,
        bytes.len(),
        language_report,
        Value::Object(content_extraction),
        Value::Object(timing_ms),
    )
}

fn finish_document(
    source_format: &str,
    title: String,
    content: String,
    file_size: usize,
    language_report: super::types::LanguageDetectionReport,
    content_extraction: Value,
    timing_ms: Value,
) -> Result<GeneralExtractedDocument> {
    let language = language_report.language.clone();
    let extraction_metadata = json!({
        "file": {
            "size": file_size,
            "encoding": "utf-8",
        },
        "content_extraction": content_extraction,
        "title_extraction": {
            "method": if title.is_empty() { "none" } else { "metadata_or_content" },
        },
        "language_detection": language_detection_metadata(&language_report),
    });

    Ok(GeneralExtractedDocument {
        source_format: source_format.to_string(),
        title,
        content_length: content.chars().count(),
        content,
        language,
        extraction_metadata,
        timing_ms,
    })
}

fn classify_document_format(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
) -> DocumentFormat {
    if let Some(mime_type) = mime_type.map(normalize_mime_type) {
        match mime_type.as_str() {
            DOCX_MIME => return DocumentFormat::Docx,
            DOCM_MIME => return DocumentFormat::Docm,
            PPTX_MIME => return DocumentFormat::Pptx,
            PPTM_MIME => return DocumentFormat::Pptm,
            DOC_MIME | "application/vnd.ms-word" | "application/x-msword" => {
                return DocumentFormat::Doc;
            }
            PPT_MIME | "application/mspowerpoint" | "application/powerpoint" => {
                return DocumentFormat::Ppt;
            }
            _ => {}
        }
    }

    if let Some(extension) = normalized_extension(source_path) {
        match extension.as_str() {
            "docx" => return DocumentFormat::Docx,
            "docm" => return DocumentFormat::Docm,
            "pptx" => return DocumentFormat::Pptx,
            "pptm" => return DocumentFormat::Pptm,
            "doc" => return DocumentFormat::Doc,
            "ppt" => return DocumentFormat::Ppt,
            _ => {}
        }
    }

    if is_zip(bytes) {
        return sniff_ooxml_format(bytes).unwrap_or(DocumentFormat::Unknown);
    }
    if is_ole_compound_file(bytes) {
        return DocumentFormat::Unknown;
    }
    DocumentFormat::Unknown
}

fn sniff_ooxml_format(bytes: &[u8]) -> Result<DocumentFormat> {
    let mut archive = ZipArchive::new(Cursor::new(bytes)).context("opening OOXML zip package")?;
    let Some(content_types) = read_zip_string(&mut archive, "[Content_Types].xml")? else {
        return Ok(DocumentFormat::Unknown);
    };
    let content_types = content_types.to_ascii_lowercase();
    if content_types.contains(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    ) {
        return Ok(DocumentFormat::Docx);
    }
    if content_types.contains("application/vnd.ms-word.document.macroenabled.main+xml") {
        return Ok(DocumentFormat::Docm);
    }
    if content_types.contains(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    ) {
        return Ok(DocumentFormat::Pptx);
    }
    if content_types.contains("application/vnd.ms-powerpoint.presentation.macroenabled.main+xml") {
        return Ok(DocumentFormat::Pptm);
    }
    Ok(DocumentFormat::Unknown)
}

fn parse_docx_document_xml(xml: &str) -> Result<DocxBody> {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(false);

    let mut body = DocxBody::default();
    let mut table_depth = 0usize;
    let mut text_depth = 0usize;
    let mut current_paragraph: Option<String> = None;
    let mut current_table: Option<Vec<Vec<String>>> = None;
    let mut current_row: Option<Vec<String>> = None;
    let mut current_cell: Option<String> = None;

    loop {
        match reader.read_event()? {
            Event::Start(event) => match event.local_name().as_ref() {
                b"tbl" => {
                    if table_depth == 0 {
                        current_table = Some(Vec::new());
                    }
                    table_depth += 1;
                }
                b"tr" if table_depth > 0 && current_row.is_none() => {
                    current_row = Some(Vec::new());
                }
                b"tc" if table_depth > 0 && current_cell.is_none() => {
                    current_cell = Some(String::new());
                }
                b"p" if table_depth == 0 && current_paragraph.is_none() => {
                    current_paragraph = Some(String::new());
                }
                b"t" => {
                    text_depth += 1;
                }
                b"tab" => append_docx_text("\t", &mut current_paragraph, &mut current_cell),
                b"br" | b"cr" => append_docx_text("\n", &mut current_paragraph, &mut current_cell),
                _ => {}
            },
            Event::Empty(event) => match event.local_name().as_ref() {
                b"tab" => append_docx_text("\t", &mut current_paragraph, &mut current_cell),
                b"br" | b"cr" => append_docx_text("\n", &mut current_paragraph, &mut current_cell),
                _ => {}
            },
            Event::Text(text) if text_depth > 0 => {
                append_docx_text(
                    &decode_xml_text(&text)?,
                    &mut current_paragraph,
                    &mut current_cell,
                );
            }
            Event::CData(text) if text_depth > 0 => {
                append_docx_text(
                    &decode_xml_cdata(&text)?,
                    &mut current_paragraph,
                    &mut current_cell,
                );
            }
            Event::End(event) => match event.local_name().as_ref() {
                b"t" => {
                    text_depth = text_depth.saturating_sub(1);
                }
                b"p" if table_depth == 0 => {
                    if let Some(paragraph) = current_paragraph.take() {
                        body.paragraphs.push(paragraph);
                    }
                }
                b"p" if current_cell.is_some() => {
                    if let Some(cell) = current_cell.as_mut() {
                        if !cell.ends_with('\n') {
                            cell.push('\n');
                        }
                    }
                }
                b"tc" if table_depth > 0 => {
                    if let Some(cell) = current_cell.take() {
                        if let Some(row) = current_row.as_mut() {
                            row.push(clean_text(&cell));
                        }
                    }
                }
                b"tr" if table_depth > 0 => {
                    if let Some(row) = current_row.take() {
                        if let Some(table) = current_table.as_mut() {
                            table.push(row);
                        }
                    }
                }
                b"tbl" => {
                    table_depth = table_depth.saturating_sub(1);
                    if table_depth == 0 {
                        if let Some(table) = current_table.take() {
                            body.tables.push(table);
                        }
                    }
                }
                _ => {}
            },
            Event::Eof => break,
            _ => {}
        }
    }

    Ok(body)
}

fn parse_pptx_slide_xml(xml: &str) -> Result<PptxSlide> {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(false);

    let mut slide = PptxSlide::default();
    let mut paragraph_depth = 0usize;
    let mut text_depth = 0usize;
    let mut current_line: Option<String> = None;

    loop {
        match reader.read_event()? {
            Event::Start(event) => match event.local_name().as_ref() {
                b"tbl" => {
                    slide.table_count += 1;
                }
                b"p" => {
                    paragraph_depth += 1;
                    if current_line.is_none() {
                        current_line = Some(String::new());
                    }
                }
                b"t" => {
                    text_depth += 1;
                }
                b"br" => append_pptx_text("\n", &mut current_line),
                _ => {}
            },
            Event::Empty(event) => match event.local_name().as_ref() {
                b"br" => append_pptx_text("\n", &mut current_line),
                b"tab" => append_pptx_text("\t", &mut current_line),
                _ => {}
            },
            Event::Text(text) if text_depth > 0 => {
                append_pptx_text(&decode_xml_text(&text)?, &mut current_line);
            }
            Event::CData(text) if text_depth > 0 => {
                append_pptx_text(&decode_xml_cdata(&text)?, &mut current_line);
            }
            Event::End(event) => match event.local_name().as_ref() {
                b"t" => {
                    text_depth = text_depth.saturating_sub(1);
                }
                b"p" => {
                    paragraph_depth = paragraph_depth.saturating_sub(1);
                    if paragraph_depth == 0 {
                        if let Some(line) = current_line.take() {
                            let line = clean_text(&line);
                            if !line.is_empty() {
                                slide.lines.push(line);
                            }
                        }
                    }
                }
                _ => {}
            },
            Event::Eof => break,
            _ => {}
        }
    }

    Ok(slide)
}

fn append_docx_text(
    text: &str,
    current_paragraph: &mut Option<String>,
    current_cell: &mut Option<String>,
) {
    if let Some(paragraph) = current_paragraph.as_mut() {
        paragraph.push_str(text);
    }
    if let Some(cell) = current_cell.as_mut() {
        cell.push_str(text);
    }
}

fn append_pptx_text(text: &str, current_line: &mut Option<String>) {
    if current_line.is_none() {
        current_line.replace(String::new());
    }
    if let Some(line) = current_line.as_mut() {
        line.push_str(text);
    }
}

fn build_docx_content(body: &DocxBody) -> String {
    let mut parts = Vec::new();
    parts.extend(body.paragraphs.iter().cloned());
    for table in &body.tables {
        for row in table {
            parts.push(row.join(" "));
        }
    }
    clean_text(&parts.join("\n"))
}

fn read_core_properties<R: Read + Seek>(archive: &mut ZipArchive<R>) -> Result<Map<String, Value>> {
    let Some(core_xml) = read_zip_string(archive, "docProps/core.xml")? else {
        return Ok(Map::new());
    };
    parse_core_properties_xml(&core_xml).context("parsing docProps/core.xml")
}

fn parse_core_properties_xml(xml: &str) -> Result<Map<String, Value>> {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(false);

    let mut properties = Map::new();
    let mut current_key: Option<&'static str> = None;
    let mut current_value = String::new();

    loop {
        match reader.read_event()? {
            Event::Start(event) => {
                if let Some(key) = core_property_key(event.local_name().as_ref()) {
                    current_key = Some(key);
                    current_value.clear();
                }
            }
            Event::Text(text) if current_key.is_some() => {
                current_value.push_str(&decode_xml_text(&text)?);
            }
            Event::CData(text) if current_key.is_some() => {
                current_value.push_str(&decode_xml_cdata(&text)?);
            }
            Event::End(event) => {
                if let Some(key) = current_key {
                    if core_property_key(event.local_name().as_ref()) == Some(key) {
                        let value = current_value.trim();
                        if !value.is_empty() {
                            properties.insert(key.to_string(), json!(value));
                        }
                        current_key = None;
                        current_value.clear();
                    }
                }
            }
            Event::Eof => break,
            _ => {}
        }
    }

    Ok(properties)
}

fn core_property_key(local_name: &[u8]) -> Option<&'static str> {
    match local_name {
        b"title" => Some("title"),
        b"subject" => Some("subject"),
        b"creator" => Some("author"),
        b"keywords" => Some("keywords"),
        b"description" => Some("comments"),
        b"lastModifiedBy" => Some("last_modified_by"),
        b"revision" => Some("revision"),
        b"created" => Some("created"),
        b"modified" => Some("modified"),
        b"category" => Some("category"),
        _ => None,
    }
}

fn title_from_properties_or_content(properties: &Map<String, Value>, content: &str) -> String {
    properties
        .get("title")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|title| !title.is_empty())
        .map(str::to_string)
        .or_else(|| {
            content
                .lines()
                .map(str::trim)
                .find(|line| !line.is_empty())
                .map(str::to_string)
        })
        .unwrap_or_default()
}

fn sorted_pptx_slide_names<R: Read + Seek>(archive: &mut ZipArchive<R>) -> Result<Vec<String>> {
    let mut slide_names = Vec::new();
    for index in 0..archive.len() {
        let file = archive.by_index(index)?;
        let name = file.name();
        if name.starts_with("ppt/slides/slide")
            && name.ends_with(".xml")
            && !name.contains("/_rels/")
        {
            slide_names.push(name.to_string());
        }
    }
    slide_names.sort_by_key(|name| pptx_slide_number(name).unwrap_or(usize::MAX));
    Ok(slide_names)
}

fn pptx_slide_number(name: &str) -> Option<usize> {
    name.strip_prefix("ppt/slides/slide")?
        .strip_suffix(".xml")?
        .parse()
        .ok()
}

fn read_zip_string<R: Read + Seek>(
    archive: &mut ZipArchive<R>,
    name: &str,
) -> Result<Option<String>> {
    let mut file = match archive.by_name(name) {
        Ok(file) => file,
        Err(ZipError::FileNotFound) => return Ok(None),
        Err(error) => return Err(error).with_context(|| format!("opening zip member {name}")),
    };
    let mut content = String::new();
    file.read_to_string(&mut content)
        .with_context(|| format!("reading zip member {name} as UTF-8 XML"))?;
    Ok(Some(content))
}

fn decode_xml_text(text: &BytesText<'_>) -> Result<String> {
    let decoded = text.xml10_content()?.into_owned();
    Ok(unescape(&decoded)?.into_owned())
}

fn decode_xml_cdata(text: &BytesCData<'_>) -> Result<String> {
    Ok(text.xml10_content()?.into_owned())
}

fn normalize_mime_type(mime_type: &str) -> String {
    mime_type
        .split(';')
        .next()
        .unwrap_or(mime_type)
        .trim()
        .to_ascii_lowercase()
}

fn normalized_extension(source_path: Option<&str>) -> Option<String> {
    let path = source_path?;
    let path = path.split(['?', '#']).next().unwrap_or(path);
    let filename = path.rsplit('/').next().unwrap_or(path);
    let (_, extension) = filename.rsplit_once('.')?;
    Some(extension.to_ascii_lowercase())
}

fn is_zip(bytes: &[u8]) -> bool {
    bytes.starts_with(b"PK\x03\x04")
        || bytes.starts_with(b"PK\x05\x06")
        || bytes.starts_with(b"PK\x07\x08")
}

fn is_ole_compound_file(bytes: &[u8]) -> bool {
    bytes.starts_with(&[0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1])
}

fn legacy_doc_text_reasonableness(file_size: usize, content: &str) -> TextReasonableness {
    let char_count = content.chars().count();
    let ratio = if file_size == 0 {
        if char_count == 0 {
            0.0
        } else {
            f64::INFINITY
        }
    } else {
        char_count as f64 / file_size as f64
    };

    if file_size == 0 {
        return TextReasonableness {
            char_count,
            ratio,
            reasonable: char_count == 0,
            reason: if char_count == 0 {
                "empty_file_empty_text"
            } else {
                "empty_file_has_text"
            },
        };
    }

    if file_size < LEGACY_DOC_SIZE_THRESHOLD_BYTES {
        return TextReasonableness {
            char_count,
            ratio,
            reasonable: char_count > 0,
            reason: if char_count > 0 {
                "small_file_has_text"
            } else {
                "small_file_no_text"
            },
        };
    }

    let enough_chars = char_count >= LEGACY_DOC_MIN_CHARS;
    let enough_ratio = ratio >= LEGACY_DOC_RATIO_THRESHOLD;
    TextReasonableness {
        char_count,
        ratio,
        reasonable: enough_chars && enough_ratio,
        reason: match (enough_chars, enough_ratio) {
            (true, true) => "large_file_ratio_ok",
            (false, true) => "large_file_text_too_short",
            (true, false) => "large_file_ratio_too_low",
            (false, false) => "large_file_text_and_ratio_too_low",
        },
    }
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
    use std::io::Write;
    use zip::write::SimpleFileOptions;
    use zip::ZipWriter;

    fn sample_docx() -> Vec<u8> {
        let cursor = Cursor::new(Vec::new());
        let mut writer = ZipWriter::new(cursor);
        let options = SimpleFileOptions::default();
        writer.start_file("[Content_Types].xml", options).unwrap();
        writer
            .write_all(br#"<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>"#)
            .unwrap();
        writer.start_file("word/document.xml", options).unwrap();
        writer
            .write_all(br#"<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Sample Document</w:t></w:r></w:p><w:p><w:r><w:t>This is a sample DOCX document for testing content extraction.</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell 1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Cell 2</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>Cell 3</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Cell 4</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>"#)
            .unwrap();
        writer.finish().unwrap().into_inner()
    }

    #[test]
    fn extracts_docx_paragraphs_and_tables_from_sample() {
        let sample = sample_docx();
        let result =
            extract_document_bytes(&sample, Some("content/sample.docx"), Some(DOCX_MIME)).unwrap();

        assert_eq!(result.source_format, "docx");
        assert_eq!(result.title, "Sample Document");
        assert!(result.content.contains("Sample Document"));
        assert!(result
            .content
            .contains("This is a sample DOCX document for testing content extraction."));
        assert!(result.content.contains("Cell 1 Cell 2"));
        assert!(result.content.contains("Cell 3 Cell 4"));
        assert_eq!(result.language.as_deref(), Some("en"));

        let metadata = &result.extraction_metadata["content_extraction"];
        assert_eq!(metadata["extractor_type"], "docx");
        assert_eq!(metadata["source_format"], "docx");
        assert_eq!(metadata["table_count"], json!(1));
        assert_eq!(metadata["file_size"], json!(sample.len()));
    }

    #[test]
    fn extracts_docm_with_docx_parser_and_docm_source_format() {
        let sample = sample_docx();
        let result =
            extract_document_bytes(&sample, Some("content/sample.docm"), Some(DOCM_MIME)).unwrap();

        assert_eq!(result.source_format, "docm");
        assert!(result.content.contains("Sample Document"));

        let metadata = &result.extraction_metadata["content_extraction"];
        assert_eq!(metadata["extractor_type"], "docx");
        assert_eq!(metadata["source_format"], "docm");
    }

    #[test]
    fn classifies_docx_from_ooxml_content_types_without_extension_or_mime() {
        let sample = sample_docx();
        assert_eq!(
            classify_document_format(&sample, None, None),
            DocumentFormat::Docx
        );
    }

    #[test]
    fn classifies_macro_enabled_document_extensions_and_mimes() {
        let sample = sample_docx();
        assert_eq!(
            classify_document_format(&sample, Some("content/sample.docm"), None),
            DocumentFormat::Docm
        );
        assert_eq!(
            classify_document_format(b"", None, Some(DOCM_MIME)),
            DocumentFormat::Docm
        );
        assert_eq!(
            classify_document_format(b"", Some("slides/sample.pptm"), None),
            DocumentFormat::Pptm
        );
        assert_eq!(
            classify_document_format(b"", None, Some(PPTM_MIME)),
            DocumentFormat::Pptm
        );
    }

    #[test]
    fn returns_fallback_error_for_unparseable_legacy_doc() {
        let err = extract_document_bytes(
            &[0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1],
            Some("legacy.doc"),
            Some(DOC_MIME),
        )
        .unwrap_err();

        assert!(err
            .to_string()
            .contains("fallback_required=legacy_doc_antiword_libreoffice_or_ocr"));
    }

    #[test]
    fn rejects_weak_legacy_doc_native_output() {
        let reasonableness = legacy_doc_text_reasonableness(1_017_344, "x");

        assert!(!reasonableness.reasonable);
        assert_eq!(reasonableness.reason, "large_file_text_and_ratio_too_low");
    }

    #[test]
    fn accepts_legacy_doc_output_at_python_doc_threshold() {
        let content = "x".repeat(100);
        let reasonableness = legacy_doc_text_reasonableness(100_000, &content);

        assert!(reasonableness.reasonable);
        assert_eq!(reasonableness.reason, "large_file_ratio_ok");
    }
}
