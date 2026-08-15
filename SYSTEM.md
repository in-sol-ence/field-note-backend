This project is for the following hackathon:
We are judging **working systems**, not slide decks or a thin API wrapper.

**Cursor + Grok 4.6 have to be load-bearing.** If they are not doing real work, it is not in.

**100 points. Three scores. Top 3 present to the room.**

## It works — 40

Does it run?

## Taste — 30

Something you’d be excited to show your family and friends.

## Nails a business use case — 30

Actually solved an insightful problem.
Pipeline Architecture Overview
The team mapped out a staged pipeline from product understanding to developer integration
T0 — Product Understanding: User inputs a website URL + optional GitHub repo + optional detail form; an agent scrapes and summarizes the product
T1 — Sources: Agent determines which sources to scrape (Reddit, X.com, GitHub); not fully deterministic since the agent needs to identify relevant subreddits etc.
T2 — Posts: Filtered, relevant posts are output as structured objects (post ID, link, timestamp, page count, comment count)
T3 — Dashboard: Issues are compiled, clustered by domain/subdomain, and displayed with a 0–10 health score (critical → healthy)
T4 — Developer Integration: Issues are matched to related code in the connected repo; repo connection is required at this stage
T5 — MCP Server: Bug fixes are suggested; MCP server is generated for developer workflow integration
Data Sources
Primary sources: Reddit, X.com, GitHub
Repo integration is optional at onboarding; website URL is the primary required input
Trust/privacy concern raised around sharing private repos — repo kept optional to address this
Additional sources discussed but deferred: YouTube (legal/technical difficulty), Stack Overflow, custom Apify actors
Scraping Approach
Agent-driven (not deterministic): agent decides which subreddits/sources to search based on product text
Multi-agent architecture planned — one agent per discovered issue to handle issue creation in parallel
Playwright suggested for website understanding at T0
Existing Reddit scraping scripts already available
X.com API costs flagged as expensive ($5/1,000 calls); Puppeteer/reverse engineering preferred
Memory & State Management
For the demo, team agreed on a one-shot approach — no persistent memory required
Long-term: memory should store scraping metadata (post ID, URL, timestamp, page/comment count) to avoid re-scraping
A lightweight metadata store (not a full vector DB) was deemed sufficient for this use case
Real-Time Dashboard Behavior
Dashboard populates as soon as a threshold of issues is reached, then continues updating in real time as the agent runs in the background
New issues found in subsequent passes should be deduplicated against existing issues
Threshold determines how expansive each scraping pass is (e.g., top 20 posts per subreddit)
Frontend / Interface
Backend should expose API endpoints; CLI or web frontend can wrap around it
CLI preferred for demo (seen as cooler to present)
Onboarding UI: three inputs — website link, optional GitHub repo, optional detail form
Team Division (Preliminary)
Scraping module — team member with existing scripts and most progress
Onboarding flow + Dashboard — suggested as a combined scope for one person
Backend API endpoints — to be built so any interface can plug in
