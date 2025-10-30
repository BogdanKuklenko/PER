export interface StoryMeta {
  slug: string;
  title: string;
  description?: string | null;
  cover?: string | null;
  source: string;
  scrapedAt: string;
  paragraphs: number;
  endings: number;
  start?: string | null;
  tags?: string[];
}

export interface StoryAction {
  label: string;
  target: string;
  css_class?: string | null;
}

export interface StoryNode {
  id: string;
  text: string;
  actions: StoryAction[];
  images: string[];
  isEnding: boolean;
}

export interface StoryGraph {
  meta: StoryMeta;
  start?: string | null;
  nodes: Record<string, StoryNode>;
  links: Array<{ from: string; to: string; label: string }>;
  order?: string[];
}

export interface StoryPayload {
  meta: StoryMeta;
  start?: string | null;
  paragraphs: Array<{
    id: string;
    text: string;
    actions: StoryAction[];
    images: string[];
    is_terminal: boolean;
  }>;
}

export interface StoryIndexEntry {
  meta: StoryMeta;
  paths: Record<string, string>;
}

export interface StoryIndex {
  updatedAt: string | null;
  stories: StoryIndexEntry[];
}

export type ParserSource = 'json' | 'storyui' | 'xml';
