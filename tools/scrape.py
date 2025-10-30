# -*- coding: utf-8 -*-
"""
tools/scrape.py
GitHub Actions scraper для сторигеймов Quest-Book.

Цели:
- Уважать robots.txt (longest-allow-wins).
- Автоматически определить структуру:
  A) все параграфы на одной HTML-странице (якоря #p123 и т.п.) => "single-page".
  B) параграфы на разных URL (кнопки/ссылки ведут на новые страницы) => "crawl graph".
- В режиме crawl: пройти по всем внутридоменным ссылкам, относящимся к ОДНОЙ истории
  (например, всё, что начинается с https://quest-book.ru/online/game17109/),
  собирать каждый параграф + его варианты переходов.
- Выдать четыре артефакта: story.txt, story.md, story.json, story_storyui.json,
  и meta.json с {title, url}.

Замечания:
- "Чистый" текст извлекается из контента параграфа, а не из всей страницы: устраняем навигацию/комменты/шапки по классам.
- Если у ссылки есть #p123 или query вида p=123/id=123/paragraph=123 — считаем это номером параграфа.
- Если номера не удаётся извлечь, назначаем стабильный synthetic id.

Ограничители:
- По умолчанию max_pages=600, delay=0.7 сек между запросами.
- domain-lock по умолчанию: только quest-book.ru.

Запуск (пример):
python tools/scrape.py --url "https://quest-book.ru/online/game17109/" --outdir "out" --domain-lock --delay 0.7 --max-pages 600
"""

from __future__ import annotations
import argparse, json, re, time, os, pathlib, sys, unicodedata, hashlib
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Set
from urllib.parse import urljoin, urlparse, parse_qs, urlunparse, urldefrag

import requests
from bs4 import BeautifulSoup, Tag, NavigableString

# ---------------- Patterns ----------------

RX_ANCHOR_ID = re.compile(r"^(?:p|para|par|paragraph|sec|part)[\-_]*([0-9]{1,6})$", re.I)
RX_ANCHOR_NAME = RX_ANCHOR_ID
RX_HASH_TARGET = re.compile(r"#(?:p|para|par|paragraph|sec|part)[\-_]*([0-9]{1,6})", re.I)
RX_NUM_IN_QUERY_KEYS = ("p", "pid", "para", "par", "paragraph", "id", "sec", "part")

DEFAULT_UA = "QuestReader-GHA/1.1 (+https://github.com)"

SKIP_CLASS_TOKENS = ("nav", "breadcrumbs", "footer", "header", "share", "social", "ads", "adblock", "comment")

# ---------------- Data ----------------

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
    url: str  # исходный URL этого параграфа

@dataclass
class Story:
    title: str
    url: str
    paragraphs: List[Paragraph]

# ---------------- Robots ----------------

def robots_allowed(url: str, ua: str) -> bool:
    p = urlparse(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        r = requests.get(robots_url, headers={"User-Agent": ua}, timeout=15)
    except Exception:
        return True
    if r.status_code != 200:
        return True
    allows, disallows = [], []
    applies = False
    for line in r.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("user-agent:"):
            agent = line.split(":",1)[1].strip()
            applies = (agent == "*" or ua.lower().startswith(agent.lower()))
            continue
        if not applies:
            continue
        if line.lower().startswith("allow:"):
            allows.append(line.split(":",1)[1].strip() or "/")
        elif line.lower().startswith("disallow:"):
            disallows.append(line.split(":",1)[1].strip() or "/")
    path = p.path or "/"
    def matches(rule: str) -> bool:
        return path.startswith(rule)
    best_a = max((x for x in allows if matches(x)), key=len, default=None)
    best_d = max((x for x in disallows if matches(x)), key=len, default=None)
    if best_a and best_d:
        return len(best_a) >= len(best_d)
    if best_d and not best_a:
        return False
    return True

# ---------------- Fetch ----------------

def fetch_html(url: str, ua: str, delay: float) -> str:
    time.sleep(max(0.0, delay))
    r = requests.get(url, headers={"User-Agent": ua}, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding or "utf-8"
    text = r.text
    # safety cap
    if len(text) > 5_000_000:
        text = text[:5_000_000]
    return text

# ---------------- Text extract ----------------

def text_of(node: Tag) -> str:
    parts: List[str] = []
    def rec(n):
        if isinstance(n, NavigableString):
            parts.append(str(n)); return
        if not isinstance(n, Tag):
            return
        tag = (n.name or "").lower()
        if tag in ("p","div","section","article","blockquote"):
            before = len(parts)
            for c in n.children: rec(c)
            if len(parts) > before: parts.append("\n\n");
            return
        if tag in ("br","hr"): parts.append("\n"); return
        if tag == "li":
            parts.append("- ");
            for c in n.children: rec(c)
            parts.append("\n");
            return
        if tag.startswith("h") and tag[1:].isdigit():
            parts.append("\n"+n.get_text(strip=True)+"\n"); return
        if tag == "a":
            parts.append(n.get_text(strip=True) or ""); return
        for c in n.children: rec(c)
    rec(node)
    out = "".join(parts)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()

def looks_like_service(el: Tag) -> bool:
    cls = " ".join(el.get("class", [])).lower()
    return any(tok in cls for tok in SKIP_CLASS_TOKENS)

def main_content_candidates(soup: BeautifulSoup) -> List[Tag]:
    # Жадная эвристика: области, где потенциально живёт текст параграфов
    cands = []
    for sel in ("main", "article", "#content", ".content", ".container", "body"):
        el = soup.select_one(sel)
        if el: cands.append(el)
    # плюс общий предок всех якорей (если они есть)
    anchors = list(iter_anchor_nodes(soup))
    if anchors:
        # общий верхний предок
        parent = anchors[0]
        while parent and parent.parent:
            parent = parent.parent
            if parent.find(lambda t: isinstance(t, Tag) and RX_ANCHOR_ID.match(t.get("id","") or "")):
                cands.insert(0, parent)
                break
    # уникальные
    uniq, out = set(), []
    for el in cands:
        if el and id(el) not in uniq:
            uniq.add(id(el)); out.append(el)
    return out or [soup.body or soup]

# ---------------- Anchors/links ----------------

def iter_anchor_nodes(soup: BeautifulSoup):
    for t in soup.find_all(True, id=True):
        if RX_ANCHOR_ID.match(t.get("id","") or ""):
            yield t
    for a in soup.find_all("a", attrs={"name": True}):
        if RX_ANCHOR_NAME.match(a.get("name","") or ""):
            yield a
    for t in soup.find_all(attrs={"data-paragraph": True}):
        v = str(t.get("data-paragraph"))
        if v.isdigit():
            yield t

def collect_until_next(start: Tag, stop_set: Set[Tag]) -> List[Tag]:
    items: List[Tag] = []
    cur = start.next_sibling
    while cur is not None and cur not in stop_set:
        if isinstance(cur, NavigableString):
            if str(cur).strip():
                items.append(cur)
        elif isinstance(cur, Tag):
            if not looks_like_service(cur):
                items.append(cur)
        cur = cur.next_sibling
    return items

def extract_choices(nodes: List[Tag], base: str) -> List[Choice]:
    out: List[Choice] = []
    for n in nodes:
        if isinstance(n, NavigableString):
            continue
        for a in n.find_all("a", href=True):
            href = a["href"].strip()
            txt = a.get_text(strip=True) or href
            lt = txt.lower()
            if lt in {"вверх","наверх","к началу","вернуться","назад"}:
                continue
            full = urljoin(base, href)
            m = RX_HASH_TARGET.search(href)
            target = int(m.group(1)) if m else extract_number_from_url(full)
            out.append(Choice(text=txt, target=target, href=full))
    # dedup by (target,text)
    uniq: Dict[str, Choice] = {}
    for c in out:
        key = f"{c.target}|{c.text}"
        if key not in uniq:
            uniq[key] = c
    return list(uniq.values())

def extract_number_from_url(u: str) -> Optional[int]:
    # пытаемся вытащить номер параграфа из query или последней части пути
    url, frag = urldefrag(u)
    q = parse_qs(urlparse(url).query)
    for k in RX_NUM_IN_QUERY_KEYS:
        if k in q:
            for v in q[k]:
                if v.isdigit():
                    return int(v)
    # из фрагмента уже делали, но на всякий случай
    m = RX_HASH_TARGET.search(frag)
    if m:
        return int(m.group(1))
    # из хвоста пути: /.../123 или -123
    tail = urlparse(url).path.strip("/").split("/")[-1]
    m2 = re.search(r"(\d{1,6})$", tail)
    if m2:
        return int(m2.group(1))
    return None

def is_same_story(root: str, href: str) -> bool:
    # root — корневой префикс истории (например, https://quest-book.ru/online/game17109/)
    url, _ = urldefrag(href)
    # Считаем «своим», если начинается с root (строго)
    return url.startswith(root)

def story_root(url: str) -> str:
    # для https://quest-book.ru/online/game17109/ → оставляем до .../game17109/
    p = urlparse(url)
    parts = p.path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "online":
        # /online/<gameid>/...
        root_path = "/" + "/".join(parts[:2]) + "/"
    else:
        # по умолчанию до каталога
        root_path = "/".join(p.path.split("/")[:-1]) + "/"
        if not root_path.startswith("/"):
            root_path = "/" + root_path
    return urlunparse((p.scheme, p.netloc, root_path, "", "", ""))

# ---------------- Parse single-page ----------------

def parse_single_page(html: str, base_url: str) -> Story:
    soup = BeautifulSoup(html, "lxml")
    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "Без названия"
    anchors = [(int(m.group(1)), tag)
               for tag in iter_anchor_nodes(soup)
               for m in [RX_ANCHOR_ID.match((tag.get("id") or tag.get("name") or tag.get("data-paragraph",""))
                                         .__str__())]
               if m]
    # fallback для data-paragraph
    if not anchors:
        # попытаемся на основе href #p\d+ в контентных зонах
        pass
    # Убираем дубликаты по pid
    by_pid: Dict[int, Tag] = {}
    for pid, tag in anchors:
        if pid not in by_pid:
            by_pid[pid] = tag
    anchors = sorted(by_pid.items(), key=lambda x: x[0])
    paragraphs: List[Paragraph] = []
    if anchors:
        stop = set(tag for _, tag in anchors)
        for pid, node in anchors:
            nodes = collect_until_next(node, stop)
            parts: List[str] = []
            for it in nodes:
                if isinstance(it, NavigableString):
                    t = str(it).strip()
                    if t: parts.append(t)
                else:
                    t = text_of(it)
                    if t: parts.append(t)
            text_joined = "\n\n".join([t for t in parts if t.strip()]).strip()
            choices = extract_choices(nodes, base=base_url)
            paragraphs.append(Paragraph(pid=pid, text=text_joined, choices=choices, url=base_url))
        paragraphs.sort(key=lambda p: p.pid)
    else:
        # не нашли якорей — берём основной контент
        main_nodes = main_content_candidates(soup)
        area = main_nodes[0]
        full = text_of(area)
        paragraphs = [Paragraph(pid=1, text=full.strip(), choices=[], url=base_url)]
    return Story(title=title, url=base_url, paragraphs=paragraphs)

# ---------------- Crawl graph ----------------

def normalize_url(u: str) -> str:
    # убираем фрагмент, нормализуем
    url, _ = urldefrag(u)
    p = urlparse(url)
    # выкидываем лишние параметры сортировки/utm (неизвестно, есть ли они, но на всякий случай)
    query = parse_qs(p.query)
    safe_q = []
    for k, vals in query.items():
        if k.lower().startswith("utm_"):
            continue
        for v in vals:
            safe_q.append((k, v))
    safe_query = "&".join(f"{k}={v}" for k, v in safe_q) if safe_q else ""
    return urlunparse((p.scheme, p.netloc, p.path, "", safe_query, ""))

def synthetic_pid_for_url(u: str) -> int:
    # стабильный синтетический id из URL (если нет числа)
    h = hashlib.sha1(normalize_url(u).encode("utf-8")).hexdigest()
    # берем первые 6 hex как число
    return int(h[:6], 16)

def extract_paragraph_text_and_links(html: str, page_url: str) -> Tuple[str, List[Choice]]:
    soup = BeautifulSoup(html, "lxml")
    # Берём первую подходящую «основную» область
    for area in main_content_candidates(soup):
        # выкинем явные служебные части
        blocks = []
        for child in area.children:
            if isinstance(child, Tag) and looks_like_service(child):
                continue
            blocks.append(child)
        # соберём текст/ссылки
        nodes = []
        for b in blocks:
            nodes.append(b)
        text_parts: List[str] = []
        for it in nodes:
            if isinstance(it, NavigableString):
                t = str(it).strip()
                if t: text_parts.append(t)
            elif isinstance(it, Tag):
                t = text_of(it)
                if t: text_parts.append(t)
        text_joined = "\n\n".join([t for t in text_parts if t.strip()]).strip()
        if text_joined:
            # ссылки берём из всей области
            choices = extract_choices(nodes=[area], base=page_url)
            return text_joined, choices
    # fallback — весь документ
    full = soup.get_text("\n", strip=True)
    return full, []

def parse_crawl(base_url: str, ua: str, delay: float, max_pages: int) -> Story:
    root = story_root(base_url)
    visited: Set[str] = set()
    queue: List[str] = [normalize_url(base_url)]
    url_to_pid: Dict[str, int] = {}
    paragraphs: List[Paragraph] = []
    title_global = None

    while queue and len(visited) < max_pages:
        cur = queue.pop(0)
        if cur in visited:
            continue
        if not is_same_story(root, cur):
            continue
        if not robots_allowed(cur, ua):
            # пропускаем запретные URL
            visited.add(cur)
            continue

        html = fetch_html(cur, ua, delay)
        soup = BeautifulSoup(html, "lxml")
        if title_global is None:
            tt = soup.find("h1") or soup.find("title")
            title_global = tt.get_text(strip=True) if tt else "Без названия"

        # Текст и выборы с текущей страницы
        text, choices = extract_paragraph_text_and_links(html, cur)

        # Определяем pid текущей страницы
        pid = extract_number_from_url(cur)
        if pid is None:
            pid = synthetic_pid_for_url(cur)
        url_to_pid[cur] = pid

        # Нормализуем цели и собираем новые узлы для обхода
        norm_choices: List[Choice] = []
        for ch in choices:
            # оставляем только ссылки в рамках истории
            if not is_same_story(root, ch.href):
                continue
            # целевой pid
            tgt_pid = ch.target
            if tgt_pid is None:
                tgt_pid = extract_number_from_url(ch.href) or synthetic_pid_for_url(ch.href)
            norm_choices.append(Choice(text=ch.text, target=tgt_pid, href=normalize_url(ch.href)))
            # добавим в очередь, если ещё не посещали/не запланировали
            nu = normalize_url(ch.href)
            if nu not in visited and nu not in queue:
                queue.append(nu)

        paragraphs.append(Paragraph(pid=pid, text=text, choices=norm_choices, url=cur))
        visited.add(cur)

    if not title_global:
        title_global = "Без названия"

    # Сортируем: если большинство pid числовые «короткие» — по ним, иначе по порядку обхода
    if paragraphs and all(isinstance(p.pid, int) for p in paragraphs):
        paragraphs.sort(key=lambda p: p.pid)

    return Story(title=title_global, url=base_url, paragraphs=paragraphs)

# ---------------- Formatters ----------------

def to_md(story: Story) -> str:
    lines = [f"# {story.title}\n", f"_Источник: {story.url}_\n"]
    for p in story.paragraphs:
        lines += ["\n---\n", f"## [{p.pid}]\n"]
        if p.text: lines.append(p.text+"\n")
        if p.choices:
            lines.append("\n**Варианты:**\n")
            for c in p.choices:
                if c.href:
                    if c.target is not None:
                        lines.append(f"- [{c.text}]({c.href}) → `{c.target}`")
                    else:
                        lines.append(f"- [{c.text}]({c.href})")
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
                arrow = f"  -> {c.target}" if c.target is not None else ""
                out.append(f" - {c.text}{arrow}\n")
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

# ---------------- Main ----------------

def slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return name or "story"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="URL истории, например https://quest-book.ru/online/game17109/")
    ap.add_argument("--outdir", required=True, help="каталог вывода (например out)")
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    ap.add_argument("--delay", type=float, default=0.7, help="пауза между запросами (сек)")
    ap.add_argument("--domain-lock", action="store_true", help="разрешать только quest-book.ru")
    ap.add_argument("--max-pages", type=int, default=600, help="максимум страниц при обходе")
    ap.add_argument("--mode", choices=["auto","single","crawl"], default="auto",
                    help="auto: определить автоматически; single: якоря на одной странице; crawl: обход ссылок")
    args = ap.parse_args()

    base_url = args.url.strip()
    ua = args.user_agent.strip()

    if args.domain_lock:
        host = urlparse(base_url).netloc.lower()
        if not host.endswith("quest-book.ru"):
            print("Domain lock: разрешён только quest-book.ru", file=sys.stderr)
            sys.exit(2)

    if not robots_allowed(base_url, ua):
        print("robots.txt запрещает доступ к базовому URL", file=sys.stderr)
        sys.exit(3)

    html0 = fetch_html(base_url, ua, delay=0.0)

    # auto detect
    story = None
    if args.mode in ("auto","single"):
        # single-page попытка: считаем количество якорей
        soup0 = BeautifulSoup(html0, "lxml")
        anchors = list(iter_anchor_nodes(soup0))
        if args.mode == "single" or (args.mode == "auto" and len(anchors) >= 5):
            story = parse_single_page(html0, base_url)

    if story is None:
        # crawl-режим
        story = parse_crawl(base_url, ua=ua, delay=args.delay, max_pages=args.max_pages)

    pathlib.Path(args.outdir).mkdir(parents=True, exist_ok=True)
    with open(os.path.join(args.outdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"title": story.title, "url": story.url}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.outdir, "story.txt"), "w", encoding="utf-8") as f:
        f.write(to_txt(story))
    with open(os.path.join(args.outdir, "story.md"), "w", encoding="utf-8") as f:
        f.write(to_md(story))
    with open(os.path.join(args.outdir, "story.json"), "w", encoding="utf-8") as f:
        f.write(to_json(story))
    with open(os.path.join(args.outdir, "story_storyui.json"), "w", encoding="utf-8") as f:
        f.write(to_storyui(story))

if __name__ == "__main__":
    main()
