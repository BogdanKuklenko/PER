# -*- coding: utf-8 -*-
"""
Скачивает HTML страницы истории с quest-book.ru (если разрешает robots.txt),
извлекает параграфы и переходы, и сохраняет:
- story.txt
- story.md
- story.json
- story_storyui.json  (action_uid/action_goto = "")
Также сохраняет meta.json c {title, url}.
"""

from __future__ import annotations
import argparse, json, re, time, os, pathlib, sys, unicodedata
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag, NavigableString

RX_ID = re.compile(r"^(?:p|para|par|paragraph|sec|part)[\-_]*([0-9]{1,6})$", re.I)
RX_NAME = RX_ID
RX_HASH = re.compile(r"#(?:p|para|par|paragraph|sec|part)[\-_]*([0-9]{1,6})", re.I)

DEFAULT_UA = "QuestReader-GHA/1.0 (+https://github.com)"

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

@dataclass
class Story:
    title: str
    url: str
    paragraphs: List[Paragraph]

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
        if not line or line.startswith("#"): continue
        if line.lower().startswith("user-agent:"):
            agent = line.split(":",1)[1].strip()
            applies = (agent == "*" or ua.lower().startswith(agent.lower()))
            continue
        if not applies: continue
        if line.lower().startswith("allow:"):
            allows.append(line.split(":",1)[1].strip() or "/")
        if line.lower().startswith("disallow:"):
            disallows.append(line.split(":",1)[1].strip() or "/")
    path = p.path or "/"
    def matches(rule: str) -> bool: return path.startswith(rule)
    best_a = max((x for x in allows if matches(x)), key=len, default=None)
    best_d = max((x for x in disallows if matches(x)), key=len, default=None)
    if best_a and best_d: return len(best_a) >= len(best_d)
    if best_d and not best_a: return False
    return True

def fetch_html(url: str, ua: str, delay: float) -> str:
    time.sleep(max(0.0, delay))
    r = requests.get(url, headers={"User-Agent": ua}, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding or "utf-8"
    text = r.text
    if len(text) > 5_000_000:
        text = text[:5_000_000]
    return text

def text_of(node: Tag) -> str:
    parts: List[str] = []
    def rec(n):
        if isinstance(n, NavigableString):
            parts.append(str(n)); return
        if not isinstance(n, Tag): return
        tag = (n.name or "").lower()
        if tag in ("p","div","section","article","blockquote"):
            before = len(parts)
            for c in n.children: rec(c)
            if len(parts)>before: parts.append("\n\n"); return
        if tag in ("br","hr"): parts.append("\n"); return
        if tag == "li":
            parts.append("- "); [rec(c) for c in n.children]; parts.append("\n"); return
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

def iter_anchors(soup: BeautifulSoup) -> List[Tuple[int, Tag]]:
    cand: List[Tuple[int, Tag]] = []
    for t in soup.find_all(True, id=True):
        m = RX_ID.match(t.get("id",""))
        if m: cand.append((int(m.group(1)), t))
    for a in soup.find_all("a", attrs={"name": True}):
        m = RX_NAME.match(a.get("name",""))
        if m: cand.append((int(m.group(1)), a))
    for t in soup.find_all(attrs={"data-paragraph": True}):
        v = str(t.get("data-paragraph"))
        if v.isdigit(): cand.append((int(v), t))
    seen: Dict[int, Tag] = {}
    for pid, el in cand:
        if pid not in seen: seen[pid] = el
    return sorted(seen.items(), key=lambda x: x[0])

def collect_until_next(start: Tag, stops: set) -> List[Tag]:
    items: List[Tag] = []
    cur = start.next_sibling
    while cur is not None and cur not in stops:
        if isinstance(cur, NavigableString):
            if str(cur).strip(): items.append(cur)
        elif isinstance(cur, Tag):
            cls = " ".join(cur.get("class", [])).lower()
            if any(k in cls for k in ("nav","breadcrumbs","footer","header","share","social","ads","adblock")):
                pass
            else:
                items.append(cur)
        cur = cur.next_sibling
    return items

def extract_choices(nodes: List[Tag], base: str) -> List[Choice]:
    out: List[Choice] = []
    for n in nodes:
        if isinstance(n, NavigableString): continue
        for a in n.find_all("a", href=True):
            href = a["href"].strip()
            txt = a.get_text(strip=True) or href
            if txt.lower() in {"вверх","наверх","к началу","вернуться","назад"}:
                continue
            m = RX_HASH.search(href)
            target = int(m.group(1)) if m else None
            full = urljoin(base, href)
            out.append(Choice(text=txt, target=target, href=full))
    uniq: Dict[str, Choice] = {}
    for c in out:
        key = f"{c.target}|{c.text}"
        if key not in uniq: uniq[key] = c
    return list(uniq.values())

def parse_story(html: str, url: str) -> Story:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.find("h1") or soup.find("title"))
    title = title.get_text(strip=True) if title else "Без названия"
    anchors = iter_anchors(soup)
    paragraphs: List[Paragraph] = []
    if anchors:
        stop_nodes = {el for _, el in anchors}
        for pid, el in anchors:
            nodes = collect_until_next(el, stop_nodes)
            pieces: List[str] = []
            for it in nodes:
                if isinstance(it, NavigableString):
                    t = str(it).strip()
                    if t: pieces.append(t)
                else:
                    t = text_of(it)
                    if t: pieces.append(t)
            text = "\n\n".join(pieces).strip()
            choices = extract_choices(nodes, base=url)
            paragraphs.append(Paragraph(pid=pid, text=text, choices=choices))
        paragraphs.sort(key=lambda p: p.pid)
    else:
        body = soup.find("main") or soup.find("article") or soup.find("div", id="content") or soup.body
        full = text_of(body) if body else soup.get_text("\n", strip=True)
        paragraphs = [Paragraph(pid=1, text=full.strip(), choices=[])]
    return Story(title=title, url=url, paragraphs=paragraphs)

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
        out += ["\n"+"="*40+"\n", f"[{p.pid}]\n"]
        if p.text: out.append(p.text+"\n")
        if p.choices:
            out.append("\nВарианты:\n")
            for c in p.choices:
                out.append(f" - {c.text}{'  -> '+str(c.target) if c.target is not None else ''}\n")
    return "".join(out)

def to_json(story: Story) -> str:
    data = {
        "title": story.title,
        "url": story.url,
        "paragraphs": [
            {"id": p.pid, "text": p.text,
             "choices": [{"text": c.text, "target": c.target, "href": c.href} for c in p.choices]}
            for p in story.paragraphs
        ]
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
        ]
    }
    return json.dumps(data, ensure_ascii=False, indent=2)

def slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return name or "story"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--outdir", required=True, help="папка для записи (например docs/data/<slug>-<ts>)")
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--domain-lock", action="store_true")
    args = ap.parse_args()

    u = args.url.strip()
    if args.domain_lock:
        host = urlparse(u).netloc.lower()
        if not host.endswith("quest-book.ru"):
            print("Domain lock: разрешён только quest-book.ru", file=sys.stderr)
            sys.exit(2)

    if not robots_allowed(u, args.user_agent):
        print("robots.txt запрещает доступ", file=sys.stderr)
        sys.exit(3)

    html = fetch_html(u, args.user_agent, args.delay)
    story = parse_story(html, u)

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
