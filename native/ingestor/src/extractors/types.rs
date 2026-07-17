use serde::Serialize;
use serde_json::Value;

#[derive(Debug, Serialize)]
pub(crate) struct GeneralExtractedDocument {
    pub(crate) source_format: String,
    pub(crate) title: String,
    pub(crate) content: String,
    pub(crate) language: Option<String>,
    pub(crate) extraction_metadata: Value,
    pub(crate) timing_ms: Value,
    pub(crate) content_length: usize,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct LanguageDetectionReport {
    pub(crate) detected_language: Option<String>,
    pub(crate) confidence: f64,
    pub(crate) threshold: f64,
    pub(crate) language: Option<String>,
    pub(crate) threshold_passed: bool,
    pub(crate) method: String,
    pub(crate) top_candidates: Vec<LanguageConfidence>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct LanguageConfidence {
    pub(crate) language: String,
    pub(crate) confidence: f64,
}
