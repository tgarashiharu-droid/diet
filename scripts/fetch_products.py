#!/usr/bin/env python3
"""コンビニ3社の公式サイトから商品の最新情報(価格・栄養成分)を取得して data/products.json に保存する。

使い方:
    pip install -r scripts/requirements.txt
    python scripts/fetch_products.py                 # 全店舗
    python scripts/fetch_products.py --store seven   # 1店舗だけ
    python scripts/fetch_products.py --no-detail     # 詳細ページを見ない(栄養成分なし・高速)

各社のサイト構造が変わっても壊れにくいよう、一覧ページは「商品詳細ページへのリンク」を起点に、
価格・栄養成分は本文テキストへの正規表現で拾う。取得に失敗した店舗は前回のデータを残す。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "products.json"
JST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; diet-product-fetcher/1.0; +https://github.com/tgarashiharu-droid/diet)"
REQUEST_INTERVAL = 0.7  # 秒。公式サイトに負荷をかけないための間隔

STORES = {
    "seven": {
        "name": "セブン-イレブン",
        "base": "https://www.sej.co.jp",
        "categories": {
            "おにぎり": "/products/a/onigiri/",
            "お寿司": "/products/a/sushi/",
            "お弁当": "/products/a/bento/",
            "サンドイッチ・ロールパン": "/products/a/sandwich/",
            "パン": "/products/a/bread/",
            "麺類": "/products/a/men/",
            "パスタ": "/products/a/pasta/",
            "グラタン・ドリア": "/products/a/gratin/",
            "惣菜": "/products/a/dailydish/",
            "サラダ": "/products/a/salad/",
            "スイーツ": "/products/a/sweets/",
            "ホットスナック": "/products/a/hotsnack/",
            "セブンプレミアム": "/products/a/7premium/",
        },
        "detail_re": re.compile(r"/products/a/item/\d+/?"),
        # 一覧の2ページ目以降: /products/a/onigiri/1/l15/ のような形式
        "page_url": lambda path, n: f"{path}{n}/l100/",
    },
    "lawson": {
        "name": "ローソン",
        "base": "https://www.lawson.co.jp",
        "categories": {
            "おにぎり": "/recommend/original/rice/",
            "お弁当": "/recommend/original/bento/",
            "サンドイッチ": "/recommend/original/sandwich/",
            "パン": "/recommend/original/bakery/",
            "麺類": "/recommend/original/noodle/",
            "サラダ": "/recommend/original/salad/",
            "惣菜": "/recommend/original/dailydish/",
            "スイーツ": "/recommend/original/dessert/",
            "からあげクン・ホットスナック": "/recommend/original/fastfood/",
        },
        "detail_re": re.compile(r"/recommend/original/detail/[\w_]+\.html"),
        "page_url": None,
    },
    "family": {
        "name": "ファミリーマート",
        "base": "https://www.family.co.jp",
        "categories": {
            "おむすび": "/goods/omusubi.html",
            "お弁当": "/goods/obento.html",
            "サンドイッチ・ロールパン": "/goods/sandwich.html",
            "パン": "/goods/bread.html",
            "麺類": "/goods/noodle.html",
            "パスタ": "/goods/pasta.html",
            "惣菜": "/goods/sozai.html",
            "サラダ": "/goods/salad.html",
            "スイーツ": "/goods/dessert.html",
            "ファミチキ・ホットスナック": "/goods/friedfoods.html",
        },
        "detail_re": re.compile(r"/goods/[\w-]+/\d+\.html"),
        "page_url": None,
    },
}

# 「140円（税込151.20円）」「151円(税込)」「税込149円」などに対応
TAX_IN_PAREN_RE = re.compile(r"税込\s*[：:]?\s*([\d,]+(?:\.\d+)?)\s*円")
PRICE_TAX_SUFFIX_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*円\s*[（(]\s*税込\s*[)）]")
PRICE_PLAIN_RE = re.compile(r"([\d,]+)\s*円")

NUTRIENT_RES = {
    "calories": re.compile(r"(?:熱量|エネルギー)\s*[：:]?\s*([\d.]+)\s*kcal", re.I),
    "protein": re.compile(r"(?:たんぱく質|たんぱく|蛋白質|タンパク質)\s*[：:]?\s*([\d.]+)\s*g"),
    "fat": re.compile(r"脂質\s*[：:]?\s*([\d.]+)\s*g"),
    "carbs": re.compile(r"炭水化物\s*[：:]?\s*([\d.]+)\s*g"),
}

NOISE_IN_NAME = re.compile(r"^(?:NEW|新商品|地域限定)\s*|\s*販売地域.*$")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Fetcher:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.5"})
        self._last = 0.0

    def get(self, url: str) -> str | None:
        wait = REQUEST_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            try:
                res = self.session.get(url, timeout=20)
                self._last = time.monotonic()
                if res.status_code == 404:
                    return None
                res.raise_for_status()
                res.encoding = res.apparent_encoding or res.encoding
                return res.text
            except requests.RequestException as e:
                log(f"  ! {url}: {e} (retry {attempt + 1}/3)")
                time.sleep(2 ** attempt)
        return None


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def to_number(s: str) -> float:
    return float(s.replace(",", ""))


def parse_price(text: str) -> int | None:
    """テキストから税込価格(円・整数)を取り出す。"""
    text = clean_text(text)
    m = TAX_IN_PAREN_RE.search(text)
    if m:
        return int(to_number(m.group(1)))  # 税込151.20円 → 151 (店頭表示は切り捨て)
    m = PRICE_TAX_SUFFIX_RE.search(text)
    if m:
        return int(to_number(m.group(1)))
    m = PRICE_PLAIN_RE.search(text)
    if m:
        return int(to_number(m.group(1)))
    return None


def parse_nutrition(text: str) -> dict:
    text = clean_text(text)
    out = {}
    for key, rx in NUTRIENT_RES.items():
        m = rx.search(text)
        if m:
            v = float(m.group(1))
            out[key] = int(round(v)) if key == "calories" else round(v, 1)
    return out


def normalize_name(name: str) -> str:
    return clean_text(NOISE_IN_NAME.sub("", name))


def item_container(a):
    """商品リンクを含む、1商品分のブロック(価格が入っている最小の祖先)を探す。"""
    node = a
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        if "円" in node.get_text():
            return node
    return a.parent


NAME_SELECTORS = [".item_ttl", ".ttl", "[class*='name']", "[class*='title']", "[class*='ttl']", "h3", "h4", "p"]


def guess_name(a, container) -> str:
    # 1) リンク文字列そのもの(価格などが混ざっていなければ)
    text = clean_text(a.get_text())
    if text and "円" not in text and len(text) <= 80:
        return text
    # 2) リンクや商品ブロック内の「商品名らしい」要素
    for scope in (a, container):
        if scope is None:
            continue
        for sel in NAME_SELECTORS:
            for el in scope.select(sel):
                t = clean_text(el.get_text())
                if t and "円" not in t and "kcal" not in t:
                    return t
    # 3) 画像リンクの alt
    img = a.find("img")
    if img and img.get("alt"):
        return clean_text(img["alt"])
    return ""


def parse_list_page(html: str, store_key: str, page_url: str) -> list[dict]:
    cfg = STORES[store_key]
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        if not cfg["detail_re"].search(urlparse(href).path):
            continue
        container = item_container(a)
        name = guess_name(a, container)
        entry = found.setdefault(href, {"url": href, "name": "", "price": None})
        if name and not entry["name"]:
            entry["name"] = name
        if entry["price"] is None and container is not None:
            entry["price"] = parse_price(container.get_text(" "))
    return [e for e in found.values() if e["name"]]


def parse_detail_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ")
    out = parse_nutrition(text)
    price = None
    for el in soup.select("[class*='price']"):
        price = parse_price(el.get_text(" "))
        if price:
            break
    if price is None:
        m = TAX_IN_PAREN_RE.search(clean_text(text)) or PRICE_TAX_SUFFIX_RE.search(clean_text(text))
        if m:
            price = int(to_number(m.group(1)))
    if price:
        out["price"] = price
    return out


def fetch_store(store_key: str, fetcher: Fetcher, with_detail: bool, max_pages: int) -> list[dict]:
    cfg = STORES[store_key]
    products: dict[str, dict] = {}
    for category, path in cfg["categories"].items():
        log(f"[{cfg['name']}] {category}")
        urls = [cfg["base"] + path]
        if cfg["page_url"]:
            urls += [cfg["base"] + cfg["page_url"](path, n) for n in range(2, max_pages + 1)]
        before = len(products)
        for url in urls:
            html = fetcher.get(url)
            if not html:
                break
            items = parse_list_page(html, store_key, url)
            new = [i for i in items if i["url"] not in products]
            if not new:
                break
            for i in new:
                i["category"] = category
                products[i["url"]] = i
        log(f"  {len(products) - before} 件")

    if with_detail:
        total = len(products)
        for n, item in enumerate(products.values(), 1):
            if n % 25 == 0:
                log(f"[{cfg['name']}] 詳細ページ {n}/{total}")
            html = fetcher.get(item["url"])
            if not html:
                continue
            detail = parse_detail_page(html)
            if detail.get("price"):
                item["price"] = detail["price"]
            for k in ("calories", "protein", "fat", "carbs"):
                if k in detail:
                    item[k] = detail[k]

    result = []
    for item in products.values():
        if not item.get("price"):
            continue
        result.append({
            "name": normalize_name(item["name"]) or item["name"],
            "price": item["price"],
            "calories": item.get("calories"),
            "protein": item.get("protein"),
            "fat": item.get("fat"),
            "carbs": item.get("carbs"),
            "category": item["category"],
            "url": item["url"],
        })
    result.sort(key=lambda p: (p["category"], p["name"]))
    return result


def load_existing() -> dict:
    try:
        return json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"stores": {}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", choices=list(STORES), action="append", help="取得する店舗(複数指定可。省略時は全店舗)")
    ap.add_argument("--no-detail", action="store_true", help="商品詳細ページを取得しない(栄養成分は空になる)")
    ap.add_argument("--max-pages", type=int, default=10, help="カテゴリごとの一覧ページ上限")
    ap.add_argument("--min-items", type=int, default=20, help="この件数未満なら取得失敗とみなし前回データを残す")
    args = ap.parse_args()

    data = load_existing()
    data.setdefault("stores", {})
    fetcher = Fetcher()
    failed = []
    for key in args.store or list(STORES):
        name = STORES[key]["name"]
        try:
            items = fetch_store(key, fetcher, not args.no_detail, args.max_pages)
        except Exception as e:  # 1店舗の失敗で全体を止めない
            log(f"[{name}] 取得エラー: {e}")
            items = []
        if len(items) < args.min_items:
            log(f"[{name}] {len(items)} 件しか取れなかったため前回のデータを維持します")
            failed.append(name)
            continue
        data["stores"][name] = {
            "updatedAt": datetime.now(JST).isoformat(timespec="seconds"),
            "source": STORES[key]["base"],
            "items": items,
        }
        log(f"[{name}] 合計 {len(items)} 件を保存")

    data["generatedAt"] = datetime.now(JST).isoformat(timespec="seconds")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log(f"→ {OUT_PATH.relative_to(ROOT)}")
    return 1 if failed and len(failed) == len(args.store or STORES) else 0


if __name__ == "__main__":
    sys.exit(main())
