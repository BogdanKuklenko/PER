import { StoryAction, StoryGraph, StoryIndex, StoryIndexEntry, StoryNode, StoryPayload, StoryMeta } from './types';

export function normaliseWhitespace(value: string): string {
  const lines = value
    .replace(/\r\n?/g, '\n')
    .replace(/\u00a0/g, ' ')
    .replace(/\t/g, ' ')
    .split('\n')
    .map((line) => line.replace(/\s+/g, (segment) => (segment.includes('\n') ? segment : ' ')).trim());

  const collapsed: string[] = [];
  for (const line of lines) {
    if (line) {
      collapsed.push(line);
    } else if (collapsed.length && collapsed[collapsed.length - 1] !== '') {
      collapsed.push('');
    }
  }
  return collapsed.join('\n').trim();
}

export function parseStoryFromJson(text: string): StoryGraph {
  const data = JSON.parse(text);
  if (isStoryGraph(data)) {
    return normaliseGraph(data);
  }
  if (isStoryPayload(data)) {
    return graphFromPayload(data);
  }
  throw new Error('JSON не похож на экспорт сторигейма.');
}

export function parseStoryFromXml(xml: string): StoryGraph {
  const parser = new DOMParser();
  const document = parser.parseFromString(xml, 'application/xml');
  const errorNode = document.querySelector('parsererror');
  if (errorNode) {
    throw new Error(`Ошибка парсинга XML: ${errorNode.textContent || 'неизвестно'}`);
  }

  const articles = Array.from(document.getElementsByTagName('article'));
  const nodes: Record<string, StoryNode> = {};
  const order: string[] = [];
  const links: Array<{ from: string; to: string; label: string }> = [];

  for (const article of articles) {
    const id = article.getAttribute('id');
    if (!id) {
      continue;
    }
    order.push(id);

    const textEl = Array.from(article.children).find((child) => child.tagName.toLowerCase() === 'text');
    const text = textEl ? normaliseWhitespace(textEl.textContent ?? '') : '';

    const actions: StoryAction[] = [];
    const images: string[] = [];

    for (const child of Array.from(article.children)) {
      const tag = child.tagName.toLowerCase();
      if (tag === 'action') {
        const action: StoryAction = {
          label: normaliseWhitespace(child.textContent ?? ''),
          target: child.getAttribute('goto') ?? '',
          css_class: child.getAttribute('class'),
        };
        if (action.target) {
          actions.push(action);
          links.push({ from: id, to: action.target, label: action.label });
        }
      }
      if (tag === 'img' && child.textContent) {
        const value = child.textContent.trim();
        if (value) {
          images.push(value);
        }
      }
    }

    nodes[id] = {
      id,
      text,
      actions,
      images,
      isEnding: actions.length === 0,
    };
  }

  const start = order.find((item) => item !== 'mitril') ?? order[0] ?? null;
  const endings = Object.values(nodes).filter((node) => node.isEnding).length;
  const meta: StoryMeta = {
    slug: document.documentElement.getAttribute('guid') ?? 'local-story',
    title: document.documentElement.getAttribute('title') ?? 'Локальный файл',
    description: null,
    cover: null,
    source: 'local-file',
    scrapedAt: new Date().toISOString(),
    paragraphs: order.length,
    endings,
    start,
    tags: [],
  };

  return { meta, start, nodes, links, order };
}

export function parseStoryContent(content: string): StoryGraph {
  const trimmed = content.trim();
  if (!trimmed) {
    throw new Error('Файл пустой.');
  }
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    return parseStoryFromJson(trimmed);
  }
  if (trimmed.startsWith('<')) {
    return normaliseGraph(parseStoryFromXml(trimmed));
  }
  throw new Error('Не удалось определить формат файла. Поддерживаются JSON и XML.');
}

export function normaliseGraph(graph: StoryGraph): StoryGraph {
  const nodes: Record<string, StoryNode> = {};
  const order: string[] = [];
  const knownOrder = Array.isArray(graph.order) ? graph.order.slice() : [];
  const seen = new Set<string>();

  const entries = Object.entries(graph.nodes ?? {});
  if (knownOrder.length === 0) {
    knownOrder.push(...entries.map(([id]) => id));
  } else {
    for (const [id] of entries) {
      if (!knownOrder.includes(id)) {
        knownOrder.push(id);
      }
    }
  }

  for (const id of knownOrder) {
    const node = graph.nodes[id];
    if (!node) {
      continue;
    }
    const actions = Array.isArray(node.actions)
      ? node.actions.map((action) => ({
          label: action.label ?? '',
          target: action.target,
          css_class: action.css_class ?? null,
        }))
      : [];
    const images = Array.isArray(node.images) ? node.images.slice() : [];
    nodes[id] = {
      id,
      text: node.text ?? '',
      actions,
      images,
      isEnding: typeof node.isEnding === 'boolean' ? node.isEnding : actions.length === 0,
    };
    order.push(id);
    seen.add(id);
  }

  const links = Array.isArray(graph.links)
    ? graph.links.map((link) => ({
        from: link.from,
        to: link.to,
        label: link.label ?? '',
      }))
    : [];

  const meta = ensureMeta(graph.meta, nodes, order, graph.start);
  const start = graph.start ?? meta.start ?? order.find((id) => id !== 'mitril') ?? order[0] ?? null;

  return { meta, nodes, links, order, start };
}

export function graphFromPayload(payload: StoryPayload): StoryGraph {
  const nodes: Record<string, StoryNode> = {};
  const links: Array<{ from: string; to: string; label: string }> = [];
  const order: string[] = [];

  for (const paragraph of payload.paragraphs) {
    const actions = Array.isArray(paragraph.actions)
      ? paragraph.actions.map((action) => ({
          label: action.label ?? '',
          target: action.target,
          css_class: action.css_class ?? null,
        }))
      : [];
    const images = Array.isArray(paragraph.images) ? paragraph.images.slice() : [];
    const id = paragraph.id;
    if (!id) {
      continue;
    }
    order.push(id);
    nodes[id] = {
      id,
      text: paragraph.text ?? '',
      actions,
      images,
      isEnding: paragraph.is_terminal ?? actions.length === 0,
    };
    for (const action of actions) {
      if (action.target) {
        links.push({ from: id, to: action.target, label: action.label });
      }
    }
  }

  const meta = ensureMeta(payload.meta, nodes, order, payload.start);
  const start = payload.start ?? meta.start ?? order.find((id) => id !== 'mitril') ?? order[0] ?? null;
  return { meta, nodes, links, order, start };
}

export function parseIndex(data: StoryIndex): StoryIndex {
  const stories = Array.isArray(data.stories) ? data.stories : [];
  return {
    updatedAt: data.updatedAt ?? null,
    stories: stories
      .filter((entry): entry is StoryIndexEntry => Boolean(entry && entry.meta && entry.paths))
      .sort((a, b) => a.meta.title.localeCompare(b.meta.title, 'ru')),
  };
}

function isStoryGraph(value: unknown): value is StoryGraph {
  return Boolean(value && typeof value === 'object' && 'nodes' in value);
}

function isStoryPayload(value: unknown): value is StoryPayload {
  return Boolean(value && typeof value === 'object' && 'paragraphs' in value);
}

function ensureMeta(meta: StoryMeta | undefined, nodes: Record<string, StoryNode>, order: string[], start?: string | null): StoryMeta {
  const now = new Date().toISOString();
  const endings = Object.values(nodes).filter((node) => node.isEnding).length;
  if (meta) {
    return {
      slug: meta.slug,
      title: meta.title || meta.slug || 'Без названия',
      description: meta.description ?? null,
      cover: meta.cover ?? null,
      source: meta.source || 'local',
      scrapedAt: meta.scrapedAt || now,
      paragraphs: meta.paragraphs ?? order.length,
      endings: meta.endings ?? endings,
      start: meta.start ?? start ?? order.find((id) => id !== 'mitril') ?? order[0] ?? null,
      tags: Array.isArray(meta.tags) ? meta.tags : [],
    };
  }
  return {
    slug: 'local',
    title: 'Локальный файл',
    description: null,
    cover: null,
    source: 'local',
    scrapedAt: now,
    paragraphs: order.length,
    endings,
    start: start ?? order.find((id) => id !== 'mitril') ?? order[0] ?? null,
    tags: [],
  };
}
