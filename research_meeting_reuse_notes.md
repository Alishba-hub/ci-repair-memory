# Research meeting reuse notes

I checked the paper list in the Google Sheet and pulled out the entries that fit our study best.

## Best things to reuse for this research

1. **CI-Repair-Bench** is the strongest match for our study.
   - It has CI failure cases, logs, patches, and fail-to-pass examples.
   - This fits our plan to compare an agent with and without CI log context.
   - It also has code and a dataset link, so it is practical to reuse.

2. **When AI Agents Touch CI/CD Configurations** is useful as a second source.
   - It has agent-generated pull requests and CI/CD workflow runs.
   - This can help us study how agents behave on real CI/CD changes.
   - It is also useful for comparing multiple agents.

3. **CI-Bench** is a good backup benchmark.
   - It contains real CI failures with logs, patches, repositories, and Docker environments.
   - It is useful if we want another dataset with reproducible CI failures.

4. **SWE-CI** is useful for the memory/history part.
   - It has repository history, commits, issues, and CI environments.
   - This can support the idea that historical project context helps agents.

## What I would use first

- Primary dataset: **CI-Repair-Bench**
- Secondary dataset: **When AI Agents Touch CI/CD Configurations**
- Backup dataset: **CI-Bench**
- Extra support for memory/history ideas: **SWE-CI**

## Short explanation in easy words

I used the sheet to find papers that already have CI logs, failed and fixed examples, and code or dataset links. The best match for our project was CI-Repair-Bench because it already contains real CI failures and fixes. I also kept one paper about agent-generated CI/CD pull requests, one backup benchmark, and one paper that shows how repository history can help agents. This gives us a stronger basis for testing whether CI log memory helps an AI coding agent fix problems.

## Why this helps our research

- It matches our main question about whether historical CI logs help an agent solve issues.
- It gives us datasets with failed and fixed examples, which we need for evaluation.
- It gives us papers we can cite to justify the study design.
- It gives us possible code and dataset sources instead of starting from scratch.
