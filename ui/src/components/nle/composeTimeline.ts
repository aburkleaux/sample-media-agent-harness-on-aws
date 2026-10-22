/**
 * composeTimeline moved to `src/shared/composeTimeline.ts` so the proxy server
 * (the `/sessions/:id/renders` endpoint) can compose per-source composites
 * SERVER-SIDE and the UI just renders finished results. Re-exported here so
 * existing UI imports + tests keep working unchanged.
 */
export { composeTimeline, groupBySourceKey } from '../../shared/composeTimeline';
