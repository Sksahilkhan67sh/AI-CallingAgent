# P0-1 Token hygiene (HUMAN-TODO)

Status: HUMAN-TODO until the owner confirms every step below.

## Why
A GitHub personal access token (fine-grained, `github_pat_` prefix) was shared in plain text
in the file `repo  httpsgithub.comSksahilkhan67s.txt` that was uploaded to this session. [READ]
Treat it as compromised. The token value is NOT reproduced anywhere in this repo.
This session did not use it: `GH_TOKEN` was not set in the environment [RAN], and the repo is public so
an anonymous `git clone` worked [RAN].

## Owner steps (in order)
1. GitHub > Settings > Developer settings > Personal access tokens > Fine-grained tokens.
   Find the token whose name/last-used matches, click **Delete** (revoke). Do this first.
2. Check GitHub > Settings > Security log (https://github.com/settings/security-log). Filter
   `action:oauth_authorization OR action:personal_access_token` and look at the last 90 days
   for requests from unfamiliar IPs, `repo.*` or `git.*` events you did not do.
3. Also check the repo: Settings > Audit/Security (if org) and Actions > recent runs for jobs you did not trigger.
4. Create a replacement: fine-grained token, **Only select repositories** = AI-CallingAgent,
   permissions: Contents = Read-only, Metadata = Read-only, Actions = Read-only. Shortest expiry you can live with (e.g. 30 days).
5. Store it only in a password manager / env var `GH_TOKEN`. Never paste into chat or files.
6. Delete the uploaded text file from any place it was saved (chat history, downloads, cloud sync).
7. If this token ever had write scope: review branches/PRs/workflows/secrets for changes you did not make.
8. Remember: any other secret used in the same places (Dograh API key, DB URLs, telephony keys) should be considered for rotation if they were ever shared the same way.

## Git history scan [RAN]
Command (output reduced to counts and file names, matched text never printed):
```
git log -p --all | grep -nE "github_pat_|ghp_|AKIA|BEGIN (RSA|PRIVATE)" | wc -l
git log --all -G'github_pat_|ghp_|AKIA|BEGIN (RSA|PRIVATE)' --name-only --pretty=format: | sort -u
```
- Refs scanned: all remote branches, 143 commits total (`git rev-list --all --count`) [RAN]
- Matching lines: **0** [RAN]
- Files with matches: none [RAN]
Limits: this only covers the patterns listed (not e.g. Dograh keys, JWTs, passwords). Not scanned: GitHub PR comments, issues, Actions logs. [ASSUMED] no secrets there.

OWNER CONFIRMATION (token revoked, security log reviewed, replacement created):
