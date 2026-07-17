use langdetect_rs::detector_factory::DetectorFactory;
use once_cell::sync::Lazy;
use serde_json::json;
use std::path::Path;

use super::types::{LanguageConfidence, LanguageDetectionReport};

const LANGUAGE_CONFIDENCE_THRESHOLD: f64 = 0.70;
const LANGDETECT_SEED: u64 = 42;
const LANGDETECT_PROFILES_DIR_ENV: &str = "DOCTRAIL_LANGDETECT_PROFILES_DIR";

static LANGDETECT_FACTORY: Lazy<DetectorFactory> = Lazy::new(build_langdetect_factory);

pub(crate) fn detect_language(text: &str) -> LanguageDetectionReport {
    let sample: String = text.chars().take(1000).collect();
    if sample.trim().is_empty() {
        return language_report(None, 0.0, "empty", Vec::new());
    }

    if let Some(report) = detect_script_language(&sample) {
        return report;
    }

    let candidates: Vec<LanguageConfidence> =
        match LANGDETECT_FACTORY.get_probabilities(&sample, None) {
            Ok(probabilities) => probabilities
                .into_iter()
                .filter_map(|language| {
                    let code = normalize_langdetect_code(language.lang.as_deref()?)?;
                    Some(LanguageConfidence {
                        language: code,
                        confidence: language.prob,
                    })
                })
                .take(5)
                .collect(),
            Err(_) => return language_report(None, 0.0, "langdetect_rs_error", Vec::new()),
        };

    let detected_language = candidates
        .first()
        .map(|candidate| candidate.language.clone());
    let detected_confidence = candidates
        .first()
        .map(|candidate| candidate.confidence)
        .unwrap_or(0.0);
    language_report(
        detected_language.as_deref(),
        detected_confidence,
        "langdetect_rs",
        candidates,
    )
}

fn build_langdetect_factory() -> DetectorFactory {
    if let Ok(path) = std::env::var(LANGDETECT_PROFILES_DIR_ENV) {
        let path = path.trim();
        if !path.is_empty() {
            let mut factory = DetectorFactory::new().build();
            factory.set_seed(LANGDETECT_SEED);
            factory
                .load_profile(Path::new(path))
                .unwrap_or_else(|error| {
                    panic!("failed to load langdetect-rs profiles from {path}: {error}")
                });
            return factory;
        }
    }

    DetectorFactory::default()
        .with_seed(Some(LANGDETECT_SEED))
        .build()
}

pub(crate) fn language_detection_metadata(report: &LanguageDetectionReport) -> serde_json::Value {
    json!({
        "detected_language": report.detected_language.clone(),
        "confidence": report.confidence,
        "threshold": report.threshold,
        "language": report.language.clone(),
        "threshold_passed": report.threshold_passed,
        "method": report.method.clone(),
        "top_candidates": report.top_candidates.clone(),
        "final_language": report.language.clone(),
    })
}

fn detect_script_language(sample: &str) -> Option<LanguageDetectionReport> {
    let mut han = 0;
    let mut kana = 0;
    let mut hangul = 0;
    let mut arabic_script = 0;
    let mut uyghur_signal = 0;

    for ch in sample.chars() {
        if ('\u{3400}'..='\u{9fff}').contains(&ch) {
            han += 1;
        }
        if ('\u{3040}'..='\u{30ff}').contains(&ch) {
            kana += 1;
        }
        if ('\u{ac00}'..='\u{d7af}').contains(&ch) {
            hangul += 1;
        }
        if is_arabic_script(ch) {
            arabic_script += 1;
        }
        if is_uyghur_signal(ch) {
            uyghur_signal += 1;
        }
    }

    if hangul >= 10 && hangul >= han {
        return Some(script_report("ko", "script_heuristic"));
    }
    if kana >= 5 && kana >= han.max(20) / 10 {
        return Some(script_report("ja", "script_heuristic"));
    }
    if han >= 20 {
        return Some(script_report("zh", "script_heuristic"));
    }
    if arabic_script >= 12 && uyghur_signal >= 2 {
        return Some(script_report("ug", "uyghur_script_heuristic"));
    }
    None
}

fn script_report(language: &str, method: &str) -> LanguageDetectionReport {
    language_report(
        Some(language),
        0.99,
        method,
        vec![LanguageConfidence {
            language: language.to_string(),
            confidence: 0.99,
        }],
    )
}

fn language_report(
    detected_language: Option<&str>,
    confidence: f64,
    method: &str,
    top_candidates: Vec<LanguageConfidence>,
) -> LanguageDetectionReport {
    let threshold_passed = confidence >= LANGUAGE_CONFIDENCE_THRESHOLD;
    let language = detected_language
        .filter(|_| threshold_passed)
        .map(str::to_string);
    LanguageDetectionReport {
        detected_language: detected_language.map(str::to_string),
        confidence,
        threshold: LANGUAGE_CONFIDENCE_THRESHOLD,
        language,
        threshold_passed,
        method: method.to_string(),
        top_candidates,
    }
}

fn is_arabic_script(ch: char) -> bool {
    let value = ch as u32;
    (0x0600..=0x06ff).contains(&value)
        || (0x0750..=0x077f).contains(&value)
        || (0x08a0..=0x08ff).contains(&value)
        || (0xfb50..=0xfdff).contains(&value)
        || (0xfe70..=0xfeff).contains(&value)
}

fn is_uyghur_signal(ch: char) -> bool {
    matches!(
        ch,
        '\u{06ad}' | '\u{06c6}' | '\u{06c7}' | '\u{06c8}' | '\u{06cb}' | '\u{06d0}' | '\u{06d5}'
    )
}

fn normalize_langdetect_code(code: &str) -> Option<String> {
    match code {
        "" | "unknown" => None,
        "zh-cn" | "zh-tw" => Some("zh".to_string()),
        "ru" | "rus" => Some("ru".to_string()),
        other => Some(other.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::detect_language;

    #[test]
    fn detects_chinese_with_script_heuristic() {
        let result =
            detect_language("这是一个中文测试文本，用来确认语言检测不会加载大型 Python 模型。");
        assert_eq!(result.language.as_deref(), Some("zh"));
        assert_eq!(result.method, "script_heuristic");
    }

    #[test]
    fn detects_english_with_langdetect() {
        let result = detect_language(
            "This English document contains enough surrounding context for language detection. The worker should classify it without loading Python or any external service.",
        );
        assert_eq!(result.language.as_deref(), Some("en"));
        assert_eq!(result.method, "langdetect_rs");
    }

    #[test]
    fn detects_uyghur_script_signal() {
        let result = detect_language("ئۇيغۇر تىلىدىكى بىر ئاددىي تېكىستنى سىناۋاتىمىز.");
        assert_eq!(result.language.as_deref(), Some("ug"));
        assert_eq!(result.method, "uyghur_script_heuristic");
    }
}
