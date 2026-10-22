/**
 * An HTML deliverable (a storyboard, a report) that the agent wrote with its images embedded as presigned S3 URLs
 * goes blank a few hours later: the links expire, and nothing the page can do brings them back. The images are
 * project files in their own right, so at VIEW time we re-link them: every S3 URL in the page is resolved to the
 * project asset registered at it (`resolve-locations`) and replaced by a fresh view-time URL. What cannot be
 * resolved is left as it was.
 *
 * The result is shown through `srcdoc` in a SANDBOXED frame (scripts allowed, never same-origin), so the page can
 * neither read this app's session nor reach its API.
 */
import { useEffect, useState } from 'react';
import { resolveLocations } from './api';
import { request as requestAssetUrls } from './assetUrlBatcher';

/** `https://<bucket>.s3[.<region>].amazonaws.com/<key>?…` in attribute values and CSS `url()`. */
const S3_URL = /https:\/\/[a-z0-9.-]+\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com\/[^"'\s)<>]+/gi;

const decode = (u: string) => u.replace(/&amp;/g, '&');

/** `true` for a document the preview should re-link (by name or a text/html source). */
export function isHtmlName(name: string): boolean {
  return /\.html?$/i.test(name);
}

/** The page with every resolvable S3 URL replaced by a fresh one. Never throws; unresolved URLs stay. */
export async function refreshEmbeddedUrls(html: string, projectId: string): Promise<string> {
  const found = [...new Set(html.match(S3_URL) ?? [])];
  if (found.length === 0) return html;
  const locations = found.map(decode);
  const resolved = await resolveLocations(projectId, locations.slice(0, 100));
  if (!resolved.ok) return html;
  const ids = [...new Set(Object.values(resolved.value).filter((v): v is string => !!v))];
  if (ids.length === 0) return html;
  const fresh = await requestAssetUrls(projectId, ids);
  let out = html;
  found.forEach((raw, i) => {
    const id = resolved.value[locations[i]];
    const hit = id ? fresh.get(id) : undefined;
    if (hit?.ok) out = out.split(raw).join(hit.entry.source.url.replace(/&/g, '&amp;'));
  });
  return out;
}

/**
 * The re-linked page for an HTML file at `src` (`null` while loading, when not HTML, or when it cannot be fetched —
 * the caller then shows `src` as before).
 */
export function useRefreshedHtml(src: string | null | undefined, name: string, projectId: string | null | undefined): string | null {
  const [doc, setDoc] = useState<string | null>(null);
  useEffect(() => {
    setDoc(null);
    if (!src || !projectId || !isHtmlName(name)) return;
    let live = true;
    void (async () => {
      try {
        const resp = await fetch(src);
        if (!resp.ok) return;
        const html = await refreshEmbeddedUrls(await resp.text(), projectId);
        if (live) setDoc(html);
      } catch { /* not fetchable (CORS, offline): the plain frame shows it as before */ }
    })();
    return () => { live = false; };
  }, [src, name, projectId]);
  return doc;
}
