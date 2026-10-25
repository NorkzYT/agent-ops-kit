# Download and network-capture patterns

> Getting files and API traffic out of a Hermes browser session
> (Browser Use backend).

## Downloads

- Local Chromium: files land in the Hermes browser download directory
  (`~/.hermes/browser/downloads/` by default). After `browser_click` on a
  download control, list that directory and report the exact filename.
- Browser Use Cloud: downloads belong to the remote session. Fetch them with
  the session's download list (Browser Use dashboard or API) or prefer a
  direct URL and `curl -L -o <file> <url>` from the terminal when the URL
  needs no session cookie.
- Always verify: size greater than zero, expected extension, and a quick
  `file <path>` or `head -c 200`.

## Reading API traffic instead of scraping

When a page draws its data from an API, take the API:

1. `browser_navigate` to the page and walk the flow once.
2. `browser_exec` with `performance.getEntriesByType('resource')` to list the
   XHR/fetch URLs the page made.
3. Note endpoint, method and required headers in the task's `context.md`.
4. Test GET calls only from the terminal with the session's cookie or token
   supplied by the operator. Never replay POST/PUT/DELETE without an explicit
   yes.

## Bulk pages

Two to five seconds between page loads. Thirty seconds page timeout, then
report rather than retry forever. Save intermediate results after every
page so a failure at page 40 keeps pages 1 to 39.

## Cleanup

Downloaded artefacts older than a week can go:

```bash
find ~/.hermes/browser/downloads -type f -mtime +7 -delete
```
