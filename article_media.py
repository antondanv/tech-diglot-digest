"""Resolve publisher pages and pick up to three actual article photographs."""

import hashlib
import io
import json
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from googlenewsdecoder import gnewsdecoder
from PIL import Image, UnidentifiedImageError

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TechDiglot/1.0)"}
MAX_IMAGE_BYTES = 9 * 1024 * 1024


def valid_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def publisher_url(link):
    if not valid_url(link):
        raise ValueError("Источник должен быть HTTP(S)-ссылкой")
    if urlparse(link).hostname == "news.google.com":
        result = gnewsdecoder(link, timeout=10)
        if not result.get("success") or not valid_url(result.get("decoded_url")):
            raise ValueError("Не удалось получить исходную статью из Google News")
        link = result["decoded_url"]
    if urlparse(link).hostname == "news.google.com":
        raise ValueError("Страница агрегатора не является источником фотографии")
    return link


def image_candidate(link):
    if not valid_url(link):
        return False
    parsed = urlparse(link)
    host = parsed.hostname.lower()
    if host == "news.google.com" or host.endswith("gstatic.com"):
        return False
    return not re.search(
        r"(?:^|[/_.-])(logo|icon|avatar|placeholder|sprite|banner|favicon)(?:[/_.-]|$)",
        parsed.path.lower(),
    )


def article_image_urls(soup, base_url):
    """Use article metadata/body only; never pick sidebar/recommendation photos."""
    candidates = []
    for attrs in (
        {"property": "og:image"},
        {"name": "og:image"},
        {"name": "twitter:image"},
    ):
        for tag in soup.find_all("meta", attrs=attrs):
            if tag.get("content"):
                candidates.append(tag["content"])

    def article_images(value):
        if isinstance(value, list):
            for child in value:
                article_images(child)
        elif isinstance(value, dict):
            kinds = value.get("@type", [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if any(kind in ("Article", "NewsArticle", "BlogPosting") for kind in kinds):
                pictures = value.get("image", [])
                pictures = [pictures] if not isinstance(pictures, list) else pictures
                for picture in pictures:
                    if isinstance(picture, str):
                        candidates.append(picture)
                    elif isinstance(picture, dict):
                        candidates.append(
                            picture.get("url", picture.get("contentUrl", ""))
                        )
            if "@graph" in value:
                article_images(value["@graph"])

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            article_images(json.loads(tag.string or tag.get_text()))
        except (ValueError, TypeError):
            continue
    bodies = soup.select('[itemprop="articleBody"]') or soup.find_all("article")
    for body in bodies:
        for tag in body.find_all("img"):
            context = " ".join(
                str(parent.get("class", "")) + " " + str(parent.get("id", ""))
                for parent in [tag, *tag.parents]
                if getattr(parent, "attrs", None)
            )
            if re.search(
                r"related|recommend|sidebar|advert|promo|author|avatar|slider|carousel",
                context,
                re.IGNORECASE,
            ):
                continue
            anchor = tag.find_parent("a", href=True)
            if anchor:
                target = urljoin(base_url, anchor["href"])
                parsed_target = urlparse(target)
                parsed_base = urlparse(base_url)
                if (parsed_target.hostname, parsed_target.path) != (
                    parsed_base.hostname,
                    parsed_base.path,
                ) and not re.search(
                    r"\.(?:jpe?g|png|webp)(?:$|/)", parsed_target.path, re.IGNORECASE
                ):
                    continue
            source = tag.get("data-src") or tag.get("src")
            srcset = tag.get("data-srcset") or tag.get("srcset")
            if srcset:
                # Last item normally has the largest responsive resolution.
                source = srcset.split(",")[-1].strip().split()[0]
            if source:
                candidates.append(source)
    title_tag = soup.find("meta", attrs={"property": "og:title"})
    title = title_tag.get("content", "").strip() if title_tag else ""
    if len(title) >= 20:
        for tag in soup.find_all("img"):
            if tag.get("alt", "").startswith(title[:50]):
                source = tag.get("data-src") or tag.get("src")
                if source:
                    candidates.insert(0, source)
    # A generated social card duplicates the headline; prefer a real photograph.
    if any(
        image_candidate(urljoin(base_url, value)) and "html-to-img" not in value
        for value in candidates
    ):
        candidates = [value for value in candidates if "html-to-img" not in value]
    candidates.sort(key=lambda value: "html-to-img" in value)
    result = []
    for candidate in candidates:
        link = urljoin(base_url, candidate.strip())
        if image_candidate(link) and link not in result:
            result.append(link)
    return result[:6]


def image_bytes(link):
    """Validate before Telegram writes, so no text-only fallback is needed."""
    if not image_candidate(link):
        raise ValueError("Логотип или некорректная ссылка вместо фотографии")
    with requests.get(link, headers=HEADERS, timeout=15, stream=True) as response:
        response.raise_for_status()
        chunks = []
        size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise ValueError("Изображение слишком большое для Telegram")
            chunks.append(chunk)
    data = b"".join(chunks)
    try:
        with Image.open(io.BytesIO(data)) as picture:
            width, height = picture.size
            if (
                min(width, height) < 260
                or max(width, height) < 480
                or width + height > 10000
                or max(width / height, height / width) > 20
                or picture.format not in ("JPEG", "PNG", "WEBP")
            ):
                raise ValueError("Изображение не подходит для фотографии Telegram")
            image_format = picture.format
            picture.verify()
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError("Источник вернул не изображение") from error
    if image_format == "WEBP":
        with Image.open(io.BytesIO(data)) as picture:
            converted = io.BytesIO()
            picture.convert("RGB").save(converted, format="JPEG", quality=92)
            data = converted.getvalue()
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("JPEG слишком большой для Telegram")
    return data


def image_asset_id(link):
    parsed = urlparse(link)
    if parsed.hostname == "static.dw.com":
        match = re.search(r"/image/(\d+)_", parsed.path)
        if match:
            return f"dw:{match[1]}"
    if "html-to-img" in parsed.path:
        match = re.search(r"article-id(\d+)", parsed.path)
        if match:
            return f"{parsed.hostname}:card:{match[1]}"
    return parsed.hostname + parsed.path


def image_fingerprint(data):
    """Compare crops as well as scaled copies of the same article photograph."""
    with Image.open(io.BytesIO(data)) as picture:
        grey = picture.convert("L")
        width, height = grey.size
        crops = [grey]
        for ratio in (1, 4 / 3, 16 / 9):
            crop_width, crop_height = (
                min(width, int(height * ratio)),
                min(height, int(width / ratio)),
            )
            for position in (0, 0.5, 1):
                left = int((width - crop_width) * position)
                top = int((height - crop_height) * position)
                crops.append(
                    grey.crop((left, top, left + crop_width, top + crop_height))
                )
        fingerprints = set()
        for crop in crops:
            pixels = list(crop.resize((9, 8)).get_flattened_data())
            bits = 0
            for row in range(8):
                for col in range(8):
                    bits = (bits << 1) | (
                        pixels[row * 9 + col] > pixels[row * 9 + col + 1]
                    )
            fingerprints.add(bits)
        return fingerprints


def inspect_article(
    link, excluded_hashes=(), excluded_urls=(), excluded_fingerprints=()
):
    article = publisher_url(link)
    response = requests.get(article, headers=HEADERS, timeout=15)
    response.raise_for_status()
    if urlparse(response.url).hostname == "news.google.com":
        raise ValueError("Не удалось перейти на сайт издания")
    soup = BeautifulSoup(response.content, "html.parser")
    urls, hashes, signatures = [], [], []
    fingerprints = {
        int(value, 16) if isinstance(value, str) else value
        for value in excluded_fingerprints
    }
    selected_fingerprints = set()
    assets = {image_asset_id(value) for value in excluded_urls}
    for image in article_image_urls(soup, response.url):
        if image in excluded_urls or image_asset_id(image) in assets:
            continue
        try:
            data = image_bytes(image)
            digest = hashlib.sha256(data).hexdigest()
            fingerprint = image_fingerprint(data)
        except (requests.RequestException, ValueError):
            continue
        if (
            digest in excluded_hashes
            or digest in hashes
            or any(
                (value ^ other).bit_count() <= 6
                for value in fingerprint
                for other in fingerprints
            )
            or any(
                (value ^ other).bit_count() <= 12
                for value in fingerprint
                for other in selected_fingerprints
            )
        ):
            continue
        urls.append(image)
        hashes.append(digest)
        selected_fingerprints.update(fingerprint)
        signatures.extend(f"{value:016x}" for value in fingerprint)
        assets.add(image_asset_id(image))
        if len(urls) == 3:
            break
    if not urls:
        raise ValueError(
            "В статье нет доступных уникальных фотографий; пост не отправлен"
        )
    body = soup.select_one('[itemprop="articleBody"]') or soup.find("article")
    if body:
        for tag in body.select(
            'script, style, nav, aside, [class*="slider"], [class*="carousel"], '
            '[class*="recommend"], [class*="related"]'
        ):
            tag.decompose()
        article_text = body.get_text(" ", strip=True)[:12000]
    else:
        # JSON-LD articleBody is common on publishers with unconventional markup.
        article_text = ""
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                value = json.loads(tag.string or tag.get_text())
                if isinstance(value, dict) and isinstance(
                    value.get("articleBody"), str
                ):
                    article_text = value["articleBody"][:12000]
                    break
            except (ValueError, TypeError):
                continue
    return {
        "article_url": response.url,
        "image_urls": urls,
        "image_hashes": hashes,
        "image_fingerprints": signatures,
        "article_text": article_text,
    }
