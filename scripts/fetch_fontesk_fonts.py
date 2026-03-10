import io
import os
import re
import sys
import json
import time
import shutil
import zipfile
import tempfile
from dataclasses import dataclass
from typing import Iterable, Optional

import requests
from bs4 import BeautifulSoup
from fontTools.ttLib import TTFont


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
REQ_TIMEOUT = 45


TARGETS = [
    ("https://fontesk.com/?s=dolbak+brush", "dolbak-brush.woff2"),
    ("https://fontesk.com/?s=tinta+script", "tinta-script.woff2"),
    ("https://fontesk.com/?s=inkwell", "inkwell.woff2"),
    ("https://fontesk.com/?s=paper+inko", "paper-inko.woff2"),
    ("https://fontesk.com/?s=mallino+script", "mallino-script.woff2"),
    ("https://fontesk.com/?s=glina+script", "glina-script.woff2"),
    ("https://fontesk.com/?s=ivory+heart", "ivory-heart.woff2"),
    ("https://fontesk.com/?s=comic+lemon", "comic-lemon.woff2"),
    ("https://fontesk.com/?s=twinkie+town", "twinkie-town.woff2"),
    ("https://fontesk.com/?s=spicy+taquito", "spicy-taquito.woff2"),
    ("https://fontesk.com/?s=krikikrak", "krikikrak.woff2"),
    ("https://fontesk.com/?s=billy+bounce", "billy-bounce.woff2"),
    ("https://fontesk.com/?s=cabana+font", "cabana.woff2"),
    ("https://fontesk.com/?s=sunny+spells", "sunny-spells.woff2"),
    ("https://fontesk.com/?s=rio+ma+font", "rio-ma.woff2"),
    ("https://fontesk.com/?s=brook+font", "brook.woff2"),
    ("https://fontesk.com/?s=give+away+font", "give-away.woff2"),
    ("https://fontesk.com/?s=heroika", "heroika.woff2"),
    ("https://fontesk.com/?s=asa+branca", "asa-branca.woff2"),
    ("https://fontesk.com/?s=historic+font", "historic.woff2"),
    ("https://fontesk.com/?s=wavere+font", "wavere.woff2"),
    ("https://fontesk.com/?s=creepy+notes", "creepy-notes.woff2"),
    ("https://fontesk.com/?s=howdy+friend", "howdy-friend.woff2"),
    ("https://fontesk.com/?s=gag+freestyle", "gag-freestyle.woff2"),
    ("https://fontesk.com/?s=orange+gummy", "orange-gummy.woff2"),
]


CHECK_CHARS = ["ç", "ã", "õ", "é", "ê", "í", "ó", "ú", "â", "ü"]


WEIGHT_KEYWORDS = [
    (100, ["thin", "hairline"]),
    (200, ["extralight", "extra-light", "ultralight", "ultra-light"]),
    (300, ["light"]),
    (400, ["regular", "normal", "book", "roman"]),
    (500, ["medium"]),
    (600, ["semibold", "semi-bold", "demibold", "demi-bold"]),
    (700, ["bold"]),
    (800, ["extrabold", "extra-bold", "ultrabold", "ultra-bold"]),
    (900, ["black", "heavy"]),
]


@dataclass(frozen=True)
class DownloadCandidate:
    url: str
    status_code: int
    content_type: str
    content_disposition: str
    is_zip: bool
    redirected_to: Optional[str]


def http_get(session: requests.Session, url: str, *, stream: bool = False, allow_redirects: bool = True) -> requests.Response:
    return session.get(
        url,
        timeout=REQ_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        stream=stream,
        allow_redirects=allow_redirects,
    )


def first_post_url_from_search(session: requests.Session, search_url: str) -> Optional[str]:
    r = http_get(session, search_url)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    a = soup.select_one("h2.entry-title a[href]")
    return a.get("href") if a else None


def extract_download_links(post_html: str) -> list[str]:
    # Fontesk costuma expor links /download/<id>/ na página do post.
    abs_links = re.findall(r"https?://(?:www\.)?fontesk\.com/download/\d+/?", post_html, flags=re.I)
    rel_links = re.findall(r"/download/\d+/?", post_html, flags=re.I)

    seen: set[str] = set()
    out: list[str] = []
    for u in list(abs_links) + [f"https://fontesk.com{p}" for p in rel_links]:
        if not u.endswith("/"):
            u = u + "/"
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def probe_download_candidate(session: requests.Session, url: str) -> DownloadCandidate:
    # Primeiro tenta sem seguir redirect para detectar "pagos" (ex: envato).
    r = http_get(session, url, stream=True, allow_redirects=False)
    redirected_to = r.headers.get("Location")
    if r.status_code in (301, 302, 303, 307, 308) and redirected_to:
        return DownloadCandidate(
            url=url,
            status_code=r.status_code,
            content_type=r.headers.get("Content-Type", "") or "",
            content_disposition=r.headers.get("Content-Disposition", "") or "",
            is_zip=False,
            redirected_to=redirected_to,
        )

    ct = (r.headers.get("Content-Type", "") or "").lower()
    cd = r.headers.get("Content-Disposition", "") or ""
    is_zip = ("application/zip" in ct) or (".zip" in cd.lower())
    return DownloadCandidate(
        url=url,
        status_code=r.status_code,
        content_type=ct,
        content_disposition=cd,
        is_zip=is_zip,
        redirected_to=None,
    )


def download_zip_bytes(session: requests.Session, url: str) -> bytes:
    r = http_get(session, url, stream=True, allow_redirects=True)
    r.raise_for_status()
    return r.content


def infer_weight_from_name(name: str) -> int:
    n = name.lower()
    for weight, keys in WEIGHT_KEYWORDS:
        for k in keys:
            if k in n:
                return weight
    return 400


def is_italic(name: str) -> bool:
    n = name.lower()
    return "italic" in n or "oblique" in n


def choose_font_file(paths: Iterable[str]) -> Optional[str]:
    candidates = []
    for p in paths:
        pl = p.lower()
        if not (pl.endswith(".ttf") or pl.endswith(".otf")):
            continue
        w = infer_weight_from_name(os.path.basename(p))
        italic_penalty = 1 if is_italic(p) else 0
        # prioriza mais perto do 400, não itálico.
        score = (abs(w - 400), italic_penalty, 0 if pl.endswith(".ttf") else 1, len(p))
        candidates.append((score, p, w))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def cmap_support(font: TTFont, chars: list[str]) -> dict[str, bool]:
    cmap = font.getBestCmap() or {}
    out = {}
    for ch in chars:
        out[ch] = ord(ch) in cmap
    return out


def convert_to_woff2(src_path: str, out_path: str) -> None:
    font = TTFont(src_path, recalcBBoxes=False, recalcTimestamp=False)
    font.flavor = "woff2"
    font.save(out_path)


def safe_mkdir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def main() -> int:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    out_dir = os.path.join(repo_root, "public", "fonts")
    safe_mkdir(out_dir)

    results = []
    session = requests.Session()

    for search_url, out_name in TARGETS:
        item = {
            "search_url": search_url,
            "output": out_name,
            "status": "failed",
            "reason": None,
            "post_url": None,
            "download_url": None,
            "original_font_file": None,
            "chosen_weight": None,
            "charset_support": None,
        }

        out_path = os.path.join(out_dir, out_name)
        if os.path.exists(out_path):
            item["status"] = "skipped"
            item["reason"] = "já existe no destino (evitando duplicata)"
            results.append(item)
            continue

        try:
            post_url = first_post_url_from_search(session, search_url)
            if not post_url:
                item["reason"] = "não encontrou resultado na busca"
                results.append(item)
                continue
            item["post_url"] = post_url

            post_html = http_get(session, post_url).text
            download_links = extract_download_links(post_html)
            if not download_links:
                item["reason"] = "não encontrou links /download/<id>/ no post"
                results.append(item)
                continue

            chosen_download = None
            for dl in download_links:
                cand = probe_download_candidate(session, dl)
                # pular redirects externos (ex: envato, gumroad etc)
                if cand.redirected_to and not cand.redirected_to.startswith("https://fontesk.com/"):
                    continue
                if cand.status_code != 200:
                    continue
                if not cand.is_zip:
                    continue
                chosen_download = dl
                break

            if not chosen_download:
                item["reason"] = "somente downloads com redirect externo/pago ou sem ZIP"
                results.append(item)
                continue

            item["download_url"] = chosen_download
            zip_bytes = download_zip_bytes(session, chosen_download)

            with tempfile.TemporaryDirectory() as td:
                zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
                members = [m for m in zf.namelist() if not m.endswith("/") and not m.startswith("__MACOSX/")]
                chosen_member = choose_font_file(members)
                if not chosen_member:
                    item["reason"] = "ZIP não contém .ttf/.otf"
                    results.append(item)
                    continue

                zf.extract(chosen_member, path=td)
                src_path = os.path.join(td, chosen_member)
                # se o zip tinha subpastas, o path acima existe; garantir normalização
                if not os.path.isfile(src_path):
                    # fallback: procurar o basename extraído
                    bn = os.path.basename(chosen_member)
                    for root, _, files in os.walk(td):
                        if bn in files:
                            src_path = os.path.join(root, bn)
                            break

                item["original_font_file"] = os.path.basename(src_path)
                item["chosen_weight"] = infer_weight_from_name(item["original_font_file"])

                # charset
                font = TTFont(src_path, recalcBBoxes=False, recalcTimestamp=False)
                item["charset_support"] = cmap_support(font, CHECK_CHARS)

                convert_to_woff2(src_path, out_path)

            item["status"] = "ok"
            results.append(item)

            # ser gentil com o site
            time.sleep(0.7)

        except zipfile.BadZipFile:
            item["reason"] = "download não era um zip válido"
            results.append(item)
        except requests.RequestException as e:
            item["reason"] = f"erro HTTP: {e.__class__.__name__}"
            results.append(item)
        except Exception as e:
            item["reason"] = f"erro: {e.__class__.__name__}: {e}"
            results.append(item)

    report_path = os.path.join(out_dir, "REPORT.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    ok = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "failed")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    print(f"Concluído: ok={ok} failed={failed} skipped={skipped}")
    print(f"Relatório: {os.path.relpath(report_path, repo_root)}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
