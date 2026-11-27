#!/usr/bin/env python3
"""
Vacancy Box Auto-Monitor (monitor.py)
- Scans all portals in portals.json + official URLs of active WordPress posts
- Filters junk links and supports 1-hop Careers/Notices sub-page discovery
- Matches new notices (Corrigendum, Exam Date, Admit Card, Answer Key, Result)
  against existing live WordPress posts using Advt No., Post Codes & Official Domain
- Sends private Telegram Admin Alerts with direct WordPress Edit links
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration (via Environment Variables / GitHub Secrets)
# ---------------------------------------------------------------------------
WP_URL = os.getenv("WP_URL", "https://vacancybox.in").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# Use your personal Telegram numeric chat ID for private admin alerts
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()

PORTALS_FILE = Path(os.getenv("PORTALS_FILE", "portals.json"))
STATE_FILE = Path(os.getenv("STATE_FILE", "seen_notices.json"))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "30"))
MAX_NEW_ALERTS_PER_RUN = int(os.getenv("MAX_NEW_ALERTS_PER_RUN", "25"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Keyword Filters
# ---------------------------------------------------------------------------
JUNK_KEYWORDS = {
    "login", "sign in", "register", "forgot password", "privacy policy",
    "terms of use", "disclaimer", "sitemap", "contact us", "about us",
    "tender", "e-tender", "e-procurement", "gallery", "photo gallery",
    "video gallery", "rti", "right to information", "feedback", "archive",
    "screen reader", "skip to main", "skip to content", "accessibility",
    "facebook", "twitter", "instagram", "youtube", "linkedin", "whatsapp",
    "home", "back to top", "copyright", "hyperlinking", "help", "faq",
    "citizen charter", "vigilance", "grievance", "annual report",
}

JOB_KEYWORDS = {
    "recruitment", "vacancy", "vacancies", "advertisement", "advt",
    "notification", "apply online", "online application", "post", "posts",
    "admit card", "call letter", "hall ticket", "e-admit", "exam city",
    "result", "merit list", "shortlist", "shortlisted", "selected",
    "score card", "scorecard", "cut off", "cutoff", "answer key",
    "objection", "response sheet", "syllabus", "exam date", "examination",
    "schedule", "corrigendum", "addendum", "extended", "last date",
    "interview", "document verification", "dv", "skill test", "typing test",
    "walk-in", "apprentice", "admission", "counselling", "counseling",
}

SUBPAGE_HINTS = (
    "career", "careers", "recruit", "recruitment", "vacancy", "vacancies",
    "notice", "notices", "notification", "latest", "result", "admit",
    "advertisement", "circular", "jobs", "opportunities",
)

UPDATE_TYPE_RULES: list[tuple[str, set[str]]] = [
    ("🔴 CORRIGENDUM / DATE CHANGE", {
        "corrigendum", "addendum", "extended", "extension", "last date",
        "revised", "cancellation", "cancelled", "postponed", "rescheduled",
    }),
    ("🎫 ADMIT CARD / CALL LETTER", {
        "admit card", "e-admit", "call letter", "hall ticket", "exam city",
        "intimation slip",
    }),
    ("📅 EXAM DATE / SCHEDULE", {
        "exam date", "examination schedule", "exam schedule", "date of exam",
        "cbt schedule", "tier-i", "tier-ii", "interview schedule", "interview date",
    }),
    ("🔑 ANSWER KEY / OBJECTION", {
        "answer key", "response sheet", "objection", "provisional key",
        "final key",
    }),
    ("✅ RESULT / MERIT LIST", {
        "result", "merit list", "shortlisted", "shortlist", "selected candidates",
        "provisionally selected", "score card", "scorecard", "cut off", "cutoff",
        "document verification",
    }),
]

STOPWORDS = {
    "the", "for", "and", "with", "from", "into", "under", "govt", "government",
    "india", "indian", "limited", "board", "commission", "recruitment",
    "online", "apply", "post", "posts", "vacancies", "vacancy", "exam",
    "examination", "notification", "notice", "2025", "2026", "2027",
    "phase", "group", "direct", "latest", "official", "download", "pdf",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def make_notice_id(url: str, title: str) -> str:
    raw = f"{url.strip().lower()}|{normalize_text(title).lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def get_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        print(f"[WARN] Failed to load {path}: {exc}")
        return default


def save_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def load_portals(path: Path) -> list[dict[str, str]]:
    """Supports both list-of-dicts and dict-of-dicts portals.json formats."""
    raw = load_json(path, [])
    portals: list[dict[str, str]] = []

    if isinstance(raw, list):
        for idx, item in enumerate(raw):
            if isinstance(item, dict) and item.get("url"):
                portals.append({
                    "id": str(item.get("id", f"portal_{idx}")),
                    "name": str(item.get("name", item.get("id", f"Portal {idx}"))),
                    "url": str(item["url"]).strip(),
                    "category": str(item.get("category", "General")),
                })
    elif isinstance(raw, dict):
        for key, item in raw.items():
            if isinstance(item, dict) and item.get("url"):
                portals.append({
                    "id": str(key),
                    "name": str(item.get("name", key)),
                    "url": str(item["url"]).strip(),
                    "category": str(item.get("category", "General")),
                })
            elif isinstance(item, str) and item.startswith("http"):
                portals.append({
                    "id": str(key),
                    "name": str(key),
                    "url": item.strip(),
                    "category": "General",
                })
    return portals


# ---------------------------------------------------------------------------
# WordPress Active Posts Fetcher (for Existing Post Update Matching)
# ---------------------------------------------------------------------------
def extract_advt_tokens(text: str) -> set[str]:
    """
    Extracts structured reference tokens like:
    - 06/2026, 809/26, 810/26, 01/2026-27
    - CDBREC2026, CRPD/PO/2025-26/01
    """
    tokens: set[str] = set()
    if not text:
        return tokens

    # Numeric slash codes (e.g., 06/2026, 809/26, 03/2026-27)
    for m in re.findall(r"\b\d{1,4}/\d{2,4}(?:-\d{2,4})?\b", text):
        tokens.add(m.lower())

    # Alphanumeric reference codes containing both letters and digits (len >= 6)
    for m in re.findall(r"\b(?=[A-Za-z0-9/-]{6,25}\b)(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9/-]+\b", text):
        low = m.lower()
        # Skip plain years or pure dates
        if not re.fullmatch(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", low):
            tokens.add(low)

    return tokens


def extract_title_keywords(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z]{3,}", (text or "").lower())
    return {w for w in words if w not in STOPWORDS}


def fetch_wp_live_posts(wp_url: str) -> list[dict[str, Any]]:
    """Fetches published posts + custom meta fields from WordPress REST API."""
    posts: list[dict[str, Any]] = []
    endpoint = f"{wp_url}/wp-json/wp/v2/posts"
    params = {"per_page": 100, "status": "publish"}

    try:
        resp = requests.get(endpoint, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return posts

        for item in data:
            post_id = item.get("id")
            raw_title = item.get("title", {}).get("rendered", "")
            title = html.unescape(normalize_text(BeautifulSoup(raw_title, "html.parser").get_text()))
            link = item.get("link", "")
            meta = item.get("meta", {}) if isinstance(item.get("meta"), dict) else {}

            advt_no = normalize_text(str(meta.get("vb_advt_no", "")))
            org_name = normalize_text(str(meta.get("vb_org", "")))
            official_url = normalize_text(str(meta.get("vb_official_url", "")))
            apply_url = normalize_text(str(meta.get("vb_apply_url", "")))
            pdf_url = normalize_text(str(meta.get("vb_pdf_url", "")))

            domains = {
                d for d in (get_domain(official_url), get_domain(apply_url), get_domain(pdf_url))
                if d
            }
            advt_tokens = extract_advt_tokens(f"{advt_no} {title}")
            title_keywords = extract_title_keywords(f"{title} {org_name}")

            posts.append({
                "id": post_id,
                "title": title,
                "link": link,
                "edit_link": f"{wp_url}/wp-admin/post.php?post={post_id}&action=edit",
                "advt_no": advt_no,
                "org": org_name,
                "official_url": official_url,
                "domains": domains,
                "advt_tokens": advt_tokens,
                "keywords": title_keywords,
            })
        print(f"[INFO] Loaded {len(posts)} live WordPress posts for update matching.")
    except Exception as exc:
        print(f"[WARN] Could not fetch WordPress live posts ({endpoint}): {exc}")

    return posts


# ---------------------------------------------------------------------------
# Portal Scraping (Fixed Indentation + 1-Hop Sub-Page Discovery)
# ---------------------------------------------------------------------------
def is_valid_notice(title: str, href: str) -> bool:
    low_title = title.lower()
    low_href = href.lower()

    if len(title) < 10 or len(title) > 260:
        return False
    if low_href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return False
    if any(junk in low_title for junk in JUNK_KEYWORDS):
        return False

    # Accept PDFs or links containing job/update keywords
    if low_href.endswith(".pdf"):
        return True
    return any(kw in low_title or kw in low_href for kw in JOB_KEYWORDS)


def fetch_soup(session: requests.Session, url: str) -> BeautifulSoup | None:
    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, verify=True)
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("Content-Type", "").lower()
        if "pdf" in content_type or "octet-stream" in content_type:
            return None
        return BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return None


def strip_chrome(soup: BeautifulSoup) -> BeautifulSoup:
    for tag in soup.find_all(["header", "footer", "nav", "script", "style", "noscript"]):
        tag.decompose()
    return soup


def fetch_portal_notices(portal: dict[str, str]) -> list[dict[str, str]]:
    """
    Scans a portal page and up to 2 same-domain 1-hop Careers/Notices sub-pages.
    Cleanly structured with zero indentation issues.
    """
    notices: list[dict[str, str]] = []
    seen_local: set[str] = set()
    base_portal_url = portal["url"]
    portal_domain = get_domain(base_portal_url)

    session = requests.Session()
    main_soup = fetch_soup(session, base_portal_url)
    if main_soup is None:
        return notices

    # Discover up to 2 relevant same-domain sub-pages before stripping nav
    subpage_urls: list[str] = []
    for a_tag in main_soup.find_all("a", href=True):
        raw_href = a_tag["href"].strip()
        abs_url = urljoin(base_portal_url, raw_href)
        if abs_url == base_portal_url or abs_url.lower().endswith(".pdf"):
            continue
        if get_domain(abs_url) != portal_domain:
            continue
        link_text = normalize_text(a_tag.get_text(" ", strip=True)).lower()
        if any(hint in link_text or hint in abs_url.lower() for hint in SUBPAGE_HINTS):
            if abs_url not in subpage_urls:
                subpage_urls.append(abs_url)
            if len(subpage_urls) >= 2:
                break

    pages_to_scan: list[tuple[BeautifulSoup, str]] = [(strip_chrome(main_soup), base_portal_url)]
    for sub_url in subpage_urls:
        sub_soup = fetch_soup(session, sub_url)
        if sub_soup is not None:
            pages_to_scan.append((strip_chrome(sub_soup), sub_url))

    for page_soup, base_url in pages_to_scan:
        for a_tag in page_soup.find_all("a", href=True):
            raw_href = a_tag["href"].strip()
            abs_url = urljoin(base_url, raw_href)
            title = normalize_text(a_tag.get_text(" ", strip=True))

            if not title:
                title = normalize_text(a_tag.get("title", ""))
            if not is_valid_notice(title, abs_url):
                continue

            nid = make_notice_id(abs_url, title)
            if nid in seen_local:
                continue
            seen_local.add(nid)

            notices.append({
                "id": nid,
                "portal_id": portal["id"],
                "portal_name": portal["name"],
                "category": portal["category"],
                "title": title,
                "url": abs_url,
                "domain": get_domain(abs_url),
            })

    return notices


# ---------------------------------------------------------------------------
# Existing Post Update Matcher
# ---------------------------------------------------------------------------
def classify_update_type(title: str, url: str) -> str:
    combined = f"{title} {url}".lower()
    for label, keywords in UPDATE_TYPE_RULES:
        if any(kw in combined for kw in keywords):
            return label
    return "📢 POST UPDATE"


def match_existing_post(
    notice: dict[str, str],
    live_posts: list[dict[str, Any]],
) -> tuple[dict[str, Any], str, str] | None:
    """
    Matches a new notice against existing live WordPress posts.
    Returns (matched_post, update_type_label, match_reason) or None.
    """
    notice_text = f"{notice['title']} {notice['url']}"
    notice_tokens = extract_advt_tokens(notice_text)
    notice_keywords = extract_title_keywords(notice["title"])
    notice_domain = notice["domain"]
    update_type = classify_update_type(notice["title"], notice["url"])

    is_explicit_update = update_type != "📢 POST UPDATE"

    for post in live_posts:
        # 1. High-confidence match: Shared Advertisement Number / Post Code
        shared_tokens = notice_tokens & post["advt_tokens"]
        if shared_tokens:
            # Confirm domain or at least 1 org/title keyword overlaps to prevent false positives
            if notice_domain in post["domains"] or (notice_keywords & post["keywords"]):
                reason = f"Advt/Code match: {', '.join(sorted(shared_tokens))}"
                return post, update_type, reason

        # 2. Domain + Update Keyword + Title Keyword Overlap
        if notice_domain and notice_domain in post["domains"] and is_explicit_update:
            overlap = notice_keywords & post["keywords"]
            if len(overlap) >= 2:
                reason = f"Official domain + keywords ({', '.join(sorted(list(overlap))[:3])})"
                return post, update_type, reason

    return None


# ---------------------------------------------------------------------------
# Telegram Admin Alerts
# ---------------------------------------------------------------------------
def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        print("[INFO] Telegram credentials not set; printing alert to stdout:\n" + text)
        return False

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(api_url, json=payload, timeout=15)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        print(f"[WARN] Telegram API error ({resp.status_code}): {resp.text}")
        return False
    except Exception as exc:
        print(f"[WARN] Telegram send failed: {exc}")
        return False


def format_post_update_alert(
    notice: dict[str, str],
    post: dict[str, Any],
    update_type: str,
    reason: str,
) -> str:
    return (
        f"🔔 <b>EXISTING POST UPDATE DETECTED</b>\n"
        f"<b>Type:</b> {html.escape(update_type)}\n"
        f"<b>Matched Live Post:</b> {html.escape(post['title'])}\n"
        f"<b>Advt No:</b> {html.escape(post['advt_no'] or 'N/A')}\n"
        f"<b>Match Reason:</b> {html.escape(reason)}\n\n"
        f"<b>Official Notice:</b> {html.escape(notice['title'])}\n"
        f"🔗 <a href=\"{html.escape(notice['url'])}\">Open Official Notice</a>\n"
        f"✏️ <a href=\"{html.escape(post['edit_link'])}\">Edit Post in WordPress</a>\n"
        f"🌐 <a href=\"{html.escape(post['link'])}\">View Live Post</a>"
    )


def format_new_notices_digest(new_notices: list[dict[str, str]]) -> str:
    lines = [f"🆕 <b>Vacancy Box Monitor: {len(new_notices)} New Notice(s)</b>\n"]
    for idx, item in enumerate(new_notices[:MAX_NEW_ALERTS_PER_RUN], start=1):
        p_name = html.escape(item["portal_name"])
        title = html.escape(item["title"][:120])
        url = html.escape(item["url"])
        lines.append(f"{idx}. <b>{p_name}</b> — <a href=\"{url}\">{title}</a>")
    if len(new_notices) > MAX_NEW_ALERTS_PER_RUN:
        lines.append(f"\n...and {len(new_notices) - MAX_NEW_ALERTS_PER_RUN} more notices.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main Runner
# ---------------------------------------------------------------------------
def main() -> int:
    init_mode = "--init" in sys.argv
    portals = load_portals(PORTALS_FILE)
    live_posts = fetch_wp_live_posts(WP_URL)

    # Also monitor official_url of every live post if not already in portals.json
    existing_urls = {p["url"].rstrip("/").lower() for p in portals}
    for post in live_posts:
        off_url = post.get("official_url", "")
        if off_url.startswith("http") and off_url.rstrip("/").lower() not in existing_urls:
            portals.append({
                "id": f"wp_post_{post['id']}",
                "name": post["org"] or post["title"][:40],
                "url": off_url,
                "category": "LivePostWatch",
            })
            existing_urls.add(off_url.rstrip("/").lower())

    if not portals:
        print(f"[ERROR] No portals found in {PORTALS_FILE} or WordPress.")
        return 1

    state = load_json(STATE_FILE, {"seen_ids": [], "last_run": ""})
    seen_ids: set[str] = set(state.get("seen_ids", []))
    is_first_baseline = init_mode or (len(seen_ids) == 0)

    print(f"[INFO] Scanning {len(portals)} portals (workers={MAX_WORKERS}, baseline={is_first_baseline})...")
    all_notices: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(fetch_portal_notices, p): p for p in portals}
        for future in as_completed(future_map):
            try:
                all_notices.extend(future.result())
            except Exception as exc:
                portal = future_map[future]
                print(f"[WARN] Portal failed ({portal['id']}): {exc}")

    # Deduplicate across portals
    unique_notices: dict[str, dict[str, str]] = {}
    for n in all_notices:
        unique_notices[n["id"]] = n

    new_notices = [n for nid, n in unique_notices.items() if nid not in seen_ids]
    print(f"[INFO] Total scraped: {len(unique_notices)} | New: {len(new_notices)}")

    if is_first_baseline:
        print("[INFO] Baseline run — recording existing notices without sending spam alerts.")
    else:
        matched_updates: list[tuple[dict[str, str], dict[str, Any], str, str]] = []
        general_new: list[dict[str, str]] = []

        for notice in new_notices:
            match = match_existing_post(notice, live_posts)
            if match is not None:
                post, update_type, reason = match
                matched_updates.append((notice, post, update_type, reason))
            else:
                general_new.append(notice)

        # 1. Send immediate individual alerts for updates to existing live posts
        for notice, post, update_type, reason in matched_updates:
            msg = format_post_update_alert(notice, post, update_type, reason)
            send_telegram_message(msg)
            time.sleep(0.5)

        # 2. Send compact digest for brand-new notices
        if general_new:
            digest = format_new_notices_digest(general_new)
            send_telegram_message(digest)

        print(f"[INFO] Alerts sent -> Existing Post Updates: {len(matched_updates)} | New Notices: {len(general_new)}")

    # Persist updated state
    seen_ids.update(unique_notices.keys())
    save_json(STATE_FILE, {
        "seen_ids": sorted(seen_ids),
        "last_run": datetime.now(timezone.utc).isoformat(),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())