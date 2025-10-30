const $ = (selector) => document.querySelector(selector);

function detectRepo() {
  const host = window.location.hostname;
  const segments = window.location.pathname.split('/').filter(Boolean);
  let owner = '';
  let repo = '';
  if (host.endsWith('.github.io')) {
    owner = host.replace('.github.io', '');
    repo = segments.length ? segments[0] : '';
  } else if (segments.length >= 2) {
    owner = segments[0];
    repo = segments[1];
  }
  return { owner, repo };
}

function prefilledIssueUrl(owner, repo, url) {
  const title = encodeURIComponent('[scrape] выгрузка');
  const body = encodeURIComponent(`URL: ${url}\n\n(из GitHub Pages)`);
  return `https://github.com/${owner}/${repo}/issues/new?title=${title}&body=${body}`;
}

function workflowUrl(owner, repo) {
  return `https://github.com/${owner}/${repo}/actions/workflows/scrape.yml`;
}

function normalisePath(path) {
  if (!path) return '';
  let result = path.replace(/^docs\//, '').replace(/\\/g, '/');
  while (result.startsWith('/')) {
    result = result.slice(1);
  }
  while (result.endsWith('/')) {
    result = result.slice(0, -1);
  }
  return result;
}

async function loadIndex() {
  try {
    const response = await fetch('./data/index.json', { cache: 'no-store' });
    if (!response.ok) {
      $('#list').textContent = 'Нет выгрузок.';
      return;
    }
    const data = await response.json();
    if (!data.items || !data.items.length) {
      $('#list').textContent = 'Нет выгрузок.';
      return;
    }
    const ul = document.createElement('ul');
    data.items.slice(0, 50).forEach((item) => {
      const li = document.createElement('li');
      const base = normalisePath(item.path);
      const prefix = base ? `${base}/` : '';
      const title = document.createElement('strong');
      title.textContent = item.title || 'Без названия';
      const src = document.createElement('a');
      src.href = item.source_url;
      src.textContent = 'Источник';
      src.target = '_blank';
      src.rel = 'noopener';

      const mkLink = (href, text) => {
        const a = document.createElement('a');
        a.href = `${prefix}${href}`;
        a.textContent = text;
        a.target = '_blank';
        a.rel = 'noopener';
        return a;
      };

      const txtLink = mkLink('story.txt', 'TXT');
      const mdLink = mkLink('story.md', 'MD');
      const jsonLink = mkLink('story.json', 'JSON');
      const jsonUiLink = mkLink('story_storyui.json', 'JSON (storyui)');

      li.append(
        title,
        document.createTextNode(' — '),
        src,
        document.createElement('br'),
        txtLink,
        document.createTextNode(' · '),
        mdLink,
        document.createTextNode(' · '),
        jsonLink,
        document.createTextNode(' · '),
        jsonUiLink,
      );
      ul.appendChild(li);
    });
    $('#list').innerHTML = '';
    $('#list').appendChild(ul);
  } catch (error) {
    console.error('Failed to load index.json', error);
    $('#list').textContent = 'Ошибка загрузки index.json';
  }
}

(function init() {
  const { owner, repo } = detectRepo();
  if (owner && repo) {
    $('#actionsLink').href = workflowUrl(owner, repo);
  } else {
    $('#actionsLink').removeAttribute('href');
    $('#actionsLink').classList.add('disabled');
  }

  $('#issueBtn').addEventListener('click', () => {
    const url = $('#url').value.trim();
    if (!/^https?:\/\/.+/.test(url)) {
      alert('Введите корректный URL');
      return;
    }
    if (!owner || !repo) {
      alert('Не удалось определить репозиторий. Откройте страницу по адресу https://<owner>.github.io/<repo>/');
      return;
    }
    const issueUrl = prefilledIssueUrl(owner, repo, url);
    window.open(issueUrl, '_blank', 'noopener,noreferrer');
  });

  loadIndex();
})();
