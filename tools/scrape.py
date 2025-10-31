# -*- coding: utf-8 -*-
"""
tools/scrape.py — Scraper для Quest-Book (GitHub Actions friendly)

Что исправлено:
- Single-page: собираем ТОЛЬКО контент между якорями (#p123 и аналоги), по потоку next_elements.
- Сервисные блоки (nav/header/footer/breadcrumbs/share/social/ads/comment, script/style) выкидываются.
- Переходы берём только из области параграфа, target определяем из #якоря или query.
- Crawl: обходим ссылки внутри одной истории (…/online/gameNNNN/), если якорей на стартовой нет.

Выход: story.txt / story.md / story.json / story_storyui.json + meta.json
"""

from __future__ import annotations
import argparse, json, re, time, os, pathlib, sys, unicodedata, hashlib
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Set
from urllib.parse import urljoin, urlparse, urlunparse, urldefrag, parse_qs

import requests
from bs4 import BeautifulSoup, Tag, NavigableString

# --------- Регэкспы/правила ----------
RX_ANCHOR_ID   = re.compile(r"^(?:p|para|par|paragraph|sec|part)[\-_]*([0-9]{1,6})$", re.I)
RX_ANCHOR_NAME = RX_ANCHOR_ID
RX_HASH_TARGET = re.compile(r"#(?:p|para|par|paragraph|sec|part)[\-_]*([0-9]{1,6})", re.I)
RX_NUM_IN_QUERY_KEYS = ("p","pid","para","par","paragraph","id","sec","part")

DEFAULT_UA = "QuestReader-GHA/1.2 (+https://github.com)"
SKIP_CLASS_TOKENS = ("nav","breadcrumbs","footer","header","share","social","ads","adblock","comment")
SKIP_TAGS = ("script","style")

# --------- Модели ----------
@dataclass
class Choice:
    text: str
    target: Optional[int]
    href: str

@dataclass
class Paragraph:
    pid: int
    text: str
    choices: List[Choice]
    url: str  # страница, откуда взят параграф (single: базовый url)

@dataclass
class Story:
    title: str
    url: str
    paragraphs: List[Paragraph]

# --------- Robots.txt ----------
def robots_allowed(url: str, ua: str) -> bool:
    p = urlparse(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        r = requests.get(robots_url, headers={"User-Agent": ua}, timeout=15)
    except Exception:
        return True
    if r.status_code != 200:
        return True
    allows, disallows, applies = [], [], False
    for line in r.text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"): continue
        if s.lower().startswith("user-agent:"):
            agent = s.split(":",1)[1].strip()
            applies = (agent == "*" or ua.lower().startswith(agent.lower()))
            continue
        if not applies: continue
        if s.lower().startswith("allow:"):
            allows.append(s.split(":",1)[1].strip() or "/")
        elif s.lower().startswith("disallow:"):
            disallows.append(s.split(":",1)[1].strip() or "/")
    path = p.path or "/"
    def match(rule: str) -> bool: return path.startswith(rule)
    best_a = max((x for x in allows if match(x)), key=len, default=None)
    best_d = max((x for x in disallows if match(x)), key=len, default=None)
    if best_a and best_d: return len(best_a) >= len(best_d)
    if best_d and not best_a: return False
    return True

# --------- HTTP ----------
def fetch_html(url: str, ua: str, delay: float) -> str:
    time.sleep(max(0.0, delay))
    r = requests.get(url, headers={"User-Agent": ua}, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding or "utf-8"
    text = r.text
    if len(text) > 5_000_000:
        text = text[:5_000_000]
    return text

# --------- Утилиты извлечения ----------
def is_service_tag(tag: Tag) -> bool:
    if tag.name and tag.name.lower() in SKIP_TAGS:
        return True
    cls = " ".join(tag.get("class", [])).lower()
    return any(tok in cls for tok in SKIP_CLASS_TOKENS)

def text_of(node: Tag) -> str:
    parts: List[str] = []
    def rec(n):
        if isinstance(n, NavigableString):
            parts.append(str(n)); return
        if not isinstance(n, Tag): return
        t = (n.name or "").lower()
        if t in SKIP_TAGS: return
        if is_service_tag(n): return
        if t in ("p","div","section","article","blockquote"):
            before = len(parts)
            for c in n.children: rec(c)
            if len(parts) > before: parts.append("\n\n"); return
        if t in ("br","hr"): parts.append("\n"); return
        if t == "li":
            parts.append("- "); [rec(c) for c in n.children]; parts.append("\n"); return
        if t.startswith("h") and t[1:].isdigit():
            parts.append("\n"+n.get_text(strip=True)+"\n"); return
        if t == "a":
            parts.append(n.get_text(strip=True) or ""); return
        for c in n.children: rec(c)
    rec(node)
    out = "".join(parts)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()

def extract_number_from_url(u: str) -> Optional[int]:
    url, frag = urldefrag(u)
    q = parse_qs(urlparse(url).query)
    for k in RX_NUM_IN_QUERY_KEYS:
        if k in q:
            for v in q[k]:
                if v.isdigit(): return int(v)
    m = RX_HASH_TARGET.search(frag)
    if m: return int(m.group(1))
    tail = urlparse(url).path.strip("/").split("/")[-1]
    m2 = re.search(r"(\d{1,6})$", tail)
    if m2: return int(m2.group(1))
    return None

def iter_anchors_with_pid(soup: BeautifulSoup) -> List[Tuple[int, Tag]]:
    out: List[Tuple[int, Tag]] = []
    # id
    for t in soup.find_all(True, id=True):
        m = RX_ANCHOR_ID.match(t.get("id","") or "")
        if m: out.append((int(m.group(1)), t))
    # name
    for a in soup.find_all("a", attrs={"name": True}):
        m = RX_ANCHOR_NAME.match(a.get("name","") or "")
        if m: out.append((int(m.group(1)), a))
    # data-paragraph
    for t in soup.find_all(attrs={"data-paragraph": True}):
        v = str(t.get("data-paragraph"))
        if v.isdigit(): out.append((int(v), t))
    # уник по pid
    by: Dict[int, Tag] = {}
    for pid, tag in out:
        if pid not in by: by[pid] = tag
    return sorted(by.items(), key=lambda x: x[0])

def collect_between(start: Tag, next_anchor: Optional[Tag]) -> List[Tag]:
    """Собираем узлы ПО ПОТОКУ (next_elements) от start (исключая его) до next_anchor (не включая)."""
    acc: List[Tag] = []
    take = False
    for el in start.next_elements:
        if el is next_anchor: break
        if el is start: continue
        if isinstance(el, Tag):
            if is_service_tag(el):  # отбрасываем сервисные области полностью
                # пропускаем её поддерево
                for _ in el.descendants: pass
                continue
            acc.append(el)
        elif isinstance(el, NavigableString):
            s = str(el).strip()
            if s: acc.append(el)
    return acc

def extract_choices(nodes: List[Tag], base: str) -> List[Choice]:
    out: List[Choice] = []
    for n in nodes:
        if isinstance(n, NavigableString): continue
        for a in n.find_all("a", href=True):
            href = a["href"].strip()
            txt = a.get_text(strip=True) or href
            lt = txt.lower()
            if lt in {"вверх","наверх","к началу","вернуться","назад"}:
                continue
            full = urljoin(base, href)
            m = RX_HASH_TARGET.search(href)
            tgt = int(m.group(1)) if m else extract_number_from_url(full)
            out.append(Choice(text=txt, target=tgt, href=full))
    # dedup (target,text)
    uniq: Dict[str, Choice] = {}
    for c in out:
        k = f"{c.target}|{c.text}"
        if k not in uniq: uniq[k] = c
    return list(uniq.values())

# --------- Single-page разбор ---------
def parse_single_page(html: str, base_url: str) -> Story:
    soup = BeautifulSoup(html, "lxml")
    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "Без названия"

    anchors = iter_anchors_with_pid(soup)
    paragraphs: List[Paragraph] = []

    if anchors:
        for i, (pid, node) in enumerate(anchors):
            next_anchor = anchors[i+1][1] if i+1 < len(anchors) else None
            nodes = collect_between(node, next_anchor)
            # текст
            parts: List[str] = []
            for it in nodes:
                if isinstance(it, NavigableString):
                    t = str(it).strip()
                    if t: parts.append(t)
                elif isinstance(it, Tag):
                    t = text_of(it)
                    if t: parts.append(t)
            text = "\n\n".join([t for t in parts if t.strip()]).strip()
            # переходы только из этой области
            choices = extract_choices(nodes, base=base_url)
            paragraphs.append(Paragraph(pid=pid, text=text, choices=choices, url=base_url))
        paragraphs.sort(key=lambda p: p.pid)
    else:
        # fallback: весь основной контент
        body = soup.find("main") or soup.find("article") or soup.find("div", id="content") or soup.body
        text = text_of(body) if body else soup.get_text("\n", strip=True)
        paragraphs = [Paragraph(pid=1, text=text.strip(), choices=[], url=base_url)]

    return Story(title=title, url=base_url, paragraphs=paragraphs)

# --------- Crawl (многосстр.) ---------
def story_root(url: str) -> str:
    p = urlparse(url)
    parts = p.path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "online":
        root_path = "/" + "/".join(parts[:2]) + "/"
    else:
        root_path = "/".join(p.path.split("/")[:-1]) + "/"
        if not root_path.startswith("/"): root_path = "/" + root_path
    return urlunparse((p.scheme, p.netloc, root_path, "", "", ""))

def is_same_story(root: str, href: str) -> bool:
    url, _ = urldefrag(href)
    return url.startswith(root)

def normalize_url(u: str) -> str:
    url, _ = urldefrag(u)
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", p.query, ""))

def synthetic_pid(u: str) -> int:
    h = hashlib.sha1(normalize_url(u).encode("utf-8")).hexdigest()
    return int(h[:6], 16)

def pick_main_area(soup: BeautifulSoup) -> Tag:
    candidates = [soup.find("main"), soup.find("article"),
                  soup.find("div", id="content"), soup.find("div", class_="content"),
                  soup.body]
    for c in candidates:
        if c: return c
    return soup

def extract_text_and_links_from_page(html: str, page_url: str) -> Tuple[str, List[Choice]]:
    soup = BeautifulSoup(html, "lxml")
    area = pick_main_area(soup)
    # уберём сервисные блоки верхнего уровня
    clean_nodes: List[Tag] = []
    for ch in area.children:
        if isinstance(ch, Tag) and is_service_tag(ch): continue
        clean_nodes.append(ch)
    # текст
    parts: List[str] = []
    for it in clean_nodes:
        if isinstance(it, NavigableString):
            t = str(it).strip()
            if t: parts.append(t)
        elif isinstance(it, Tag):
            t = text_of(it)
            if t: parts.append(t)
    text = "\n\n".join([t for t in parts if t.strip()]).strip()
    # переходы из всей области
    choices = extract_choices([area], base=page_url)
    return text, choices

def parse_crawl(base_url: str, ua: str, delay: float, max_pages: int) -> Story:
    visited: Set[str] = set()
    queue: List[str] = [normalize_url(base_url)]
    root = story_root(base_url)
    paragraphs: List[Paragraph] = []
    title = None

    while queue and len(visited) < max_pages:
        cur = queue.pop(0)
        if cur in visited or not is_same_story(root, cur): continue
        if not robots_allowed(cur, ua):
            visited.add(cur)
            continue
        html = fetch_html(cur, ua, delay)
        soup = BeautifulSoup(html, "lxml")
        if title is None:
            t = soup.find("h1") or soup.find("title")
            title = t.get_text(strip=True) if t else "Без названия"

        text, choices = extract_text_and_links_from_page(html, cur)
        pid = extract_number_from_url(cur) or synthetic_pid(cur)

        norm_choices: List[Choice] = []
        for ch in choices:
            if not is_same_story(root, ch.href): continue
            tgt = ch.target or extract_number_from_url(ch.href) or synthetic_pid(ch.href)
            nh = normalize_url(ch.href)
            norm_choices.append(Choice(text=ch.text, target=tgt, href=nh))
            if nh not in visited and nh not in queue:
                queue.append(nh)

        paragraphs.append(Paragraph(pid=pid, text=text, choices=norm_choices, url=cur))
        visited.add(cur)

    if title is None: title = "Без названия"
    paragraphs.sort(key=lambda p: p.pid)
    return Story(title=title, url=base_url, paragraphs=paragraphs)

# --------- Форматтеры ----------
def to_md(story: Story) -> str:
    lines = [f"# {story.title}\n", f"_Источник: {story.url}_\n"]
    for p in story.paragraphs:
        lines += ["\n---\n", f"## [{p.pid}]\n"]
        if p.text: lines.append(p.text+"\n")
        if p.choices:
            lines.append("\n**Варианты:**\n")
            for c in p.choices:
                if c.href:
                    lines.append(f"- [{c.text}]({c.href})" + (f" → `{c.target}`" if c.target is not None else ""))
                else:
                    lines.append(f"- {c.text}")
    return "\n".join(lines).strip()+"\n"

def to_txt(story: Story) -> str:
    out = [f"{story.title}\n", f"Источник: {story.url}\n"]
    for p in story.paragraphs:
        out += ["\n"+"="*40+"\n", f"[{p.pid}]  ({p.url})\n"]
        if p.text: out.append(p.text+"\n")
        if p.choices:
            out.append("\nВарианты:\n")
            for c in p.choices:
                out.append(f" - {c.text}" + (f"  -> {c.target}\n" if c.target is not None else "\n"))
    return "".join(out)

def to_json(story: Story) -> str:
    data = {
        "title": story.title,
        "url": story.url,
        "paragraphs": [
            {"id": p.pid, "url": p.url, "text": p.text,
             "choices": [{"text": c.text, "target": c.target, "href": c.href} for c in p.choices]}
            for p in story.paragraphs
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)

def to_storyui(story: Story) -> str:
    data = {
        "version": "storyui_v1",
        "title": story.title,
        "source_url": story.url,
        "nodes": [
            {"pid": p.pid, "paragraph": p.text,
             "buttons": [{"caption": c.text, "goto_pid": c.target, "action_uid": "", "action_goto": ""} for c in p.choices]}
            for p in story.paragraphs
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)

# --------- Main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--domain-lock", action="store_true")
    ap.add_argument("--max-pages", type=int, default=600)
    ap.add_argument("--mode", choices=["auto","single","crawl"], default="auto")
    args = ap.parse_args()

    base = args.url.strip()
    ua = args.user_agent.strip()

    if args.domain_lock and not urlparse(base).netloc.lower().endswith("quest-book.ru"):
        print("Domain lock: разрешён только quest-book.ru", file=sys.stderr); sys.exit(2)

    if not robots_allowed(base, ua):
        print("robots.txt запрещает доступ", file=sys.stderr); sys.exit(3)

    html0 = fetch_html(base, ua, delay=0.0)

    story: Optional[Story] = None
    if args.mode in ("auto","single"):
        soup0 = BeautifulSoup(html0, "lxml")
        anchors = iter_anchors_with_pid(soup0)
        if args.mode == "single" or (args.mode == "auto" and len(anchors) >= 3):
            story = parse_single_page(html0, base)

    if story is None:
        story = parse_crawl(base, ua=ua, delay=args.delay, max_pages=args.max_pages)

    pathlib.Path(args.outdir).mkdir(parents=True, exist_ok=True)
    with open(os.path.join(args.outdir,"meta.json"), "w", encoding="utf-8") as f:
        json.dump({"title": story.title, "url": story.url}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.outdir,"story.txt"), "w", encoding="utf-8") as f:
        f.write(to_txt(story))
    with open(os.path.join(args.outdir,"story.md"), "w", encoding="utf-8") as f:
        f.write(to_md(story))
    with open(os.path.join(args.outdir,"story.json"), "w", encoding="utf-8") as f:
        f.write(to_json(story))
    with open(os.path.join(args.outdir,"story_storyui.json"), "w", encoding="utf-8") as f:
        f.write(to_storyui(story))

if __name__ == "__main__":
    main()
