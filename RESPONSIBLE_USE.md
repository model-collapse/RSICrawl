# Responsible use

RSICrawl learns to fetch a site's data **directly**: from the JSON API its pages call, from JSON embedded in its HTML, or from the HTML itself. That is efficient, and it calls for care.

Built-in behaviour (enforced in code, not left to the LLM):
- **robots.txt is obeyed for every request**, including the sub-requests a page makes while it is rendered and direct API calls. Patterns whose data API is disallowed stay on whatever the rules permit.
- **One shared per-host rate budget** (default 2 requests/s) covers every request from every tier, including verification and agent experiments. Learning traffic is capped at 15% of it.
- **Bot walls and CAPTCHAs are detected and skipped, never solved.** The agent is instructed, and checked by the controls in the benchmark, to give up on signed or protected requests instead of forging them.
- **No logins.** The crawler does not use credentials.
- **It identifies itself:** `RSICrawl/<version> (+repository URL)`. Override with `RSICRAWL_USER_AGENT` and include your own contact details.

Your responsibilities:
- Respect each site's terms of service and applicable law (copyright, database rights, privacy and data-protection rules).
- Do not collect personal data you have no lawful basis to process.
- Keep the rate budget conservative for sites you do not own, and stop if a site asks you to.
- The benchmark runs only against local synthetic sites (`siteforge/`). Test your changes there before you crawl anything real.
