# Checkpoint 00 — Project Initialization

This is the workflow specification this checkpoint was implemented against
(provided by the project owner alongside the full spec bundle in
`docs/specs/`). Recorded here for audit trail / reference by future
checkpoints.

---

## Repository

https://github.com/Sksahilkhan67sh/AI-CallingAgent

## Branch strategy

```
main
  ↓
develop
  ↓
feature/checkpoint-NN-<name>
```

## Development workflow (per checkpoint)

1. Create a dedicated feature branch off `develop`.
2. Implement only the current checkpoint's scope.
3. Run tests, type checking, lint, build, and runtime verification.
4. Review the diff and clean unnecessary code.
5. Commit, push the feature branch, open a Pull Request into `develop`.
6. Stop and wait for the project owner to review, verify, and merge.

The agent never merges its own PR, never pushes directly to `main`, and
never deletes a feature branch unless explicitly instructed. A checkpoint is
complete only once the owner has verified and merged the PR — the next
checkpoint does not begin before that.
