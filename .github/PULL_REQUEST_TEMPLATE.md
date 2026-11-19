## What does this PR do?

<!-- Describe the change clearly. What problem does it solve? Why is this approach the right one? -->



## Related Issue

<!-- Link the issue this PR addresses. If no issue exists, consider creating one first. -->

Fixes #

## Type of Change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Security fix (undisclosed vulnerabilities go to [Security Advisories](https://github.com/muntasirrmahdi/minimalrouter/security/advisories/new) first, not a public PR — see SECURITY.md scope)
- [ ] Documentation update
- [ ] Tests (adding or improving test coverage)
- [ ] Refactor (no behavior change)
- [ ] Registry entry (new provider, with primary-source endpoints + live-probe verdict)

## Changes Made

<!-- List the specific changes. Include file paths for code changes. -->

-

## How to Test

<!-- Steps to verify this change works. For bugs: reproduction steps + proof the fix works,
     including proof the regression test fails against the pre-fix engine (MR_TEST_ENGINE). -->

1.
2.
3.

## Checklist

### Code

- [ ] I've read the [Contributing Guide](https://github.com/muntasirrmahdi/minimalrouter/blob/main/CONTRIBUTING.md)
- [ ] My commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
- [ ] I searched for [existing PRs](https://github.com/muntasirrmahdi/minimalrouter/pulls) to make sure this isn't a duplicate
- [ ] My PR contains **only** changes related to this fix/feature (no unrelated commits)
- [ ] `python -m unittest discover -s tests` passes
- [ ] `ruff check --select F minimalrouter.py tests` is clean
- [ ] I ran the flow I changed live, including one failure case (bad key, dead host, rate-limited row)
- [ ] I exercised the dashboard keys I touched through a real terminal (boot-and-quit is not enough)
- [ ] I've added a regression test that fails without the fix (required for bug fixes)
- [ ] I've tested on my platform: <!-- e.g. Ubuntu 24.04, macOS 15, WSL2 -->

### Documentation & Housekeeping

- [ ] I've updated relevant documentation (README, docs/, docstrings) — or N/A
- [ ] Every new command lands in `-h`, the module docstring and the README in the same pass — or N/A
- [ ] I've considered cross-platform impact (no /proc-only paths, no os.kill signal 0) — or N/A
- [ ] No personal names, private account details, or real keys/tokens in any committed file
- [ ] Provider verdicts carry evidence and dates; no login-only tool advertised as a working route

## Screenshots / Logs

<!-- If applicable, add screenshots or log output showing the fix/feature in action (keys redacted). -->
