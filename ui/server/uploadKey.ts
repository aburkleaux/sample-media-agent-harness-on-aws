/**
 * Where an uploaded file lands in S3.
 *
 * `artifacts/<session>/input/<name>` — the session-scoped INPUT prefix, matching the
 * documented convention (`artifacts/<session>/{input,output}/`) and the agent-side
 * implementation (`main._artifact_input_prefix`).
 *
 * WHY THE PATH MATTERS, not just tidiness: it makes an artifact's ROLE derivable from its
 * location — `/input/` is a source, `/output/` is a deliverable. Role is currently inferred
 * from the `s3://` SCHEME (`hooks/provider.extract_source_uris` requires
 * `uri.startswith("s3://")`), which stops working the moment references become canonical
 * CloudFront https. A path-derived role survives that change; a scheme-derived one does not,
 * and when source detection fails the input renders as its own tab.
 *
 * Before this existed, `POST /upload` built `artifacts/${Date.now()}-${filename}` and dropped
 * the session entirely: 269 flat, session-less objects and ZERO `/input/` ones, while the
 * correct agent-side path sat unreachable behind a `uploadFile()` helper nothing called.
 *
 * Back-compat: this only decides where NEW uploads go. Existing flat keys are read by key and
 * keep resolving, so there is nothing to migrate.
 *
 * Extracted as a pure function so the placement rule is testable without S3 or a request.
 */

/** Strip anything that could escape the prefix, and any directory component. */
function safeSegment(raw: string): string {
  const base = String(raw || "").split(/[\\/]/).pop() || "";
  return base.replace(/\.\./g, "").replace(/^\.+/, "").trim();
}

/**
 * `ownerTag` (artifactOwner.ts) scopes a SESSION-LESS upload to its uploader:
 * `artifacts/uploads/<tag>/input/<leaf>`. The read policy serves that prefix to the tag's owner
 * only. Without a tag the legacy shared `artifacts/uploads/input/` form is produced, which the read
 * policy no longer serves to anyone but a member of a project that registered it, so every caller
 * that writes a session-less upload passes one.
 */
export function uploadKeyFor(
  sessionId: string | undefined | null,
  filename: string,
  now: number,
  ownerTag?: string,
): string {
  // The leaf is ALWAYS timestamped. The alternative — a bare filename — would make a
  // re-upload of the same file idempotent (one key, one identity), which is appealing for
  // dedup, but it lets two DIFFERENT files called `upload.mp4` clobber each other. Busboy
  // even defaults an unnamed part to "upload", so generic names are common. Losing a user's
  // file is worse than an extra key, and render dedup no longer keys off upload URLs anyway
  // (it keys off the job/manifest identity), so collision safety wins.
  const leaf = `${now}-${safeSegment(filename) || "upload"}`;
  const session = safeSegment(sessionId || "");
  // Session-less (a pre-session upload) still lands under an `input/` segment, so role stays
  // path-derivable either way.
  if (session) return `artifacts/${session}/input/${leaf}`;
  const tag = safeSegment(ownerTag || "");
  return tag ? `artifacts/uploads/${tag}/input/${leaf}` : `artifacts/uploads/input/${leaf}`;
}
