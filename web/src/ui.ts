import { StoryGraph, StoryIndex, StoryIndexEntry } from './types';

export function renderMessage(container: HTMLElement, message: string): void {
  container.innerHTML = `<p class="message">${escapeHtml(message)}</p>`;
}

export function renderError(container: HTMLElement, message: string): void {
  container.innerHTML = `<div class="error">⚠️ ${escapeHtml(message)}</div>`;
}

export function renderStoryList(
  container: HTMLElement,
  index: StoryIndex,
  onSelect: (entry: StoryIndexEntry) => void
): void {
  container.innerHTML = '';
  if (!index.stories.length) {
    renderMessage(container, 'Пока нет выгруженных историй. Запустите workflow, чтобы добавить записи.');
    return;
  }

  if (index.updatedAt) {
    const hint = document.createElement('p');
    hint.className = 'muted';
    const date = new Date(index.updatedAt);
    hint.textContent = `Обновлено: ${date.toLocaleString('ru-RU')}`;
    container.appendChild(hint);
  }

  const list = document.createElement('ul');
  list.className = 'story-list';

  index.stories.forEach((entry) => {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'story-link';
    button.textContent = entry.meta.title || entry.meta.slug;
    button.addEventListener('click', () => onSelect(entry));

    const meta = document.createElement('span');
    meta.className = 'story-meta-inline';
    meta.textContent = `${entry.meta.paragraphs} параграфов • ${entry.meta.endings} концовок`;

    item.appendChild(button);
    item.appendChild(meta);
    list.appendChild(item);
  });

  container.appendChild(list);
}

export function renderStory(container: HTMLElement, story: StoryGraph): void {
  container.innerHTML = '';

  const metaSection = document.createElement('section');
  metaSection.className = 'story-meta-block';
  const meta = story.meta;

  const title = document.createElement('h2');
  title.textContent = meta.title || meta.slug;
  metaSection.appendChild(title);

  const summary = document.createElement('p');
  summary.className = 'muted';
  summary.innerHTML = `Источник: <a href="${meta.source}" target="_blank" rel="noreferrer">${escapeHtml(meta.source)}</a>`;
  metaSection.appendChild(summary);

  if (meta.description) {
    const description = document.createElement('p');
    description.textContent = meta.description;
    metaSection.appendChild(description);
  }

  const stats = document.createElement('p');
  stats.className = 'muted';
  stats.textContent = `${meta.paragraphs} параграфов · ${meta.endings} концовок`;
  metaSection.appendChild(stats);

  if (meta.tags && meta.tags.length) {
    const tags = document.createElement('p');
    tags.className = 'tags';
    tags.innerHTML = meta.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join(' ');
    metaSection.appendChild(tags);
  }

  container.appendChild(metaSection);

  const toc = document.createElement('nav');
  toc.className = 'story-toc';
  toc.innerHTML = '<h3>Содержание</h3>';
  const tocList = document.createElement('ol');
  const order = story.order && story.order.length ? story.order : Object.keys(story.nodes);
  order.forEach((id) => {
    const node = story.nodes[id];
    if (!node) {
      return;
    }
    const item = document.createElement('li');
    const anchor = document.createElement('a');
    anchor.href = `#node-${id}`;
    anchor.textContent = id;
    item.appendChild(anchor);
    if (node.isEnding) {
      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.textContent = 'концовка';
      item.appendChild(badge);
    }
    tocList.appendChild(item);
  });
  toc.appendChild(tocList);
  container.appendChild(toc);

  const nodesContainer = document.createElement('section');
  nodesContainer.className = 'story-nodes';

  order.forEach((id) => {
    const node = story.nodes[id];
    if (!node) {
      return;
    }
    const article = document.createElement('article');
    article.className = 'story-node';
    article.id = `node-${id}`;

    const heading = document.createElement('h3');
    heading.textContent = id;
    article.appendChild(heading);

    if (node.text) {
      const textBlock = document.createElement('p');
      textBlock.className = 'story-text';
      node.text.split('\n').forEach((line, index) => {
        if (index > 0) {
          textBlock.appendChild(document.createElement('br'));
        }
        textBlock.appendChild(document.createTextNode(line));
      });
      article.appendChild(textBlock);
    }

    if (node.images.length) {
      const imagesList = document.createElement('ul');
      imagesList.className = 'story-images';
      node.images.forEach((img) => {
        const li = document.createElement('li');
        const link = document.createElement('a');
        link.href = img;
        link.textContent = img;
        link.target = '_blank';
        link.rel = 'noreferrer';
        li.appendChild(link);
        imagesList.appendChild(li);
      });
      article.appendChild(imagesList);
    }

    if (node.actions.length) {
      const actionsList = document.createElement('ul');
      actionsList.className = 'story-actions';
      node.actions.forEach((action) => {
        const li = document.createElement('li');
        const link = document.createElement('a');
        link.href = `#node-${action.target}`;
        link.textContent = action.label || action.target;
        li.appendChild(link);
        actionsList.appendChild(li);
      });
      article.appendChild(actionsList);
    } else {
      const ending = document.createElement('p');
      ending.className = 'muted';
      ending.textContent = 'Концовка';
      article.appendChild(ending);
    }

    nodesContainer.appendChild(article);
  });

  container.appendChild(nodesContainer);
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => {
    switch (char) {
      case '&':
        return '&amp;';
      case '<':
        return '&lt;';
      case '>':
        return '&gt;';
      case '"':
        return '&quot;';
      case '\'':
        return '&#39;';
      default:
        return char;
    }
  });
}
