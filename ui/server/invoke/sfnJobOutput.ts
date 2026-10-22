// Pure parser for a Step Functions execution output → the artifact URIs an
// external caller cares about. Used by GET /invoke/:agentId/jobs to enrich a
// SUCCEEDED SFN job with its outputs read DIRECTLY from Step Functions — no
// agent turn, no tokens.
//
// The media micro-workflows share a consistent terminal-state output shape
// (verified on process_media + speech-diarization executions):
//
//   {
//     "output": { "s3uri": "s3://.../micro-workflow/<trio>/<uuid>", ... },
//     "input":  { "s3uri": "s3://.../artifacts/<source>", ... },
//     "data":   { "output_keys": { "proxy": "<prefix>/proxy/transcoded.mp4" },
//                 "duration_sec": 180.05, ... },              // media-processing
//     ...
//   }
//
// `output.output.s3uri` (the artifact PREFIX) is always present; `data.output_keys`
// (specific artifacts, relative to the output bucket) is present for trios that
// emit named files. We surface the prefix plus any resolved artifact URIs.

/** A single resolved artifact the caller can fetch. */
export interface JobArtifact {
  /** Short label, e.g. "proxy" / "probe" (the output_keys key), or "output". */
  label: string;
  /** Full s3:// URI to the artifact (or its prefix when only a prefix is known). */
  uri: string;
}

/** Parsed, agent-free view of a completed job's outputs. */
export interface JobOutput {
  /** The artifact prefix (`output.output.s3uri`), when present. */
  outputPrefix: string | null;
  /** The source asset the job ran on (`input.s3uri`), when present. */
  sourceUri: string | null;
  /** Resolved artifacts (from `data.output_keys`), possibly empty. */
  artifacts: JobArtifact[];
}

/** Join an output bucket + a bucket-relative key into an s3:// URI. */
const joinS3 = (bucket: string, key: string): string =>
  `s3://${bucket}/${String(key).replace(/^\/+/, "")}`;

/** Derive the output bucket from the artifact prefix `s3://<bucket>/<...>`. */
const bucketOf = (s3uri: string | null): string | null => {
  if (!s3uri || !s3uri.startsWith("s3://")) return null;
  const rest = s3uri.slice("s3://".length);
  const slash = rest.indexOf("/");
  return slash === -1 ? rest : rest.slice(0, slash);
};

/**
 * Parse a Step Functions `DescribeExecution` output string (or already-parsed
 * object) into a {@link JobOutput}. Never throws — malformed/partial output
 * yields a mostly-empty result so the caller can still render the job status.
 */
export function parseSfnJobOutput(output: unknown): JobOutput {
  let root: any = output;
  if (typeof output === "string") {
    try {
      root = JSON.parse(output);
    } catch {
      return { outputPrefix: null, sourceUri: null, artifacts: [] };
    }
  }
  if (!root || typeof root !== "object") {
    return { outputPrefix: null, sourceUri: null, artifacts: [] };
  }

  const outputPrefix =
    typeof root?.output?.s3uri === "string" ? root.output.s3uri : null;
  const sourceUri =
    typeof root?.input?.s3uri === "string" ? root.input.s3uri : null;

  const artifacts: JobArtifact[] = [];
  const outputKeys = root?.data?.output_keys;
  const bucket = bucketOf(outputPrefix);
  if (outputKeys && typeof outputKeys === "object" && bucket) {
    for (const [label, key] of Object.entries(outputKeys)) {
      if (typeof key === "string" && key) {
        artifacts.push({ label, uri: joinS3(bucket, key) });
      }
    }
  }

  return { outputPrefix, sourceUri, artifacts };
}
