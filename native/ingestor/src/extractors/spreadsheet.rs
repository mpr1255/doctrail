use anyhow::{Context, Result};
use calamine::{open_workbook_auto_from_rs, Data, Range, Reader};
use chardetng::EncodingDetector;
use encoding_rs::UTF_8;
use quick_xml::events::{BytesStart, Event};
use quick_xml::Reader as XmlReader;
use serde_json::{json, Map, Value};
use std::collections::HashSet;
use std::io::{Cursor, Read};
use std::path::Path;
use std::time::{Duration, Instant};
use zip::ZipArchive;

use super::language::{detect_language, language_detection_metadata};
use super::types::GeneralExtractedDocument;

const ENCODING_DETECTION_BYTES: usize = 10_000;
const MAX_CONTENT_CHARS: usize = 1024 * 1024;
const CSV_TEXT_DECODE_BYTES: usize = MAX_CONTENT_CHARS * 4;
const OPENXML_DIMENSION_SCAN_BYTES: u64 = 64 * 1024;
const MAX_CALAMINE_DECLARED_OPENXML_ROWS: u64 = 200_000;
const MAX_CALAMINE_DECLARED_OPENXML_CELLS: u64 = 6_000_000;
const CFB_HEADER_LEN: usize = 512;
const CFB_SIGNATURE: &[u8; 8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1";
const CFB_DIFSECT: u32 = 0xFFFF_FFFC;
const CFB_FATSECT: u32 = 0xFFFF_FFFD;
const CFB_ENDOFCHAIN: u32 = 0xFFFF_FFFE;
const CFB_FREESECT: u32 = 0xFFFF_FFFF;
const CFB_RESERVED_SECTORS: u32 = 0xFFFF_FFFA;

pub(crate) fn extract_spreadsheet_bytes(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
) -> Result<GeneralExtractedDocument> {
    let started_at = Instant::now();
    let source_format = infer_source_format(source_path, mime_type, bytes);
    match source_format.kind {
        SpreadsheetKind::Csv => extract_csv_bytes(
            bytes,
            source_path,
            mime_type,
            source_format.name,
            started_at,
        ),
        SpreadsheetKind::Excel => extract_workbook_bytes(
            bytes,
            source_path,
            mime_type,
            source_format.name,
            started_at,
        ),
    }
}

fn extract_workbook_bytes(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
    source_format: &str,
    started_at: Instant,
) -> Result<GeneralExtractedDocument> {
    guard_legacy_xls_cfb(bytes, source_format)?;
    let openxml_preflight = inspect_openxml_dimensions(bytes, source_format)
        .context("preflighting OpenXML spreadsheet dimensions")?;
    if openxml_preflight.requires_sparse_fallback() {
        anyhow::bail!(
            "OpenXML spreadsheet declares dimensions too large for in-process Rust parsing; fallback_required=spreadsheet_openxml_declared_dimension_libreoffice; max_declared_rows={} max_declared_columns={} max_declared_cells={} max_allowed_rows={} max_allowed_cells={}",
            openxml_preflight.max_declared_rows,
            openxml_preflight.max_declared_columns,
            openxml_preflight.max_declared_cells,
            MAX_CALAMINE_DECLARED_OPENXML_ROWS,
            MAX_CALAMINE_DECLARED_OPENXML_CELLS,
        );
    }

    let open_started_at = Instant::now();
    let cursor = Cursor::new(bytes);
    let mut workbook =
        open_workbook_auto_from_rs(cursor).context("opening spreadsheet workbook")?;
    let open_duration = open_started_at.elapsed();

    let sheet_started_at = Instant::now();
    let sheet_names = workbook.sheet_names();
    let mut content = String::new();
    let mut sheets = Map::new();
    let mut structured_sheets = Vec::new();
    let mut total_rows = 0usize;
    let mut max_columns = 0usize;

    for sheet_name in &sheet_names {
        let range = workbook
            .worksheet_range(sheet_name)
            .with_context(|| format!("reading worksheet {sheet_name:?}"))?;
        let table = table_from_range(&range);
        let rendered_table = render_structured_sheet(sheet_name, &table.headers, &table.rows);
        let normalized_rows = table.all_rows();

        if !rendered_table.is_empty() {
            content.push_str(&rendered_table);
            content.push('\n');
        }
        content.push('\n');

        total_rows += table.rows.len();
        max_columns = max_columns.max(table.column_count());
        sheets.insert(
            sheet_name.to_string(),
            json!({
                "rows": table.rows.len(),
                "columns": table.column_count(),
                "column_names": table.headers,
            }),
        );
        structured_sheets.push(json!({
            "sheet_name": sheet_name,
            "rows": normalized_rows,
            "csv": rows_to_csv(&table.all_rows()),
            "text": render_structured_sheet(sheet_name, &table.headers, &table.rows),
        }));
    }
    let sheet_duration = sheet_started_at.elapsed();

    let mut timing_ms = Map::new();
    timing_ms.insert(
        "open_workbook".to_string(),
        json!(duration_millis(open_duration)),
    );
    timing_ms.insert(
        "read_sheets".to_string(),
        json!(duration_millis(sheet_duration)),
    );
    timing_ms.insert(
        "total".to_string(),
        json!(duration_millis(started_at.elapsed())),
    );

    let mut metadata = Map::new();
    metadata.insert("extractor_type".to_string(), json!("excel"));
    metadata.insert("source_format".to_string(), json!(source_format));
    metadata.insert("file_size".to_string(), json!(bytes.len()));
    metadata.insert("sheet_count".to_string(), json!(sheet_names.len()));
    metadata.insert("sheet_names".to_string(), json!(sheet_names));
    metadata.insert("row_count".to_string(), json!(total_rows));
    metadata.insert("column_count".to_string(), json!(max_columns));
    metadata.insert("sheets".to_string(), Value::Object(sheets));
    metadata.insert(
        "structured_sheets".to_string(),
        Value::Array(structured_sheets),
    );
    metadata.insert("timing_ms".to_string(), Value::Object(timing_ms.clone()));
    if openxml_preflight.scanned_sheets > 0 {
        metadata.insert(
            "openxml_preflight".to_string(),
            openxml_preflight.to_value(false),
        );
    }
    if let Some(source_path) = source_path {
        metadata.insert("original_bucket_path".to_string(), json!(source_path));
    }
    if let Some(mime_type) = mime_type {
        metadata.insert("mime_type".to_string(), json!(mime_type));
    }

    build_document(
        source_format,
        title_from_source_path(source_path),
        content,
        metadata,
        timing_ms,
        None,
    )
}

fn extract_csv_bytes(
    bytes: &[u8],
    source_path: Option<&str>,
    mime_type: Option<&str>,
    source_format: &str,
    started_at: Instant,
) -> Result<GeneralExtractedDocument> {
    let decode_started_at = Instant::now();
    let decoded = decode_csv_bytes(bytes);
    let decode_duration = decode_started_at.elapsed();
    let delimiter = detect_delimiter(&decoded.text, source_format);
    let rows = parse_delimited_rows(&decoded.text, delimiter)?;
    let sheet_name = title_from_source_path(source_path).unwrap_or_else(|| "Sheet1".to_string());
    let structured_text = render_rows_as_sheet(&sheet_name, &rows);
    let content_truncated =
        decoded.input_truncated || structured_text.chars().count() > MAX_CONTENT_CHARS;

    let mut timing_ms = Map::new();
    timing_ms.insert(
        "decode".to_string(),
        json!(duration_millis(decode_duration)),
    );
    timing_ms.insert(
        "total".to_string(),
        json!(duration_millis(started_at.elapsed())),
    );

    let mut metadata = Map::new();
    metadata.insert("extractor_type".to_string(), json!("csv"));
    metadata.insert("source_format".to_string(), json!(source_format));
    metadata.insert("file_size".to_string(), json!(bytes.len()));
    metadata.insert("decoded_bytes".to_string(), json!(decoded.decoded_bytes));
    metadata.insert(
        "decode_byte_limit".to_string(),
        json!(CSV_TEXT_DECODE_BYTES),
    );
    metadata.insert(
        "decode_input_truncated".to_string(),
        json!(decoded.input_truncated),
    );
    metadata.insert("encoding".to_string(), json!(decoded.encoding));
    metadata.insert("encoding_had_errors".to_string(), json!(decoded.had_errors));
    if let Some(fallback_from) = decoded.fallback_from {
        metadata.insert("encoding_fallback_from".to_string(), json!(fallback_from));
    }
    metadata.insert("content_truncated".to_string(), json!(content_truncated));
    metadata.insert(
        "delimiter".to_string(),
        json!((delimiter as char).to_string()),
    );
    metadata.insert("row_count".to_string(), json!(rows.len()));
    metadata.insert(
        "column_count".to_string(),
        json!(rows.iter().map(Vec::len).max().unwrap_or(0)),
    );
    metadata.insert(
        "structured_sheets".to_string(),
        json!([{
            "sheet_name": sheet_name,
            "rows": rows,
            "csv": rows_to_csv(&rows),
            "text": structured_text,
            "delimiter": (delimiter as char).to_string(),
            "source_format": source_format,
        }]),
    );
    metadata.insert(
        "content_truncation_limit_chars".to_string(),
        json!(MAX_CONTENT_CHARS),
    );
    metadata.insert("timing_ms".to_string(), Value::Object(timing_ms.clone()));
    if let Some(source_path) = source_path {
        metadata.insert("original_bucket_path".to_string(), json!(source_path));
    }
    if let Some(mime_type) = mime_type {
        metadata.insert("mime_type".to_string(), json!(mime_type));
    }

    build_document(
        source_format,
        title_from_source_path(source_path),
        structured_text,
        metadata,
        timing_ms,
        Some(decoded.encoding),
    )
}

fn build_document(
    source_format: &str,
    title: Option<String>,
    content: String,
    mut content_metadata: Map<String, Value>,
    timing_ms: Map<String, Value>,
    encoding: Option<&str>,
) -> Result<GeneralExtractedDocument> {
    let title_method = if title.is_some() {
        "bucket_path_filename"
    } else {
        "none"
    };
    let title = title.unwrap_or_default();
    let content = clean_text(&chunk_large_content(&content));
    let content_length = content.chars().count();
    let language_report = detect_language(&content);
    let language = language_report.language.clone();

    content_metadata.insert("content_length".to_string(), json!(content_length));
    let file_size = content_metadata
        .get("file_size")
        .cloned()
        .unwrap_or(Value::Null);
    let content_extraction = Value::Object(content_metadata);
    let language_detection = language_detection_metadata(&language_report);

    let extraction_metadata = json!({
        "file": {
            "size": file_size,
            "encoding": encoding,
        },
        "content_extraction": content_extraction,
        "title_extraction": {
            "method": title_method,
        },
        "language_detection": language_detection,
    });

    Ok(GeneralExtractedDocument {
        source_format: source_format.to_string(),
        title,
        content,
        language,
        extraction_metadata,
        timing_ms: Value::Object(timing_ms),
        content_length,
    })
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SpreadsheetKind {
    Csv,
    Excel,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct SourceFormat {
    name: &'static str,
    kind: SpreadsheetKind,
}

fn infer_source_format(
    source_path: Option<&str>,
    mime_type: Option<&str>,
    bytes: &[u8],
) -> SourceFormat {
    if let Some(extension) = source_path.and_then(file_extension) {
        match extension.as_str() {
            "csv" => {
                return SourceFormat {
                    name: "csv",
                    kind: SpreadsheetKind::Csv,
                }
            }
            "xls" => {
                return SourceFormat {
                    name: "xls",
                    kind: SpreadsheetKind::Excel,
                }
            }
            "xlsx" | "xlsm" | "xlsb" | "ods" => {
                return SourceFormat {
                    name: match extension.as_str() {
                        "xlsm" => "xlsm",
                        "xlsb" => "xlsb",
                        "ods" => "ods",
                        _ => "xlsx",
                    },
                    kind: SpreadsheetKind::Excel,
                }
            }
            _ => {}
        }
    }

    if let Some(mime_type) = mime_type.map(|value| value.to_ascii_lowercase()) {
        if mime_type.contains("csv") {
            return SourceFormat {
                name: "csv",
                kind: SpreadsheetKind::Csv,
            };
        }
        if mime_type.contains("spreadsheetml") {
            return SourceFormat {
                name: "xlsx",
                kind: SpreadsheetKind::Excel,
            };
        }
        if mime_type.contains("sheet.macroenabled.12") {
            return SourceFormat {
                name: "xlsm",
                kind: SpreadsheetKind::Excel,
            };
        }
        if mime_type.contains("sheet.binary.macroenabled.12") {
            return SourceFormat {
                name: "xlsb",
                kind: SpreadsheetKind::Excel,
            };
        }
        if mime_type.contains("opendocument.spreadsheet") {
            return SourceFormat {
                name: "ods",
                kind: SpreadsheetKind::Excel,
            };
        }
        if mime_type.contains("excel") || mime_type.contains("ms-excel") {
            return SourceFormat {
                name: "xls",
                kind: SpreadsheetKind::Excel,
            };
        }
    }

    if looks_like_excel_bytes(bytes) {
        SourceFormat {
            name: "xlsx",
            kind: SpreadsheetKind::Excel,
        }
    } else {
        SourceFormat {
            name: "csv",
            kind: SpreadsheetKind::Csv,
        }
    }
}

fn looks_like_excel_bytes(bytes: &[u8]) -> bool {
    bytes.starts_with(b"PK\x03\x04") || bytes.starts_with(&[0xd0, 0xcf, 0x11, 0xe0])
}

#[derive(Debug, Default)]
struct OpenXmlDimensionPreflight {
    scanned_sheets: usize,
    max_declared_rows: u64,
    max_declared_columns: u64,
    max_declared_cells: u64,
    dimensions: Vec<OpenXmlSheetDimension>,
}

impl OpenXmlDimensionPreflight {
    fn requires_sparse_fallback(&self) -> bool {
        self.max_declared_rows > MAX_CALAMINE_DECLARED_OPENXML_ROWS
            || self.max_declared_cells > MAX_CALAMINE_DECLARED_OPENXML_CELLS
    }

    fn to_value(&self, fallback_used: bool) -> Value {
        json!({
            "scanned_sheets": self.scanned_sheets,
            "max_declared_rows": self.max_declared_rows,
            "max_declared_columns": self.max_declared_columns,
            "max_declared_cells": self.max_declared_cells,
            "max_calamine_declared_rows": MAX_CALAMINE_DECLARED_OPENXML_ROWS,
            "max_calamine_declared_cells": MAX_CALAMINE_DECLARED_OPENXML_CELLS,
            "sparse_fallback_required": self.requires_sparse_fallback(),
            "sparse_fallback_used": fallback_used,
            "dimensions": self.dimensions,
        })
    }
}

#[derive(Debug, serde::Serialize)]
struct OpenXmlSheetDimension {
    member_name: String,
    reference: String,
    rows: u64,
    columns: u64,
    cells: u64,
}

fn inspect_openxml_dimensions(
    bytes: &[u8],
    source_format: &str,
) -> Result<OpenXmlDimensionPreflight> {
    if !matches!(source_format, "xlsx" | "xlsm") || !bytes.starts_with(b"PK") {
        return Ok(OpenXmlDimensionPreflight::default());
    }

    let mut archive = ZipArchive::new(Cursor::new(bytes)).context("opening OpenXML zip package")?;
    let mut preflight = OpenXmlDimensionPreflight::default();

    for index in 0..archive.len() {
        let mut file = archive
            .by_index(index)
            .with_context(|| format!("opening OpenXML zip member at index {index}"))?;
        let member_name = file.name().to_string();
        if !is_openxml_worksheet_member(&member_name) {
            continue;
        }

        preflight.scanned_sheets += 1;
        let mut prefix = Vec::new();
        file.by_ref()
            .take(OPENXML_DIMENSION_SCAN_BYTES)
            .read_to_end(&mut prefix)
            .with_context(|| format!("reading OpenXML worksheet prefix {member_name}"))?;
        let Some(reference) = parse_worksheet_dimension_reference(&prefix)
            .with_context(|| format!("parsing worksheet dimension for {member_name}"))?
        else {
            continue;
        };
        let Some((rows, columns)) = parse_dimension_rows_columns(&reference) else {
            continue;
        };
        let cells = rows.saturating_mul(columns);
        preflight.max_declared_rows = preflight.max_declared_rows.max(rows);
        preflight.max_declared_columns = preflight.max_declared_columns.max(columns);
        preflight.max_declared_cells = preflight.max_declared_cells.max(cells);
        preflight.dimensions.push(OpenXmlSheetDimension {
            member_name,
            reference,
            rows,
            columns,
            cells,
        });
    }

    Ok(preflight)
}

fn is_openxml_worksheet_member(name: &str) -> bool {
    name.starts_with("xl/worksheets/") && name.ends_with(".xml") && !name.contains("/_rels/")
}

fn parse_worksheet_dimension_reference(prefix: &[u8]) -> Result<Option<String>> {
    let xml = String::from_utf8_lossy(prefix);
    let mut reader = XmlReader::from_str(&xml);
    reader.config_mut().trim_text(false);

    loop {
        match reader.read_event() {
            Ok(Event::Start(event)) | Ok(Event::Empty(event)) => {
                if event.local_name().as_ref() == b"dimension" {
                    return dimension_ref_attr(&event);
                }
            }
            Ok(Event::Eof) => break,
            Err(error) => return Err(error).context("reading worksheet XML prefix"),
            _ => {}
        }
    }

    Ok(None)
}

fn dimension_ref_attr(event: &BytesStart<'_>) -> Result<Option<String>> {
    for attribute in event.attributes() {
        let attribute = attribute.context("reading worksheet dimension attribute")?;
        if attribute.key.local_name().as_ref() == b"ref" {
            return Ok(Some(
                String::from_utf8_lossy(attribute.value.as_ref()).into_owned(),
            ));
        }
    }
    Ok(None)
}

fn parse_dimension_rows_columns(reference: &str) -> Option<(u64, u64)> {
    let (_, end) = reference.rsplit_once(':').unwrap_or(("", reference));
    let start = reference.split(':').next().unwrap_or(reference);
    let (_, start_row) = split_cell_reference(start)?;
    let (end_column, end_row) = split_cell_reference(end)?;
    let rows = end_row.saturating_sub(start_row).saturating_add(1);
    let columns = column_name_to_number(end_column)?;
    Some((rows, columns))
}

fn split_cell_reference(reference: &str) -> Option<(&str, u64)> {
    let reference = reference.trim().trim_matches('$');
    let digit_start = reference.find(|character: char| character.is_ascii_digit())?;
    let column = reference[..digit_start].trim_matches('$');
    let row = reference[digit_start..].trim_matches('$').parse().ok()?;
    Some((column, row))
}

fn column_name_to_number(column: &str) -> Option<u64> {
    let mut value = 0u64;
    for character in column.chars() {
        if !character.is_ascii_alphabetic() {
            return None;
        }
        value = value
            .saturating_mul(26)
            .saturating_add(character.to_ascii_uppercase() as u64 - 'A' as u64 + 1);
    }
    (value > 0).then_some(value)
}

fn guard_legacy_xls_cfb(bytes: &[u8], source_format: &str) -> Result<()> {
    if source_format != "xls" || !bytes.starts_with(CFB_SIGNATURE) {
        return Ok(());
    }

    validate_cfb_sector_chains(bytes).context("validating XLS CFB sector chains")
}

#[derive(Debug)]
struct CfbHeader {
    sector_size: usize,
    sector_count: usize,
    dir_start: u32,
    fat_len: usize,
    mini_fat_start: u32,
    mini_fat_len: usize,
    difat_start: u32,
    difat_len: usize,
    first_difat: Vec<u32>,
}

fn validate_cfb_sector_chains(bytes: &[u8]) -> Result<()> {
    let header = parse_cfb_header(bytes)?;
    let fat_sector_ids = collect_fat_sector_ids(bytes, &header)?;
    let fats = read_fat_entries(bytes, &header, &fat_sector_ids)?;

    validate_fat_chain("directory", header.dir_start, &fats, header.sector_count)?;
    if header.mini_fat_len > 0 && header.mini_fat_start < CFB_RESERVED_SECTORS {
        validate_fat_chain(
            "mini FAT",
            header.mini_fat_start,
            &fats,
            header.sector_count,
        )?;
    }

    Ok(())
}

fn parse_cfb_header(bytes: &[u8]) -> Result<CfbHeader> {
    anyhow::ensure!(
        bytes.len() >= CFB_HEADER_LEN,
        "invalid XLS CFB: file is shorter than the CFB header"
    );
    anyhow::ensure!(
        bytes.starts_with(CFB_SIGNATURE),
        "invalid XLS CFB: missing compound file signature"
    );

    let sector_size = match read_u16_le(bytes, 30)? {
        0x0009 => 512usize,
        0x000c => 4096usize,
        found => anyhow::bail!("invalid XLS CFB: unsupported sector shift 0x{found:04x}"),
    };
    let mini_sector_shift = read_u16_le(bytes, 32)?;
    anyhow::ensure!(
        mini_sector_shift == 0x0006,
        "invalid XLS CFB: unsupported mini sector shift 0x{mini_sector_shift:04x}"
    );

    let sector_count = bytes.len().saturating_sub(CFB_HEADER_LEN) / sector_size;
    anyhow::ensure!(sector_count > 0, "invalid XLS CFB: no sectors after header");

    let fat_len = read_u32_le(bytes, 44)? as usize;
    anyhow::ensure!(
        fat_len <= sector_count,
        "invalid XLS CFB: FAT sector count {fat_len} exceeds file sector count {sector_count}"
    );

    let first_difat = bytes[76..CFB_HEADER_LEN]
        .chunks_exact(4)
        .map(u32_from_le)
        .collect::<Vec<_>>();

    Ok(CfbHeader {
        sector_size,
        sector_count,
        dir_start: read_u32_le(bytes, 48)?,
        fat_len,
        mini_fat_start: read_u32_le(bytes, 60)?,
        mini_fat_len: read_u32_le(bytes, 64)? as usize,
        difat_start: read_u32_le(bytes, 68)?,
        difat_len: read_u32_le(bytes, 72)? as usize,
        first_difat,
    })
}

fn collect_fat_sector_ids(bytes: &[u8], header: &CfbHeader) -> Result<Vec<u32>> {
    let mut fat_sector_ids = Vec::with_capacity(header.fat_len);
    add_fat_sector_ids(&header.first_difat, header, &mut fat_sector_ids)?;

    let mut next_difat = header.difat_start;
    let mut seen_difat = HashSet::new();
    for _ in 0..header.difat_len {
        if next_difat >= CFB_RESERVED_SECTORS {
            break;
        }
        ensure_sector_id("DIFAT", next_difat, header.sector_count)?;
        anyhow::ensure!(
            seen_difat.insert(next_difat),
            "invalid XLS CFB: cyclic DIFAT sector chain at sector {next_difat}"
        );

        let sector = read_sector(bytes, header, next_difat)?;
        let entries = sector.chunks_exact(4).map(u32_from_le).collect::<Vec<_>>();
        if let Some((last, fat_entries)) = entries.split_last() {
            add_fat_sector_ids(fat_entries, header, &mut fat_sector_ids)?;
            next_difat = *last;
        } else {
            break;
        }
    }

    anyhow::ensure!(
        next_difat >= CFB_RESERVED_SECTORS || seen_difat.len() < header.difat_len,
        "invalid XLS CFB: DIFAT chain did not terminate within declared sector count"
    );
    anyhow::ensure!(
        fat_sector_ids.len() >= header.fat_len,
        "invalid XLS CFB: declared {} FAT sectors but found {}",
        header.fat_len,
        fat_sector_ids.len()
    );
    fat_sector_ids.truncate(header.fat_len);
    Ok(fat_sector_ids)
}

fn add_fat_sector_ids(entries: &[u32], header: &CfbHeader, out: &mut Vec<u32>) -> Result<()> {
    for &entry in entries {
        if out.len() >= header.fat_len {
            break;
        }
        if entry == CFB_FREESECT || entry == CFB_ENDOFCHAIN || entry == CFB_DIFSECT {
            continue;
        }
        anyhow::ensure!(
            entry != CFB_FATSECT,
            "invalid XLS CFB: FAT sector list contains FATSECT marker"
        );
        ensure_sector_id("FAT", entry, header.sector_count)?;
        out.push(entry);
    }
    Ok(())
}

fn read_fat_entries(bytes: &[u8], header: &CfbHeader, fat_sector_ids: &[u32]) -> Result<Vec<u32>> {
    let mut fats = Vec::new();
    for &sector_id in fat_sector_ids {
        let sector = read_sector(bytes, header, sector_id)?;
        fats.extend(sector.chunks_exact(4).map(u32_from_le));
    }
    Ok(fats)
}

fn validate_fat_chain(name: &str, start: u32, fats: &[u32], sector_count: usize) -> Result<()> {
    anyhow::ensure!(
        start < CFB_RESERVED_SECTORS,
        "invalid XLS CFB: {name} chain starts at reserved sector marker 0x{start:08x}"
    );

    let mut sector_id = start;
    let mut seen = HashSet::new();
    while sector_id != CFB_ENDOFCHAIN {
        ensure_sector_id(name, sector_id, sector_count)?;
        anyhow::ensure!(
            seen.insert(sector_id),
            "invalid XLS CFB: cyclic {name} sector chain at sector {sector_id}"
        );
        let next = *fats.get(sector_id as usize).ok_or_else(|| {
            anyhow::anyhow!("invalid XLS CFB: {name} sector {sector_id} is outside the FAT table")
        })?;
        if next == CFB_FREESECT || next == CFB_DIFSECT || next == CFB_FATSECT {
            anyhow::bail!(
                "invalid XLS CFB: {name} sector {sector_id} points to reserved marker 0x{next:08x}"
            );
        }
        sector_id = next;
        anyhow::ensure!(
            seen.len() <= sector_count,
            "invalid XLS CFB: {name} chain exceeds file sector count"
        );
    }

    Ok(())
}

fn ensure_sector_id(name: &str, sector_id: u32, sector_count: usize) -> Result<()> {
    anyhow::ensure!(
        (sector_id as usize) < sector_count,
        "invalid XLS CFB: {name} sector {sector_id} points past end of file with {sector_count} sectors"
    );
    Ok(())
}

fn read_sector<'a>(bytes: &'a [u8], header: &CfbHeader, sector_id: u32) -> Result<&'a [u8]> {
    ensure_sector_id("CFB", sector_id, header.sector_count)?;
    let start = CFB_HEADER_LEN + sector_id as usize * header.sector_size;
    let end = start + header.sector_size;
    bytes
        .get(start..end)
        .ok_or_else(|| anyhow::anyhow!("invalid XLS CFB: sector {sector_id} is truncated"))
}

fn read_u16_le(bytes: &[u8], offset: usize) -> Result<u16> {
    bytes
        .get(offset..offset + 2)
        .map(|slice| u16::from_le_bytes([slice[0], slice[1]]))
        .ok_or_else(|| anyhow::anyhow!("invalid XLS CFB: missing u16 at offset {offset}"))
}

fn read_u32_le(bytes: &[u8], offset: usize) -> Result<u32> {
    bytes
        .get(offset..offset + 4)
        .map(u32_from_le)
        .ok_or_else(|| anyhow::anyhow!("invalid XLS CFB: missing u32 at offset {offset}"))
}

fn u32_from_le(bytes: &[u8]) -> u32 {
    u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]])
}

fn file_extension(path: &str) -> Option<String> {
    Path::new(path)
        .extension()
        .and_then(|extension| extension.to_str())
        .map(|extension| extension.trim_start_matches('.').to_ascii_lowercase())
}

fn title_from_source_path(source_path: Option<&str>) -> Option<String> {
    let title = Path::new(source_path?)
        .file_stem()
        .and_then(|stem| stem.to_str())?
        .trim();
    (!title.is_empty()).then(|| title.to_string())
}

struct Table {
    headers: Vec<String>,
    rows: Vec<Vec<String>>,
}

impl Table {
    fn column_count(&self) -> usize {
        self.rows
            .iter()
            .map(Vec::len)
            .chain(std::iter::once(self.headers.len()))
            .max()
            .unwrap_or(0)
    }

    fn all_rows(&self) -> Vec<Vec<String>> {
        std::iter::once(self.headers.clone())
            .chain(self.rows.clone())
            .filter(|row| !row.is_empty())
            .collect()
    }
}

fn detect_delimiter(text: &str, source_format: &str) -> u8 {
    let fallback = if source_format == "tsv" { b'\t' } else { b',' };
    let sample = text.lines().take(20).collect::<Vec<_>>().join("\n");
    [b',', b';', b'\t', b'|']
        .into_iter()
        .max_by_key(|delimiter| {
            sample
                .as_bytes()
                .iter()
                .filter(|byte| *byte == delimiter)
                .count()
        })
        .filter(|delimiter| sample.as_bytes().contains(delimiter))
        .unwrap_or(fallback)
}

fn parse_delimited_rows(text: &str, delimiter: u8) -> Result<Vec<Vec<String>>> {
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(false)
        .flexible(true)
        .delimiter(delimiter)
        .from_reader(text.as_bytes());
    let mut rows = Vec::new();
    for record in reader.records() {
        let mut row = record
            .context("parsing delimited spreadsheet row")?
            .iter()
            .map(|cell| cell.trim().to_string())
            .collect::<Vec<_>>();
        trim_trailing_empty_cells(&mut row);
        if row.iter().any(|cell| !cell.is_empty()) {
            rows.push(row);
        }
    }
    Ok(rows)
}

fn rows_to_csv(rows: &[Vec<String>]) -> String {
    let mut writer = csv::Writer::from_writer(Vec::new());
    for row in rows {
        if writer.write_record(row).is_err() {
            return String::new();
        }
    }
    match writer.into_inner() {
        Ok(bytes) => String::from_utf8(bytes).unwrap_or_default(),
        Err(_) => String::new(),
    }
}

fn render_structured_sheet(sheet_name: &str, headers: &[String], rows: &[Vec<String>]) -> String {
    let rendered = std::iter::once(headers)
        .chain(rows.iter().map(Vec::as_slice))
        .filter(|row| !row.is_empty())
        .map(|row| row.join(" | "))
        .collect::<Vec<_>>()
        .join("\n");
    if rendered.trim().is_empty() {
        String::new()
    } else {
        format!("Sheet: {sheet_name}\n{rendered}")
    }
}

fn render_rows_as_sheet(sheet_name: &str, rows: &[Vec<String>]) -> String {
    let Some((headers, body)) = rows.split_first() else {
        return String::new();
    };
    render_structured_sheet(sheet_name, headers, body)
}

fn table_from_range(range: &Range<Data>) -> Table {
    let mut rows = range
        .rows()
        .map(|row| {
            let mut cells = row.iter().map(cell_to_string).collect::<Vec<_>>();
            trim_trailing_empty_cells(&mut cells);
            cells
        })
        .collect::<Vec<_>>();

    trim_trailing_empty_rows(&mut rows);

    let headers = if rows.is_empty() {
        Vec::new()
    } else {
        rows.remove(0)
    };
    Table { headers, rows }
}

fn cell_to_string(cell: &Data) -> String {
    match cell {
        Data::Empty => String::new(),
        _ => cell.to_string(),
    }
}

struct DecodedCsv {
    text: String,
    encoding: &'static str,
    had_errors: bool,
    fallback_from: Option<&'static str>,
    decoded_bytes: usize,
    input_truncated: bool,
}

fn decode_csv_bytes(bytes: &[u8]) -> DecodedCsv {
    let mut detector = EncodingDetector::new();
    let detection_prefix = &bytes[..bytes.len().min(ENCODING_DETECTION_BYTES)];
    detector.feed(detection_prefix, true);

    let decoded_bytes = bytes.len().min(CSV_TEXT_DECODE_BYTES);
    let decode_sample = &bytes[..decoded_bytes];
    let input_truncated = decoded_bytes < bytes.len();
    let detected_encoding = detector.guess(None, true);
    let (decoded, _, had_errors) = detected_encoding.decode(decode_sample);

    if had_errors && detected_encoding.name() != UTF_8.name() {
        let (utf8_decoded, _, utf8_had_errors) = UTF_8.decode(decode_sample);
        return DecodedCsv {
            text: utf8_decoded.into_owned(),
            encoding: UTF_8.name(),
            had_errors: utf8_had_errors,
            fallback_from: Some(detected_encoding.name()),
            decoded_bytes,
            input_truncated,
        };
    }

    DecodedCsv {
        text: decoded.into_owned(),
        encoding: detected_encoding.name(),
        had_errors,
        fallback_from: None,
        decoded_bytes,
        input_truncated,
    }
}

fn trim_trailing_empty_cells(row: &mut Vec<String>) {
    while row.last().map(|cell| cell.is_empty()).unwrap_or(false) {
        row.pop();
    }
}

fn trim_trailing_empty_rows(rows: &mut Vec<Vec<String>>) {
    while rows
        .last()
        .map(|row| row.iter().all(|cell| cell.is_empty()))
        .unwrap_or(false)
    {
        rows.pop();
    }
}

fn chunk_large_content(content: &str) -> String {
    let original_len = content.chars().count();
    if original_len <= MAX_CONTENT_CHARS {
        return content.to_string();
    }

    let mut truncated = content.chars().take(MAX_CONTENT_CHARS).collect::<String>();
    truncated.push_str(&format!(
        "\n\n[TRUNCATED: Original content was {original_len} characters, showing first {MAX_CONTENT_CHARS}]"
    ));
    truncated
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

    const SAMPLE_CSV: &[u8] = b"Name,Age,Email\nJohn Doe,42,john@example.test\n";

    fn sample_xlsx() -> Vec<u8> {
        let cursor = Cursor::new(Vec::new());
        let mut writer = ZipWriter::new(cursor);
        let options = SimpleFileOptions::default();
        let files = [
            (
                "[Content_Types].xml",
                r#"<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>"#,
            ),
            (
                "_rels/.rels",
                r#"<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>"#,
            ),
            (
                "xl/workbook.xml",
                r#"<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Employees" sheetId="1" r:id="rId1"/><sheet name="Products" sheetId="2" r:id="rId2"/></sheets></workbook>"#,
            ),
            (
                "xl/_rels/workbook.xml.rels",
                r#"<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>"#,
            ),
            (
                "xl/worksheets/sheet1.xml",
                r#"<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:B3"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c><c r="B1" t="inlineStr"><is><t>Role</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Alice Smith</t></is></c><c r="B2" t="inlineStr"><is><t>Analyst</t></is></c></row><row r="3"><c r="A3" t="inlineStr"><is><t>Bob Johnson</t></is></c><c r="B3" t="inlineStr"><is><t>Editor</t></is></c></row></sheetData></worksheet>"#,
            ),
            (
                "xl/worksheets/sheet2.xml",
                r#"<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:B2"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Product</t></is></c><c r="B1" t="inlineStr"><is><t>Price</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Widget C</t></is></c><c r="B2"><v>30</v></c></row></sheetData></worksheet>"#,
            ),
        ];
        for (path, body) in files {
            writer.start_file(path, options).unwrap();
            writer.write_all(body.as_bytes()).unwrap();
        }
        writer.finish().unwrap().into_inner()
    }

    #[test]
    fn extracts_csv_sample() {
        let extracted =
            extract_spreadsheet_bytes(SAMPLE_CSV, Some("folder/sample.csv"), Some("text/csv"))
                .expect("CSV should extract");

        assert_eq!(extracted.source_format, "csv");
        assert_eq!(extracted.title, "sample");
        assert!(extracted.content.contains("Name | Age | Email"));
        assert!(extracted.content.contains("John Doe"));
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["row_count"],
            json!(2)
        );
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["content_truncated"],
            json!(false)
        );
    }

    #[test]
    fn extracts_xlsx_sample_sheets() {
        let sample = sample_xlsx();
        let extracted = extract_spreadsheet_bytes(
            &sample,
            Some("folder/sample.xlsx"),
            Some("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        )
        .expect("XLSX should extract");

        assert_eq!(extracted.source_format, "xlsx");
        assert_eq!(extracted.title, "sample");
        assert!(extracted.content.contains("Sheet: Employees"));
        assert!(extracted.content.contains("Sheet: Products"));
        assert!(extracted.content.contains("Bob Johnson"));
        assert!(extracted.content.contains("Widget C"));
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["sheet_count"],
            json!(2)
        );
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["sheets"]["Employees"]["rows"],
            json!(2)
        );
    }

    #[test]
    fn preserves_macro_binary_and_opendocument_spreadsheet_source_formats() {
        let sample = sample_xlsx();
        assert_eq!(
            infer_source_format(Some("folder/sample.xlsm"), None, &sample).name,
            "xlsm"
        );
        assert_eq!(
            infer_source_format(Some("folder/sample.xlsb"), None, &sample).name,
            "xlsb"
        );
        assert_eq!(
            infer_source_format(Some("folder/sample.ods"), None, &sample).name,
            "ods"
        );
        assert_eq!(
            infer_source_format(
                None,
                Some("application/vnd.ms-excel.sheet.macroenabled.12"),
                &sample,
            )
            .name,
            "xlsm"
        );
        assert_eq!(
            infer_source_format(
                None,
                Some("application/vnd.ms-excel.sheet.binary.macroenabled.12"),
                &sample,
            )
            .name,
            "xlsb"
        );
        assert_eq!(
            infer_source_format(
                None,
                Some("application/vnd.oasis.opendocument.spreadsheet"),
                &sample,
            )
            .name,
            "ods"
        );
    }

    #[test]
    fn csv_is_structurally_parsed_with_detected_delimiter() {
        let bytes = b"Name;Age;Email\nAda;36;ada@example.test\n";
        let extracted = extract_spreadsheet_bytes(bytes, Some("semicolon.csv"), Some("text/csv"))
            .expect("semicolon CSV should extract");

        assert!(extracted.content.contains("Name | Age | Email"));
        assert!(extracted.content.contains("Ada | 36 | ada@example.test"));
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["delimiter"],
            json!(";")
        );
    }

    #[test]
    fn large_csv_content_is_bounded_as_plain_text() {
        let mut csv = String::with_capacity(CSV_TEXT_DECODE_BYTES + 1024);
        csv.push_str("id,text\n");
        let mut index = 0usize;
        while csv.len() <= CSV_TEXT_DECODE_BYTES + 1024 {
            csv.push_str(&format!("{index},{}\n", "x".repeat(1_000)));
            index += 1;
        }

        let extracted =
            extract_spreadsheet_bytes(csv.as_bytes(), Some("large.csv"), Some("text/csv"))
                .expect("large CSV should extract");

        assert!(extracted.content.contains("[TRUNCATED:"));
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["decoded_bytes"],
            json!(CSV_TEXT_DECODE_BYTES)
        );
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["decode_byte_limit"],
            json!(CSV_TEXT_DECODE_BYTES)
        );
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["decode_input_truncated"],
            json!(true)
        );
        assert_eq!(
            extracted.extraction_metadata["content_extraction"]["content_truncated"],
            json!(true)
        );
    }

    #[test]
    fn openxml_preflight_flags_sparse_huge_declared_dimension() {
        let bytes = workbook_with_worksheet_dimension("A1:L1048576");
        let preflight = inspect_openxml_dimensions(&bytes, "xlsx")
            .expect("OpenXML dimension preflight should parse synthetic workbook");

        assert_eq!(preflight.scanned_sheets, 1);
        assert_eq!(preflight.max_declared_rows, 1_048_576);
        assert_eq!(preflight.max_declared_columns, 12);
        assert_eq!(preflight.max_declared_cells, 12_582_912);
        assert!(preflight.requires_sparse_fallback());
    }

    #[test]
    fn sparse_openxml_workbook_requests_external_office_fallback() {
        let bytes = workbook_with_worksheet_dimension("A1:XFD8523");
        let error = extract_spreadsheet_bytes(
            &bytes,
            Some("incoming/pathological.xlsx"),
            Some("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        )
        .expect_err("sparse workbook should not be parsed in-process");
        let message = error.to_string();

        assert!(message
            .contains("fallback_required=spreadsheet_openxml_declared_dimension_libreoffice"));
        assert!(message.contains("max_declared_columns=16384"));
    }

    #[test]
    fn rejects_cyclic_legacy_xls_cfb_before_calamine() {
        let mut bytes = vec![0u8; CFB_HEADER_LEN + 2 * 512];
        bytes[..8].copy_from_slice(CFB_SIGNATURE);
        bytes[26..28].copy_from_slice(&3u16.to_le_bytes());
        bytes[30..32].copy_from_slice(&9u16.to_le_bytes());
        bytes[32..34].copy_from_slice(&6u16.to_le_bytes());
        bytes[44..48].copy_from_slice(&1u32.to_le_bytes());
        bytes[48..52].copy_from_slice(&0u32.to_le_bytes());
        bytes[60..64].copy_from_slice(&CFB_ENDOFCHAIN.to_le_bytes());
        bytes[68..72].copy_from_slice(&CFB_ENDOFCHAIN.to_le_bytes());
        bytes[76..80].copy_from_slice(&1u32.to_le_bytes());
        for offset in (80..CFB_HEADER_LEN).step_by(4) {
            bytes[offset..offset + 4].copy_from_slice(&CFB_FREESECT.to_le_bytes());
        }

        let fat_sector = CFB_HEADER_LEN + 512;
        bytes[fat_sector..fat_sector + 4].copy_from_slice(&0u32.to_le_bytes());
        bytes[fat_sector + 4..fat_sector + 8].copy_from_slice(&CFB_FATSECT.to_le_bytes());
        for offset in ((fat_sector + 8)..(fat_sector + 512)).step_by(4) {
            bytes[offset..offset + 4].copy_from_slice(&CFB_FREESECT.to_le_bytes());
        }

        let error = validate_cfb_sector_chains(&bytes).expect_err("cyclic CFB should fail fast");
        assert!(
            error.to_string().contains("cyclic directory sector chain"),
            "unexpected error: {error}"
        );
    }

    #[test]
    fn does_not_validate_non_cfb_xls_bytes() {
        guard_legacy_xls_cfb(b"not a real xls", "xls").expect("non-CFB bytes are left to calamine");
        guard_legacy_xls_cfb(b"not a real xlsx", "xlsx")
            .expect("non-legacy spreadsheet formats are left to calamine");
    }

    fn workbook_with_worksheet_dimension(dimension: &str) -> Vec<u8> {
        let cursor = Cursor::new(Vec::new());
        let mut writer = ZipWriter::new(cursor);
        let options = SimpleFileOptions::default();
        writer
            .start_file("xl/worksheets/sheet1.xml", options)
            .expect("start worksheet member");
        write!(
            writer,
            r#"<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="{dimension}"/><sheetData/></worksheet>"#
        )
        .expect("write worksheet member");
        writer
            .finish()
            .expect("finish synthetic workbook")
            .into_inner()
    }
}
