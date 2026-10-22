import { invokeAgentStreaming, fetchModels, setAgentModel } from '../api';
import type { StreamCallback } from '../api';
import type { RenderHistoryEntry } from '../components/renderer/types';
import { getAccessToken } from '../auth/authManager';

export interface ChatOptions {
  sessionId: string;
  onStep?: (step: { status: string; label: string; duration_ms?: number }) => void;
  onText?: (text: string) => void;
  onSummary?: (summary: { total_duration_ms: number; cycles: number }) => void;
  onRenderBlock?: (entry: RenderHistoryEntry) => void;
}

// Artifact types the Render Agent visualizes richly (NLE timeline).
// Rendering is now dispatched by the Main Agent to the Render Agent; its
// output_ref (manifest S3 key) is forwarded to the UI as a `render` SSE
// event. The frontend no longer scans response text for media URLs — that
// client-side regex path was retired in favor of the designed dispatch flow.
const RENDERABLE_EXTENSIONS = new Set(['mp4', 'webm', 'mov', 'mp3', 'wav', 'flac']);

export { RENDERABLE_EXTENSIONS };

/**
 * Raised by {@link AgentService.upload} when the proxy rejects a file for exceeding
 * its upload size cap (HTTP 413). Carries the server's `maxBytes` so the UI can show
 * a specific limit ("max 2 GB") rather than a generic failure.
 */
export class UploadTooLargeError extends Error {
  readonly code = 'FILE_TOO_LARGE';
  constructor(public readonly maxBytes: number) {
    super('File exceeds the maximum upload size');
    this.name = 'UploadTooLargeError';
  }
}

export class AgentService {
  static async chat(prompt: string, opts: ChatOptions): Promise<string> {
    let fullText = '';

    const callback: StreamCallback = (event) => {
      switch (event.type) {
        case 'step':
          opts.onStep?.(event.step);
          break;
        case 'text':
          fullText += event.text;
          opts.onText?.(event.text);
          break;
        case 'execution_summary':
          opts.onSummary?.(event.summary);
          break;
        case 'render':
          // Render Agent manifest (dispatched by the main agent) — fetch
          // same-origin (CloudFront lacks CORS for JSON) and render the NLE
          // timeline. This is the designed output_ref → UI render path.
          opts.onRenderBlock?.({
            turnIndex: 0,
            timestamp: Date.now(),
            blocks: [{
              template: 'manifest-router',
              title: event.label || 'Render Output',
              data: { manifestUrl: `/api/artifact?key=${encodeURIComponent(event.manifestKey)}` },
            }],
          });
          break;
      }
    };

    await invokeAgentStreaming({ prompt, session_id: opts.sessionId }, callback);
    return fullText;
  }

  /**
   * Upload a file for the current turn.
   *
   * `sessionId` decides WHERE the object lands: with it, `artifacts/<session>/input/...`;
   * without it, the uploader's own session-less `artifacts/uploads/<owner tag>/input/...`. Both keep the `/input/`
   * segment that marks the object as a source rather than a deliverable (see
   * `server/uploadKey.ts`). Pass it whenever a session exists — it is optional only so a
   * pre-session upload still succeeds.
   */
  static async upload(file: File, sessionId?: string, onProgress?: (fraction: number) => void, signal?: AbortSignal): Promise<{
    key: string;
    size: number;
    bucket: string;
    url: string;
    cloudfrontUrl: string;
    filename: string;
  }> {
    // XHR, not fetch: only `XMLHttpRequest.upload` exposes real upload progress
    // events (fetch cannot observe request-body progress), which the chat surface
    // needs to show a live "Uploading… %" bar for large media files.
    //
    // Because this bypasses `authFetch`, the access token must be attached BY HAND —
    // `/upload` is authenticated now (it writes to S3 and returns a public CloudFront
    // URL, so an anonymous POST was arbitrary content hosting), and the proxy derives
    // the object key from the validated actor.
    const accessToken = await getAccessToken();
    return new Promise((resolve, reject) => {
      const formData = new FormData();
      // Field order matters for nothing to busboy, but the session goes first so the
      // proxy has it before the (slow) file part and can scope the object's key.
      if (sessionId) formData.append('session_id', sessionId);
      formData.append('file', file);

      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/upload');
      if (accessToken) xhr.setRequestHeader('Authorization', `Bearer ${accessToken}`);
      xhr.responseType = 'json';

      if (onProgress) {
        xhr.upload.onprogress = (e: ProgressEvent) => {
          if (e.lengthComputable && e.total > 0) onProgress(Math.min(1, e.loaded / e.total));
        };
      }

      xhr.onload = () => {
        const body = (xhr.response ?? {}) as { error?: string; maxBytes?: number } & Record<string, unknown>;
        if (xhr.status >= 200 && xhr.status < 300) {
          if (onProgress) onProgress(1);
          resolve(body as never);
          return;
        }
        // The proxy streams to S3 and rejects anything over its cap with a 413 +
        // structured body — surface it as a typed error the UI can render cleanly.
        if (xhr.status === 413) {
          reject(new UploadTooLargeError(Number(body?.maxBytes) || 0));
          return;
        }
        reject(new Error(body?.error ?? `Upload failed: ${xhr.status}`));
      };
      xhr.onerror = () => reject(new Error('Upload failed: network error'));
      xhr.onabort = () => reject(new Error('Upload cancelled'));

      // Cancel (the chat upload sheet): abort the request; `onabort` rejects.
      if (signal) {
        if (signal.aborted) {
          reject(new Error('Upload cancelled'));
          return;
        }
        signal.addEventListener('abort', () => xhr.abort(), { once: true });
      }

      xhr.send(formData);
    });
  }

  static async listModels(): Promise<{ id: string; name: string }[]> {
    return fetchModels();
  }

  static async switchModel(modelId: string): Promise<void> {
    await setAgentModel(modelId);
  }
}

export default AgentService;
