# Long-running jobs

Some work takes a while — analyzing a long video, processing large data, or
running a multi-step pipeline. That work runs in the **background as a job** so
you don't have to wait in place, and you can collect the result when it's done.

## How it works for you

1. **You ask** for the work as usual.
2. **It starts running** in the background. You'll get a way to track it — a job
   reference and status.
3. **You can keep working** or come back later.
4. **When it finishes**, you retrieve the result — ask for it, or open the job
   from its status.

## Checking on a job

- Ask "what's the status of my job?" or "are my jobs done?".
- Ask "show my jobs" to see what's running or finished.
- Job status updates as work progresses, so you can tell running from finished
  from failed.

## Getting the result

When a job is finished, ask for its result (or open it) and it's delivered like
any other result — preview inline and download.

## If a job seems stuck or failed

- Give it a little time; longer tasks genuinely take longer.
- Ask "did my job fail, and why?" — you'll get a plain explanation and what to
  try. See `troubleshooting.md`.
