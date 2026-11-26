import html
import json
import os
from pathlib import Path
import requests

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "broadcast_state.json"

_raw_wp = os.getenv("WP_URL", "https://vacancybox.in").strip().strip('"').strip("'").rstrip("/")
if _raw_wp and not _raw_wp.startswith(("http://", "https://")):
    _raw_wp = "https://" + _raw_wp
WP_URL = _raw_wp or "https://vacancybox.in"

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHANNEL = os.getenv("TELEGRAM_CHANNEL_ID", "@vacancybox_in").strip() or "@vacancybox_in"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"initialized": False, "posts": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def get_cat_name(post: dict) -> str:
    try:
        terms = post.get("_embedded", {}).get("wp:term", [])
        if terms and terms[0]:
            names = [t.get("name", "") for t in terms[0] if t.get("name")]
            if names:
                return ", ".join(names)
    except Exception:
        pass
    return "Govt Update"


def format_message(post: dict, is_update: bool) -> str:
    title = html.unescape(post.get("title", {}).get("rendered", "")).strip()
    link = post.get("link", "")
    cat = get_cat_name(post)
    meta = post.get("meta", {}) or {}
    advt = str(meta.get("vb_advt_no", "") or "").strip()
    total = str(meta.get("vb_total_posts", "") or "").strip()
    last = str(meta.get("vb_last_date", "") or "").strip()

    badge = f"🔄 UPDATED: {cat}" if is_update else f"📢 NEW {cat}"
    lines = [badge, "", title]
    if advt:
        lines.append(f"📌 Advt. No.: {advt}")
    if total:
        lines.append(f"👥 Vacancies: {total}")
    if last:
        lines.append(f"⏳ Last Date: {last}")
    lines.append("")
    lines.append(f"🔗 Check Details & Apply: {link}")
    lines.append("")
    lines.append("📲 Telegram: https://t.me/vacancybox_in")
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    if not TG_TOKEN:
        print("[ERR] TELEGRAM_BOT_TOKEN secret missing.")
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHANNEL,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    if r.status_code == 200:
        print(f"[SENT] Posted to {TG_CHANNEL}")
        return True
    print(f"[ERR] Telegram failed ({r.status_code}): {r.text[:250]}")
    return False


def main() -> None:
    url = f"{WP_URL}/wp-json/wp/v2/posts?_embed=wp:term&per_page=10&orderby=modified&order=desc"
    resp = requests.get(url, timeout=25)
    resp.raise_for_status()
    posts = resp.json()
    state = load_state()
    seen = state.get("posts", {})

    if not state.get("initialized", False):
        # First run: send 1 latest post as live test, record all others without spamming
        if posts:
            latest = posts[0]
            ok = send_telegram(format_message(latest, is_update=False))
            print(f"[INIT] Test broadcast for Post #{latest['id']}: ok={ok}")
        for p in posts:
            seen[str(p["id"])] = p.get("modified_gmt", "")
        state["initialized"] = True
        state["posts"] = seen
        save_state(state)
        return

    sent_count = 0
    for p in reversed(posts):
        pid = str(p["id"])
        mod = p.get("modified_gmt", "")
        if pid not in seen:
            if send_telegram(format_message(p, is_update=False)):
                sent_count += 1
            seen[pid] = mod
        elif seen[pid] != mod:
            if send_telegram(format_message(p, is_update=True)):
                sent_count += 1
            seen[pid] = mod

    state["posts"] = seen
    save_state(state)
    print(f"[DONE] Broadcasted {sent_count} new/updated posts.")


if __name__ == "__main__":
    main()
