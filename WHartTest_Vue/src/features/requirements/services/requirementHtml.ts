import type { ObjectDirective } from 'vue';
import { request } from '@/utils/request';

// Only these same-origin API paths may receive the current user's credentials.
const imagePath = /^\/api(\/requirements\/documents\/[0-9a-f-]{36}\/images\/[\w-]+\/)$/i;
const previews = new WeakMap<HTMLElement, { controller: AbortController; urls: string[] }>();

function releasePreviews(element: HTMLElement) {
  const previewsForElement = previews.get(element);
  if (!previewsForElement) return;
  previewsForElement.controller.abort();
  previewsForElement.urls.forEach(url => URL.revokeObjectURL(url));
  previews.delete(element);
}

// Input is the sanitized Markdown HTML from the document or chat renderer.
function render(element: HTMLElement, html: string) {
  releasePreviews(element);
  if (!html.includes('/requirements/documents/')) {
    element.innerHTML = html;
    return;
  }

  const template = document.createElement('template');
  template.innerHTML = html;
  const state = { controller: new AbortController(), urls: [] as string[] };
  previews.set(element, state);
  for (const img of template.content.querySelectorAll('img[src]')) {
    const src = img.getAttribute('src') || '';
    const path = imagePath.exec(src)?.[1];
    if (!path) continue;
    img.removeAttribute('src');
    request<Blob>({
      url: path, method: 'GET', responseType: 'blob', signal: state.controller.signal,
    }).then(result => {
      if (!result.success || !(result.data instanceof Blob) || state.controller.signal.aborted) return;
      const url = URL.createObjectURL(result.data);
      state.urls.push(url);
      img.setAttribute('src', url);
    });
  }
  element.replaceChildren(template.content);
}

export const vRequirementHtml: ObjectDirective<HTMLElement, string> = {
  mounted(element, { value }) { render(element, value); },
  updated(element, { value, oldValue }) {
    if (value !== oldValue) render(element, value);
  },
  beforeUnmount: releasePreviews,
};
