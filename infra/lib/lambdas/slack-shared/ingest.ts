// Pure file-ingest helpers. No I/O.

export function sanitizeFilename(name: string): string {
  let base = (name || "file").split(/[\\/]/).pop() || "file";
  base = base
    .replace(/[\x00-\x1f\x7f]/g, "")
    .replace(/[^A-Za-z0-9._-]/g, "_")
    .replace(/^\.+/, "");
  if (!base) base = "file";
  if (base.length > 120) {
    const dot = base.lastIndexOf(".");
    const ext = dot > 0 ? base.slice(dot) : "";
    base = base.slice(0, Math.max(1, 120 - ext.length)) + ext;
  }
  return base;
}

export function inputKey(sessionId: string, fileId: string, safeName: string): string {
  return `artifacts/${sessionId}/input/slack-${fileId}-${safeName}`;
}

export interface FileCandidate {
  id: string;
  name: string;
  mimetype: string;
  size: number;
}

export interface CapConfig {
  maxFiles: number;
  maxFileBytes: number;
  maxAggregateBytes: number;
}

export interface CapDecision {
  accept: FileCandidate[];
  reject: Array<{ id: string; name: string; reason: string }>;
}

export function decideByCaps(files: FileCandidate[], caps: CapConfig): CapDecision {
  const accept: FileCandidate[] = [];
  const reject: Array<{ id: string; name: string; reason: string }> = [];
  let agg = 0;
  files.forEach((f, i) => {
    if (i >= caps.maxFiles) {
      reject.push({ id: f.id, name: f.name, reason: `exceeds file-count cap (${caps.maxFiles})` });
      return;
    }
    if (f.size > caps.maxFileBytes) {
      reject.push({ id: f.id, name: f.name, reason: `file too large (${f.size} > ${caps.maxFileBytes} bytes)` });
      return;
    }
    if (agg + f.size > caps.maxAggregateBytes) {
      reject.push({ id: f.id, name: f.name, reason: `would exceed aggregate cap (${caps.maxAggregateBytes} bytes)` });
      return;
    }
    agg += f.size;
    accept.push(f);
  });
  return { accept, reject };
}
