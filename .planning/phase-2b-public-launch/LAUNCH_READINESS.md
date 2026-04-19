# Phase 2b — Launch Readiness Checklist

**Target state:** Every row below is checked and has a link to evidence.

## GOAL-1: README rewrite shipped on main
- [ ] README.md rewritten per RESEARCH.md §4 (evidence: commit SHA + GitHub URL)
- [ ] Hero demo thumbnail links to real YouTube URL (evidence: grep -F 'youtu.be' README.md)
- [ ] Quick-start executes cleanly on a fresh Windows 10/11 VM (evidence: screen recording or terminal log in DEMO_DOGFOOD.md, optional)
- [ ] Architecture diagram present (evidence: grep -A 10 'TEST MACHINE' README.md)
- [ ] MCP client configs for Claude Code + Cursor + Claude Desktop (evidence: 3 fenced code blocks under the MCP client section)
- [ ] Honest-marketing grep passes (evidence: grep results logged)

## GOAL-2: Landing page at <domain> live
- [ ] <domain> resolves and returns 200 (evidence: curl -sI https://<domain>)
- [ ] Lighthouse >= 90 on Performance, Accessibility, Best Practices, SEO (evidence: `npx lighthouse https://<domain> --view` saved output; scores in this file)
- [ ] Video embedded above the fold (evidence: lighthouse screenshot or view-source)
- [ ] OG + Twitter cards set (evidence: `curl -s https://<domain> | grep -E 'og:|twitter:'`)
- [ ] Favicon + logo consistent with repo (evidence: view tab icon + logo.svg)
- [ ] Page source committed under site/ (evidence: git log site/)

## GOAL-3: 60-second demo video recorded + uploaded
- [ ] YouTube URL public (evidence: incognito visit)
- [ ] Captions/subtitles present (evidence: YouTube CC button shows English)
- [ ] Video URL linked from README and landing page (evidence: grep + landing page visual)

## GOAL-4: >= 4 MCP directory submissions tracked
- [ ] DIRECTORY_SUBMISSIONS.md shows >= 4 rows with submission dates
- [ ] At least 4 rows are pending or live (rejected rows don't count)
- [ ] Each row has: directory, submission URL, submission date, listing URL (once live), status, metadata notes

## GOAL-5: Launch post drafts written
- [ ] LAUNCH_POSTS.md has HN + r/cpp + r/gamedev + Twitter sections
- [ ] Top banner `NOT YET PUBLISHED` present
- [ ] r/programming noted as `DEFERRED_TO_2C`

## GOAL-6: Open-source scaffolding
- [ ] CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md exist at repo root
- [ ] .github/ISSUE_TEMPLATE/*.yml + .github/PULL_REQUEST_TEMPLATE.md exist
- [ ] GitHub repo About panel (description, homepage, topics) populated
- [ ] Social preview image uploaded

## GOAL-7: Discoverability
- [ ] robots.txt + sitemap.xml served from <domain>
- [ ] Google Search Console verified for <domain>; sitemap submitted
- [ ] Repo description + homepage + topics set (see GOAL-6)

## Pre-publication (separate go/no-go, NOT a 2b exit criterion)
- [ ] 48-hour soak period on landing page post-deploy (R4 mitigation)
- [ ] Sidebar rules for r/cpp and r/gamedev re-verified at post time
- [ ] Final Lighthouse re-run immediately before HN post
- [ ] CHANGELOG 0.2.1 entry finalized and tagged

## Exit status
- **All GOAL rows checked:** <YES / NO>
- **Phase 2b exit date:** <YYYY-MM-DD>
- **Next phase:** Phase 2c (PyPI + onboarding polish)
