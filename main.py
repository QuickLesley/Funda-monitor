import os
import re
import json
import requests
from bs4 import BeautifulSoup

# Matches Funda listing detail URLs, e.g.
# /detail/koop/geldrop/huis-herdersveld-25/44556468/
# This pattern is far more stable over time than CSS classes or
# data-test-id attributes, which Funda changes periodically.
DETAIL_URL_RE = re.compile(r"/detail/(koop|huur)/[^\"'>]+?/(\d+)/?")
PRICE_RE = re.compile(r"€\s?[\d.,]+")

FUNDA_URL = os.environ["FUNDA_URL"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SEEN_FILE = "seen_listings.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}


def fetch_listings():
    """Fetch the Funda search results page and parse listing cards.

    Tries curl_cffi first (impersonates a real Chrome browser at the network
    level, which helps get past basic/medium anti-bot checks). Falls back to
    plain requests if curl_cffi isn't available for some reason.
    """
    try:
        from curl_cffi import requests as curl_requests
        response = curl_requests.get(
            FUNDA_URL, headers=HEADERS, impersonate="chrome124", timeout=30
        )
    except ImportError:
        response = requests.get(FUNDA_URL, headers=HEADERS, timeout=30)

    if response.status_code != 200:
        print(f"Warning: got status code {response.status_code} from Funda")
        return []

    print(f"Received {len(response.text)} characters of HTML from Funda")

    soup = BeautifulSoup(response.text, "html.parser")
    listings = {}

    # Walk every link on the page and keep the ones that point at a listing
    # detail page. Each listing on the search results page usually appears
    # twice (once wrapping the photo, once wrapping the address heading) --
    # we keep the one that actually has visible text, since that's the
    # address link, and skip the empty image link.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = DETAIL_URL_RE.search(href)
        if not match:
            continue

        listing_id = match.group(2)
        address = a.get_text(strip=True)
        if not address:
            continue
        if listing_id in listings:
            continue

        full_url = href if href.startswith("http") else f"https://www.funda.nl{href}"

        # Look for a nearby price (marked with a euro sign) by walking up
        # a few parent elements from the address link.
        price = "Price unknown"
        container = a
        for _ in range(4):
            container = container.parent
            if container is None:
                break
            price_match = PRICE_RE.search(container.get_text(" ", strip=True))
            if price_match:
                price = price_match.group(0)
                break

        listings[listing_id] = {
            "id": listing_id,
            "address": address,
            "price": price,
            "url": full_url,
        }

    return list(listings.values())


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r") as f:
        return set(json.load(f))


def save_seen(seen_ids):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen_ids), f, indent=2)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, data=payload, timeout=15)
    if resp.status_code != 200:
        print(f"Failed to send Telegram message: {resp.status_code} {resp.text}")


def main():
    listings = fetch_listings()
    print(f"Fetched {len(listings)} listings from Funda")

    if not listings:
        print(
            "No listings found. Either there really are none, or Funda blocked/"
            "changed the request. Check the Actions log for the status code above."
        )
        return

    seen_ids = load_seen()
    is_first_run = len(seen_ids) == 0
    new_listings = [l for l in listings if l["id"] not in seen_ids]

    if is_first_run:
        # Don't spam you with every listing currently on the search on the very
        # first run -- just record what's there now as the baseline.
        print(f"First run detected. Recording {len(listings)} listings as seen, no notifications sent.")
    else:
        for listing in new_listings:
            message = (
                f"\U0001F3E0 <b>New listing!</b>\n"
                f"{listing['address']}\n"
                f"{listing['price']}\n"
                f"{listing['url']}"
            )
            send_telegram_message(message)
            print(f"Notified about: {listing['address']}")

        if not new_listings:
            print("No new listings this run.")

    all_ids = seen_ids.union(l["id"] for l in listings)
    save_seen(all_ids)


if __name__ == "__main__":
    main()
