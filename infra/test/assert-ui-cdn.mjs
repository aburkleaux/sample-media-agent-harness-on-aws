#!/usr/bin/env node
// Regression guard for the SPA-vs-API CloudFront fix.
//
// The UI CloudFront distribution must NOT carry distribution-wide
// `403/404 -> 200 /index.html` error rewrites. Those applied to EVERY behavior,
// so a real JSON 4xx from an API path (e.g. `DELETE /jobs?runId=` returning
// 404/409) was rewritten to `200 index.html` AND cached (ErrorCachingMinTTL),
// which the client saw as "non-JSON response (status 200); the route may be
// unavailable" (and a retry got the cached HTML). SPA deep-link routing is
// instead handled by a CloudFront Function (viewer-request) on the DEFAULT
// (S3) behavior only, so API behaviors return their real status untouched.
//
// Runs against the synthesized template (see the `test:ui-cdn` npm script).
import fs from "node:fs";
import path from "node:path";

const TEMPLATE = path.join("cdk.out", "mediaworks-harness-infra-staging.template.json");
if (!fs.existsSync(TEMPLATE)) {
  console.error(
    `missing ${TEMPLATE}; run 'cdk synth mediaworks-harness-infra-staging -c env=staging' first`,
  );
  process.exit(2);
}

const template = JSON.parse(fs.readFileSync(TEMPLATE, "utf8"));
const resources = template.Resources || {};
const fails = [];

// 1) A CloudFront Function (the SPA history rewrite) must exist.
const functions = Object.entries(resources).filter(
  ([, v]) => v.Type === "AWS::CloudFront::Function",
);
if (functions.length === 0) {
  fails.push("no AWS::CloudFront::Function (SPA history rewrite) found");
}

// 2) The UI distribution must not mask API errors, and must wire the rewrite fn.
const uiDists = Object.entries(resources).filter(
  ([k, v]) => v.Type === "AWS::CloudFront::Distribution" && /UiDistribution/.test(k),
);
if (uiDists.length !== 1) {
  fails.push(`expected exactly 1 UiDistribution resource, found ${uiDists.length}`);
}
for (const [k, v] of uiDists) {
  const dc = (v.Properties && v.Properties.DistributionConfig) || {};
  const cer = dc.CustomErrorResponses || [];
  if (cer.length > 0) {
    fails.push(
      `${k}: CustomErrorResponses present (${cer.length}) — would rewrite/cache real API 4xx as SPA HTML`,
    );
  }
  const fa = (dc.DefaultCacheBehavior && dc.DefaultCacheBehavior.FunctionAssociations) || [];
  if (!fa.some((a) => a.EventType === "viewer-request")) {
    fails.push(`${k}: default behavior missing a viewer-request FunctionAssociation (SPA rewrite)`);
  }
}

if (fails.length) {
  console.error("UI CDN assertions FAILED:\n - " + fails.join("\n - "));
  process.exit(1);
}
console.log(
  "UI CDN assertions passed: UiDistribution has no CustomErrorResponses; " +
    "SPA rewrite CloudFront Function is on the default behavior.",
);
