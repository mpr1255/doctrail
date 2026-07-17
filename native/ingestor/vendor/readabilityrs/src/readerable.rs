//! Quick readability check without full parsing.
//!
//! This module provides the [`is_probably_readerable`] function, which performs
//! a fast pre-flight check to determine if a document is likely to have extractable
//! article content without doing a full parse.
//!
//! ## Use Case
//!
//! Use this function to quickly filter out pages that are unlikely to contain article
//! content, saving the cost of a full parse:
//!
//! ```rust
//! use readabilityrs::{is_probably_readerable, Readability};
//!
//! let html = "<html>...</html>";
//!
//! // Quick check first
//! if is_probably_readerable(html, None) {
//!     // Do full parse
//!     let readability = Readability::new(html, None, None).unwrap();
//!     if let Some(article) = readability.parse() {
//!         println!("Article extracted!");
//!     }
//! } else {
//!     println!("Not an article page, skipping parse");
//! }
//! ```
//!
//! ## Performance
//!
//! This check is significantly faster than a full parse because it only looks
//! for basic content signals without doing deep analysis or scoring.

use scraper::{Html, Selector};

/// Options for the readability pre-flight check.
///
/// Controls the thresholds used by [`is_probably_readerable`] to determine
/// if a document is likely to be parseable.
///
/// ## Example
///
/// ```rust
/// use readabilityrs::{is_probably_readerable, ReaderableOptions};
///
/// let html = "<html>...</html>";
///
/// let options = ReaderableOptions {
///     min_content_length: 200,
///     min_score: 30.0,
/// };
///
/// let is_readerable = is_probably_readerable(html, Some(options));
/// ```
#[derive(Debug, Clone)]
pub struct ReaderableOptions {
    /// Minimum content length to consider a paragraph.
    ///
    /// Paragraphs shorter than this are ignored when calculating the
    /// readability score.
    ///
    /// Default: `140`
    pub min_content_length: usize,

    /// Minimum score threshold to consider a page readerable.
    ///
    /// The score is calculated based on the length and number of content
    /// paragraphs found in the document.
    ///
    /// Default: `20.0`
    pub min_score: f64,
}

impl Default for ReaderableOptions {
    fn default() -> Self {
        Self {
            min_content_length: 140,
            min_score: 20.0,
        }
    }
}

/// Quick check to determine if a document is likely to be readerable.
///
/// This function performs a fast analysis to predict whether full article extraction
/// is likely to succeed, without doing the expensive full parse. It looks for basic
/// content signals like paragraphs with sufficient text.
///
/// ## Arguments
///
/// * `html` - The HTML document to check
/// * `options` - Optional custom thresholds (uses defaults if `None`)
///
/// ## Returns
///
/// `true` if the document likely contains extractable article content, `false` otherwise.
///
/// ## Example
///
/// ```rust
/// use readabilityrs::is_probably_readerable;
///
/// let article_html = r#"
///     <html><body>
///         <article>
///             <p>This is a substantial paragraph with enough content to indicate that this page
///             likely contains article text that can be extracted by the readability algorithm.
///             The paragraph needs to be long enough to pass the minimum content length threshold.</p>
///         </article>
///     </body></html>
/// "#;
///
/// assert!(is_probably_readerable(article_html, None));
///
/// let non_article_html = "<html><body><p>Short</p></body></html>";
/// assert!(!is_probably_readerable(non_article_html, None));
/// ```
///
/// ## With Custom Options
///
/// ```rust
/// use readabilityrs::{is_probably_readerable, ReaderableOptions};
///
/// let html = "<html>...</html>";
/// let options = ReaderableOptions {
///     min_content_length: 200,
///     min_score: 30.0,
/// };
///
/// if is_probably_readerable(html, Some(options)) {
///     println!("Likely readerable with stricter thresholds");
/// }
/// ```
///
/// ## Algorithm
///
/// The function finds all `<p>`, `<pre>`, and `<article>` elements in the document,
/// then filters out paragraphs shorter than the configured `min_content_length`. A score
/// is calculated based on the remaining content length, and the function returns `true`
/// if this score exceeds the `min_score` threshold.
///
/// ## Performance
///
/// This function is much faster than a full parse, making it ideal for batch processing
/// large numbers of URLs, pre-filtering in crawlers or scrapers, and quick content
/// classification tasks.
pub fn is_probably_readerable(html: &str, options: Option<ReaderableOptions>) -> bool {
    let options = options.unwrap_or_default();
    let document = Html::parse_document(html);

    // TODO: Implement full isProbablyReaderable logic
    // For now, just do a basic check

    let p_selector = Selector::parse("p, pre, article").unwrap();
    let paragraphs: Vec<_> = document.select(&p_selector).collect();

    if paragraphs.is_empty() {
        return false;
    }

    let mut score = 0.0;

    for p in paragraphs {
        let text = p.text().collect::<String>();
        let text_len = text.trim().len();

        if text_len < options.min_content_length {
            continue;
        }

        score += ((text_len - options.min_content_length) as f64).sqrt();

        if score > options.min_score {
            return true;
        }
    }

    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_is_probably_readerable() {
        let html = r#"
            <html>
                <body>
                    <article>
                        <p>This is a long enough paragraph that should make the content readerable.
                        It has sufficient content to pass the minimum threshold check. Adding more text here to ensure
                        we definitely exceed the 140 character minimum requirement for each paragraph element.</p>
                        <p>Another paragraph with more content to increase the score. This paragraph also needs to be
                        long enough to contribute to the overall readability score calculation and help us pass the test.</p>
                    </article>
                </body>
            </html>
        "#;

        assert!(is_probably_readerable(html, None));
    }

    #[test]
    fn test_not_readerable() {
        let html = r#"
            <html>
                <body>
                    <p>Short</p>
                </body>
            </html>
        "#;

        assert!(!is_probably_readerable(html, None));
    }
}
