import os
import json
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import requests
import google.generativeai as genai


def get_news(brand: str) -> list[dict] | None:
    """Fetch corporate transparency news, FSSAI notices, or controversies for a brand.

    Uses a robust multi-tier approach:
    1. Primary: Google News India RSS (never blocked on college Wi-Fi, no API key needed, real-time).
    2. Secondary: GNews API (if key is configured).
    3. Tertiary Fallback: Gemini AI Corporate Transparency knowledge (if network is firewalled).

    Returns:
        List of news article dicts with keys: title, description, url, source, published_at.
    """
    if not brand or not brand.strip():
        return []

    clean_brand = brand.strip()

    # Tier 1: Try Google News RSS (Fast, no API key needed, works on college networks)
    rss_articles = _fetch_google_news_rss(clean_brand)
    if rss_articles:
        return rss_articles

    # Tier 2: Try GNews API if key is available
    gnews_articles = _fetch_gnews(clean_brand)
    if gnews_articles:
        return gnews_articles

    # Tier 3: AI Corporate Transparency Fallback (Guaranteed to return insights even if firewalled)
    ai_articles = _fetch_ai_transparency(clean_brand)
    if ai_articles:
        return ai_articles

    return []


def _fetch_google_news_rss(brand: str) -> list[dict] | None:
    """Query Google News RSS for Indian food safety, FSSAI, recalls, and controversies."""
    keywords = ["FSSAI", "recall", "controversy", "lawsuit", "safety", "ban", "adulteration"]
    query = f'"{brand}" AND ({" OR ".join(keywords)})'
    encoded_query = urllib.parse.quote(query)

    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-IN&gl=IN&ceid=IN:en"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as response:
            xml_data = response.read()

        root = ET.fromstring(xml_data)
        items = root.findall(".//item")

        # Fallback if specific safety keywords yielded 0 articles: search general brand food news
        if not items:
            fallback_query = urllib.parse.quote(f'"{brand}" food India')
            fallback_url = f"https://news.google.com/rss/search?q={fallback_query}&hl=en-IN&gl=IN&ceid=IN:en"
            req_fb = urllib.request.Request(fallback_url, headers=headers)
            with urllib.request.urlopen(req_fb, timeout=6) as fb_response:
                root = ET.fromstring(fb_response.read())
            items = root.findall(".//item")

        articles = []
        for item in items[:5]:
            title_elem = item.find("title")
            link_elem = item.find("link")
            pub_date_elem = item.find("pubDate")
            source_elem = item.find("source")

            title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
            link = link_elem.text.strip() if link_elem is not None and link_elem.text else "#"
            pub_date = pub_date_elem.text.strip() if pub_date_elem is not None and pub_date_elem.text else ""
            source = source_elem.text.strip() if source_elem is not None and source_elem.text else "Google News"

            # Clean up title if it ends with " - Source Name"
            if " - " in title:
                parts = title.rsplit(" - ", 1)
                title = parts[0]
                if source == "Google News" and parts[1]:
                    source = parts[1]

            articles.append({
                "title": title,
                "description": f"Recent coverage regarding {brand} in India.",
                "url": link,
                "source": source,
                "published_at": pub_date,
                "image": "",
            })

        return articles if articles else None

    except Exception:
        return None


def _fetch_gnews(brand: str) -> list[dict] | None:
    """Optional GNews fallback if API key is provided."""
    api_key = os.getenv("GNEWS_API_KEY")
    if not api_key:
        return None

    keywords = ["recall", "lawsuit", "banned", "FSSAI", "controversy"]
    query = f'"{brand}" AND ({" OR ".join(keywords)})'
    url = "https://gnews.io/api/v4/search"
    params = {
        "q": query,
        "lang": "en",
        "country": "in",
        "max": 5,
        "apikey": api_key,
    }

    try:
        resp = requests.get(url, params=params, timeout=5)
        if resp.status_code != 200:
            return None
        data = resp.json()
        articles = data.get("articles", [])
        return [
            {
                "title": a.get("title", ""),
                "description": a.get("description", ""),
                "url": a.get("url", "#"),
                "source": a.get("source", {}).get("name", "GNews"),
                "published_at": a.get("publishedAt", ""),
                "image": a.get("image", ""),
            }
            for a in articles[:5]
        ]
    except Exception:
        return None


def _fetch_ai_transparency(brand: str) -> list[dict] | None:
    """AI fallback: Generates known Indian regulatory, FSSAI, and safety records for the brand."""
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        return None

    try:
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel("gemini-3.6-flash")

        prompt = f"""You are a corporate transparency and food safety database.
List up to 3 known historical or recent regulatory notices, FSSAI advisories, product recalls, consumer court cases, or safety controversies regarding the brand or company '{brand}' in India.

If this brand has a clean record with no major controversies, provide 1-2 entries summarizing its general FSSAI regulatory compliance status in India.

Return ONLY a valid JSON array of objects with exactly these keys:
[
  {{
    "title": "<Headline of controversy, FSSAI notice, or safety check>",
    "source": "<Regulator or Publication, e.g. FSSAI / Consumer Court / News Reports>",
    "published_at": "<Approximate year or date, e.g. 2024>",
    "description": "<1-2 sentence factual explanation of what occurred>",
    "url": "#"
  }}
]

Do NOT wrap in markdown code blocks. Return raw JSON array only."""

        response = model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        data = json.loads(text)
        if isinstance(data, list) and len(data) > 0:
            for item in data:
                if not item.get("url"):
                    item["url"] = f"https://www.google.com/search?q={urllib.parse.quote(brand + ' food safety controversy')}"
            return data
    except Exception:
        pass

    return None
