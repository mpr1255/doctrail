//! Adversarial regression guard for the ingestor extraction path.
//!
//! Every case here is malformed / hostile input. The property under test is:
//! `extract_bytes` must RETURN (Ok or Err) without PANICKING and without
//! HANGING. Each case runs on a worker thread inside `catch_unwind`; the main
//! thread waits with a bounded timeout so a regression that reintroduces an
//! infinite loop / pathological blowup fails the test fast instead of wedging
//! the whole test run.
//!
//! Sizing note: the full-scale versions of the deep-nesting, mass-attribute,
//! and table-soup shapes below are known to HANG (kuchiki/readabilityrs/
//! scraper are super-linear on them) or to ABORT via stack overflow
//! (kuchiki/readabilityrs recurse on drop of deeply nested inline elements).
//! Those catastrophic full-scale shapes live in `examples/fuzz_extract.rs`.
//! Here the same shapes are kept at a moderate size that currently completes
//! in well under a second, so this stays a fast, green `cargo test` guard that
//! still exercises the same parser code paths.

use std::fs;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::sync::Once;
use std::time::Duration;

use _ingest_native::{extract_bytes, ExtractOptions, HtmlKind};

const WATCHDOG: Duration = Duration::from_secs(30);

static SILENCE_PANICS: Once = Once::new();

fn kind_of(k: &str) -> HtmlKind {
    match k {
        "html" => HtmlKind::Html,
        "mhtml" => HtmlKind::Mhtml,
        _ => HtmlKind::Auto,
    }
}

/// Run one case on a watchdog thread. Returns "ok" / "err" (both acceptable)
/// or "panic" / "hang" (both failures).
fn guarded(bytes: Vec<u8>, kind: &str, mime: Option<&str>, ext: Option<&str>) -> &'static str {
    SILENCE_PANICS.call_once(|| {
        std::panic::set_hook(Box::new(|_| {}));
        // Keep the wall-clock deadline short so the full-scale hang shapes below
        // return a terminal error quickly instead of spending the default 20 s.
        std::env::set_var("DOCTRAIL_INGEST_TIMEOUT_MS", "5000");
    });
    let kind = kind_of(kind);
    let mime = mime.map(str::to_string);
    let ext = ext.map(str::to_string);
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let opts = ExtractOptions {
            mime_type: mime.as_deref(),
            source_path: ext.as_deref(),
            kind,
        };
        let outcome = catch_unwind(AssertUnwindSafe(|| extract_bytes(&bytes, opts)));
        let tag = match outcome {
            Ok(Ok(_)) => "ok",
            Ok(Err(_)) => "err",
            Err(_) => "panic",
        };
        let _ = tx.send(tag);
    });
    // On timeout the worker thread is left as a daemon; it dies when the test
    // process exits. We do NOT join it (a real hang cannot be interrupted).
    rx.recv_timeout(WATCHDOG).unwrap_or("hang")
}

fn html(bytes: Vec<u8>) -> &'static str {
    guarded(bytes, "html", Some("text/html"), Some("page.html"))
}

fn mhtml(bytes: Vec<u8>) -> &'static str {
    guarded(bytes, "mhtml", Some("message/rfc822"), Some("page.mhtml"))
}

fn pdf(bytes: Vec<u8>) -> &'static str {
    guarded(bytes, "auto", Some("application/pdf"), Some("document.pdf"))
}

fn assert_safe(label: &str, tag: &str) {
    assert!(
        tag == "ok" || tag == "err",
        "adversarial case `{label}` did not return safely: got `{tag}` (panic or hang)"
    );
}

fn deep(open: &str, close: &str, depth: usize) -> Vec<u8> {
    let mut b = String::from("<html><body>");
    for _ in 0..depth {
        b.push_str(open);
    }
    b.push_str("innermost text content long enough to matter");
    for _ in 0..depth {
        b.push_str(close);
    }
    b.push_str("</body></html>");
    b.into_bytes()
}

fn deterministic_bytes(seed: u64, size: usize) -> Vec<u8> {
    let mut state = seed;
    (0..size)
        .map(|_| {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 33) as u8
        })
        .collect()
}

fn sample_pdf_bytes() -> Vec<u8> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/files/federalist_fixture.pdf");
    fs::read(path).expect("read bundled sample PDF")
}

fn adversarial_pdf_cases() -> Vec<(String, Vec<u8>)> {
    let sample = sample_pdf_bytes();
    let mut sample_with_junk_suffix = sample.clone();
    sample_with_junk_suffix.extend(deterministic_bytes(0xa11c_e5ed, 16 * 1024));

    let mut pdf_header_random = b"%PDF-1.7\n%\xff\xff\xff\xff\n".to_vec();
    pdf_header_random.extend(deterministic_bytes(0x1234_5678, 128 * 1024));

    let huge_declared_stream = b"%PDF-1.7
1 0 obj
<< /Length 999999999 /Filter /FlateDecode >>
stream
not actually compressed
endstream
endobj
2 0 obj
<< /Type /Catalog /Pages 3 0 R >>
endobj
3 0 obj
<< /Type /Pages /Count 1 /Kids [4 0 R] >>
endobj
4 0 obj
<< /Type /Page /Parent 3 0 R /Contents 1 0 R >>
endobj
xref
0 5
0000000000 65535 f
0000000015 00000 n
0000000118 00000 n
0000000170 00000 n
0000000230 00000 n
trailer
<< /Root 2 0 R /Size 5 >>
startxref
290
%%EOF
"
    .to_vec();

    let cyclic_pages = b"%PDF-1.7
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Count 1000000 /Kids [2 0 R] >>
endobj
xref
0 3
0000000000 65535 f
0000000015 00000 n
0000000067 00000 n
trailer
<< /Root 1 0 R /Size 3 >>
startxref
130
%%EOF
"
    .to_vec();

    let mut cases = vec![
        ("empty-pdf".to_string(), Vec::new()),
        ("pdf-header-only".to_string(), b"%PDF-1.7\n".to_vec()),
        (
            "pdf-random-body".to_string(),
            deterministic_bytes(0xfeed_cafe, 64 * 1024),
        ),
        ("pdf-header-random-body".to_string(), pdf_header_random),
        ("pdf-huge-declared-stream".to_string(), huge_declared_stream),
        ("pdf-cyclic-pages".to_string(), cyclic_pages),
        ("sample-pdf".to_string(), sample.clone()),
        (
            "sample-pdf-with-junk-suffix".to_string(),
            sample_with_junk_suffix,
        ),
    ];

    for length in [1usize, 8, 64, 256, 1024, sample.len().saturating_sub(1)] {
        if length > 0 && length < sample.len() {
            cases.push((
                format!("sample-pdf-truncated-{length}"),
                sample[..length].to_vec(),
            ));
        }
    }
    cases
}

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(default)
}

fn collect_pdf_paths(root: &Path, limit: usize, paths: &mut Vec<PathBuf>) {
    if paths.len() >= limit {
        return;
    }
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_pdf_paths(&path, limit, paths);
        } else if path
            .extension()
            .and_then(|extension| extension.to_str())
            .is_some_and(|extension| extension.eq_ignore_ascii_case("pdf"))
        {
            paths.push(path);
        }
        if paths.len() >= limit {
            return;
        }
    }
}

#[test]
fn empty_and_truncated_inputs_are_safe() {
    assert_safe("empty-html", html(Vec::new()));
    assert_safe("empty-mhtml", mhtml(Vec::new()));
    assert_safe("single-lt", html(b"<".to_vec()));
    assert_safe("lt-bang", html(b"<!".to_vec()));
    assert_safe("open-html", html(b"<html".to_vec()));
    assert_safe("doctype-only", html(b"<!DOCTYPE html>".to_vec()));
    assert_safe("whitespace", html(vec![b' '; 4096]));
    assert_safe(
        "mhtml-headers-no-body",
        mhtml(b"Content-Type: multipart/related; boundary=\"b\"\r\n\r\n".to_vec()),
    );
}

#[test]
fn random_binary_with_html_hints_is_safe() {
    for size in [16usize, 1024, 65536] {
        let bytes = deterministic_bytes(0x1234_5678_9abc_def0, size);
        assert_safe(&format!("random-html-{size}"), html(bytes.clone()));
        assert_safe(&format!("random-mhtml-{size}"), mhtml(bytes));
    }
}

#[test]
fn malformed_pdf_inputs_are_safe() {
    for (label, bytes) in adversarial_pdf_cases() {
        assert_safe(&label, pdf(bytes));
    }
}

#[test]
fn pdf_parallel_stress_is_safe() {
    SILENCE_PANICS.call_once(|| {
        std::panic::set_hook(Box::new(|_| {}));
        std::env::set_var("DOCTRAIL_INGEST_TIMEOUT_MS", "5000");
    });

    let mut cases = adversarial_pdf_cases();
    if let Ok(root) = std::env::var("DOCTRAIL_PDF_STRESS_CORPUS_DIR") {
        let limit = env_usize("DOCTRAIL_PDF_STRESS_CORPUS_LIMIT", 25);
        let mut paths = Vec::new();
        collect_pdf_paths(Path::new(&root), limit, &mut paths);
        for path in paths {
            if let Ok(bytes) = fs::read(&path) {
                cases.push((path.display().to_string(), bytes));
            }
        }
    }

    let threads = env_usize("DOCTRAIL_PDF_STRESS_THREADS", 4);
    let iterations = env_usize("DOCTRAIL_PDF_STRESS_ITERATIONS", 8);
    let timeout_seconds = env_usize("DOCTRAIL_PDF_STRESS_TIMEOUT_SECONDS", 120) as u64;
    let (tx, rx) = mpsc::channel();

    for thread_index in 0..threads {
        let tx = tx.clone();
        let cases = cases.clone();
        std::thread::spawn(move || {
            let mut ok = 0usize;
            let mut err = 0usize;
            for iteration in 0..iterations {
                let case_index = (thread_index + iteration) % cases.len();
                let (label, bytes) = &cases[case_index];
                let options = ExtractOptions {
                    mime_type: Some("application/pdf"),
                    source_path: Some(label.as_str()),
                    kind: HtmlKind::Auto,
                };
                match catch_unwind(AssertUnwindSafe(|| extract_bytes(bytes, options))) {
                    Ok(Ok(_)) => ok += 1,
                    Ok(Err(_)) => err += 1,
                    Err(_) => {
                        let _ = tx.send(Err(format!(
                            "panic in thread {thread_index}, iteration {iteration}, case {label}"
                        )));
                        return;
                    }
                }
            }
            let _ = tx.send(Ok((ok, err)));
        });
    }
    drop(tx);

    let mut total_ok = 0usize;
    let mut total_err = 0usize;
    for _ in 0..threads {
        match rx.recv_timeout(Duration::from_secs(timeout_seconds)) {
            Ok(Ok((ok, err))) => {
                total_ok += ok;
                total_err += err;
            }
            Ok(Err(message)) => panic!("{message}"),
            Err(_) => panic!(
                "PDF stress test did not finish within {timeout_seconds}s; threads={threads}, iterations={iterations}, cases={}",
                cases.len()
            ),
        }
    }

    assert_eq!(total_ok + total_err, threads * iterations);
}

#[test]
fn malformed_mhtml_is_safe() {
    assert_safe(
        "mhtml-no-boundary",
        mhtml(b"Content-Type: multipart/related\r\n\r\n--x\r\nblah\r\n--x--\r\n".to_vec()),
    );
    assert_safe(
        "mhtml-boundary-absent",
        mhtml(
            b"Content-Type: multipart/related; boundary=\"AAA\"\r\n\r\nnothing here\r\n".to_vec(),
        ),
    );
    assert_safe(
        "mhtml-truncated-base64",
        mhtml(b"Content-Type: multipart/related; boundary=\"b\"\r\n\r\n--b\r\nContent-Type: text/html\r\nContent-Transfer-Encoding: base64\r\n\r\nSGVsbG8=garbage!!!@@@\r\n--b--\r\n".to_vec()),
    );
    assert_safe(
        "mhtml-truncated-qp",
        mhtml(b"Content-Type: multipart/related; boundary=\"b\"\r\n\r\n--b\r\nContent-Type: text/html\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n<p>hi=\r\n=E4=B8=\r\n--b--\r\n".to_vec()),
    );
    assert_safe(
        "mhtml-only-image-part",
        mhtml(b"Content-Type: multipart/related; boundary=\"b\"\r\n\r\n--b\r\nContent-Type: image/png\r\n\r\n\x89PNGnothtml\r\n--b--\r\n".to_vec()),
    );
    assert_safe(
        "mhtml-empty-boundary",
        mhtml(b"Content-Type: multipart/related; boundary=\"\"\r\n\r\n----\r\nContent-Type: text/html\r\n\r\n<p>x</p>\r\n------\r\n".to_vec()),
    );
    assert_safe(
        "mhtml-boundary-in-body",
        mhtml(b"Content-Type: multipart/related; boundary=\"b\"\r\n\r\n--b\r\nContent-Type: text/html\r\n\r\n<html><body>--b--b--b</body></html>\r\n--b--\r\n".to_vec()),
    );
}

#[test]
fn charset_traps_are_safe() {
    let body = "中文正文内容用于测试编码陷阱，这段文字足够长以进入抽取流程。".repeat(3);
    assert_safe(
        "meta-gbk-utf8-bytes",
        html(format!("<html><head><meta charset=\"gbk\"><title>标题</title></head><body><article><p>{body}</p></article></body></html>").into_bytes()),
    );
    let gbk_src = format!("<html><head><meta charset=\"utf-8\"></head><body><article><p>{body}</p></article></body></html>");
    let (gbk, _, _) = encoding_rs::GBK.encode(&gbk_src);
    assert_safe("meta-utf8-gbk-bytes", html(gbk.into_owned()));
    for bogus in ["potato", "", "utf-8\"><script", "gb\x0018030"] {
        assert_safe(
            "bogus-charset",
            html(format!("<html><head><meta charset=\"{bogus}\"></head><body><article><p>{body}</p></article></body></html>").into_bytes()),
        );
    }
    // conflicting BOM + meta
    let mut bom = vec![0xef, 0xbb, 0xbf];
    bom.extend_from_slice(format!("<html><head><meta charset=\"utf-16le\"></head><body><article><p>{body}</p></article></body></html>").as_bytes());
    assert_safe("utf8-bom-meta-utf16", html(bom));
    assert_safe(
        "meta-utf16-odd-length",
        html(b"<html><head><meta charset=\"utf-16\"></head><body>odd".to_vec()),
    );
}

#[test]
fn entity_expansion_is_safe() {
    let billion_laughs = r#"<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
 <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
 <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
]>
<html><body><p>&lol6;</p></body></html>"#;
    assert_safe("billion-laughs", html(billion_laughs.as_bytes().to_vec()));
    let mut amp = String::from("<html><body><p>");
    amp.push_str(&"&amp;".repeat(20_000));
    amp.push_str("</p></body></html>");
    assert_safe("amp-flood", html(amp.into_bytes()));
    let mut refs = String::from("<html><body><p>");
    refs.push_str(&"&#x4e2d;".repeat(20_000));
    refs.push_str("</p></body></html>");
    assert_safe("numref-flood", html(refs.into_bytes()));
    let mut bogus = String::from("<html><body><p>");
    bogus.push_str(&"&notarealentity;".repeat(20_000));
    bogus.push_str("</p></body></html>");
    assert_safe("bogus-entity-flood", html(bogus.into_bytes()));
}

#[test]
fn unicode_escape_floods_are_safe() {
    let mut cjk = String::from("<html><body><article><p>");
    cjk.push_str(&r"新疆".repeat(30_000));
    cjk.push_str("</p></article></body></html>");
    assert_safe("cjk-u-escapes", html(cjk.into_bytes()));

    let mut surrogate = String::from("<html><body><article><p>");
    surrogate.push_str(&r"𐀀".repeat(20_000));
    surrogate.push_str("</p></article></body></html>");
    assert_safe("surrogate-u-escapes", html(surrogate.into_bytes()));

    let mut malformed = String::from("<html><body><article><p>");
    malformed.push_str(&r"\uZZZZ\u12".repeat(20_000));
    malformed.push_str("</p></article></body></html>");
    assert_safe("malformed-u-escapes", html(malformed.into_bytes()));
}

#[test]
fn invalid_utf8_and_nul_bytes_are_safe() {
    for byte in [0x80u8, 0xff, 0xc0] {
        let mut b = Vec::from(&b"<html><body><article><p>"[..]);
        b.extend(std::iter::repeat_n(byte, 50_000));
        b.extend_from_slice(b"</p></article></body></html>");
        assert_safe("invalid-utf8", html(b));
    }
    assert_safe(
        "nul-in-tags",
        html(b"<h\0tml><bo\0dy><art\0icle><p>content\0with\0nuls that is long enough to reach the extraction threshold used by the pipeline.</p></article></body></html>".to_vec()),
    );
    let mut nulflood = Vec::from(&b"<html><body><article><p>"[..]);
    nulflood.extend(std::iter::repeat_n(0u8, 200_000));
    nulflood.extend_from_slice(b"tail</p></article></body></html>");
    assert_safe("nul-flood", html(nulflood));
}

#[test]
fn tag_soup_and_httrack_and_forms_are_safe() {
    assert_safe(
        "overlapping-b-i",
        html(b"<html><body><b><i>bold italic</b> just italic</i><p>tail paragraph with adequate length for the extraction threshold requirement here.</p></body></html>".to_vec()),
    );
    // HTTrack marker with >100 bytes of junk before the html start
    let mut httrack = vec![b'#'; 300];
    httrack.extend_from_slice(b"<!-- Mirrored from example.com/ by HTTrack Website Copier -->\n<!DOCTYPE html><html><head><title>Cached</title></head><body><article><p>Cached page content that is long enough to pass extraction thresholds without any issue here.</p></article></body></html>");
    assert_safe("httrack-junk-prefix", html(httrack));
    assert_safe(
        "whole-page-form",
        html(b"<html><body><form><article><h1>Title</h1><p>Body text that is reasonably long so that the extraction threshold is comfortably exceeded for this whole-page form wrapper scenario.</p></article></form></body></html>".to_vec()),
    );
}

#[test]
fn long_single_line_is_safe() {
    let mut line = String::from("<html><body><article><p>");
    line.push_str(&"word ".repeat(200_000)); // ~1MB single line
    line.push_str("</p></article></body></html>");
    assert_safe("long-single-line", html(line.into_bytes()));
}

// Under the depth guard (< MAX_NESTING_DEPTH=512) the parser must run and
// succeed; at or above it the pre-parse gate returns a terminal error before any
// parser is invoked. Both outcomes are "safe"; the watchdog fails the test if a
// regression makes any shape panic or hang.
#[test]
fn nesting_below_guard_reaches_parser_and_is_safe() {
    // Depth 400 is below the guard, so these traverse the real parser path.
    assert_safe("deep-div-400", html(deep("<div>", "</div>", 400)));
    assert_safe("deep-span-400", html(deep("<span>", "</span>", 400)));
    assert_safe(
        "deep-blockquote-400",
        html(deep("<blockquote>", "</blockquote>", 400)),
    );
}

#[test]
fn deep_nesting_at_and_above_guard_is_safe() {
    assert_safe("deep-div-700", html(deep("<div>", "</div>", 700)));
    assert_safe("deep-span-700", html(deep("<span>", "</span>", 700)));
    assert_safe(
        "deep-table-500",
        html(deep("<table><tr><td>", "</td></tr></table>", 500)),
    );
    // unclosed deep nesting (no closing tags)
    let mut unclosed = String::from("<html><body>");
    for _ in 0..700 {
        unclosed.push_str("<div>");
    }
    unclosed.push_str("tail");
    assert_safe("deep-div-unclosed-700", html(unclosed.into_bytes()));
}

// The fuzz-discovered bypass: text between opens does NOT close a <div>, so this
// is genuinely 100k deep and previously slipped the guard and hung the parser.
// The accurate depth counter now rejects it before any parser runs.
#[test]
fn full_scale_deep_nesting_with_text_between_opens_is_safe() {
    let mut b = String::from("<html><body>");
    for _ in 0..100_000 {
        b.push_str("<div>a");
    }
    b.push_str("</body></html>");
    assert_safe("div-with-text-100k", html(b.into_bytes()));
}

// The review-discovered bypass: HTML5 ignores the trailing slash on a non-void
// element, so <div/> still opens a div and nests 100k deep. The guard must reject
// it rather than let it reach (and hang) readabilityrs.
#[test]
fn full_scale_self_closing_non_void_nesting_is_safe() {
    let mut b = String::from("<html><body>");
    for _ in 0..100_000 {
        b.push_str("<div/>");
    }
    b.push_str("</body></html>");
    assert_safe("self-closing-div-100k", html(b.into_bytes()));
}

#[test]
fn moderate_mass_attributes_is_safe() {
    let mut b = String::from("<html><body><div");
    for i in 0..2500 {
        b.push_str(&format!(" a{i}=\"{i}\""));
    }
    b.push_str(">text content that is reasonably long for the extraction threshold here.</div></body></html>");
    assert_safe("mass-attributes-2500", html(b.into_bytes()));
}

#[test]
fn moderate_table_soup_is_safe() {
    let mut b = String::from("<html><body><table>");
    for i in 0..700 {
        b.push_str(&format!("<tr><td>row {i} cell without closing tags"));
    }
    assert_safe("unclosed-table-rows-700", html(b.into_bytes()));
}

// Full-scale unclosed table soup is a BREADTH pathology: depth stays ~1 so the
// structural guard (correctly) lets it through, but readabilityrs is super-linear
// on it. The wall-clock deadline bounds it — this returns a terminal error (or
// completes) instead of hanging.
#[test]
fn full_scale_table_soup_is_bounded() {
    let mut b = String::from("<html><body><table>");
    for i in 0..20_000 {
        b.push_str(&format!("<tr><td>row {i} cell without closing tags"));
    }
    b.push_str("</table></body></html>");
    assert_safe("unclosed-table-rows-20k", html(b.into_bytes()));
}
