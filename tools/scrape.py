#!/usr/bin/env python3
"""Utilities for scraping Quest-Book stories.

The module can be executed as a script.  Example::

    python tools/scrape.py --story game18149

The script downloads the Quest-Book "storygame" page, checks ``robots.txt``
permissions, extracts paragraphs and transitions from the story data feed and
emits a bundle of artifacts under the ``data/`` directory:

* ``story.json`` – structured story graph with raw text;
* ``story_storyui.json`` – compact graph tuned for the SPA viewer;
* ``story.md`` / ``story.txt`` – human readable dumps;
* ``meta.json`` – metadata summary for cataloguing.

The script also maintains ``data/index.json`` which lists every collected story
and the relative paths to its artifacts.  The implementation favours clarity
and extensive logging so it can run unattended inside GitHub Actions.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET


QUEST_BOOK_BASE = "https://quest-book.ru"
DEFAULT_USER_AGENT = "PER-scraper/1.0 (+https://github.com/PER)"
DATA_FILENAMES = {
    "markdown": "story.md",
    "text": "story.txt",
    "json": "story.json",
    "storyui": "story_storyui.json",
    "meta": "meta.json",
}
INDEX_FILE = Path("data/index.json")


class ScrapeError(RuntimeError):
    """Custom error signalling unrecoverable scraping issues."""


@dataclass
class Action:
    """A single transition that can be taken from a paragraph."""

    label: str
    target: str
    css_class: Optional[str] = None


@dataclass
class Paragraph:
    """A Quest-Book paragraph ("article" in their terminology)."""

    pid: str
    text: str
    actions: List[Action] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    raw_html: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return not self.actions


@dataclass
class Story:
    """Full story graph along with metadata."""

    slug: str
    source_url: str
    title: str
    description: Optional[str]
    cover_image: Optional[str]
    scraped_at: datetime
    paragraphs: List[Paragraph]
    tags: List[str] = field(default_factory=list)

    @property
    def paragraph_count(self) -> int:
        return len(self.paragraphs)

    @property
    def ending_count(self) -> int:
        return sum(1 for p in self.paragraphs if p.is_terminal)

    @property
    def start_id(self) -> Optional[str]:
        for paragraph in self.paragraphs:
            if paragraph.pid != "mitril":
                return paragraph.pid
        return self.paragraphs[0].pid if self.paragraphs else None


def log(message: str) -> None:
    """Simple logger for deterministic workflow output."""

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{timestamp}] {message}")


def normalise_slug(raw_value: str) -> str:
    """Extract Quest-Book slug (e.g. ``game123``) from user input."""

    value = raw_value.strip()
    if not value:
        raise ScrapeError("Story slug or URL is empty")

    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        match = re.search(r"(game\\d+)", parsed.path)
        if not match:
            raise ScrapeError(f"Cannot determine story slug from URL: {value}")
        value = match.group(1)

    if not re.fullmatch(r"game\\d+", value):
        raise ScrapeError(
            "Story must be provided as a slug like 'game18149' "
            "or a full Quest-Book URL"
        )
    return value


class QuestBookScraper:
    """Stateful scraper that respects ``robots.txt`` directives."""

    def __init__(
        self,
        slug: str,
        output_root: Path,
        *,
        base_url: str = QUEST_BOOK_BASE,
        user_agent: str = DEFAULT_USER_AGENT,
        skip_robots: bool = False,
    ) -> None:
        self.slug = slug
        self.base_url = base_url.rstrip("/")
        self.story_url = urljoin(self.base_url + "/", f"online/{slug}")
        self.data_url = urljoin(self.base_url + "/", f"online/{slug}/data/index.json")
        self.output_root = output_root
        self.user_agent = user_agent
        self.skip_robots = skip_robots
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})
        self._robots: Optional[RobotFileParser] = None
        self._last_request: Optional[float] = None

    # ------------------------------------------------------------------
    # Networking helpers
    # ------------------------------------------------------------------
    def _load_robots(self) -> RobotFileParser:
        if self._robots is not None:
            return self._robots
        robots_url = urljoin(self.base_url + "/", "robots.txt")
        log(f"Loading robots.txt from {robots_url}")
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.read()
        self._robots = parser
        return parser

    def _wait_for_crawl_delay(self) -> None:
        if self._last_request is None:
            return
        delay = 0.0
        if self._robots is not None:
            recorded = self._robots.crawl_delay(self.user_agent)
            if recorded is None:
                recorded = self._robots.crawl_delay("*")
            delay = float(recorded or 0)
        if delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        wait_for = delay - elapsed
        if wait_for > 0:
            log(f"Respecting crawl delay ({delay:.1f}s) – sleeping {wait_for:.2f}s")
            time.sleep(wait_for)

    def _check_allowed(self, url: str) -> None:
        if self.skip_robots:
            return
        robots = self._load_robots()
        if not robots.can_fetch(self.user_agent, url):
            raise ScrapeError(f"Robots.txt forbids fetching {url}")

    def _fetch(self, url: str) -> requests.Response:
        self._check_allowed(url)
        self._wait_for_crawl_delay()
        log(f"Fetching {url}")
        response = self.session.get(url, timeout=60)
        self._last_request = time.monotonic()
        if response.status_code != 200:
            raise ScrapeError(f"Failed to fetch {url} (HTTP {response.status_code})")
        return response

    # ------------------------------------------------------------------
    # Scraping logic
    # ------------------------------------------------------------------
    def scrape(self) -> Story:
        html_response = self._fetch(self.story_url)
        html_response.encoding = html_response.encoding or "windows-1251"
        html_text = html_response.text
        soup = BeautifulSoup(html_text, "html.parser")

        title_raw = (soup.find("title") or {}).get_text(strip=True) if soup.find("title") else None
        title = (title_raw or self.slug).split("/")[0].strip() or self.slug

        cover_image = None
        meta_cover = soup.find("meta", attrs={"property": "og:image"})
        if meta_cover and meta_cover.get("content"):
            cover_image = meta_cover["content"].strip()

        description = None
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            description = meta_desc["content"].strip()

        data_response = self._fetch(self.data_url)
        data_response.encoding = data_response.encoding or "utf-8"
        xml_text = data_response.text

        paragraphs = self._parse_paragraphs(xml_text)
        if not paragraphs:
            raise ScrapeError("Story does not contain any paragraphs")

        # If description is missing fall back to the first paragraph.
        if not description:
            for paragraph in paragraphs:
                if paragraph.pid != "mitril" and paragraph.text:
                    description = paragraph.text.strip()
                    break

        tags = self._extract_tags(xml_text)
        story = Story(
            slug=self.slug,
            source_url=self.story_url,
            title=title,
            description=description,
            cover_image=cover_image,
            scraped_at=datetime.now(timezone.utc),
            paragraphs=paragraphs,
            tags=tags,
        )
        return story

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------
    def _parse_paragraphs(self, xml_text: str) -> List[Paragraph]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ScrapeError(f"Unable to parse story XML: {exc}") from exc

        paragraphs: List[Paragraph] = []
        for article in root.findall(".//article"):
            pid = article.get("id")
            if not pid:
                continue
            text_el = article.find("text")
            raw_html = None
            text = ""
            if text_el is not None:
                raw_html = ET.tostring(text_el, encoding="unicode", method="html")
                text = "".join(text_el.itertext())
                text = normalise_whitespace(text)

            actions: List[Action] = []
            for action_el in article.findall("action"):
                label = normalise_whitespace(action_el.text or "")
                target = action_el.get("goto") or ""
                css_class = action_el.get("class")
                if target:
                    actions.append(Action(label=label, target=target, css_class=css_class))

            images = [img.text.strip() for img in article.findall("img") if img.text and img.text.strip()]
            paragraphs.append(Paragraph(pid=pid, text=text, actions=actions, images=images, raw_html=raw_html))
        return paragraphs

    def _extract_tags(self, xml_text: str) -> List[str]:
        # Extract the JSON literal passed into the mitril setup function.
        match = re.search(r"}\)\((\{.*?\})\);", xml_text, re.DOTALL)
        if not match:
            return []
        try:
            raw = match.group(1)
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        tags = data.get("tags") or []
        if isinstance(tags, list):
            return [str(tag) for tag in tags]
        return []


def normalise_whitespace(value: str) -> str:
    """Collapse consecutive whitespace while preserving intentional line breaks."""

    # Replace CR/LF combos with ``\n``
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    # Normalise NBSP and tabs
    value = value.replace("\xa0", " ").replace("\t", " ")
    # Collapse duplicate spaces on each line
    lines = [re.sub(r" +", " ", line).strip() for line in value.split("\n")]
    # Remove consecutive blank lines
    collapsed: List[str] = []
    for line in lines:
        if line:
            collapsed.append(line)
        elif collapsed and collapsed[-1] != "":
            collapsed.append("")
    return "\n".join(collapsed).strip()


def story_to_json(story: Story) -> Dict[str, object]:
    return {
        "meta": story_meta_dict(story),
        "start": story.start_id,
        "paragraphs": [
            {
                "id": p.pid,
                "text": p.text,
                "actions": [dataclasses.asdict(a) for a in p.actions],
                "images": p.images,
                "raw_html": p.raw_html,
                "is_terminal": p.is_terminal,
            }
            for p in story.paragraphs
        ],
    }


def story_to_storyui(story: Story) -> Dict[str, object]:
    nodes: Dict[str, Dict[str, object]] = {}
    links: List[Dict[str, object]] = []
    for paragraph in story.paragraphs:
        nodes[paragraph.pid] = {
            "id": paragraph.pid,
            "text": paragraph.text,
            "actions": [dataclasses.asdict(a) for a in paragraph.actions],
            "images": paragraph.images,
            "isEnding": paragraph.is_terminal,
        }
        for action in paragraph.actions:
            links.append({
                "from": paragraph.pid,
                "to": action.target,
                "label": action.label,
            })
    return {
        "meta": story_meta_dict(story),
        "start": story.start_id,
        "nodes": nodes,
        "links": links,
        "order": [p.pid for p in story.paragraphs],
    }


def story_meta_dict(story: Story) -> Dict[str, object]:
    return {
        "slug": story.slug,
        "title": story.title,
        "description": story.description,
        "cover": story.cover_image,
        "source": story.source_url,
        "scrapedAt": story.scraped_at.isoformat(),
        "paragraphs": story.paragraph_count,
        "endings": story.ending_count,
        "start": story.start_id,
        "tags": story.tags,
    }


def render_markdown(story: Story) -> str:
    lines = [f"# {story.title}"]
    if story.description:
        lines.append("")
        lines.append(story.description)
    lines.append("")
    lines.append(f"Источник: {story.source_url}")
    lines.append(f"Сборка: {story.scraped_at.isoformat()}")
    lines.append("")
    for paragraph in story.paragraphs:
        lines.append(f"## {paragraph.pid}")
        if paragraph.text:
            lines.append("")
            lines.extend(paragraph.text.split("\n"))
            lines.append("")
        if paragraph.images:
            lines.append("Изображения:")
            for image in paragraph.images:
                lines.append(f"- {image}")
            lines.append("")
        if paragraph.actions:
            lines.append("Переходы:")
            for action in paragraph.actions:
                label = action.label or "Продолжить"
                lines.append(f"- [{label}](#{action.target}) → {action.target}")
        else:
            lines.append("(Концовка)")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_text(story: Story) -> str:
    lines = [story.title, "=" * len(story.title), ""]
    if story.description:
        lines.append(story.description)
        lines.append("")
    for paragraph in story.paragraphs:
        lines.append(f"[{paragraph.pid}]")
        if paragraph.text:
            lines.extend(paragraph.text.split("\n"))
        if paragraph.images:
            lines.append("Images:")
            for image in paragraph.images:
                lines.append(f"  - {image}")
        if paragraph.actions:
            lines.append("Choices:")
            for action in paragraph.actions:
                label = action.label or "Продолжить"
                lines.append(f"  * {label} -> {action.target}")
        else:
            lines.append("  * Конец истории")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def save_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    log(f"Saved {path}")


def save_json(path: Path, payload: Dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"Saved {path}")


def update_index(index_path: Path, story: Story, story_dir: Path) -> None:
    ensure_directory(index_path.parent)
    if index_path.exists():
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
    else:
        index_data = {"updatedAt": None, "stories": []}

    stories = index_data.get("stories")
    if not isinstance(stories, list):
        stories = []

    paths = {
        name: str((story_dir / filename).as_posix())
        for name, filename in DATA_FILENAMES.items()
    }

    entry = {"meta": story_meta_dict(story), "paths": paths}
    updated = False
    for idx, existing in enumerate(stories):
        if isinstance(existing, dict):
            slug = existing.get("meta", {}).get("slug") if isinstance(existing.get("meta"), dict) else existing.get("slug")
            if slug == story.slug:
                stories[idx] = entry
                updated = True
                break
    if not updated:
        stories.append(entry)

    stories.sort(key=lambda item: item.get("meta", {}).get("title", ""))
    index_data["stories"] = stories
    index_data["updatedAt"] = datetime.now(timezone.utc).isoformat()

    save_json(index_path, index_data)


def write_story_artifacts(story: Story, output_root: Path) -> Path:
    story_dir = output_root / story.slug
    ensure_directory(story_dir)

    save_json(story_dir / DATA_FILENAMES["json"], story_to_json(story))
    save_json(story_dir / DATA_FILENAMES["storyui"], story_to_storyui(story))
    save_json(story_dir / DATA_FILENAMES["meta"], story_meta_dict(story))
    save_text(story_dir / DATA_FILENAMES["markdown"], render_markdown(story))
    save_text(story_dir / DATA_FILENAMES["text"], render_text(story))
    return story_dir


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Quest-Book stories")
    parser.add_argument("--story", required=True, help="Quest-Book slug (game123) or URL")
    parser.add_argument("--output", default="data", help="Directory for story artifacts")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="Custom User-Agent header")
    parser.add_argument("--skip-robots", action="store_true", help="Ignore robots.txt (use with caution)")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    try:
        slug = normalise_slug(args.story)
        output_root = Path(args.output)
        ensure_directory(output_root)
        scraper = QuestBookScraper(
            slug=slug,
            output_root=output_root,
            base_url=QUEST_BOOK_BASE,
            user_agent=args.user_agent,
            skip_robots=args.skip_robots,
        )
        story = scraper.scrape()
        story_dir = write_story_artifacts(story, output_root)
        update_index(INDEX_FILE, story, story_dir)
        log("Scraping completed successfully")
        return 0
    except ScrapeError as exc:
        log(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
