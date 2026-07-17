#!/usr/bin/env python3
"""Site scraper -> per-page section JSON + downloaded images.

Crawls one domain (same-host internal links, static HTML), and for each page
writes output/pages/<slug>.json describing every section: headings, images
used (name + alt), a plain-language description, and an ai_agent_description
(what the section is about + how it is laid out) for downstream AI agents.

Section descriptions are RULE-BASED (built from the section's own headings,
text and image counts) — not LLM output. Run a second LLM pass over the JSON
if you want richer prose.

Usage: python scraper.py https://example.com/ [--max-pages N] [--delay S]
"""
import sys, os, re, json, time, argparse, hashlib
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag, unquote
import requests
from bs4 import BeautifulSoup

OUT = "output"
PAGES_DIR = os.path.join(OUT, "pages")
IMG_DIR = os.path.join(OUT, "images")
UA = "Mozilla/5.0 (compatible; SiteScraper/1.0; +local)"
HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def slugify(url, home_host):
    p = urlparse(url)
    path = p.path.strip("/")
    slug = path if path else "index"
    if p.query:
        slug += "__" + re.sub(r"[^\w]+", "-", p.query)
    slug = re.sub(r"[^\w\-./]+", "-", slug).strip("-/").replace("/", "__")
    return slug or "index"


def same_host(url, host):
    return urlparse(url).netloc.replace("www.", "") == host.replace("www.", "")


def clean_text(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def sniff_ext(content, content_type=""):
    """True image extension from magic bytes (URL/Content-Type lie — e.g. JPEG
    bytes served at a .png URL, or extensionless Unsplash URLs)."""
    b = content[:16]
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if b[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    if b[:5] == b"<?xml" or b.lstrip()[:4] == b"<svg" or "svg" in content_type:
        return "svg"
    return "jpg"  # last resort; better than an unopenable fake ext


def img_basename(src):
    path = urlparse(src).path
    base = unquote(os.path.basename(path)) or "image"
    return re.sub(r"[^\w\-.]+", "_", os.path.splitext(base)[0]) or "image"


def download_image(session, src, seen):
    if src in seen:
        return seen[src]
    h = hashlib.md5(src.encode()).hexdigest()[:8]
    try:
        r = session.get(src, timeout=20)
        r.raise_for_status()
        content = r.content
    except Exception as e:
        print(f"  ! image fail {src}: {e}")
        seen[src] = None
        return None
    ext = sniff_ext(content, r.headers.get("content-type", ""))
    fname = f"{h}_{img_basename(src)}.{ext}"
    dest = os.path.join(IMG_DIR, fname)
    if not os.path.exists(dest):
        with open(dest, "wb") as f:
            f.write(content)
    seen[src] = fname
    return fname


def collect_images(node, base_url, session, img_seen):
    """Return list of {name, alt, src} for images inside a node; downloads them."""
    out = []
    for img in node.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if not src:
            srcset = img.get("srcset") or img.get("data-srcset")
            if srcset:
                src = srcset.split(",")[0].strip().split(" ")[0]
        if not src or src.startswith("data:"):
            continue
        full = urljoin(base_url, src)
        fname = download_image(session, full, img_seen)
        if fname:
            out.append({"name": fname, "alt": clean_text_str(img.get("alt", "")), "src": full})
    return out


def clean_text_str(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def describe_section(idx, heading, subtitle, images, body_text):
    """Rule-based plain-language description + AI-agent layout description."""
    words = len(body_text.split())
    n = len(images)
    topic = heading or subtitle or (body_text[:60] + "…" if body_text else f"section {idx+1}")

    # plain description
    if body_text:
        snippet = body_text[:280] + ("…" if len(body_text) > 280 else "")
    else:
        snippet = "No body text; likely visual/spacer block."
    desc = f'Section about "{topic}". {snippet}'

    # ai agent layout description
    parts = []
    if heading:
        parts.append(f'Leads with the heading "{heading}".')
    if subtitle and subtitle != heading:
        parts.append(f'Supporting subtitle "{subtitle}".')
    if n == 0:
        parts.append("No images — text-only block.")
    elif n == 1:
        parts.append("Single image, likely a banner or feature visual.")
    else:
        parts.append(f"{n} images — grid/gallery or repeated cards.")
    if words > 120:
        parts.append("Long-form copy (paragraph content).")
    elif words > 0:
        parts.append("Short copy (label/CTA/tagline).")
    role = guess_role(idx, heading, n, words)
    parts.append(f"Likely role: {role}.")
    return desc, " ".join(parts)


def guess_role(idx, heading, n_images, words):
    h = (heading or "").lower()
    if idx == 0:
        return "hero / top banner"
    for kw, role in [("contact", "contact"), ("footer", "footer"), ("faq", "FAQ"),
                     ("price", "pricing"), ("test", "testimonial"), ("team", "team"),
                     ("about", "about"), ("service", "services"), ("feature", "features"),
                     ("blog", "blog/news"), ("portfolio", "portfolio")]:
        if kw in h:
            return role
    if n_images >= 3 and words < 60:
        return "gallery / logo strip"
    if words > 120:
        return "content / editorial"
    return "generic content band"


def extract_sections(soup, base_url, session, img_seen):
    body = soup.body or soup
    # prefer semantic <section>/<header>/<footer>; fallback to direct children of main/body
    blocks = body.find_all(["section", "header", "footer"], recursive=True)
    if not blocks:
        main = soup.find("main") or body
        blocks = [c for c in main.find_all(recursive=False) if getattr(c, "name", None)]

    sections = []
    for idx, b in enumerate(blocks):
        headings = b.find_all(HEADING_TAGS)
        heading = clean_text(headings[0]) if headings else ""
        subtitle = clean_text(headings[1]) if len(headings) > 1 else ""
        images = collect_images(b, base_url, session, img_seen)
        body_text = clean_text(b)
        # skip empty noise blocks
        if not heading and not images and len(body_text) < 15:
            continue
        desc, ai_desc = describe_section(idx, heading, subtitle, images, body_text)
        sections.append({
            "index": idx,
            "title": heading or subtitle or f"Section {idx+1}",
            "subtitle": subtitle,
            "heading": heading,
            "image_count": len(images),
            "images": [im["name"] for im in images],
            "image_details": images,
            "description": desc,
            "ai_agent_description": ai_desc,
        })
    return sections


def page_meta(soup, url):
    def meta(attr, val):
        t = soup.find("meta", attrs={attr: val})
        return clean_text_str(t.get("content")) if t and t.get("content") else ""
    canon = soup.find("link", rel="canonical")
    return {
        "description": meta("name", "description"),
        "keywords": meta("name", "keywords"),
        "og_title": meta("property", "og:title"),
        "og_description": meta("property", "og:description"),
        "og_image": meta("property", "og:image"),
        "canonical": canon.get("href") if canon and canon.get("href") else url,
    }


def scrape(start_url, max_pages, delay):
    for d in (PAGES_DIR, IMG_DIR):
        os.makedirs(d, exist_ok=True)
    host = urlparse(start_url).netloc
    session = requests.Session()
    session.headers["User-Agent"] = UA
    img_seen = {}
    seen_pages = set()
    q = deque([start_url])
    index = []

    while q and len(seen_pages) < max_pages:
        url = urldefrag(q.popleft())[0]
        if url in seen_pages:
            continue
        seen_pages.add(url)
        try:
            r = session.get(url, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"! page fail {url}: {e}")
            continue
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        print(f"[{len(seen_pages)}] {url}")

        title = clean_text(soup.title) if soup.title else ""
        sections = extract_sections(soup, url, session, img_seen)
        data = {
            "url": url,
            "title": title,
            "meta": page_meta(soup, url),
            "section_count": len(sections),
            "total_images": sum(s["image_count"] for s in sections),
            "sections": sections,
        }
        slug = slugify(url, host)
        with open(os.path.join(PAGES_DIR, f"{slug}.json"), "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        index.append({"url": url, "slug": slug, "title": title,
                      "sections": len(sections), "images": data["total_images"]})

        # enqueue internal links
        for a in soup.find_all("a", href=True):
            nxt = urldefrag(urljoin(url, a["href"]))[0]
            if nxt.startswith("http") and same_host(nxt, host) and nxt not in seen_pages:
                q.append(nxt)
        time.sleep(delay)

    with open(os.path.join(OUT, "index.json"), "w") as f:
        json.dump({"start": start_url, "pages": index,
                   "total_pages": len(index),
                   "total_images_downloaded": len([v for v in img_seen.values() if v])},
                  f, indent=2, ensure_ascii=False)
    print(f"\nDone. {len(index)} pages, "
          f"{len([v for v in img_seen.values() if v])} images -> {OUT}/")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--delay", type=float, default=0.5)
    a = ap.parse_args()
    scrape(a.url, a.max_pages, a.delay)
