# QuestBook GitHub-only Scraper

Сайт на GitHub Pages и workflow для выгрузки сторигеймов с [quest-book.ru](https://quest-book.ru).

## Как это работает

1. Включите GitHub Pages: **Settings → Pages → Deploy from a branch → main /docs**.
2. Откройте сайт: `https://<OWNER>.github.io/<REPO>/`.
3. Вставьте URL сторигейма и нажмите «Открыть Issue [scrape]».
4. Подождите выполнения Actions (или запустите вручную в **Actions → Scrape Quest-Book page → Run workflow**).
5. Обновите сайт — появятся ссылки на свежие артефакты (TXT, MD, JSON, JSON storyui).

Workflow уважает `robots.txt` и принудительно ограничен доменом `quest-book.ru`.

## Структура репозитория

```
questbook-gh-only/
  README.md
  LICENSE
  .editorconfig
  .gitignore
  tools/
    scrape.py
  .github/
    workflows/
      scrape.yml
  docs/
    index.html
    main.js
    styles.css
    data/
      index.json
```

## Definition of Done

- Страница на Pages доступна и показывает форму + список выгрузок.
- Создание Issue с URL запускает workflow, который добавляет каталог `docs/data/<slug>-<timestamp>/` с файлами:
  - `story.txt`
  - `story.md`
  - `story.json`
  - `story_storyui.json`
  - `meta.json`
- `docs/data/index.json` пополняется новой записью (prepend).
- Ссылки на файлы кликабельны с Pages.
- Если `robots.txt` запрещает выгрузку, workflow завершается без файлов и без ошибок.
