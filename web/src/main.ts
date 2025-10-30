import './style.css';
import { parseStoryContent, parseStoryFromJson, parseIndex, normaliseGraph } from './parser';
import { renderError, renderMessage, renderStory, renderStoryList } from './ui';
import { StoryGraph, StoryIndexEntry } from './types';

const root = document.getElementById('app');
if (!root) {
  throw new Error('Не найден корневой элемент #app');
}

root.innerHTML = `
  <div class="page">
    <header class="page-header">
      <h1>Quest-Book Story Explorer</h1>
      <p class="muted">Просматривайте сторигеймы из архива <code>data/</code> или анализируйте локальные выгрузки.</p>
    </header>
    <main class="page-body">
      <section class="panel panel-controls">
        <h2>Локальный файл</h2>
        <label class="file-picker">
          <span>Выберите HTML/XML/JSON от Quest-Book</span>
          <input type="file" id="local-file" accept=".html,.htm,.xml,.json,.txt" />
        </label>
        <p class="hint">Файл не покидает браузер — парсинг выполняется локально.</p>
        <hr />
        <h2>Архив выгрузок</h2>
        <div id="index-panel" class="index-panel"></div>
      </section>
      <section class="panel panel-view">
        <div id="output" class="output"></div>
      </section>
    </main>
  </div>
`;

const indexContainer = document.getElementById('index-panel') as HTMLElement;
const outputContainer = document.getElementById('output') as HTMLElement;
const fileInput = document.getElementById('local-file') as HTMLInputElement;

renderMessage(outputContainer, 'Выберите источник, чтобы увидеть историю.');
loadIndex();

fileInput.addEventListener('change', async () => {
  const file = fileInput.files?.[0];
  if (!file) {
    return;
  }
  try {
    renderMessage(outputContainer, `Загружаем «${file.name}»...`);
    const text = await file.text();
    const story = parseStoryContent(text);
    story.meta = {
      ...story.meta,
      slug: file.name,
      title: story.meta.title && story.meta.title !== 'Локальный файл' ? story.meta.title : file.name,
      source: `${file.name} (локально)`
    };
    renderStory(outputContainer, story);
  } catch (error) {
    renderError(outputContainer, (error as Error).message);
  } finally {
    fileInput.value = '';
  }
});

async function loadIndex(): Promise<void> {
  try {
    renderMessage(indexContainer, 'Загружаем список...');
    const response = await fetch('data/index.json', { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    const index = parseIndex(payload);
    renderStoryList(indexContainer, index, (entry) => void loadRemoteStory(entry));
    if (!index.stories.length) {
      renderMessage(outputContainer, 'Архив пуст. Запустите workflow scrape, чтобы добавить первую историю.');
    }
  } catch (error) {
    renderError(indexContainer, `Не удалось загрузить data/index.json: ${(error as Error).message}`);
  }
}

async function loadRemoteStory(entry: StoryIndexEntry): Promise<void> {
  const path = entry.paths['storyui'] || entry.paths['json'];
  if (!path) {
    renderError(outputContainer, 'Для записи нет доступного JSON файла.');
    return;
  }
  try {
    renderMessage(outputContainer, `Загружаем ${entry.meta.title}...`);
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const text = await response.text();
    const story = parseStoryFromJson(text);
    const meta = { ...story.meta, title: story.meta.title || entry.meta.title, source: entry.meta.source };
    const normalized: StoryGraph = normaliseGraph({ ...story, meta });
    renderStory(outputContainer, normalized);
  } catch (error) {
    renderError(outputContainer, (error as Error).message);
  }
}
