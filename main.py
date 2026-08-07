import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

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

# Funda shows around 15 results per page. Since we force newest-first sort
# order below, we only need the first few pages to catch anything new
# between runs -- no need to page through the entire search every time.
MAX_PAGES = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}


def force_newest_first(base_url):
    """Force the search to be sorted by date, newest first.

    This matters because we only fetch the first few pages (see MAX_PAGES) --
    if the results weren't sorted newest-first, that shortcut could miss
    listings sitting further back in the results.
    """
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["sort"] = ['"date_down"']
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def build_page_url(base_url, page):
    """Return base_url with the search_result (page number) query param set."""
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["search_result"] = [str(page)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def fetch_page_html(url):
    """Fetch one page's HTML. Returns None on failure."""
    try:
        from curl_cffi import requests as curl_requests
        response = curl_requests.get(
            url, headers=HEADERS, impersonate="chrome124", timeout=30
        )
    except ImportError:
        response = requests.get(url, headers=HEADERS, timeout=30)

    if response.status_code != 200:
        print(f"Warning: got status code {response.status_code} for {url}")
        return None

    return response.text


def parse_listings(html):
    """Parse listing cards out of one search-results page's HTML.

    Each listing on the page typically has two links pointing at the same
    detail URL: one wrapping the photo (which can include badge text like
    "Sold" or promo captions mixed into it), and one wrapping the address
    as a heading. We deliberately only read the heading link, since that's
    the one that reliably contains just the street address.
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = {}

    for heading in soup.find_all(["h1", "h2", "h3"]):
        a = heading.find("a", href=True)
        if not a:
            continue

        href = a["href"]
        match = DETAIL_URL_RE.search(href)
        if not match:
            continue

        listing_id = match.group(2)
        address = a.get_text(strip=True)
        if not address or listing_id in listings:
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

    return listings


def fetch_listings():
    """Fetch the first few pages (newest-first) of the Funda search."""
    all_listings = {}
    sorted_url = force_newest_first(FUNDA_URL)

    for page in range(1, MAX_PAGES + 1):
        page_url = build_page_url(sorted_url, page)
        html = fetch_page_html(page_url)
        if html is None:
            break

        page_listings = parse_listings(html)
        new_ids = [lid for lid in page_listings if lid not in all_listings]

        print(f"Page {page}: found {len(page_listings)} listings ({len(new_ids)} new)")

        if not page_listings or not new_ids:
            # Empty page, or a page that repeats what we already have --
            # either way we've reached the end of the results.
            break

        all_listings.update(page_listings)

        if page < MAX_PAGES:
            time.sleep(1)  # be a little polite between requests

    return list(all_listings.values())


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
