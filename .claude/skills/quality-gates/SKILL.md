---
name: quality-gates
description: Self-verification and review checklist for coding agents. Re-read changed files, run tests, check commit format, and perform self-review on large changes.
---

# Quality Gates — Self-Verification Checklist

Apply this skill after every code change, before committing. Never mark a task done without passing these gates.

## Gate 1: Re-Read Changed Files

After editing any file, re-read it in full before moving on. This catches:

- Accidental deletions or overwrites
- Indentation/syntax errors from bad edits
- Leftover debug code or TODO markers
- Merge conflicts or duplicate content

**Rule:** Every `Edit` or `Write` must be followed by a `Read` of the same file.

## Gate 2: Build

Run the build command from TOOLS.md (if one exists):

- If build fails: fix the error, re-run, do NOT commit broken code
- If no build command exists: check for syntax errors manually

## Gate 3: Test

Run the test command from TOOLS.md:

- All existing tests must pass
- If you added new functionality, add tests for it
- If tests fail: fix the cause, don't skip or disable tests
- If no test command exists: manually verify the changed behavior

## Gate 4: Conventional Commit Check

Before committing, verify the commit message follows conventional format:

```
type(scope): short description

[optional body]
```

Valid types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `style`, `perf`, `ci`, `build`

**Never include:**
- `Co-Authored-By` trailers
- Generic messages like "update files" or "fix stuff"

## Gate 5: Self-Review (4+ files changed)

If your change touches 4 or more files, run this self-review checklist:

1. **Correctness:** Does each change do what was intended?
2. **Completeness:** Are all related files updated (imports, exports, types, tests)?
3. **Consistency:** Do new patterns match existing ones in the codebase?
4. **No regressions:** Could any change break an unrelated feature?
5. **No secrets:** Are credentials, tokens, or API keys excluded?
6. **No dead code:** Did you leave behind unused imports, variables, or functions?

Write the self-review summary to the task report.

## Gate 6: Definition of Done

Before marking complete, verify:

- [ ] Code compiles/builds without errors
- [ ] All tests pass
- [ ] Changes are committed on a feature branch
- [ ] Commit message follows conventional format
- [ ] Task report is written with what changed and test results
- [ ] No TODO/FIXME left without a corresponding issue

## When Gates Fail

- **Build failure:** Fix immediately. Never commit.
- **Test failure:** Fix the test or the code. Never skip.
- **Self-review finds issues:** Fix them before committing.
- **Can't fix:** Report the blocker clearly with error details. Do not mark the task as done.
