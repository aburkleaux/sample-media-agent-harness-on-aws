/**
 * Run async work one-at-a-time per key, in arrival order.
 *
 * Why: AgentCore refuses a second invocation on a runtime session that is still busy
 * (`RetryableConflictException: Session operation in progress, please retry`, reproduced against the
 * staging Render Agent: concurrent → 1 conflict + 1 ok, sequential → 2 ok). A two-video upload
 * composes both source tabs together with the same session id, so the second failed (session
 * c70967c9: only tidal composed, Meridian stayed "composing"). Queuing per render session makes the
 * second wait for the first instead.
 *
 * Per proxy process. A key's entry is dropped once its last queued task settles, so the map holds
 * only sessions with work in flight. A rejected task never blocks the ones after it.
 */
export function createSerializer() {
  const tails = new Map<string, Promise<unknown>>();
  return function serializeBy<T>(key: string, task: () => Promise<T>): Promise<T> {
    const prev = tails.get(key) ?? Promise.resolve();
    const run = prev.then(task, task);
    const tail = run.then(() => undefined, () => undefined);
    tails.set(key, tail);
    void tail.then(() => {
      if (tails.get(key) === tail) tails.delete(key);
    });
    return run;
  };
}

export type Serializer = ReturnType<typeof createSerializer>;
