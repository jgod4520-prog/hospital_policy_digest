#!/usr/bin/env python3
"""
Federal Hospital Policy Digest
Fetches, filters, and emails a weekly summary of federal hospital policy news.
"""

import argparse
import html as html_lib
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import feedparser
import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from ddgs import DDGS

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
EMAIL_ADDRESS = "jgod4520@gmail.com"
MODEL = "claude-sonnet-4-6"

RSS_FEEDS = {
    "Politico Healthcare": "https://rss.politico.com/healthcare.xml",
    "STAT News": "https://www.statnews.com/feed/",
    "KFF Health News": "https://kffhealthnews.org/feed/",
    "Roll Call": "https://rollcall.com/category/health-care/feed/",
    "Becker's Hospital Review": "https://www.beckershospitalreview.com/feed/",
}

CUTOFF = datetime.now(timezone.utc) - timedelta(days=7)

# Keywords for cheap local pre-filtering before the Claude API call.
# An article must match at least one keyword (case-insensitive, checked against
# title + content) to be sent to Claude. Keeps costs down on high-volume feeds.
PREFILTER_KEYWORDS = [
    "hospital",
    "medicare",
    "medicaid",
    "cms",
    "hhs",
    "congress",
    "federal",
    "legislation",
    "rulemaking",
    "prior authorization",
    "ipps",
    "opps",
    "dsh",
    "cah",
    "site-neutral",
    "site neutral",
    "price transparency",
    "rural health",
    "rural hospital",
    "conditions of participation",
    "inpatient",
    "outpatient",
]

SEARCH_QUERIES = [
    "CMS hospital payment rule rulemaking",
    "Medicare hospital payment policy HHS",
    "federal hospital legislation Congress",
    "hospital price transparency CMS",
    "rural hospital federal policy",
    "site:beckershospitalreview.com hospital CMS Medicare federal",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def day_str(dt: datetime) -> str:
    """Cross-platform date format: 'Feb 7' (no leading zero)."""
    return f"{dt.strftime('%b')} {dt.day}"


# ---------------------------------------------------------------------------
# Step 1: Collect articles
# ---------------------------------------------------------------------------

def _parse_entry(entry: dict, source: str) -> dict | None:
    """Parse a feedparser entry into our article dict. Returns None if out of range."""
    pub = entry.get("published_parsed") or entry.get("updated_parsed")
    if not pub:
        return None
    pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
    if pub_dt < CUTOFF:
        return None

    content = ""
    if entry.get("content"):
        content = BeautifulSoup(
            entry.content[0].value, "html.parser"
        ).get_text(" ", strip=True)
    elif entry.get("summary"):
        content = BeautifulSoup(
            entry.summary, "html.parser"
        ).get_text(" ", strip=True)

    return {
        "source": source,
        "title": entry.get("title", "").strip(),
        "url": entry.get("link", ""),
        "date": pub_dt.strftime("%Y-%m-%d"),
        "content": content[:800],
    }


def fetch_rss_articles() -> list[dict]:
    articles = []
    for source, base_url in RSS_FEEDS.items():
        print(f"  Fetching {source}...")
        source_articles = []
        seen_urls: set[str] = set()

        for page in range(1, 21):  # cap at 20 pages per feed
            url = base_url if page == 1 else f"{base_url}?paged={page}"
            try:
                feed = feedparser.parse(url)
            except Exception as exc:
                print(f"    Error on page {page}: {exc}")
                break

            entries = feed.entries
            if not entries:
                break  # no more pages

            # Detect non-paginating feeds: if page 2 returns the same URLs as page 1, stop
            if page == 2 and all(e.get("link") in seen_urls for e in entries):
                break

            all_older_than_cutoff = True
            for entry in entries:
                url_key = entry.get("link", "")
                if url_key in seen_urls:
                    continue
                seen_urls.add(url_key)

                parsed = _parse_entry(entry, source)
                if parsed:
                    source_articles.append(parsed)
                    all_older_than_cutoff = False
                else:
                    # Check if this entry has a date at all; if it's just missing dates, keep going
                    pub = entry.get("published_parsed") or entry.get("updated_parsed")
                    if pub:
                        pass  # entry exists but is older than cutoff — that's fine
                    else:
                        all_older_than_cutoff = False  # no date, don't stop

            if all_older_than_cutoff and page > 1:
                break  # entire page was older than our window; no point going further

        print(f"    {len(source_articles)} articles in range")
        articles.extend(source_articles)
    return articles


def scrape_axios_vitals() -> list[dict]:
    """
    Scrape the Axios Vitals newsletter archive for editions from the past 7 days.
    Each newsletter edition is returned as a single article whose content is the
    full newsletter text (capped at 1 500 chars for the Claude prompt).
    """
    articles = []
    print("  Scraping Axios Vitals archive...")

    try:
        resp = requests.get(AXIOS_VITALS_ARCHIVE_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"    Could not fetch Axios Vitals archive page: {exc}")
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    # Collect unique edition links — sub-paths of the newsletter archive URL
    seen: set[str] = set()
    edition_urls: list[str] = []
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if (
            "/newsletters/axios-vitals/" in href
            and href.rstrip("/") != "/newsletters/axios-vitals"
        ):
            full = href if href.startswith("http") else f"https://www.axios.com{href}"
            if full not in seen:
                seen.add(full)
                edition_urls.append(full)

    if not edition_urls:
        print(
            "    No edition links found on the Axios Vitals archive page. "
            "The page layout may have changed or requires JavaScript."
        )
        return articles

    print(f"    Found {len(edition_urls)} edition link(s). Checking dates...")

    for edition_url in edition_urls[:15]:  # cap to stay polite
        time.sleep(1)
        try:
            # Fast path: try to read the date from the URL itself
            m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", edition_url)
            url_date: datetime | None = None
            if m:
                url_date = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    tzinfo=timezone.utc,
                )
                if url_date < CUTOFF:
                    continue  # older than 7 days — skip fetching entirely

            eresp = requests.get(edition_url, headers=HEADERS, timeout=30)
            eresp.raise_for_status()
            esoup = BeautifulSoup(eresp.text, "html.parser")

            # Resolve pub date from page metadata when it wasn't in the URL
            pub_date = url_date
            if pub_date is None:
                time_tag = esoup.find("time", attrs={"datetime": True})
                if time_tag:
                    try:
                        pub_date = datetime.fromisoformat(
                            time_tag["datetime"].replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass
                if pub_date and pub_date < CUTOFF:
                    continue

            # Strip boilerplate tags
            for tag in esoup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()

            main = esoup.find("main") or esoup.find("article") or esoup.body
            text = main.get_text(" ", strip=True) if main else ""
            text = re.sub(r"\s{3,}", "  ", text)

            h1 = esoup.find("h1")
            title = h1.get_text(strip=True) if h1 else "Axios Vitals"
            if "axios vitals" not in title.lower():
                title = f"Axios Vitals — {title}"

            date_str = pub_date.strftime("%Y-%m-%d") if pub_date else "Unknown"
            articles.append({
                "source": "Axios Vitals",
                "title": title,
                "url": edition_url,
                "date": date_str,
                "content": text[:1500],
            })
            print(f"    Added edition: {date_str} — {title[:60]}")

        except Exception as exc:
            print(f"    Error fetching {edition_url}: {exc}")

    return articles


def search_web_articles(existing_urls: set[str]) -> list[dict]:
    """Search DuckDuckGo News for each query and return new article dicts."""
    articles = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print("  Searching DuckDuckGo News...")
    try:
        ddgs = DDGS()
        for query in SEARCH_QUERIES:
            try:
                results = ddgs.news(query, timelimit="w", max_results=8)
                for r in results:
                    url = r.get("url", "")
                    if not url or url in existing_urls:
                        continue
                    existing_urls.add(url)
                    articles.append({
                        "source": "Web Search",
                        "title": r.get("title", "").strip(),
                        "url": url,
                        "date": r.get("date", today)[:10],
                        "content": r.get("body", "")[:800],
                    })
            except Exception as exc:
                print(f"    Query '{query}' failed: {exc}")
    except Exception as exc:
        print(f"    DuckDuckGo search unavailable: {exc}")
    print(f"    {len(articles)} new articles from web search")
    return articles


# ---------------------------------------------------------------------------
# Step 1b: Keyword pre-filter (local, no API cost)
# ---------------------------------------------------------------------------

def keyword_prefilter(articles: list[dict]) -> list[dict]:
    """
    Drop articles that contain none of the PREFILTER_KEYWORDS in their
    title or content. This is a cheap first pass before the Claude API call.
    """
    kept = []
    for art in articles:
        haystack = (art["title"] + " " + art["content"]).lower()
        if any(kw in haystack for kw in PREFILTER_KEYWORDS):
            kept.append(art)
    return kept


# ---------------------------------------------------------------------------
# Step 2: Filter and summarize with Claude
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are an expert federal health policy analyst specializing in hospital policy. "
    "Your job is to filter news articles and identify those that are primarily about "
    "federal hospital policy, then summarize the relevant ones."
)

_FILTER_PROMPT = """\
Review the {n} news articles below and identify those that qualify as federal hospital \
policy news.

KEEP an article only when BOTH conditions are true:
  1. The FEDERAL GOVERNMENT is the primary actor (Congress, HHS, CMS, the White House, \
federal courts).
  2. HOSPITALS are the primary subject — not just mentioned in passing.

Topics that qualify:
  • Medicare / Medicaid hospital payment policy (IPPS, OPPS, CAH, DSH, add-on payments)
  • The Rural Health Transformation Program
  • Site-neutral payment policies
  • Hospital price transparency rules and CMS enforcement
  • CMS rulemaking directly targeting hospitals (conditions of participation, etc.)
  • Congressional legislation whose primary effect falls on hospitals
  • HHS / CMS administrative actions on hospital operations
  • Federal prior authorization reform

EXCLUDE articles whose primary subject is:
  • Immigration or who hospitals must serve
  • Drug or device pricing (unless the mechanism directly changes hospital payment rates)
  • Physician, nursing, or general workforce policy — unless it is specifically a federal \
hospital staffing mandate
  • Medicaid expansion debates
  • Public-health campaigns where hospitals are incidental backdrop
  • Politics / elections where hospitals are incidental backdrop

Worked examples:
  ✓ KEEP  — Roll Call story on Labor-HHS earmarks that directly fund hospital programs
  ✗ EXCLUDE — KFF story on nurses relocating to Canada due to Trump policies \
(primary subject: nurses & politics, not federal hospital policy)

For every article that passes the filter, write a 2–3 sentence factual summary focused \
on policy implications.

Group the passing articles under one of these themes (use only themes that have articles):
  • Payment Policy
  • Rural Health
  • CMS Rulemaking
  • Legislation
  • Price Transparency
  • Other Federal Hospital Policy

Articles to evaluate:
{articles_block}

Respond with ONLY valid JSON — no markdown fences, no extra text — using this structure:
{{
  "groups": [
    {{
      "theme": "<theme name>",
      "articles": [
        {{
          "index": <1-based article number>,
          "title": "<title>",
          "url": "<url>",
          "date": "<YYYY-MM-DD>",
          "source": "<source>",
          "summary": "<2-3 sentence summary>"
        }}
      ]
    }}
  ],
  "total_relevant": <integer>
}}

If no articles are relevant, return: {{"groups": [], "total_relevant": 0}}"""


def filter_and_summarize(articles: list[dict]) -> dict:
    if not articles:
        return {"groups": [], "total_relevant": 0}

    lines = []
    for i, art in enumerate(articles, 1):
        lines.append(
            f"Article {i}:\n"
            f"  Title:   {art['title']}\n"
            f"  Source:  {art['source']}\n"
            f"  Date:    {art['date']}\n"
            f"  URL:     {art['url']}\n"
            f"  Content: {art['content']}\n"
        )
    articles_block = "\n---\n".join(lines)

    prompt = _FILTER_PROMPT.format(n=len(articles), articles_block=articles_block)

    print(f"  Sending {len(articles)} articles to Claude ({MODEL})...")
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    # Strip accidental markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        print("  Warning: could not parse Claude response as JSON.")
        print("  Raw response (first 600 chars):", raw[:600])
        return {"groups": [], "total_relevant": 0}


# ---------------------------------------------------------------------------
# Step 3: Format HTML email
# ---------------------------------------------------------------------------

def _e(text: str) -> str:
    """HTML-escape a string."""
    return html_lib.escape(str(text))


def format_html_email(digest: dict, week_label: str) -> str:
    groups = [g for g in digest.get("groups", []) if g.get("articles")]
    total = digest.get("total_relevant", 0)

    if total == 0:
        return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Georgia,serif;max-width:680px;margin:0 auto;padding:24px;color:#222;">
  <h1 style="font-size:24px;border-bottom:3px solid #b00;padding-bottom:8px;margin-bottom:4px;">
    Federal Hospital Policy Digest
  </h1>
  <p style="color:#888;font-size:13px;margin-top:0;">{_e(week_label)}</p>
  <p>No relevant federal hospital policy articles were identified this week.</p>
</body>
</html>"""

    groups_html = ""
    for group in groups:
        articles_html = ""
        for art in group["articles"]:
            articles_html += f"""
      <div style="margin-bottom:22px;padding-bottom:22px;border-bottom:1px solid #e8e8e8;">
        <p style="margin:0 0 3px 0;font-size:12px;color:#999;">{_e(art["source"])} &nbsp;&middot;&nbsp; {_e(art["date"])}</p>
        <h3 style="margin:0 0 6px 0;font-size:16px;font-weight:bold;line-height:1.3;">
          <a href="{art["url"]}" style="color:#1a0dab;text-decoration:none;">{_e(art["title"])}</a>
        </h3>
        <p style="margin:0;font-size:14px;line-height:1.65;color:#333;">{_e(art["summary"])}</p>
      </div>"""

        groups_html += f"""
    <div style="margin-bottom:36px;">
      <h2 style="margin:0 0 16px 0;font-size:17px;color:#b00;
                 border-left:4px solid #b00;padding:4px 0 4px 12px;background:#fafafa;">
        {_e(group["theme"])}
      </h2>
      {articles_html}
    </div>"""

    count_label = f"{total} article{'s' if total != 1 else ''}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
</head>
<body style="font-family:Georgia,serif;max-width:680px;margin:0 auto;
             padding:24px;color:#222;background:#fff;">
  <h1 style="font-size:26px;border-bottom:3px solid #b00;
             padding-bottom:10px;margin-bottom:4px;">
    Federal Hospital Policy Digest
  </h1>
  <p style="margin:0 0 32px 0;color:#888;font-size:13px;">
    {_e(week_label)} &nbsp;&middot;&nbsp; {count_label}
  </p>
  {groups_html}
  <hr style="border:none;border-top:1px solid #ddd;margin:32px 0 16px;">
  <p style="font-size:11px;color:#aaa;line-height:1.6;">
    Sources: Politico Healthcare, STAT News, KFF Health News, Roll Call, Becker's Hospital Review, Web Search (DuckDuckGo)<br>
    Filtered and summarized using Claude ({MODEL}, Anthropic)
  </p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Step 4: Send email via Gmail SMTP
# ---------------------------------------------------------------------------

def send_email(html: str, week_label: str) -> None:
    subject = f"Federal Hospital Policy Digest \u2014 {week_label}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = EMAIL_ADDRESS
    msg.attach(MIMEText(html, "html", "utf-8"))

    print("  Connecting to Gmail SMTP (port 465)...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(EMAIL_ADDRESS, EMAIL_ADDRESS, msg.as_string())
    print("  Email sent successfully.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not ANTHROPIC_API_KEY:
        raise SystemExit("Error: ANTHROPIC_API_KEY is not set in environment or .env file.")
    if not GMAIL_APP_PASSWORD:
        raise SystemExit("Error: GMAIL_APP_PASSWORD is not set in environment or .env file.")

    now = datetime.now(timezone.utc)
    week_label = f"{day_str(now - timedelta(days=7))}\u2013{day_str(now)}, {now.year}"

    print(f"\n=== Federal Hospital Policy Digest: {week_label} ===\n")

    # 1. Collect
    print("[1/4] Collecting articles...")
    all_articles = fetch_rss_articles()
    existing_urls = {a["url"] for a in all_articles}
    web_articles = search_web_articles(existing_urls)
    all_articles.extend(web_articles)
    print(f"  + {len(web_articles)} articles from web search")
    print(f"  Total: {len(all_articles)} articles combined\n")

    if not all_articles:
        print("No articles found. Exiting without sending email.")
        return

    # 1b. Keyword pre-filter
    prefiltered = keyword_prefilter(all_articles)
    print(f"  After keyword pre-filter: {len(prefiltered)} articles\n")

    if not prefiltered:
        print("No articles passed the keyword filter. Exiting without sending email.")
        return

    # 2. Filter & summarize
    print("[2/4] Filtering and summarizing with Claude...")
    digest = filter_and_summarize(prefiltered)
    print(f"  Relevant articles found: {digest.get('total_relevant', 0)}\n")

    # 3. Format
    print("[3/4] Formatting HTML email...")
    html = format_html_email(digest, week_label)

    # 4. Send
    print("[4/4] Sending email...")
    send_email(html, week_label)

    print("\nDone.")


def list_articles() -> None:
    """Fetch all articles, apply keyword pre-filter, and print both groups."""
    now = datetime.now(timezone.utc)
    week_label = f"{day_str(now - timedelta(days=7))}\u2013{day_str(now)}, {now.year}"
    print(f"\n=== Article list: {week_label} ===\n")

    all_articles = fetch_rss_articles()
    existing_urls = {a["url"] for a in all_articles}
    all_articles.extend(search_web_articles(existing_urls))
    passed = keyword_prefilter(all_articles)
    passed_urls = {a["url"] for a in passed}
    rejected = [a for a in all_articles if a["url"] not in passed_urls]

    def print_article(i: int, art: dict) -> None:
        print(f"[{i:02d}] {art['source']} — {art['date']}")
        print(f"     {art['title']}")
        print(f"     {art['url']}")
        snippet = art["content"][:200].replace("\n", " ")
        print(f"     {snippet}...")
        print()

    print(f"PASSED keyword filter: {len(passed)} of {len(all_articles)} articles")
    print("=" * 80)
    for i, art in enumerate(passed, 1):
        print_article(i, art)

    print(f"\nREJECTED by keyword filter: {len(rejected)} articles")
    print("=" * 80)
    for i, art in enumerate(rejected, 1):
        print_article(i, art)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Federal Hospital Policy Digest")
    parser.add_argument(
        "--list",
        action="store_true",
        help="Fetch and print all articles without filtering or sending email",
    )
    args = parser.parse_args()

    if args.list:
        list_articles()
    else:
        main()
