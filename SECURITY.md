# Security

RSICrawl runs code that an LLM writes. Treat it accordingly.

## What runs where
| Code | Written by | Runs on the crawl path? | Isolation |
|---|---|---|---|
| Recipes | analyzer / agent | yes | Declarative JSON interpreted by `rsicrawl/recipe.py`; there is no `eval`. Network access goes through the budgeted, robots-checked HTTP client only. |
| Playbooks | agent (Distill) | yes (as recipes) | Same as recipes. |
| Skills (analyzer plug-ins) | agent (Improve) | **analysis only**: they produce candidate recipes, which are then verified | `RSICRAWL_SANDBOX=docker` (default when docker exists): no network, read-only filesystem, unprivileged user, all capabilities dropped, `no-new-privileges`, 1 GB memory, 1 CPU, 64 pids. `RSICRAWL_SANDBOX=process` (fallback): separate interpreter with CPU/memory/file-size rlimits and the socket module disabled. **The process mode is not a security boundary.** |

## Recommendations
- Use the docker sandbox, or run all of RSICrawl inside a disposable VM or container.
- Review `memory/skills/*.py` before you reuse a memory directory you did not produce yourself. Skills are code.
- Give the crawler host no credentials it does not need. The agent never sees your API keys, but skills run on your machine.
- Lightpanda is a young browser; run it as an unprivileged user.

## Reporting a vulnerability
Please open a GitHub security advisory (Security → Report a vulnerability) rather than a public issue.
