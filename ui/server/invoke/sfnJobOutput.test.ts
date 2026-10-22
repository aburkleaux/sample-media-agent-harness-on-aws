// Unit tests for the pure Step Functions output parser.

import { describe, it, expect } from "vitest";
import { parseSfnJobOutput } from "./sfnJobOutput";

// Trimmed real process_media terminal output (artifact prefix + output_keys).
const MEDIA_PROCESSING_OUTPUT = JSON.stringify({
  operation: "StateUsageTracking",
  output: {
    s3uri:
      "s3://mediaworks-proxy-100000000001-us-west-2/micro-workflow/media-processing/1183efd0",
  },
  input: {
    s3uri:
      "s3://mediaworks-harness-skills-store-100000000001-us-west-2/artifacts/src.mp4",
  },
  data: {
    output_keys: {
      proxy: "micro-workflow/media-processing/1183efd0/proxy/transcoded.mp4",
    },
    duration_sec: 180.05,
  },
});

// speech-diarization output has the prefix but no data.output_keys.
const DIARIZATION_OUTPUT = JSON.stringify({
  output: {
    s3uri:
      "s3://mediaworks-proxy-100000000001-us-west-2/micro-workflow/speech-diarization/aceff38b",
  },
  input: { s3uri: "s3://bucket/artifacts/src.mp4" },
});

describe("parseSfnJobOutput", () => {
  it("extracts prefix, source, and resolved artifacts from output_keys", () => {
    const out = parseSfnJobOutput(MEDIA_PROCESSING_OUTPUT);
    expect(out.outputPrefix).toBe(
      "s3://mediaworks-proxy-100000000001-us-west-2/micro-workflow/media-processing/1183efd0",
    );
    expect(out.sourceUri).toBe(
      "s3://mediaworks-harness-skills-store-100000000001-us-west-2/artifacts/src.mp4",
    );
    expect(out.artifacts).toEqual([
      {
        label: "proxy",
        uri: "s3://mediaworks-proxy-100000000001-us-west-2/micro-workflow/media-processing/1183efd0/proxy/transcoded.mp4",
      },
    ]);
  });

  it("returns the prefix with no artifacts when output_keys is absent", () => {
    const out = parseSfnJobOutput(DIARIZATION_OUTPUT);
    expect(out.outputPrefix).toContain("speech-diarization/aceff38b");
    expect(out.artifacts).toEqual([]);
  });

  it("accepts an already-parsed object", () => {
    const out = parseSfnJobOutput(JSON.parse(MEDIA_PROCESSING_OUTPUT));
    expect(out.artifacts).toHaveLength(1);
  });

  it("never throws on malformed / empty input", () => {
    expect(parseSfnJobOutput("not json")).toEqual({
      outputPrefix: null,
      sourceUri: null,
      artifacts: [],
    });
    expect(parseSfnJobOutput(null)).toEqual({
      outputPrefix: null,
      sourceUri: null,
      artifacts: [],
    });
    expect(parseSfnJobOutput({})).toEqual({
      outputPrefix: null,
      sourceUri: null,
      artifacts: [],
    });
  });
});
