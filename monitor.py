import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = Path(__file__).parent
PORTALS_FILE = BASE_DIR / "portals.json"
SEEN_FILE = BASE_DIR / "seen_notices.json"

WP_URL = os.getenv("WP_URL", "https://vacancybox.in").rstrip("/")
WP_USERNAME = os.getenv("WP_USERNAME", "").strip()
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MAX_DRAFTS_PER_RUN = int(os.getenv("MAX_DRAFTS_PER_RUN", "10"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

RELEVANT_KEYWORDS = [
    "recruitment", "vacancy", "vacancies", "notification", "advertisement",
    "advt", "online form", "apply", "admit card", "hall ticket", "call letter",
    "exam city", "result", "merit list", "score card", "cut off", "cutoff",
    "answer key", "response sheet", "syllabus", "exam pattern", "admission",
    "counselling", "bharti", "pariksha", "notice", "examination", "cgl",
    "chsl", "mts", "ntpc", "group d", "constable", "sub inspector", "po", "clerk",
]

IGNORE_KEYWORDS = [
    "privacy policy", "terms and conditions", "contact us", "about us",
    "sitemap", "feedback", "screen reader", "skip to main", "hindi version",
    "english version", "login", "forgot password", "rti", "tender", "tenders",
    "hyperlinking policy", "copyright", "disclaimer", "gallery", "archive",
]

# Vacancy Box WordPress Category IDs
CATEGORY_RULES = [
    (3, ["admit card", "hall ticket", "call letter", "exam city", "e-admit"]),
    (4, ["result", "merit list", "score card", "scorecard", "cut off", "cutoff", "selected candidates"]),
    (5, ["answer key", "response sheet", "objection", "provisional key"]),
    (6, ["syllabus", "exam pattern", "scheme of exam"]),
    (7, ["admission", "counselling", "cuet", "neet", "jee", "entrance"]),
    (2, ["recruitment", "vacancy", "vacancies", "notification", "advt", "online form", "bharti", "apply"]),
]


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {"initialized": False, "seen": []}


def save_seen(data: dict) -> None:
    # Keep latest 5000 hashes so file stays small
    data["seen"] = data["seen"][-5000:]
    SEEN_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def make_hash(portal_id: str, title: str, link: str) -> str:
    raw = f"{portal_id}|{title.lower().strip()}|{link.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def is_relevant(title: str, href: str) -> bool:
    t = title.lower()
    if len(t) < 12 or len(t) > 220:
        return False
    if any(bad in t for bad in IGNORE_KEYWORDS):
        return False
    if href.lower().endswith(".pdf"):
        return True
    return any(kw in t for kw in RELEVANT_KEYWORDS)


def detect_category(title: str, default_cat: int) -> int:
    t = title.lower()
    for cat_id, keywords in CATEGORY_RULES:
        if any(kw in t for kw in keywords):
            return cat_id
    return default_cat

def extract_advt_no(title: str, url: str) -> str:
    text = f"{title} {url}"
    patterns = [
        r"(CRPD/[A-Z0-9/\-]+)",
        r"(CEN\s*No\.?\s*[0-9]+/[0-9]{4})",
        r"(CEN\s*[0-9]+/[0-9]{4})",
        r"(Advt\.?\s*No\.?\s*[A-Za-z0-9/\-]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""
    
def fetch_portal_notices(portal: dict) -> list:
    notices = []
    try:
        resp = requests.get(portal["url"], headers=HEADERS, timeout=12, verify=False)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        seen_local = set()

        for a in soup.find_all("a", href=True):
            title = clean_text(a.get_text(" ", strip=True))
            href = a["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            full_url = urljoin(portal["url"], href)
            if not is_relevant(title, full_url):
                continue
            key = (title.lower(), full_url)
            if key in seen_local:
                continue
            seen_local.add(key)
            notices.append({
                "portal_id": portal["id"],
                "portal_name": portal["name"],
                "official_site": portal["official_site"],
                "title": title,
                "link": full_url,
                "category_id": detect_category(title, portal.get("default_category_id", 2)),
            })
            if len(notices) >= 25:
                break
        print(f"[OK] {portal['id']}: {len(notices)} relevant links found")
    except Exception as exc:
        print(f"[WARN] {portal['id']} failed: {exc}")
    return notices


def create_wp_draft(notice: dict) -> str | None:
    if not WP_USERNAME or not WP_APP_PASSWORD:
        print("[SKIP] WP_USERNAME or WP_APP_PASSWORD not set.")
        return None

    endpoint = f"{WP_URL}/wp-json/wp/v2/posts"
    cat_id = notice["category_id"]
    is_pdf = notice["link"].lower().endswith(".pdf")

    meta = {
        "vb_short_info": (
            f"{notice['portal_name']} has published a new official update: "
            f"{notice['title']}. Verify official dates, eligibility, and notification PDF below before publishing."
        ),
        "vb_org_name": notice["portal_name"],
        "vb_official_url": notice["official_site"],
            "vb_advt_no": extract_advt_no(notice["title"], notice["link"]),
    }
    if cat_id == 3:  
        meta["vb_admit_url"] = notice["link"]
    elif cat_id == 4:
        meta["vb_result_url"] = notice["link"]
    elif cat_id == 5:
        meta["vb_answer_key_url"] = notice["link"]
    elif is_pdf:
        meta["vb_pdf_url"] = notice["link"]
    else:
        meta["vb_apply_url"] = notice["link"]
        meta["vb_pdf_url"] = notice["link"]

    payload = {
        "title": f"{notice['title']}",
        "status": "draft",
        "categories": [cat_id],
        "meta": meta,
    }

    resp = requests.post(
        endpoint,
        auth=(WP_USERNAME, WP_APP_PASSWORD),
        json=payload,
        timeout=25,
    )
    if resp.status_code in (200, 201):
        post_id = resp.json().get("id")
        edit_url = f"{WP_URL}/wp-admin/post.php?post={post_id}&action=edit"
        print(f"[DRAFT CREATED] Post #{post_id} -> {notice['title']}")
        return edit_url
    else:
        print(f"[ERR] WP Draft failed ({resp.status_code}): {resp.text[:200]}")
        return None


def send_telegram_alert(notice: dict, edit_url: str | None) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    msg = (
        f"New Official Update Detected!\n\n"
        f"Portal: {notice['portal_name']}\n"
        f"Title: {notice['title']}\n"
        f"Official Link: {notice['link']}\n"
    )
    if edit_url:
        msg += f"Edit & Publish Draft: {edit_url}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception as exc:
        print(f"[WARN] Telegram alert failed: {exc}")


def main() -> None:
    portals = json.loads(PORTALS_FILE.read_text(encoding="utf-8"))
    state = load_seen()
    seen_set = set(state.get("seen", []))
    initialized = bool(state.get("initialized", False))

    all_notices = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(fetch_portal_notices, p) for p in portals]
        for fut in as_completed(futures):
            all_notices.extend(fut.result())

    new_notices = []
    for n in all_notices:
        h = make_hash(n["portal_id"], n["title"], n["link"])
        if h not in seen_set:
            seen_set.add(h)
            state["seen"].append(h)
            new_notices.append(n)

    if not initialized:
        # First baseline run: record existing notices without flooding WordPress drafts
        print(f"[INIT] Baseline saved with {len(new_notices)} existing notices. Future runs will create drafts for NEW updates only.")
        state["initialized"] = True
        save_seen(state)
        return

    print(f"[INFO] Found {len(new_notices)} brand-new notices.")
    created = 0
    for notice in new_notices[:MAX_DRAFTS_PER_RUN]:
        edit_url = create_wp_draft(notice)
        send_telegram_alert(notice, edit_url)
        created += 1

    save_seen(state)
    print(f"[DONE] Processed {created} new notices.")


if __name__ == "__main__":
    main()
