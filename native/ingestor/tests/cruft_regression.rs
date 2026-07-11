use std::collections::HashSet;

use _ingest_native::{content_quality_rejection, extract_bytes, ExtractOptions, HtmlKind};
use anyhow::{anyhow, Result};

const THRESHOLD: f64 = 0.8;

fn worker_content(html: &str) -> Result<String> {
    let extracted = extract_bytes(
        html.as_bytes(),
        ExtractOptions {
            mime_type: Some("text/html"),
            source_path: Some("synthetic.html"),
            kind: HtmlKind::Html,
        },
    )?;
    if let Some(reason) = content_quality_rejection(&extracted.content, &extracted.title) {
        return Err(anyhow!(reason));
    }
    Ok(extracted.content)
}

fn token_jaccard(left: &str, right: &str) -> f64 {
    let left_tokens: HashSet<String> = left.split_whitespace().map(str::to_string).collect();
    let right_tokens: HashSet<String> = right.split_whitespace().map(str::to_string).collect();
    if left_tokens.is_empty() && right_tokens.is_empty() {
        return 1.0;
    }
    let intersection = left_tokens.intersection(&right_tokens).count();
    let union = left_tokens.union(&right_tokens).count();
    intersection as f64 / union as f64
}

fn without_whitespace(text: &str) -> String {
    text.chars().filter(|ch| !ch.is_whitespace()).collect()
}

fn shared_chrome() -> String {
    let mut links = String::new();
    for label in [
        "首页",
        "政务公开",
        "政务服务",
        "政民互动",
        "走进本地",
        "领导介绍",
        "政策法规",
        "通知公告",
        "资料下载",
        "网站地图",
        "联系我们",
        "友情链接",
    ] {
        links.push_str(&format!(r#"<a href="/{label}">{label}</a>"#));
    }
    format!(
        r#"
        <header class="site-header">{links}</header>
        <nav class="breadcrumb">当前位置：首页 &gt; 政务公开 &gt; 通知公告</nav>
        <footer class="footer">
          版权所有：测试政府网站 新ICP备000000号 公网安备000000
          友情链接 中国政府网 新疆政府网 自治区部门网站
        </footer>
        "#
    )
}

fn government_article(title: &str, body: &str) -> String {
    let chrome = shared_chrome();
    format!(
        r#"
        <html>
          <head><title>{title}</title></head>
          <body>
            {chrome}
            <main id="content">
              <article>
                <h1>{title}</h1>
                <p>{body}</p>
                <p>会议要求相关单位建立台账、压实责任、定期公开进展，确保工作措施落到实处。</p>
              </article>
            </main>
            {chrome}
          </body>
        </html>
        "#
    )
}

#[test]
fn shared_government_chrome_does_not_drive_dedupe_similarity() {
    let left = worker_content(&government_article(
        "饮用水水质检验公告",
        "县住建管理局发布第一季度饮用水水质检验公告，检测项目包括浊度、余氯、菌落总数和管网末梢水质。",
    ))
    .unwrap();
    let right = worker_content(&government_article(
        "建设工程规划许可批前公示",
        "自然资源局发布物流商贸城建设工程规划许可批前公示，公示内容包括建设规模、用地位置和意见反馈方式。",
    ))
    .unwrap();

    assert!(left.contains("饮用水水质检验公告"));
    assert!(right.contains("建设工程规划许可批前公示"));
    assert!(!left.contains("友情链接"));
    assert!(!right.contains("友情链接"));
    assert!(token_jaccard(&left, &right) < THRESHOLD);
}

#[test]
fn shared_related_links_footer_does_not_make_distinct_articles_match() {
    let related = r#"
      <aside class="related">
        相关新闻 陇周刊（2017年 第3期） 陇周刊（2017年 第4期）
        精彩推荐 关注我们 版权声明：凡注有稿件来源为测试网的稿件均为版权稿件。
      </aside>
    "#;
    let article = |title: &str, body: &str| {
        format!(
            r#"<html><head><title>{title}</title></head><body>
               <article><h1>{title}</h1><p>{body}</p></article>{related}
               </body></html>"#
        )
    };
    let left = worker_content(&article(
        "年度网络谣言盘点",
        "联合辟谣平台梳理年度网络谣言，提醒公众核验来源，不传播未经证实的信息。",
    ))
    .unwrap();
    let right = worker_content(&article(
        "厨王争霸赛举行",
        "餐饮行业技能竞赛在新区举行，多家企业厨师现场展示菜品制作和复工复产成果。",
    ))
    .unwrap();

    assert!(left.contains("年度网络谣言盘点"));
    assert!(right.contains("厨王争霸赛举行"));
    assert!(token_jaccard(&left, &right) < THRESHOLD);
}

#[test]
fn http_error_templates_are_rejected_before_dedupe() {
    let page = |url: &str| {
        format!(
            r#"<html><head><title>HTTP 错误 404.0 - Not Found</title></head><body>
               <h1>HTTP 错误 404.0 - Not Found</h1>
               <p>您要找的资源已被删除、已更名或暂时不可用。</p>
               <h2>详细错误信息</h2>
               <p>模块 IIS Web Core 通知 MapRequestHandler 处理程序 StaticFile 错误代码 0x80070002</p>
               <p>请求的 URL {url}</p>
               <p>物理路径 D:\webroot\site\missing.html 登录方法 匿名 登录用户 匿名</p>
               <p>此错误表明文件或目录在服务器上不存在。请创建文件或目录并重新尝试请求。</p>
               </body></html>"#
        )
    };

    let left = worker_content(&page("http://example.test/a.jpg")).unwrap_err();
    let right = worker_content(&page("http://example.test/b.jpg")).unwrap_err();

    assert!(left.to_string().contains("http error template"));
    assert!(right.to_string().contains("http error template"));
}

#[test]
fn apache_php_error_templates_are_rejected_without_a_status_title() {
    let page = r#"
        <html>
          <head><title>Unexpected response</title></head>
          <body>
            <p>The requested URL /index.php was not found on this server.</p>
            <p>Apache/2.4.39 (Win64) OpenSSL/1.1.1b PHP/7.2.18 mod_fcgid/2.3.10-dev Server at yunchuangyinyue.com Port 443</p>
          </body>
        </html>
    "#;

    let error = worker_content(page).unwrap_err();
    assert!(error
        .to_string()
        .contains("server error template (apache_php_short_http_error)"));
}

#[test]
fn apache_php_404_stub_with_status_title_is_rejected_before_dedupe() {
    let page = r#"
        <html>
          <head><title>404 Not Found</title></head>
          <body>
            <h1>404 Not Found</h1>
            <p>The requested URL /index.php was not found on this server.</p>
            <p>Apache/2.4.39 (Win64) OpenSSL/1.1.1b PHP/7.2.18 mod_fcgid/2.3.10-dev Server at yunchuangyinyue.com Port 443</p>
          </body>
        </html>
    "#;

    let error = worker_content(page).unwrap_err();
    assert!(error
        .to_string()
        .contains("server error template (apache_php_short_http_error)"));
}

#[test]
fn shell_only_listing_pages_are_rejected_or_kept_below_dedupe_threshold() {
    let listing = |headline: &str, items: &[&str]| {
        let chrome = shared_chrome();
        let items = items
            .iter()
            .map(|item| format!(r#"<li><a href="/item">{item}</a></li>"#))
            .collect::<String>();
        format!(
            r#"<html><head><title>{headline}</title></head><body>
               {chrome}
               <section class="list"><h1>{headline}</h1><ul>{items}</ul></section>
               {chrome}
               </body></html>"#
        )
    };

    let left = worker_content(&listing(
        "财政公开公告",
        &[
            "预算外资金管理办法",
            "会计继续教育培训通知",
            "财政局依法行政重点工作安排",
        ],
    ));
    let right = worker_content(&listing(
        "图片新闻",
        &["藏桂胡杨林", "皮亚曼石榴酒堡", "皮山大清真寺"],
    ));

    match (left, right) {
        (Err(left), Err(right)) => {
            assert!(left.to_string().contains("low-value extracted content"));
            assert!(right.to_string().contains("low-value extracted content"));
        }
        (Ok(left), Ok(right)) => {
            assert!(token_jaccard(&left, &right) < THRESHOLD);
        }
        other => {
            panic!("listing pages should be rejected together or extracted distinctly: {other:?}")
        }
    }
}

#[test]
fn literal_unicode_escaped_article_repairs_to_match_clean_copy() {
    let clean = government_article(
        "中央企业人工智能专题推进会",
        "国务院国资委召开人工智能专题推进会，强调推动中央企业在人工智能领域实现更好发展、发挥更大作用，并围绕算力建设、数据治理、产业协同和场景应用作出部署。",
    );
    let escaped = clean.replace("国务院国资委", r"\u56fd\u52a1\u9662\u56fd\u8d44\u59d4");
    let left = worker_content(&clean).unwrap();
    let right = worker_content(&escaped).unwrap();

    assert!(right.contains("国务院国资委"));
    assert_eq!(without_whitespace(&left), without_whitespace(&right));
}
